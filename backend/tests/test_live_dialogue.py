"""First-round speech must enter the dialogue history (regression test).

The live classroom, teacher timeline and student window's 3s poll all read
companion_turns; without this the initial answer only lived in raw_text and
vanished from every dialogue view.
"""

import json
import os
import subprocess
import sys
import unittest


class FirstRoundDialogueTests(unittest.TestCase):
    def test_import_text_records_first_student_turn(self):
        script = r"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="weixue_dialogue_")
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
from database import Course, Student, DebateTopic, SessionLocal

_cfg = main.feishu_client.config
_cfg.app_id = ""
_cfg.app_secret = ""
_cfg.teacher_open_id = ""

out = {}
with TestClient(main.app) as client:
    seed.seed(force=True)
    db = SessionLocal()
    course = db.query(Course).first()
    cid = course.id
    student = db.query(Student).first()
    topic = db.query(DebateTopic).first()
    out["sid"] = student.id
    out["tid"] = topic.id
    db.close()

    first = client.post(f"/api/courses/{cid}/responses/text", json={
        "student_id": out["sid"], "topic_id": out["tid"],
        "text": "我觉得应该放回野外，因为老鹰属于天空。", "source": "student_device",
    }).json()
    out["rid"] = first["id"]
    out["raw_text"] = first["raw_text"]

    dialogue = client.get(f"/api/companion/{out['rid']}").json()
    out["dialogue_after_first"] = [
        {"role": t["role"], "content": t["content"]} for t in dialogue
    ]

    # 第二轮走 appendTurn，也应出现在同一对话里
    client.post(f"/api/responses/{out['rid']}/turns", json={
        "role": "ai_suggestion", "content": "如果它不会自己捕食怎么办？", "turn_type": "scaffold",
    })
    dialogue2 = client.get(f"/api/companion/{out['rid']}").json()
    out["dialogue_after_ai"] = [
        {"role": t["role"], "content": t["content"]} for t in dialogue2
    ]

    # 推给 AI 评估前先记录当场判断：只存 teacher_rating / teacher_note，
    # 不标记 teacher_reviewed（正式五维批改仍在评估页进行）。
    qr = client.post(f"/api/responses/{out['rid']}/quick-rating", json={
        "rating": "guide", "note": "需要引导举例",
    }).json()
    out["quick_rating"] = qr.get("teacher_rating")
    out["quick_note"] = qr.get("teacher_note")
    out["quick_reviewed"] = qr.get("teacher_reviewed")
    out["bad_rating_status"] = client.post(
        f"/api/responses/{out['rid']}/quick-rating", json={"rating": "wat"}
    ).status_code

    # 推给 AI 评估后：当场判断保留，仍未标记已确认。
    assessed = client.post(f"/api/responses/{out['rid']}/assess").json()
    out["assessed_status"] = assessed.get("processing_status")
    out["rating_after_assess"] = assessed.get("teacher_rating")
    out["reviewed_after_assess"] = assessed.get("teacher_reviewed")

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
        self.assertIn("老鹰属于天空", out["raw_text"])
        self.assertEqual(
            out["dialogue_after_first"],
            [{"role": "student", "content": "我觉得应该放回野外，因为老鹰属于天空。"}],
        )
        self.assertEqual(
            [t["role"] for t in out["dialogue_after_ai"]],
            ["student", "ai_suggestion"],
        )
        self.assertEqual(out["quick_rating"], "guide")
        self.assertEqual(out["quick_note"], "需要引导举例")
        self.assertFalse(out["quick_reviewed"])
        self.assertEqual(out["bad_rating_status"], 400)
        self.assertEqual(out["assessed_status"], "submitted")
        self.assertEqual(out["rating_after_assess"], "guide")
        self.assertFalse(out["reviewed_after_assess"])

    def test_clear_course_responses_keeps_students_and_topics(self):
        script = r"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="weixue_clear_")
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
from database import Course, Student, DebateTopic, StudentResponse, SessionLocal

_cfg = main.feishu_client.config
_cfg.app_id = ""
_cfg.app_secret = ""
_cfg.teacher_open_id = ""

out = {}
with TestClient(main.app) as client:
    seed.seed(force=True)
    db = SessionLocal()
    course = db.query(Course).first()
    cid = course.id
    out["students_before"] = db.query(Student).count()
    out["topics_before"] = db.query(DebateTopic).count()
    out["responses_before"] = db.query(StudentResponse).count()
    tids = [t.id for t in course.topics]
    out["topic_count"] = len(tids)
    db.close()

    # 先保存一份讲评计划，验证清除发言时一并清掉备课辅助数据。
    client.put(f"/api/courses/{cid}/prep/plan", json={
        "lesson_plan": tids, "notes": {str(tids[0]): "旧备注"}, "confirmed": True,
    })
    out["plan_before"] = client.get(f"/api/courses/{cid}/prep/plan").json()["lesson_plan"]

    r = client.post(f"/api/courses/{cid}/responses/clear").json()
    out["cleared"] = r

    out["plan_after"] = client.get(f"/api/courses/{cid}/prep/plan").json()["lesson_plan"]

    db = SessionLocal()
    out["students_after"] = db.query(Student).count()
    out["topics_after"] = db.query(DebateTopic).count()
    out["responses_after"] = db.query(StudentResponse).count()
    student = db.query(Student).first()
    topic = db.query(DebateTopic).first()
    out["sid"] = student.id
    out["tid"] = topic.id
    db.close()

    # 清空后第一位学生重新发言：新建作答必须成功且首轮进入对话历史。
    new_resp = client.post(f"/api/courses/{cid}/responses/text", json={
        "student_id": out["sid"], "topic_id": out["tid"],
        "text": "清空后我重新说一遍。", "source": "student_device",
    })
    out["reimport_status"] = new_resp.status_code
    out["reimport_rid"] = new_resp.json().get("id") if new_resp.status_code == 200 else None
    if out["reimport_rid"]:
        out["dialogue_after_reimport"] = [
            {"role": t["role"], "content": t["content"]}
            for t in client.get(f"/api/companion/{out['reimport_rid']}").json()
        ]

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
        self.assertGreater(out["responses_before"], 0)
        self.assertEqual(len(out["plan_before"]), out["topic_count"])
        self.assertEqual(out["cleared"]["responses_cleared"], out["responses_before"])
        self.assertEqual(out["responses_after"], 0)
        self.assertEqual(out["plan_after"], [])
        self.assertEqual(out["students_after"], out["students_before"])
        self.assertEqual(out["topics_after"], out["topics_before"])
        self.assertEqual(out["reimport_status"], 200)
        self.assertEqual(
            out["dialogue_after_reimport"],
            [{"role": "student", "content": "清空后我重新说一遍。"}],
        )


if __name__ == "__main__":
    unittest.main()
