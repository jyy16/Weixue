import json
import os
import shutil
import tempfile
import unittest
import atexit
from types import SimpleNamespace

# Isolate the database before importing the models. NOTE: this is the first
# module-level WEIXUE_DB_PATH in the suite, so the shared engine binds to this
# directory for every test module; it must only be removed at process exit.
_TMP_DIR = tempfile.mkdtemp(prefix="weixue_bitable_test_")
os.environ["WEIXUE_DB_PATH"] = os.path.join(_TMP_DIR, "test.db")
atexit.register(shutil.rmtree, _TMP_DIR, True)

import httpx

from database import (
    Course,
    DebateTopic,
    FeishuBinding,
    Student,
    StudentResponse,
    SessionLocal,
    init_db,
)
from feishu.bitable import (
    BitableService,
    SINGLE_SELECT_OPTIONS,
    TABLE_PREP_PLANS,
    TABLE_RESPONSES,
)
from feishu.client import FeishuClient, FeishuConfig

# feishu.client's import runs load_dotenv on backend/.env; purge any real
# credentials so "unconfigured" tests stay deterministic on machines that
# already have live Feishu config.
for _key in [k for k in os.environ if k.startswith("FEISHU_")]:
    os.environ.pop(_key, None)

from feishu.sync import (
    BitableSyncer,
    TEACHER_FIELDS_BY_TABLE,
    _field_list,
    _field_str,
    _parse_score_summary,
    bitable_is_configured,
    bitable_status,
    build_course_record,
    build_response_record,
    build_student_record,
    build_topic_record,
    teacher_fields_hash,
)


def _reset_db():
    """Drop and recreate all tables so rows/bindings never leak across classes."""
    from database import Base, engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _configured_client(calls: dict) -> tuple[FeishuClient, httpx.AsyncClient]:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal/"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "success",
                    "tenant_access_token": "t-test-token",
                    "expire": 7200,
                },
            )
        if request.url.path.endswith("/records/search"):
            calls.setdefault("search", []).append(request.url.path)
            # path: /bitable/v1/apps/{app}/tables/{table_id}/records/search
            table_id = request.url.path.split("/")[-3]
            body = json.loads(request.content.decode()) if request.content else {}
            filter_spec = body.get("filter")
            if filter_spec:
                calls.setdefault("search_filters", []).append(filter_spec)
                if calls.get("search_fail_filter"):
                    # Simulate a remote table that predates the 班级 field.
                    return httpx.Response(
                        200,
                        json={"code": 1254043, "msg": "FieldNameNotFound: 班级"},
                    )
            records = calls.get("remote_records", {}).get(table_id, [])
            if filter_spec:
                # Apply the per-course filter like the live API would, so the
                # tests can prove other courses' rows never reach pull logic.
                wanted = {
                    (cond.get("value") or [None])[0]
                    for cond in filter_spec.get("conditions", [])
                    if cond.get("field_name") == "班级"
                }
                records = [
                    r for r in records
                    if (r.get("fields") or {}).get("班级") in wanted
                ]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "items": records,
                        "has_more": False,
                        "page_token": "",
                        "total": len(records),
                    },
                },
            )
        if request.url.path.endswith("/records/batch_create"):
            payload = json.loads(request.content.decode())
            calls["create"].append(payload)
            first_fields = (payload.get("records") or [{}])[0].get("fields") or {}
            if calls.get("fail_create_student") and first_fields.get("学生") == calls["fail_create_student"]:
                return httpx.Response(
                    200, json={"code": 1254999, "msg": "simulated create failure"}
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "records": [{"record_id": f"rec_{len(calls['create'])}"}]
                    },
                },
            )
        if request.url.path.endswith("/records/batch_update"):
            calls["update"].append(json.loads(request.content.decode()))
            return httpx.Response(
                200, json={"code": 0, "msg": "success", "data": {"records": []}}
            )
        if request.url.path.endswith("/fields") and request.method == "GET":
            table_id = request.url.path.split("/")[-2]
            calls.setdefault("list_fields", []).append(table_id)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "items": calls.get("existing_fields", {}).get(table_id, []),
                        "has_more": False,
                    },
                },
            )
        if request.url.path.endswith("/fields") and request.method == "POST":
            payload = json.loads(request.content.decode())
            calls.setdefault("create_field", []).append(
                (request.url.path.split("/")[-2], payload)
            )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "success",
                    "data": {"field": {"field_id": f"fld_new_{len(calls['create_field'])}"}},
                },
            )
        if "/fields/" in request.url.path and request.method == "PUT":
            payload = json.loads(request.content.decode())
            calls.setdefault("update_field_options", []).append(
                (request.url.path, payload)
            )
            return httpx.Response(
                200, json={"code": 0, "msg": "success", "data": {"field": {}}}
            )
        return httpx.Response(
            404, json={"code": 99999, "msg": "unexpected path: " + request.url.path}
        )

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="https://example.test")
    config = FeishuConfig()
    config.app_id = "cli_test"
    config.app_secret = "secret"
    config.base_url = "https://example.test"
    config.bitable_app_token = "bascn_test"
    config.bitable_table_ids = {
        "courses": "tbl_courses",
        "topics": "tbl_topics",
        "students": "tbl_students",
        "responses": "tbl_responses",
    }
    client = FeishuClient(config, http_client=http)
    return client, http


