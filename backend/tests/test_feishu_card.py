"""Tests for the Feishu interactive-card callback (signature + dispatch).

Unit-level checks (signature algorithm, card payload decryption, card JSON
shape) run in-process. The API-level tests (button dispatch writes to the DB,
toast responses, auth failures) run in a child process with an isolated SQLite
database, mirroring test_asr.py so we never touch the real grading.db.
"""

import hashlib
import json
import os
import subprocess
import sys
import unittest

import httpx

from feishu.bitable import BitableService, TABLE_RESPONSES, SINGLE_SELECT_OPTIONS
from feishu.bot import BotService
from feishu.client import FeishuClient, FeishuConfig


def _config(token="vt_test", encrypt_key="") -> FeishuConfig:
    cfg = FeishuConfig()
    cfg.verification_token = token
    cfg.encrypt_key = encrypt_key
    return cfg


class CardSignatureTests(unittest.TestCase):
    def test_valid_sha1_signature_passes(self):
        cfg = _config(token="vt_test")
        raw = b'{"schema":"2.0","header":{"token":"vt_test"}}'
        ts, nonce = "1700000000", "n1"
        sig = hashlib.sha1(f"{ts}{nonce}vt_test".encode() + raw).hexdigest()
        self.assertTrue(
            BotService.verify_card_signature(cfg, raw, ts, nonce, sig)
        )

    def test_tampered_body_fails(self):
        cfg = _config(token="vt_test")
        raw = b'{"schema":"2.0","header":{"token":"vt_test"}}'
        ts, nonce = "1700000000", "n1"
        sig = hashlib.sha1(f"{ts}{nonce}vt_test".encode() + raw).hexdigest()
        self.assertFalse(
            BotService.verify_card_signature(
                cfg, b'{"schema":"2.0","header":{"token":"EVIL"}}', ts, nonce, sig
            )
        )

    def test_missing_config_or_headers_rejects(self):
        cfg = _config(token="")
        raw = b"{}"
        self.assertFalse(
            BotService.verify_card_signature(cfg, raw, "1", "n", "s")
        )
        cfg = _config(token="vt_test")
        self.assertFalse(BotService.verify_card_signature(cfg, b"", "1", "n", "s"))
        self.assertFalse(BotService.verify_card_signature(cfg, raw, "", "n", "s"))


class CardPayloadTests(unittest.TestCase):
    def test_plain_payload_passes_through(self):
        cfg = _config()
        raw = b'{"schema":"2.0","header":{"token":"vt_test"}}'
        self.assertEqual(BotService.decrypt_card_payload(cfg, raw), raw)

    def test_invalid_json_raises(self):
        cfg = _config()
        with self.assertRaises(ValueError):
            BotService.decrypt_card_payload(cfg, b"not json")

    def test_encrypted_payload_is_decrypted(self):
        # Round-trip with the same AES scheme used by _decrypt_event.
        import base64

        try:
            from cryptography.hazmat.primitives import padding
            from cryptography.hazmat.primitives.ciphers import (
                Cipher,
                algorithms,
                modes,
            )
        except ImportError:
            self.skipTest("cryptography not installed in this venv")

        encrypt_key = "0123456789abcdef0123456789abcdef"
        cfg = _config(encrypt_key=encrypt_key)
        inner = b'{"schema":"2.0","header":{"token":"vt_test"}}'
        key = encrypt_key.encode("utf-8")[:32]
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        enc = cipher.encryptor()
        padder = padding.PKCS7(128).padder()
        padded = padder.update(inner) + padder.finalize()
        blob = base64.b64encode(enc.update(padded) + enc.finalize()).decode()
        raw = json.dumps({"encrypt": blob}).encode("utf-8")
        self.assertEqual(BotService.decrypt_card_payload(cfg, raw), inner)


