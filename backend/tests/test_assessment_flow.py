"""Tests for the batch assessment pipeline (assess / progress / reset).

Runs FastAPI in a child process with an isolated SQLite database (same pattern
as test_asr.py), so nothing touches ``backend/data/grading.db``. The LLM layer
is monkeypatched inside the child: one scenario raises to exercise the failure
path (no network, no API quota), another returns a canned evaluation to
exercise the success path deterministically.
"""

import json
import os
import subprocess
import sys
import unittest


_CHILD_SCRIPT = r"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="weixue_assess_api_")
os.environ["WEIXUE_DB_PATH"] = os.path.join(_TMP, "test.db")
# No real credentials in tests; ASR stays on the offline mock provider.
os.environ["LLM_API_KEY"] = ""
os.environ["ASR_API_KEY"] = ""
os.environ["ASR_PROVIDER"] = "mock"
sys.path.insert(0, os.getcwd())

import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
import main
import seed
from fastapi.testclient import TestClient
from database import Course, DimensionTag, SessionLocal, StudentResponse

out = {}
with TestClient(main.app) as client:
    seed.seed(force=True)
    db = SessionLocal()
    course = db.query(Course).first()
    cid = course.id

    def wait_done(timeout_polls=200):
        for _ in range(timeout_polls):
            p = client.get(f"/api/courses/{cid}/assessment-progress").json()
            if not p["active"]:
                return p
        raise RuntimeError("assessment never finished")

    def unreview(n):
        # Make n responses eligible for assessment again.
        rows = (
            db.query(StudentResponse)
            .filter(StudentResponse.teacher_reviewed.is_(True))
            .order_by(StudentResponse.id)
            .limit(n)
            .all()
        )
        for r in rows:
            r.teacher_reviewed = False
        db.commit()
        return [r.id for r in rows]

    # ── 0) Progress defaults for a course that never ran ────────────────
    defaults = client.get("/api/courses/{cid}/assessment-progress".format(cid=9999))
    out["default_progress"] = defaults.json()

    # ── 1) All responses teacher-reviewed -> nothing to assess ──────────
    started = client.post(f"/api/courses/{cid}/assess")
    out["noop_status"] = started.status_code
    out["noop_total"] = started.json().get("total")

    # ── 2) LLM failure: engine degrades gracefully (uncertain, retryable) ─
    # AssessmentEngine.assess catches LLM exceptions itself and returns an
    # "AI评估失败" fallback, so these are counted as llm_calls, not errors.
    def _boom(*args, **kwargs):
        raise RuntimeError("offline test: LLM disabled")

    main.evaluator.llm.chat = _boom
    main.evaluator.llm.chat_json = _boom

    failed_ids = unreview(5)
    started = client.post(f"/api/courses/{cid}/assess")
    out["fail_started_status"] = started.status_code
    out["fail_started_total"] = started.json().get("total")
    p = wait_done()
    out["fail_progress"] = p

    db.expire_all()
    rows = db.query(StudentResponse).filter(StudentResponse.id.in_(failed_ids)).all()
    out["fail_all_uncertain"] = all(r.ai_confidence == "uncertain" for r in rows)
    out["fail_scores_cleared"] = all(r.ai_dimension_scores is None for r in rows)
    out["fail_notes"] = all(("AI评估失败" in (r.ai_note or "")) for r in rows)

    # ── 3) Re-entrancy guard + reset guard while active ─────────────────
    main._assessment_progress[cid]["active"] = True
    out["reenter_status"] = client.post(f"/api/courses/{cid}/assess").status_code
    out["reset_while_active_status"] = client.post(f"/api/courses/{cid}/reset").status_code
    main._assessment_progress[cid]["active"] = False

    # ── 4) Assessor crash: _run_assessment's except branch counts errors ─
    async def _raiser(*args, **kwargs):
        raise RuntimeError("offline test: assessor crashed")

    main.evaluator.assess = _raiser
    started = client.post(f"/api/courses/{cid}/assess")
    out["retry_total"] = started.json().get("total")
    p = wait_done()
    out["crash_progress"] = p
    db.expire_all()
    rows = db.query(StudentResponse).filter(StudentResponse.id.in_(failed_ids)).all()
    out["crash_all_uncertain"] = all(r.ai_confidence == "uncertain" for r in rows)
    out["crash_notes"] = all(("AI评估异常" in (r.ai_note or "")) for r in rows)

    # ── 5) Success scenario: canned evaluation -> processed + stored ────
    canned = {
        "cleaned_text": "我认为应该放生，因为动物属于大自然。",
        "dimension_scores": {"position": "A", "material": "B+", "structure": "B", "language": "A-", "perspective": "B+"},
        "confidence": "certain_good",
        "reasoning": {"summary": "测试推理"},
        "extracted_features": {"claim": "应该放生"},
        "note": "",
        "suggested_tags": ["观点明确"],
    }

    async def _canned(*args, **kwargs):
        return dict(canned)

    main.evaluator.assess = _canned

    ok_ids = unreview(3)
    started = client.post(f"/api/courses/{cid}/assess")
    out["ok_started_total"] = started.json().get("total")
    p = wait_done()
    out["ok_progress"] = p
    db.expire_all()
    rows = db.query(StudentResponse).filter(StudentResponse.id.in_(ok_ids)).all()
    out["ok_all_certain"] = all(r.ai_confidence == "certain_good" for r in rows)
    out["ok_scores"] = all(
        (r.ai_dimension_scores or {}).get("position") == "A" for r in rows
    )
    out["ok_tag_synced"] = (
        db.query(DimensionTag)
        .filter(DimensionTag.name == "观点明确", DimensionTag.course_id == cid)
        .count()
        == 1
    )

    # ── 6) Reviewed responses are always skipped ────────────────────────
    started = client.post(f"/api/courses/{cid}/assess")
    out["after_ok_total"] = started.json().get("total")
    wait_done()

    # ── 7) Reset clears everything and companion tags ───────────────────
    reset = client.post(f"/api/courses/{cid}/reset")
    out["reset_status"] = reset.status_code
    db.expire_all()
    rows = db.query(StudentResponse).all()
    out["reset_cleared"] = all(
        not r.teacher_reviewed
        and r.ai_dimension_scores is None
        and r.ai_confidence == "uncertain"
        and r.processing_status == "not_started"
        for r in rows
    )
    out["reset_progress_default"] = client.get(
        f"/api/courses/{cid}/assessment-progress"
    ).json().get("active")