def _unconfigured_client() -> FeishuClient:
    config = FeishuConfig()
    config.app_id = ""
    config.app_secret = ""
    return FeishuClient(config, http_client=httpx.AsyncClient())


class RecordBuilderTests(unittest.TestCase):
    def test_course_record(self):
        course = SimpleNamespace(
            class_name="思辨一班", grade_level=3, created_at=None
        )
        fields = build_course_record(course)["fields"]
        self.assertEqual(fields["班级名"], "思辨一班")
        self.assertEqual(fields["年级"], 3)
        self.assertIsInstance(fields["创建时间"], int)

    def test_topic_record_uses_chinese_labels(self):
        topic = SimpleNamespace(
            title="动物应该养在动物园吗？",
            topic_type="dilemma",
            cognitive_tier="developing",
            stimulus_material="材料",
            reference_arguments=["正方一", "反方一"],
            order=1,
        )
        fields = build_topic_record(topic)["fields"]
        self.assertEqual(fields["标题"], "动物应该养在动物园吗？")
        self.assertEqual(fields["类型"], "两难")
        self.assertEqual(fields["认知梯段"], "发展层")
        self.assertIn("正方一", fields["参考论据"])

    def test_student_record(self):
        student = SimpleNamespace(
            name="小雨",
            grade=2,
            cognitive_tier="basic",
            course=SimpleNamespace(class_name="思辨一班"),
            comment_draft="",
        )
        fields = build_student_record(student)["fields"]
        self.assertEqual(fields["姓名"], "小雨")
        self.assertEqual(fields["认知梯段"], "基础层")
        self.assertEqual(fields["班级"], "思辨一班")

    def test_response_record_status_and_multi_select(self):
        response = SimpleNamespace(
            teacher_reviewed=True,
            ai_dimension_scores={"position": "A", "material": "A-", "structure": "B+", "language": "A-", "perspective": "B+"},
            ai_confidence="certain_good",
            ai_suggested_tags=["结构清晰", "选材具体"],
            ai_bonus_flags=["有新意"],
            teacher_dimension_scores={"position": "A"},
            teacher_tags=["选材具体"],
            teacher_note="表达流畅",
            raw_text="原文",
            cleaned_text="清洗稿",
            source="asr",
        )
        student = SimpleNamespace(name="小雨", course=SimpleNamespace(class_name="思辨一班"))
        topic = SimpleNamespace(title="动物应该养在动物园吗？")
        fields = build_response_record(response, student, topic)["fields"]
        self.assertEqual(fields["学生"], "小雨")
        self.assertEqual(fields["班级"], "思辨一班")
        self.assertEqual(fields["来源"], "音频转写")
        self.assertEqual(fields["AI置信度"], "高")
        self.assertEqual(fields["状态"], "教师已审")
        self.assertEqual(fields["AI建议标签"], ["结构清晰", "选材具体"])
        self.assertEqual(fields["加分项"], ["有新意"])
        self.assertIn("立意:A", fields["AI评分摘要"])


class BitableSyncTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        _reset_db()

    # NOTE: the temp DB dir is shared by the whole suite's engine; cleanup is
    # registered at module level via atexit, never in a tearDownClass.

    def _seed_course(self, db):
        course = Course(title="测试课程", class_name="思辨一班", grade_level=3)
        db.add(course)
        db.flush()
        topic = DebateTopic(
            course_id=course.id,
            title="动物应该养在动物园吗？",
            topic_type="dilemma",
            cognitive_tier="developing",
        )
        db.add(topic)
        db.flush()
        student = Student(course_id=course.id, name="小雨", grade=2)
        db.add(student)
        db.flush()
        response = StudentResponse(
            student_id=student.id,
            topic_id=topic.id,
            raw_text="我觉得应该放回野外。",
            cleaned_text="我觉得应该放回野外。",
            source="manual",
            ai_dimension_scores={"position": "A"},
            ai_confidence="uncertain",
            ai_suggested_tags=["结构清晰"],
            teacher_reviewed=True,
            teacher_dimension_scores={"position": "A"},
        )
        db.add(response)
        db.commit()
        return course.id

    async def test_sync_course_creates_then_updates(self):
        calls = {"create": [], "update": []}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db)
            syncer = BitableSyncer(client)
            self.assertTrue(syncer.available)

            first = await syncer.sync_course(db, course_id)
            self.assertTrue(first["configured"])
            self.assertEqual(
                first["tables"]["responses"], {"created": 1, "updated": 0, "errors": 0, "skipped": 0}
            )
            self.assertEqual(len(calls["create"]), 4)  # course, topic, student, response
            self.assertEqual(calls["update"], [])

            second = await syncer.sync_course(db, course_id)
            self.assertEqual(second["tables"]["responses"]["updated"], 1)
            self.assertEqual(len(calls["create"]), 4)  # no duplicates
            self.assertEqual(len(calls["update"]), 4)  # all entities updated

            # Count only this course's bindings: the suite shares one DB and
            # other test classes accumulate bindings of their own.
            entities = {("course", course_id)}
            entities |= {("topic", t.id) for t in db.query(DebateTopic).filter_by(course_id=course_id)}
            entities |= {("student", s.id) for s in db.query(Student).filter_by(course_id=course_id)}
            entities |= {
                ("response", r.id)
                for r in db.query(StudentResponse).join(Student).filter(Student.course_id == course_id)
            }
            bindings = [
                b for b in db.query(FeishuBinding).all() if (b.entity_type, b.entity_id) in entities
            ]
            self.assertEqual(len(bindings), 4)
            self.assertTrue(all(b.remote_record_id for b in bindings))
        finally:
            db.close()
            await http.aclose()

    async def test_sync_response_single(self):
        calls = {"create": [], "update": []}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db)
            response = (
                db.query(StudentResponse)
                .join(Student)
                .filter(Student.course_id == course_id)
                .first()
            )
            syncer = BitableSyncer(client)
            result = await syncer.sync_response(db, response.id)
            self.assertEqual(result["tables"]["responses"]["created"], 1)
            self.assertEqual(len(calls["create"]), 1)
        finally:
            db.close()
            await http.aclose()

    async def test_unconfigured_sync_is_safe(self):
        calls = {"create": [], "update": []}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db)
            unconfigured = _unconfigured_client()
            try:
                syncer = BitableSyncer(unconfigured)
                self.assertFalse(syncer.available)
                result = await syncer.sync_course(db, course_id)
                self.assertFalse(result["configured"])
                self.assertEqual(result["mode"], "deferred")
                self.assertEqual(calls["create"], [])  # no network calls at all
            finally:
                await unconfigured._http.aclose()
        finally:
            db.close()
            await http.aclose()

    def test_status_and_configuration_helpers(self):
        config = FeishuConfig()
        self.assertFalse(bitable_is_configured(config))
        status = bitable_status(config)
        self.assertEqual(status["mode"], "deferred")
        self.assertFalse(status["configured"])

    async def test_sync_partial_failure_isolated_per_entity(self):
        """Review issue 5: one entity's failure must not sink its siblings."""
        calls = {"create": [], "update": [], "fail_create_student": "豆豆2"}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course = Course(title="事务测试课程", class_name="思辨三班", grade_level=3)
            db.add(course)
            db.flush()
            topic = DebateTopic(
                course_id=course.id,
                title="部分失败辩题",
                topic_type="dilemma",
                cognitive_tier="basic",
            )
            db.add(topic)
            db.flush()
            resp_ids = {}
            for name in ("豆豆1", "豆豆2"):
                student = Student(course_id=course.id, name=name, grade=2)
                db.add(student)
                db.flush()
                response = StudentResponse(
                    student_id=student.id,
                    topic_id=topic.id,
                    raw_text=f"{name}的发言",
                    cleaned_text=f"{name}的发言",
                    source="manual",
                    ai_confidence="uncertain",
                )
                db.add(response)
                db.flush()
                resp_ids[name] = response.id
            db.commit()

            syncer = BitableSyncer(client)
            summary = await syncer.sync_course(db, course.id)
            counters = summary["tables"]["responses"]
            self.assertEqual(counters["created"], 1)
            self.assertEqual(counters["errors"], 1)
            self.assertNotIn("error", summary)  # course-level commit succeeded

            bound = {
                b.entity_id
                for b in db.query(FeishuBinding).all()
                if b.entity_type == "response" and b.table_key == "responses"
            }
            self.assertIn(resp_ids["豆豆1"], bound)
            self.assertNotIn(resp_ids["豆豆2"], bound)  # savepoint rolled back
        finally:
            db.close()
            await http.aclose()