class CommentCardTests(unittest.TestCase):
    def test_card_has_three_buttons_with_entity_ids(self):
        card = BotService.build_comment_card(
            title="思辨星 · 小雨 评语确认",
            content="**学生**：小雨",
            course_id=1,
            student_id=2,
            response_id=3,
            comment_hash="hash_v1",
        )
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(
            card["header"]["title"]["content"], "思辨星 · 小雨 评语确认"
        )
        # Schema 2.0: buttons sit directly in body.elements (no "action" container).
        buttons = card["body"]["elements"][1:]
        self.assertEqual(len(buttons), 3)
        names = {b["text"]["content"]: b["value"]["action"] for b in buttons}
        self.assertEqual(names["确认评分"], "review_confirm")
        self.assertEqual(names["去网页修改"], "request_change")
        self.assertEqual(names["发送给学生"], "send_comment")
        for b in buttons:
            self.assertEqual(b["value"]["course_id"], 1)
            self.assertEqual(b["value"]["student_id"], 2)
            self.assertEqual(b["value"]["response_id"], 3)
            self.assertEqual(b["value"]["comment_hash"], "hash_v1")

    def test_student_card_is_read_only_and_contains_comment(self):
        card = BotService.build_student_comment_card(
            student_name="小雨",
            comment="# 我的目标\n*认真倾听*\n- 再补充一个理由",
        )
        self.assertEqual(card["header"]["template"], "green")
        self.assertIn("小雨", card["header"]["title"]["content"])
        elements = card["body"]["elements"]
        self.assertEqual(elements[0]["tag"], "div")
        self.assertEqual(elements[0]["text"]["tag"], "plain_text")
        self.assertEqual(
            elements[0]["text"]["content"],
            "# 我的目标\n*认真倾听*\n- 再补充一个理由",
        )
        self.assertFalse(any(e.get("tag") == "button" for e in elements))


class BitableSchemaTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, calls: dict) -> BitableService:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/tenant_access_token/internal/"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "msg": "success",
                        "tenant_access_token": "t-test",
                        "expire": 7200,
                    },
                )
            path = request.url.path
            if path.endswith("/tables/tbl_resp/fields") and request.method == "GET":
                return httpx.Response(200, json={"code": 0, "data": {"items": calls["existing"]}})
            if path.endswith("/tables/tbl_resp/fields") and request.method == "POST":
                calls["created"].append(json.loads(request.content.decode()))
                return httpx.Response(200, json={"code": 0, "data": {"field": {}}})
            if "/fields/" in path and request.method == "PUT":
                calls["updated"].append(json.loads(request.content.decode()))
                return httpx.Response(200, json={"code": 0, "data": {"field": {}}})
            return httpx.Response(404, json={"code": 99999, "msg": path})

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport, base_url="https://example.test")
        config = FeishuConfig()
        config.app_id = "cli_test"
        config.app_secret = "secret"
        config.base_url = "https://example.test"
        config.bitable_app_token = "bascn_test"
        config.bitable_table_ids = {"responses": "tbl_resp"}
        client = FeishuClient(config=config, http_client=http)
        return BitableService(client)

    async def test_ensure_schema_creates_missing_fields_then_updates_options(self):
        calls = {"existing": [], "created": [], "updated": []}
        service = self._service(calls)

        # First run: no fields exist -> every schema field is created, select
        # fields carry their option lists.
        report = await service.ensure_schema({"responses": TABLE_RESPONSES})
        self.assertEqual(report["responses"]["status"], "ok")
        self.assertEqual(len(calls["created"]), len(TABLE_RESPONSES))
        status_field = next(
            c for c in calls["created"] if c["field_name"] == "状态"
        )
        self.assertEqual(
            [o["name"] for o in status_field["property"]["options"]],
            SINGLE_SELECT_OPTIONS["状态"],
        )
        self.assertEqual(calls["updated"], [])

        # Second run: fields exist but a select field misses an option.
        calls["created"] = []
        calls["updated"] = []
        calls["existing"] = [
            {
                "field_id": f"fld_{i}",
                "field_name": name,
                "type": ftype,
                "property": (
                    {
                        "options": [
                            {"name": o}
                            for o in SINGLE_SELECT_OPTIONS.get(name, [])[:-1]
                        ]
                    }
                    if name in SINGLE_SELECT_OPTIONS
                    else {}
                ),
            }
            for i, (name, ftype) in enumerate(TABLE_RESPONSES.items())
        ]
        report = await service.ensure_schema({"responses": TABLE_RESPONSES})
        self.assertEqual(report["responses"]["status"], "ok")
        self.assertEqual(calls["created"], [])
        self.assertEqual(
            {u["property"]["options"][-1]["name"] for u in calls["updated"]},
            {
                SINGLE_SELECT_OPTIONS[name][-1]
                for name in ("来源", "AI置信度", "加分项", "状态")
            },
        )


