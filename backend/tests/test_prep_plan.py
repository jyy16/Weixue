"""Tests for the lesson-prep plan: API persistence, Bitable record, bot card.

The API lifecycle runs FastAPI in a child process with an isolated SQLite
database (same pattern as test_assessment_flow.py); Feishu credentials are
purged so the push endpoint reports 待联调 instead of hitting the network.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

_TMP_DIR = tempfile.mkdtemp(prefix="weixue_prep_plan_test_")
os.environ["WEIXUE_DB_PATH"] = os.path.join(_TMP_DIR, "test.db")

# feishu.client's import runs load_dotenv on backend/.env; purge any real
# credentials so unconfigured paths stay deterministic on live machines.
for _key in [k for k in os.environ if k.startswith("FEISHU_")]:
    os.environ.pop(_key, None)

from database import Course, DebateTopic, PrepPlan, SessionLocal, init_db  # noqa: E402
from feishu.bot import BotService  # noqa: E402
from feishu.card_actions import dispatch_card_action  # noqa: E402
from feishu.sync import build_prep_plan_record  # noqa: E402


class PrepPlanRecordTests(unittest.TestCase):
    def test_draft_and_confirmed_record(self):
        course = SimpleNamespace(class_name="思辨一班")
        topic_map = {
            1: SimpleNamespace(title="动物应该养在动物园吗？"),
            2: SimpleNamespace(title="手机该不该带进课堂？"),
        }
        plan = SimpleNamespace(
            lesson_plan=[1, 2],
            notes={"1": "先讲证据意识", "2": ""},
            confirmed=False,
            updated_at=None,
            summary={"overview": "整体良好", "problems": "结构偏弱"},
        )
        fields = build_prep_plan_record(plan, course, topic_map)["fields"]
        self.assertEqual(fields["班级"], "思辨一班")
        self.assertEqual(fields["计划状态"], "草稿")
        self.assertIn("整体良好", fields["AI总结"])
        self.assertIn("结构偏弱", fields["AI总结"])
        self.assertIn("1. 动物应该养在动物园吗？", fields["讲评顺序"])
        self.assertIn("2. 手机该不该带进课堂？", fields["讲评顺序"])
        self.assertIn("先讲证据意识", fields["备注"])
        self.assertNotIn("手机该不该带进课堂", fields["备注"])
        self.assertIsInstance(fields["更新时间"], int)

        plan.confirmed = True
        fields = build_prep_plan_record(plan, course, topic_map)["fields"]
        self.assertEqual(fields["计划状态"], "已确认")


class PrepPlanCardTests(unittest.TestCase):
    def test_card_buttons_and_values(self):
        card = BotService.build_prep_plan_card(
            title="思辨星 · 思辨一班 讲评计划",
            content="**讲评顺序**\n1. 动物应该养在动物园吗？",
            course_id=7,
            change_url="http://example.test/?tab=prep",
        )
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["title"]["content"], "思辨星 · 思辨一班 讲评计划")
        buttons = card["body"]["elements"][1:]
        jump = next(
            (b for b in buttons if b.get("url", "").startswith("http://")),
            None,
        )
        self.assertIsNotNone(jump)
        self.assertIn("http://example.test/?tab=prep", jump["url"])
        confirm = next(b for b in buttons if b["value"]["action"] == "prep_confirm")
        self.assertEqual(confirm["value"]["course_id"], 7)

        # Without a change_url the jump button falls back to a callback action.
        card_no_url = BotService.build_prep_plan_card(
            title="t", content="c", course_id=7, change_url=""
        )
        fallback_actions = {
            b["value"]["action"]
            for b in card_no_url["body"]["elements"][1:]
            if b.get("value")
        }
        self.assertEqual(fallback_actions, {"prep_confirm", "prep_open"})


class PrepCardContentTests(unittest.TestCase):
    """Card markdown builder: stats block, structured per-topic lines, no-data hint."""

    def test_card_content_blocks(self):
        # 惰性导入：避免在收集阶段 import main 把 backend/.env 的飞书凭证
        # 灌回共享 os.environ，影响其他测试模块。
        import main as main_module
        course = SimpleNamespace(class_name="思辨一班", grade_level=4)
        plan = SimpleNamespace(
            lesson_plan=[1, 2, 3],
            notes={"2": "先讲结构"},
            confirmed=True,
            summary={
                "overview": "整体均分2.9，参与积极。",
                "problems": "- 选材维度偏弱。\n- 结构维度偏弱。",
                "topics": {"1": {"overview": "本题亮点较多。", "generated_by": "llm"}},
            },
        )
        rows = {
            1: {
                "title": "老鹰题",
                "avg_dimension_scores": {"position": 3.0, "material": 2.5},
                "weak_dimensions": ["material"],
                "low_students": ["小雨(1.0)"],
            },
            2: {
                "title": "动物园观点题",
                "avg_dimension_scores": {},
                "weak_dimensions": [],
                "low_students": [],
            },
            3: {
                "title": "濒危动物题",
                "avg_dimension_scores": {"position": 3.33},
                "weak_dimensions": [],
                "low_students": [],
            },
        }
        insights = {
            "participation": {
                "students_answered": 9, "students_total": 9,
                "responses_total": 9, "class_avg": 2.9,
            },
            "tier_summary": {"developing": {"students": 9, "avg_score": 2.9}},
            "top_tags": [{"tag": "结构清晰", "count": 5}],
            "highlights": [
                {"student_name": "小明", "topic_title": "老鹰题", "avg": 4.0, "bonus_flags": ["有新意"]},
            ],
            "topic_highlights": [],
            "problem_patterns": [],
        }
        card = main_module._build_prep_plan_card_content(course, plan, rows, insights)
        self.assertIn("**总体统计**", card)
        self.assertIn("参评 9/9 人", card)
        self.assertIn("班级均分 2.9", card)
        self.assertIn("全课高频标签", card)
        self.assertIn("维度均分：立意：3.0；选材：2.5", card)
        self.assertIn("薄弱：选材", card)
        self.assertIn("低分学生：小雨(1.0)", card)
        self.assertIn("暂无评估数据", card)
        self.assertIn("**优质发言**", card)
        self.assertIn("小明《老鹰题》均分4.0（有新意）", card)
        self.assertIn("本题总结 · 老鹰题", card)
        self.assertIn("**总体情况**\n整体均分2.9，参与积极。", card)


class PrepPlanCardActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        db = SessionLocal()
        course = Course(title="测试课程", class_name="思辨一班", grade_level=3)
        db.add(course)
        db.flush()
        topic = DebateTopic(
            course_id=course.id, title="动物应该养在动物园吗？",
            topic_type="dilemma", cognitive_tier="developing",
        )
        db.add(topic)
        db.flush()
        db.add(PrepPlan(course_id=course.id, lesson_plan=[topic.id], confirmed=False))
        db.commit()
        cls.course_id = course.id
        db.close()

    def test_confirm_action_sets_flag(self):
        db = SessionLocal()
        result = dispatch_card_action(
            db, {"action": "prep_confirm", "course_id": self.course_id}
        )
        self.assertEqual(result["toast"]["type"], "success")
        plan = (
            db.query(PrepPlan)
            .filter(PrepPlan.course_id == self.course_id)
            .first()
        )
        self.assertTrue(plan.confirmed)
        db.close()

    def test_confirm_unknown_course_is_honest(self):
        db = SessionLocal()
        result = dispatch_card_action(
            db, {"action": "prep_confirm", "course_id": 99999}
        )
        self.assertEqual(result["toast"]["type"], "error")
        db.close()


class PrepPlanAPITests(unittest.TestCase):
    """Full API lifecycle in a child process (isolated DB, no Feishu creds)."""

    def test_plan_get_put_validate_push(self):
        script = r"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="weixue_prep_api_")
os.environ["WEIXUE_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["ASR_API_KEY"] = ""
os.environ["ASR_PROVIDER"] = "mock"
for k in [k for k in os.environ if k.startswith("FEISHU_")]:
    os.environ.pop(k, None)
sys.path.insert(0, os.getcwd())

import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
import main
import seed
from fastapi.testclient import TestClient
from database import Course, PrepPlan, SessionLocal

# feishu.client's module-level load_dotenv re-populates FEISHU_* from
# backend/.env even after we purge them; reset the config objects so the push
# path is deterministic (待联调) and no network call is attempted.
_cfg = main.feishu_client.config
_cfg.app_id = ""
_cfg.app_secret = ""
_cfg.teacher_open_id = ""
_cfg.bitable_app_token = ""
_cfg.bitable_table_ids = {}

out = {}
with TestClient(main.app) as client:
    seed.seed(force=True)
    db = SessionLocal()
    course = db.query(Course).first()
    cid = course.id
    tids = [t.id for t in course.topics]
    out["topic_ids"] = tids
    db.close()

    # Fresh course -> empty draft plan
    plan = client.get(f"/api/courses/{cid}/prep/plan").json()
    out["initial"] = plan

    # Save a plan with order + notes + confirmed
    body = {
        "lesson_plan": [tids[1], tids[0]],
        "notes": {str(tids[0]): "先讲证据意识"},
        "confirmed": True,
    }
    saved = client.put(f"/api/courses/{cid}/prep/plan", json=body).json()
    out["saved"] = saved

    # Round-trip read
    again = client.get(f"/api/courses/{cid}/prep/plan").json()
    out["again"] = again

    # Reject topics that do not belong to the course
    bad = client.put(
        f"/api/courses/{cid}/prep/plan",
        json={"lesson_plan": [999999], "notes": {}, "confirmed": False},
    )
    out["bad_status"] = bad.status_code

    # Push without Feishu teacher binding -> honest 待联调, never fake success
    pushed = client.post(f"/api/courses/{cid}/prep/plan/push").json()
    out["pushed"] = pushed

print(json.dumps(out, ensure_ascii=False))
"""
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["initial"]["lesson_plan"], [])
        self.assertFalse(out["initial"]["confirmed"])
        self.assertEqual(out["saved"]["lesson_plan"], [out["topic_ids"][1], out["topic_ids"][0]])
        self.assertTrue(out["saved"]["confirmed"])
        self.assertEqual(out["again"], out["saved"])
        self.assertEqual(out["bad_status"], 400)
        self.assertEqual(out["pushed"]["ok"], True)
        self.assertEqual(out["pushed"]["status"], "pending_delivery")