class PullHelperTests(unittest.TestCase):
    def test_parse_score_summary_round_trip(self):
        scores = _parse_score_summary("立意:A；选材:B+；结构：A-")
        self.assertEqual(scores, {"position": "A", "material": "B+", "structure": "A-"})

    def test_parse_score_summary_tolerates_junk(self):
        self.assertEqual(_parse_score_summary(""), {})
        self.assertEqual(_parse_score_summary("；；"), {})
        # Unknown labels are kept verbatim; segments without a grade are skipped.
        scores = _parse_score_summary("自定义维度:A；坏的段；立意:B")
        self.assertEqual(scores, {"自定义维度": "A", "position": "B"})

    def test_field_readers_tolerate_read_shapes(self):
        self.assertEqual(_field_str("  x "), "x")
        self.assertEqual(_field_str({"text": "y"}), "y")
        self.assertEqual(_field_str(None), "")
        self.assertEqual(_field_list(["a", {"text": "b"}]), ["a", "b"])
        self.assertEqual(_field_list("solo"), ["solo"])
        self.assertEqual(_field_list(None), [])

    def test_hash_is_echo_stable(self):
        keys = TEACHER_FIELDS_BY_TABLE["responses"]
        push_shape = {"教师评分": "立意:A", "教师标签": ["选材具体"], "教师批注": "n", "状态": "教师已审"}
        # Same content read back in object/segment shapes must hash identically.
        read_shape = {
            "教师评分": {"text": "立意:A"},
            "教师标签": [{"text": "选材具体"}],
            "教师批注": "n",
            "状态": "教师已审",
        }
        self.assertEqual(teacher_fields_hash(push_shape, keys), teacher_fields_hash(read_shape, keys))
        self.assertNotEqual(
            teacher_fields_hash(push_shape, keys),
            teacher_fields_hash({**push_shape, "教师批注": "changed"}, keys),
        )


class BitablePullTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        _reset_db()

    def _seed_course(self, db, reviewed: bool = False):
        course = Course(title="拉取测试课程", class_name="思辨二班", grade_level=4)
        db.add(course)
        db.flush()
        topic = DebateTopic(
            course_id=course.id,
            title="动物园有必要吗？",
            topic_type="fact_opinion",
            cognitive_tier="developing",
        )
        db.add(topic)
        db.flush()
        student = Student(course_id=course.id, name="豆豆", grade=2)
        db.add(student)
        db.flush()
        response = StudentResponse(
            student_id=student.id,
            topic_id=topic.id,
            raw_text="我觉得动物园有必要。",
            cleaned_text="我觉得动物园有必要。",
            source="manual",
            ai_dimension_scores={"position": "B+"},
            ai_confidence="uncertain",
            ai_suggested_tags=[],
            teacher_reviewed=reviewed,
            teacher_dimension_scores={"position": "A"} if reviewed else None,
            teacher_note="本地批注" if reviewed else "",
        )
        db.add(response)
        db.commit()
        return course.id

    def _response_binding(self, db, course_id):
        resp = (
            db.query(StudentResponse)
            .join(Student)
            .filter(Student.course_id == course_id)
            .first()
        )
        binding = (
            db.query(FeishuBinding)
            .filter(
                FeishuBinding.entity_type == "response",
                FeishuBinding.entity_id == resp.id,
                FeishuBinding.table_key == "responses",
            )
            .first()
        )
        return resp, binding

    async def test_push_snapshots_hash_then_pull_applies_edits(self):
        calls = {"create": [], "update": [], "remote_records": {}}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db)
            syncer = BitableSyncer(client)
            await syncer.sync_course(db, course_id)
            resp, binding = self._response_binding(db, course_id)
            self.assertIsNotNone(binding)
            self.assertTrue(binding.last_synced_hash)  # echo baseline stored on push

            calls["remote_records"]["tbl_responses"] = [
                {
                    "record_id": binding.remote_record_id,
                    "fields": {
                        "班级": "思辨二班",
                        "教师评分": "立意:A+；选材:B+",
                        "教师标签": ["选材具体", "结构清晰"],
                        "教师批注": "表格里的批注",
                        "状态": "教师已审",
                    },
                }
            ]
            result = await syncer.pull_course(db, course_id)
            self.assertEqual(
                result["tables"]["responses"],
                {"checked": 1, "updated": 1, "unchanged": 0},
            )
            db.expire_all()
            resp = db.get(StudentResponse, resp.id)
            self.assertEqual(resp.teacher_dimension_scores, {"position": "A+", "material": "B+"})
            self.assertEqual(resp.teacher_tags, ["选材具体", "结构清晰"])
            self.assertEqual(resp.teacher_note, "表格里的批注")
            self.assertTrue(resp.teacher_reviewed)
        finally:
            db.close()
            await http.aclose()

    async def test_pull_echo_of_own_push_is_no_change(self):
        calls = {"create": [], "update": [], "remote_records": {}}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db)
            syncer = BitableSyncer(client)
            await syncer.sync_course(db, course_id)
            resp, binding = self._response_binding(db, course_id)

            # Remote still shows exactly what we pushed: no teacher edits.
            pushed = build_response_record(resp, resp.student, resp.topic)
            calls["remote_records"]["tbl_responses"] = [
                {"record_id": binding.remote_record_id, "fields": pushed["fields"]}
            ]
            result = await syncer.pull_course(db, course_id)
            counters = result["tables"]["responses"]
            self.assertEqual(counters["updated"], 0)
            self.assertEqual(counters["unchanged"], 1)
        finally:
            db.close()
            await http.aclose()

    async def test_pull_never_unreviews(self):
        calls = {"create": [], "update": [], "remote_records": {}}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db, reviewed=True)
            syncer = BitableSyncer(client)
            await syncer.sync_course(db, course_id)
            resp, binding = self._response_binding(db, course_id)

            calls["remote_records"]["tbl_responses"] = [
                {
                    "record_id": binding.remote_record_id,
                    "fields": {
                        "班级": "思辨二班",
                        "教师评分": "立意:B",
                        "教师标签": [],
                        "教师批注": "改过的批注",
                        "状态": "AI已评",  # teacher did NOT mark reviewed remotely
                    },
                }
            ]
            await syncer.pull_course(db, course_id)
            db.expire_all()
            resp = db.get(StudentResponse, resp.id)
            self.assertTrue(resp.teacher_reviewed)  # never un-reviewed
            self.assertEqual(resp.teacher_note, "改过的批注")  # other fields still import
            self.assertEqual(resp.teacher_dimension_scores, {"position": "B"})
        finally:
            db.close()
            await http.aclose()

    async def test_pull_unmatched_remote_rows_are_counted_not_created(self):
        calls = {"create": [], "update": [], "remote_records": {}}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db)
            syncer = BitableSyncer(client)
            await syncer.sync_course(db, course_id)
            before = db.query(StudentResponse).count()

            calls["remote_records"]["tbl_responses"] = [
                {
                    "record_id": "rec_unknown_row",
                    # Belongs to this class (班级 matches) but has no local
                    # binding → genuinely unmatched for this course.
                    "fields": {"班级": "思辨二班", "教师批注": "表格手加的行", "状态": "教师已审"},
                }
            ]
            result = await syncer.pull_course(db, course_id)
            self.assertEqual(result["unmatched_remote"], 1)
            self.assertEqual(result["tables"]["responses"]["updated"], 0)
            self.assertEqual(db.query(StudentResponse).count(), before)
        finally:
            db.close()
            await http.aclose()

    async def test_pull_legacy_binding_adopts_baseline_first(self):
        calls = {"create": [], "update": [], "remote_records": {}}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db, reviewed=True)
            resp = (
                db.query(StudentResponse)
                .join(Student)
                .filter(Student.course_id == course_id)
                .first()
            )
            # Simulate a pre-two-way binding: mapped but no hash baseline.
            legacy = FeishuBinding(
                entity_type="response",
                entity_id=resp.id,
                table_key="responses",
                remote_record_id="rec_legacy",
            )
            db.add(legacy)
            db.commit()
            syncer = BitableSyncer(client)

            calls["remote_records"]["tbl_responses"] = [
                {
                    "record_id": "rec_legacy",
                    "fields": {"班级": "思辨二班", "教师评分": "", "教师标签": [], "教师批注": "", "状态": "待评估"},
                }
            ]
            result = await syncer.pull_course(db, course_id)
            self.assertEqual(result["tables"]["responses"]["updated"], 0)
            self.assertEqual(result["tables"]["responses"]["unchanged"], 1)
            db.expire_all()
            resp = db.get(StudentResponse, resp.id)
            self.assertEqual(resp.teacher_note, "本地批注")  # empty remote did NOT wipe local
            self.assertTrue(resp.teacher_reviewed)
            legacy = db.query(FeishuBinding).filter_by(remote_record_id="rec_legacy").first()
            self.assertTrue(legacy.last_synced_hash)  # baseline adopted

            # A genuine remote edit after baseline adoption does apply.
            calls["remote_records"]["tbl_responses"][0]["fields"]["教师批注"] = "第二次编辑"
            result = await syncer.pull_course(db, course_id)
            self.assertEqual(result["tables"]["responses"]["updated"], 1)
            db.expire_all()
            resp = db.get(StudentResponse, resp.id)
            self.assertEqual(resp.teacher_note, "第二次编辑")
        finally:
            db.close()
            await http.aclose()

    async def test_pull_student_comment_draft(self):
        calls = {"create": [], "update": [], "remote_records": {}}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db)
            syncer = BitableSyncer(client)
            await syncer.sync_course(db, course_id)
            student = db.query(Student).filter(Student.course_id == course_id).first()
            binding = (
                db.query(FeishuBinding)
                .filter(
                    FeishuBinding.entity_type == "student",
                    FeishuBinding.entity_id == student.id,
                    FeishuBinding.table_key == "students",
                )
                .first()
            )
            calls["remote_records"]["tbl_students"] = [
                {"record_id": binding.remote_record_id, "fields": {"班级": "思辨二班", "评语草稿": "表格写的新评语"}}
            ]
            result = await syncer.pull_course(db, course_id)
            self.assertEqual(result["tables"]["students"]["updated"], 1)
            db.expire_all()
            student = db.get(Student, student.id)
            self.assertEqual(student.comment_draft, "表格写的新评语")
        finally:
            db.close()
            await http.aclose()

    async def test_pull_unconfigured_is_safe(self):
        db = SessionLocal()
        try:
            course_id = self._seed_course(db)
            unconfigured = _unconfigured_client()
            try:
                syncer = BitableSyncer(unconfigured)
                result = await syncer.pull_course(db, course_id)
                self.assertFalse(result["configured"])
                self.assertEqual(result["mode"], "deferred")
            finally:
                await unconfigured._http.aclose()
        finally:
            db.close()

    async def test_pull_filters_by_course_and_hides_other_classes(self):
        """Review issue 1: rows of other courses never reach pull logic."""
        calls = {"create": [], "update": [], "remote_records": {}}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db)
            syncer = BitableSyncer(client)
            await syncer.sync_course(db, course_id)
            resp, binding = self._response_binding(db, course_id)

            pushed = build_response_record(resp, resp.student, resp.topic)
            self.assertEqual(pushed["fields"]["班级"], "思辨二班")

            calls["remote_records"]["tbl_responses"] = [
                {
                    "record_id": binding.remote_record_id,
                    "fields": {
                        "班级": "思辨二班",
                        "教师评分": "",
                        "教师标签": [],
                        "教师批注": "本班的编辑",
                        "状态": "待评估",
                    },
                },
                {
                    # Another course's row: the server-side filter must keep
                    # it out, so it can neither be applied nor counted.
                    "record_id": "rec_other_class",
                    "fields": {"班级": "思辨三班", "教师批注": "别班的行"},
                },
            ]
            result = await syncer.pull_course(db, course_id)
            self.assertTrue(result["filtered"])
            self.assertEqual(result["unmatched_remote"], 0)
            self.assertEqual(result["tables"]["responses"]["updated"], 1)

            filters = calls.get("search_filters", [])
            self.assertTrue(filters)
            cond = filters[0]["conditions"][0]
            self.assertEqual(cond["field_name"], "班级")
            self.assertEqual(cond["operator"], "is")
            self.assertEqual(cond["value"], ["思辨二班"])

            db.expire_all()
            resp = db.get(StudentResponse, resp.id)
            self.assertEqual(resp.teacher_note, "本班的编辑")
        finally:
            db.close()
            await http.aclose()

    async def test_pull_degrades_when_filter_unsupported(self):
        """Review issue 1: remote table without 班级 → full scan, no counting."""
        calls = {
            "create": [],
            "update": [],
            "remote_records": {},
            "search_fail_filter": True,
        }
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db)
            syncer = BitableSyncer(client)
            await syncer.sync_course(db, course_id)
            resp, binding = self._response_binding(db, course_id)

            calls["remote_records"]["tbl_responses"] = [
                {
                    "record_id": binding.remote_record_id,
                    "fields": {
                        "教师评分": "",
                        "教师标签": [],
                        "教师批注": "降级后的编辑",
                        "状态": "待评估",
                    },
                },
                {"record_id": "rec_other_class", "fields": {"教师批注": "别班的行"}},
            ]
            result = await syncer.pull_course(db, course_id)
            self.assertFalse(result["filtered"])
            # Unreliable by design in degraded mode: never reported.
            self.assertEqual(result["unmatched_remote"], 0)
            self.assertEqual(result["tables"]["responses"]["updated"], 1)
            db.expire_all()
            resp = db.get(StudentResponse, resp.id)
            self.assertEqual(resp.teacher_note, "降级后的编辑")
        finally:
            db.close()
            await http.aclose()

    async def test_orphan_bindings_are_pruned(self):
        """Review issue 3: deleting a local entity leaves no binding behind."""
        calls = {"create": [], "update": [], "remote_records": {}}
        client, http = _configured_client(calls)
        db = SessionLocal()
        try:
            course_id = self._seed_course(db)
            syncer = BitableSyncer(client)
            await syncer.sync_course(db, course_id)
            resp, binding = self._response_binding(db, course_id)
            self.assertIsNotNone(binding)

            # Mirrors DELETE /api/responses paths: the entity goes away but
            # nobody touched the binding or the remote row.
            db.delete(resp)
            db.commit()

            result = await syncer.pull_course(db, course_id)
            self.assertEqual(result["pruned_bindings"], 1)
            self.assertTrue(result["filtered"])
            # The orphaned binding row itself is gone (other courses' live
            # bindings in the shared test DB are untouched).
            self.assertIsNone(db.get(FeishuBinding, binding.id))
        finally:
            db.close()
            await http.aclose()