_CHILD_SCRIPT = r"""
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
import httpx

_TEST_TMP_ROOT = os.path.join(os.path.dirname(os.getcwd()), "tmp", "tests")
os.makedirs(_TEST_TMP_ROOT, exist_ok=True)
_TMP = tempfile.mkdtemp(prefix="weixue_feishu_card_child_", dir=_TEST_TMP_ROOT)
os.environ["WEIXUE_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["FEISHU_VERIFICATION_TOKEN"] = "vt_test"

# Neutralize load_dotenv BEFORE any project import: several modules call it at
# import time (feishu.client and grading.llm both do, the latter via main's
# import chain), which would re-load live credentials from backend/.env after
# any env purge and break the "unconfigured" fallback tests on machines that
# already have a live Feishu config. Patching first guarantees isolation.
import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False
# The parent test process may already have loaded the developer's real .env.
# Never let the isolated child inherit live Feishu recipients or credentials.
for _key in [k for k in os.environ if k.startswith("FEISHU_")]:
    os.environ.pop(_key, None)
os.environ["FEISHU_VERIFICATION_TOKEN"] = "vt_test"

from fastapi.testclient import TestClient  # noqa: E402

from database import (  # noqa: E402
    CalibrationRecord,
    Course,
    DebateTopic,
    DimensionTag,
    Student,
    StudentResponse,
    SessionLocal,
    init_db,
)
from main import app  # noqa: E402
from feishu.card_actions import dispatch_card_action  # noqa: E402
from feishu.comment_delivery import deliver_student_comment  # noqa: E402
from feishu.client import FeishuClient, FeishuConfig  # noqa: E402

init_db()
db = SessionLocal()
course = Course(title="测试课", class_name="三年级", grade_level=3)
db.add(course)
db.flush()
topic = DebateTopic(
    course_id=course.id,
    title="手机该不该进校园",
    order=1,
    topic_type="dilemma",
)
db.add(topic)
db.flush()
student = Student(
    course_id=course.id,
    name="小雨",
    grade=3,
    comment_draft="你表达很清晰，继续加油！",
)
db.add(student)
db.flush()
resp = StudentResponse(
    student_id=student.id,
    topic_id=topic.id,
    raw_text="我认为应该限制使用。",
    ai_dimension_scores={"position": "B", "material": "A-", "structure": "B+", "language": "A", "perspective": "B+"},
    ai_suggested_tags=["选材具体"],
    teacher_reviewed=False,
    processing_status="submitted",
)
db.add(resp)
db.commit()
cid = course.id
sid = student.id
rid = resp.id
db.close()

client = TestClient(app)


def signed_post(payload, extra_headers=None, token="vt_test", header_token=None):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts, nonce = "1700000000", "n1"
    sig = hashlib.sha1(f"{ts}{nonce}{token}".encode() + raw).hexdigest()
    headers = {
        "X-Lark-Request-Timestamp": ts,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": sig,
        "Content-Type": "application/json",
    }
    headers.update(extra_headers or {})
    return client.post("/api/feishu/card", content=raw, headers=headers)


def card(action, value_extra=None, header_token=None):
    value = {
        "action": action,
        "response_id": rid,
        "course_id": cid,
        "student_id": sid,
    }
    if value_extra:
        value.update(value_extra)
    return {
        "schema": "2.0",
        "header": {
            "token": header_token if header_token is not None else "vt_test",
            "event_type": "card.action.trigger",
            "event_id": "evt_1",
            "app_id": "cli_test",
        },
        "event": {
            "operator": {"open_id": "ou_teacher"},
            "token": "card_token",
            "action": {"tag": "button", "value": value},
            "context": {"open_message_id": "om_1"},
        },
    }


results = {}

# 1. Confirm review with a teacher modification -> calibration record created.
r = signed_post(
    card(
        "review_confirm",
        value_extra={"dimension_scores": {"position": "A", "material": "A", "structure": "A", "language": "A", "perspective": "A"}},
    )
)
results["confirm_status"] = r.status_code
results["confirm_toast"] = r.json().get("toast", {})

db = SessionLocal()
resp = db.get(StudentResponse, rid)
results["teacher_reviewed"] = bool(resp.teacher_reviewed)
results["teacher_scores"] = resp.teacher_dimension_scores
results["processing_status"] = resp.processing_status
results["calibrations"] = db.query(CalibrationRecord).count()
results["tag_use_count"] = (
    db.query(DimensionTag)
    .filter(DimensionTag.name == "选材具体")
    .first()
    .use_count
)
db.close()

# 2. request_change -> info toast only.
r = signed_post(card("request_change"))
results["change_toast"] = r.json().get("toast", {})

# 3. An unbound student is rejected honestly.
r = signed_post(card("send_comment"))
results["send_toast"] = r.json().get("toast", {})

# 3b. The student API validates and exposes the binding.
r = client.put("/api/students/%d" % sid, json={"feishu_open_id": "bad_id"})
results["bad_open_id_status"] = r.status_code
r = client.put("/api/students/%d" % sid, json={"feishu_open_id": "ou_student"})
results["bind_status"] = r.status_code
results["bound_open_id"] = r.json().get("feishu_open_id")

# Reserve one delivery and reject a duplicate click.
db = SessionLocal()
scheduled = []
r1 = dispatch_card_action(
    db,
    card("send_comment")["event"]["action"]["value"],
    schedule_comment_delivery=lambda student_id, draft_hash: scheduled.append(
        (student_id, draft_hash)
    ),
)
r2 = dispatch_card_action(
    db,
    card("send_comment")["event"]["action"]["value"],
    schedule_comment_delivery=lambda student_id, draft_hash: scheduled.append(
        (student_id, draft_hash)
    ),
)
student = db.get(Student, sid)
delivery_hash = student.comment_delivery_hash
results["reserved_toast"] = r1.get("toast", {})
results["duplicate_toast"] = r2.get("toast", {})
results["scheduled_count"] = len(scheduled)
results["reserved_status"] = student.comment_delivery_status
db.close()

# A stale teacher card cannot send a draft changed after the card was issued.
db = SessionLocal()
student = db.get(Student, sid)
student.comment_delivery_status = "not_sent"
student.comment_delivery_hash = ""
db.commit()
stale_value = card("send_comment")["event"]["action"]["value"]
stale_value["comment_hash"] = hashlib.sha256(b"old comment").hexdigest()
stale = dispatch_card_action(
    db,
    stale_value,
    schedule_comment_delivery=lambda *_: scheduled.append(("stale", "stale")),
)
results["stale_toast"] = stale.get("toast", {})
results["scheduled_after_stale"] = len(scheduled)
# Restore the legitimate reservation for the mocked delivery below.
student.comment_delivery_status = "sending"
student.comment_delivery_hash = delivery_hash
db.commit()
db.close()

# 3c. Deliver with a fake Feishu API and persist the successful result.
sent = {}
async def handler(request):
    if request.url.path.endswith("/tenant_access_token/internal/"):
        return httpx.Response(200, json={
            "code": 0, "msg": "success",
            "tenant_access_token": "t-test", "expire": 7200,
        })
    if request.url.path.endswith("/im/v1/messages"):
        sent.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"code": 0, "msg": "success", "data": {}})
    return httpx.Response(404, json={"code": 99999, "msg": request.url.path})

config = FeishuConfig()
config.app_id = "cli_test"
config.app_secret = "secret"
config.base_url = "https://example.test"
http = httpx.AsyncClient(
    transport=httpx.MockTransport(handler), base_url="https://example.test"
)
feishu_client = FeishuClient(config=config, http_client=http)
import asyncio
asyncio.run(deliver_student_comment(sid, delivery_hash, feishu_client))
asyncio.run(feishu_client.close())
db = SessionLocal()
student = db.get(Student, sid)
results["delivered_status"] = student.comment_delivery_status
results["delivery_error"] = student.comment_delivery_error
results["sent_receive_id"] = sent.get("receive_id")
results["sent_msg_type"] = sent.get("msg_type")
db.close()

# 3d. Regenerating explicitly starts a new delivery cycle even if the model
# happens to return byte-for-byte identical text.
import main as main_module

class SameDraftLLM:
    async def chat(self, **kwargs):
        return "你表达很清晰，继续加油！"

main_module.LLMClient = SameDraftLLM
db = SessionLocal()
resp = db.get(StudentResponse, rid)
resp.teacher_reviewed = True
db.commit()
db.close()
r = client.post(
    "/api/courses/%d/comments" % cid,
    json={"student_id": sid},
)
results["regenerate_status"] = r.status_code
db = SessionLocal()
student = db.get(Student, sid)
results["status_after_same_regenerate"] = student.comment_delivery_status
results["hash_after_same_regenerate"] = student.comment_delivery_hash
results["delivered_at_after_same_regenerate"] = (
    student.comment_delivered_at.isoformat() if student.comment_delivered_at else None
)
student.comment_delivery_status = "delivered"
student.comment_delivery_hash = hashlib.sha256(
    student.comment_draft.encode("utf-8")
).hexdigest()
student.comment_delivered_at = datetime.now(timezone.utc)
db.commit()
db.close()

# 3e. Batch regeneration follows the same new-delivery semantics.
r = client.post("/api/courses/%d/comments/batch" % cid)
results["batch_regenerate_status"] = r.status_code
db = SessionLocal()
student = db.get(Student, sid)
results["status_after_same_batch_regenerate"] = student.comment_delivery_status
results["hash_after_same_batch_regenerate"] = student.comment_delivery_hash
results["delivered_at_after_same_batch_regenerate"] = (
    student.comment_delivered_at.isoformat() if student.comment_delivered_at else None
)
db.close()

# 3f. Editing the draft creates a new unsent delivery item.
r = client.post(
    "/api/courses/%d/comments/save" % cid,
    json={"student_id": sid, "draft": "这是一份更新后的评语。"},
)
results["draft_save_status"] = r.status_code
db = SessionLocal()
student = db.get(Student, sid)
results["status_after_edit"] = student.comment_delivery_status
results["hash_after_edit"] = student.comment_delivery_hash
db.close()

# 4. Bad signature -> 401.
r = signed_post(
    card("review_confirm"), extra_headers={"X-Lark-Signature": "deadbeef"}
)
results["bad_signature_status"] = r.status_code

# 5. header.token mismatch -> 403.
r = signed_post(card("review_confirm", header_token="vt_other"))
results["token_mismatch_status"] = r.status_code

# 6. Unknown action -> warning toast, HTTP 200.
r = signed_post(card("unknown_action"))
results["unknown_toast"] = r.json().get("toast", {})

# 7. Event subscription: challenge + ack for im.message.receive_v1.
r = client.post(
    "/api/feishu/events",
    json={"type": "url_verification", "challenge": "ch_1", "token": "vt_test"},
)
results["challenge"] = r.json()
r = client.post(
    "/api/feishu/events",
    json={
        "type": "im.message.receive_v1",
        "token": "vt_test",
        "event": {
            "message": {
                "message_id": "om_9",
                "message_type": "text",
                "content": json.dumps({"text": "帮助"}, ensure_ascii=False),
            }
        },
    },
)
results["event_ack_status"] = r.status_code

# 8. Web comment send without Feishu config -> saved, honestly 待联调.
r = client.post(
    "/api/courses/%d/comments/send"
    % cid,
    json={"student_id": sid, "draft": "你的表达很有条理，继续保持！"},
)
results["web_send_status"] = r.status_code
results["web_send_body"] = r.json()

print("FEISHU_CARD_TEST_RESULT " + json.dumps(results, ensure_ascii=False))
"""