print("ASSESS_TEST_RESULT " + json.dumps(out, ensure_ascii=False))
"""


class AssessmentFlowTests(unittest.TestCase):
    def test_assess_progress_reset_lifecycle(self):
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env={**dict(os.environ), "PYTHONIOENCODING": "utf-8"},
            timeout=240,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"child process failed\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        result = None
        for line in proc.stdout.splitlines():
            if line.startswith("ASSESS_TEST_RESULT "):
                result = json.loads(line[len("ASSESS_TEST_RESULT "):])
        self.assertIsNotNone(result, msg=proc.stdout)

        # 0) defaults
        self.assertFalse(result["default_progress"]["active"])
        self.assertEqual(result["default_progress"]["total"], 0)

        # 1) nothing eligible -> total 0
        self.assertEqual(result["noop_status"], 200)
        self.assertEqual(result["noop_total"], 0)

        # 2) LLM failure -> graceful degradation inside the engine
        self.assertEqual(result["fail_started_status"], 200)
        self.assertEqual(result["fail_started_total"], 5)
        progress = result["fail_progress"]
        # completed counts every visited row, including skipped reviewed ones
        self.assertEqual(progress["completed"], 9)
        self.assertEqual(progress["skipped"], 4)
        self.assertEqual(progress["errors"], 0)
        self.assertEqual(progress["llm_calls"], 5)
        self.assertTrue(result["fail_all_uncertain"])
        self.assertTrue(result["fail_scores_cleared"])
        self.assertTrue(result["fail_notes"])

        # 3) guards while active
        self.assertEqual(result["reenter_status"], 409)
        self.assertEqual(result["reset_while_active_status"], 409)

        # 4) assessor crash -> errors counter, still retryable
        self.assertEqual(result["retry_total"], 5)
        crash = result["crash_progress"]
        self.assertEqual(crash["completed"], 9)
        self.assertEqual(crash["skipped"], 4)
        self.assertEqual(crash["errors"], 5)
        self.assertEqual(crash["llm_calls"], 0)
        self.assertTrue(result["crash_all_uncertain"])
        self.assertTrue(result["crash_notes"])

        # 5) success scenario: 5 retryable + 3 newly unreviewed
        self.assertEqual(result["ok_started_total"], 8)
        self.assertEqual(result["ok_progress"]["completed"], 9)
        self.assertEqual(result["ok_progress"]["skipped"], 1)
        self.assertEqual(result["ok_progress"]["llm_calls"], 8)
        self.assertTrue(result["ok_all_certain"])
        self.assertTrue(result["ok_scores"])
        self.assertTrue(result["ok_tag_synced"])

        # 6) certain responses are skipped on the next run
        self.assertEqual(result["after_ok_total"], 0)

        # 7) reset
        self.assertEqual(result["reset_status"], 200)
        self.assertTrue(result["reset_cleared"])
        self.assertFalse(result["reset_progress_default"])


if __name__ == "__main__":
    unittest.main()