class BitableSchemaBootstrapTests(unittest.IsolatedAsyncioTestCase):
    """Schema bootstrap (field management) behavior against a mock API."""

    ALL_TABLE_IDS = {
        "courses": "tbl_courses",
        "topics": "tbl_topics",
        "students": "tbl_students",
        "responses": "tbl_responses",
        "prep_plans": "tbl_prep_plans",
    }

    @staticmethod
    def _existing_fields_for(schema: dict) -> list[dict]:
        fields = []
        for name, ftype in schema.items():
            field = {"field_id": f"fld_{name}", "field_name": name, "type": ftype}
            if name in SINGLE_SELECT_OPTIONS:
                field["property"] = {
                    "options": [{"name": n} for n in SINGLE_SELECT_OPTIONS[name]]
                }
            fields.append(field)
        return fields

    def _service(self, calls, table_ids=None):
        client, http = _configured_client(calls)
        service = BitableService(
            client, app_token="bascn_test", table_ids=table_ids or self.ALL_TABLE_IDS
        )
        return service, http

    async def test_ensure_schema_covers_five_tables_and_creates_missing_fields(self):
        calls = {"create": [], "update": [], "existing_fields": {}}
        service, http = self._service(calls)
        try:
            report = await service.ensure_schema()
            self.assertEqual(
                set(report),
                {"courses", "topics", "students", "responses", "prep_plans"},
            )
            self.assertTrue(all(v["status"] == "ok" for v in report.values()))

            created_by_table: dict[str, list[dict]] = {}
            for table_id, payload in calls.get("create_field", []):
                created_by_table.setdefault(table_id, []).append(payload)
            # Empty tables → every schema field created, prep_plans included.
            self.assertEqual(
                len(created_by_table["tbl_prep_plans"]), len(TABLE_PREP_PLANS)
            )
            self.assertEqual(
                len(created_by_table["tbl_responses"]), len(TABLE_RESPONSES)
            )
            by_name = {p["field_name"]: p for p in created_by_table["tbl_responses"]}
            self.assertIn("班级", by_name)
            # Single-select fields carry their option list on create...
            self.assertEqual(by_name["状态"]["type"], 3)
            self.assertEqual(
                [o["name"] for o in by_name["状态"]["property"]["options"]],
                SINGLE_SELECT_OPTIONS["状态"],
            )
            # ...plain text fields get no property payload.
            self.assertEqual(by_name["原始文本"]["type"], 1)
            self.assertNotIn("property", by_name["原始文本"])
        finally:
            await http.aclose()

    async def test_ensure_schema_preserves_teacher_custom_options(self):
        existing = self._existing_fields_for(TABLE_RESPONSES)
        source = next(f for f in existing if f["field_name"] == "来源")
        # Teacher added a custom option in the console; 音频转写 is still missing.
        source["property"] = {
            "options": [{"name": "手动录入"}, {"name": "教师自定义"}]
        }
        calls = {
            "create": [],
            "update": [],
            "existing_fields": {"tbl_responses": existing},
        }
        service, http = self._service(calls, table_ids={"responses": "tbl_responses"})
        try:
            report = await service.ensure_schema()
            self.assertEqual(report["responses"]["created_fields"], [])
            self.assertEqual(report["responses"]["updated_options"], ["来源"])
            self.assertEqual(len(calls["update_field_options"]), 1)
            path, payload = calls["update_field_options"][0]
            self.assertTrue(path.endswith("/fields/fld_来源"))
            # PUT /fields/{id} requires field_name and type in the body.
            self.assertEqual(payload["field_name"], "来源")
            self.assertEqual(payload["type"], 3)
            names = [o["name"] for o in payload["property"]["options"]]
            # Union, existing order first: the custom option must survive.
            self.assertEqual(names, ["手动录入", "教师自定义", "音频转写"])
        finally:
            await http.aclose()

    async def test_ensure_schema_is_noop_when_complete(self):
        from feishu.bitable import TABLE_COURSES, TABLE_STUDENTS, TABLE_TOPICS

        calls = {
            "create": [],
            "update": [],
            "existing_fields": {
                "tbl_courses": self._existing_fields_for(TABLE_COURSES),
                "tbl_topics": self._existing_fields_for(TABLE_TOPICS),
                "tbl_students": self._existing_fields_for(TABLE_STUDENTS),
                "tbl_responses": self._existing_fields_for(TABLE_RESPONSES),
                "tbl_prep_plans": self._existing_fields_for(TABLE_PREP_PLANS),
            },
        }
        service, http = self._service(calls)
        try:
            report = await service.ensure_schema()
            self.assertTrue(all(v["status"] == "ok" for v in report.values()))
            self.assertEqual(calls.get("create_field"), None)
            self.assertEqual(calls.get("update_field_options"), None)
            for table_report in report.values():
                self.assertEqual(table_report["created_fields"], [])
                self.assertEqual(table_report["updated_options"], [])
        finally:
            await http.aclose()


class FeishuBindingSchemaTests(unittest.TestCase):
    """Review issue 4: (entity_type, entity_id, table_key) must be unique."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def test_binding_unique_constraint(self):
        from sqlalchemy.exc import IntegrityError

        db = SessionLocal()
        try:
            db.add(
                FeishuBinding(
                    entity_type="response",
                    entity_id=999999,
                    table_key="responses",
                    remote_record_id="rec_dup_a",
                )
            )
            db.commit()
            db.add(
                FeishuBinding(
                    entity_type="response",
                    entity_id=999999,
                    table_key="responses",
                    remote_record_id="rec_dup_b",
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()
            db.query(FeishuBinding).filter(
                FeishuBinding.entity_id == 999999
            ).delete()
            db.commit()
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