class CardCallbackAPITests(unittest.TestCase):
    def test_card_and_event_flow_in_isolated_child_process(self):
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=os.getcwd(),
            env={**dict(os.environ), "PYTHONIOENCODING": "utf-8"},
            timeout=120,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"child process failed\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        result = None
        for line in proc.stdout.splitlines():
            if line.startswith("FEISHU_CARD_TEST_RESULT "):
                result = json.loads(line[len("FEISHU_CARD_TEST_RESULT ") :])
        self.assertIsNotNone(result, msg=proc.stdout)

        self.assertEqual(result["confirm_status"], 200)
        self.assertEqual(result["confirm_toast"]["type"], "success")
        self.assertTrue(result["teacher_reviewed"])
        self.assertEqual(result["teacher_scores"], {"position": "A", "material": "A", "structure": "A", "language": "A", "perspective": "A"})
        self.assertEqual(result["processing_status"], "processed")
        self.assertEqual(result["calibrations"], 1)
        self.assertEqual(result["tag_use_count"], 1)

        self.assertEqual(result["change_toast"]["type"], "info")
        self.assertEqual(result["send_toast"]["type"], "warning")
        self.assertIn("尚未绑定飞书账号", result["send_toast"]["content"])
        self.assertEqual(result["reserved_toast"]["type"], "success")
        self.assertEqual(result["bad_open_id_status"], 400)
        self.assertEqual(result["bind_status"], 200)
        self.assertEqual(result["bound_open_id"], "ou_student")
        self.assertEqual(result["duplicate_toast"]["type"], "info")
        self.assertEqual(result["scheduled_count"], 1)
        self.assertEqual(result["reserved_status"], "sending")
        self.assertEqual(result["stale_toast"]["type"], "warning")
        self.assertIn("重新推送确认卡", result["stale_toast"]["content"])
        self.assertEqual(result["scheduled_after_stale"], 1)
        self.assertEqual(result["delivered_status"], "delivered")
        self.assertEqual(result["delivery_error"], "")
        self.assertEqual(result["sent_receive_id"], "ou_student")
        self.assertEqual(result["sent_msg_type"], "interactive")
        self.assertEqual(result["regenerate_status"], 200)
        self.assertEqual(result["status_after_same_regenerate"], "not_sent")
        self.assertEqual(result["hash_after_same_regenerate"], "")
        self.assertIsNone(result["delivered_at_after_same_regenerate"])
        self.assertEqual(result["batch_regenerate_status"], 200)
        self.assertEqual(result["status_after_same_batch_regenerate"], "not_sent")
        self.assertEqual(result["hash_after_same_batch_regenerate"], "")
        self.assertIsNone(result["delivered_at_after_same_batch_regenerate"])
        self.assertEqual(result["draft_save_status"], 200)
        self.assertEqual(result["status_after_edit"], "not_sent")
        self.assertEqual(result["hash_after_edit"], "")

        self.assertEqual(result["bad_signature_status"], 401)
        self.assertEqual(result["token_mismatch_status"], 403)
        self.assertEqual(result["unknown_toast"]["type"], "warning")

        self.assertEqual(result["challenge"], {"challenge": "ch_1"})
        self.assertEqual(result["event_ack_status"], 200)

        self.assertEqual(result["web_send_status"], 200)
        self.assertEqual(result["web_send_body"]["status"], "saved_pending_delivery")
        self.assertIn("待联调", result["web_send_body"]["message"])


if __name__ == "__main__":
    unittest.main()