class PrepInsightsAPITests(unittest.TestCase):
    """Insights endpoint + LLM summary fallback in an isolated child process."""

    def test_insights_and_summary_fallback(self):
        script = r"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="weixue_prep_insights_")
os.environ["WEIXUE_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["LLM_API_KEY"] = ""
os.environ["ASR_API_KEY"] = ""
os.environ["ASR_PROVIDER"] = "mock"
for k in [k for k in os.environ if k.startswith("FEISHU_")]:
    os.environ.pop(k, None)
sys.path.insert(0, os.getcwd())

import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
import main
import seed
from fastapi.testclient import TestClient
from database import Course, PrepPlan, SessionLocal

_cfg = main.feishu_client.config
_cfg.app_id = ""
_cfg.app_secret = ""
_cfg.teacher_open_id = ""

out = {}
with TestClient(main.app) as client:
    seed.seed(force=True)
    cid = SessionLocal().query(Course).first().id
    tids = [t.id for t in SessionLocal().query(Course).first().topics]
    client.put(f"/api/courses/{cid}/prep/plan", json={
        "lesson_plan": tids, "notes": {}, "confirmed": True,
    })

    ins = client.get(f"/api/courses/{cid}/prep/insights").json()
    out["participation"] = ins["participation"]
    out["quick_rating_counts"] = ins["quick_rating_counts"]
    out["per_topic_quick"] = ins["participation"]["per_topic"][0].get("quick_ratings")
    out["highlight_count"] = len(ins["highlights"])
    out["topic_highlight_count"] = len(ins["topic_highlights"])
    out["problem_count"] = len(ins["problem_patterns"])
    out["tier_keys"] = sorted(ins["tier_summary"].keys())
    out["top_tags"] = ins["top_tags"]
    if ins["highlights"]:
        out["first_highlight"] = {
            k: ins["highlights"][0][k]
            for k in ("student_name", "topic_title", "avg", "bonus_flags")
        }

    report = client.get(f"/api/courses/{cid}/report").json()
    out["report_quick"] = report["quick_rating_counts"]
    out["report_student_quick"] = report["student_stats"][0]["quick_ratings"]

    summary = client.post(f"/api/courses/{cid}/prep/summary").json()
    out["summary"] = summary

    # Normalization: no label prefix, problems are bullets
    out["summary_problems_starts_dash"] = summary["problems"].startswith("- ")
    out["summary_overview_has_prefix"] = summary["overview"].startswith("总体情况")

    plan = client.get(f"/api/courses/{cid}/prep/plan").json()
    out["plan_summary_persisted"] = plan["summary"]

    # Per-topic summary generation (template fallback) + persistence
    tid = out["participation"]["per_topic"][0]["topic_id"]
    ts = client.post(f"/api/courses/{cid}/prep/topics/{tid}/summary").json()
    out["topic_summary"] = ts

    # Teacher edits a topic summary -> persisted with edited=True
    edited = client.put(f"/api/courses/{cid}/prep/summary", json={
        "topic_id": tid,
        "overview": "教师改写后的本题总结",
    }).json()
    out["topic_edited"] = edited["topics"][str(tid)]

    # Teacher edits the class summary -> edited=True
    class_edited = client.put(f"/api/courses/{cid}/prep/summary", json={
        "overview": "教师改写后的总体总结",
    }).json()
    out["class_edited"] = class_edited

    # Card content: deterministic stats + structured per-topic lines
    db = SessionLocal()
    course = db.get(Course, cid)
    plan = db.query(PrepPlan).filter(PrepPlan.course_id == cid).first()
    rows = {r["topic_id"]: r for r in main._prep_topic_rows(cid, db)}
    insights = main._prep_insights(cid, db)
    card = main._build_prep_plan_card_content(course, plan, rows, insights)
    out["card"] = card
    db.close()

print(json.dumps(out, ensure_ascii=False))
"""
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(out["participation"]["students_total"], 9)
        self.assertGreaterEqual(out["participation"]["responses_total"], 1)
        self.assertIn("class_avg", out["participation"])
        self.assertGreaterEqual(out["highlight_count"], 1)
        self.assertGreaterEqual(out["topic_highlight_count"], 1)
        self.assertEqual(sum(out["quick_rating_counts"].values()), 9)
        self.assertEqual(set(out["per_topic_quick"].keys()), {"good", "guide", "echo"})
        self.assertEqual(sum(out["report_quick"].values()), 9)
        self.assertEqual(set(out["report_student_quick"].keys()), {"good", "guide", "echo"})
        self.assertIn("tier_keys", out)
        self.assertIsInstance(out["top_tags"], list)
        self.assertEqual(out["summary"]["generated_by"], "template")
        self.assertTrue(out["summary"]["overview"])
        self.assertTrue(out["summary"]["problems"])
        self.assertTrue(out["summary"]["suggestions"])
        self.assertEqual(
            out["plan_summary_persisted"]["overview"],
            out["summary"]["overview"],
        )
        self.assertEqual(out["topic_summary"]["generated_by"], "template")
        self.assertTrue(out["topic_summary"]["overview"])
        self.assertTrue(out["topic_summary"]["problems"].startswith("- "))
        self.assertTrue(out["topic_edited"]["edited"])
        self.assertEqual(out["topic_edited"]["overview"], "教师改写后的本题总结")
        self.assertTrue(out["class_edited"]["edited"])
        self.assertEqual(out["class_edited"]["overview"], "教师改写后的总体总结")
        self.assertTrue(out["summary_problems_starts_dash"])
        self.assertFalse(out["summary_overview_has_prefix"])
        self.assertIn("**总体统计**", out["card"])
        self.assertIn("全课高频标签", out["card"])
        self.assertIn("**优质发言**", out["card"])
        self.assertIn("维度均分：", out["card"])


if __name__ == "__main__":
    unittest.main()
