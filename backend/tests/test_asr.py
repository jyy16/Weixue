"""Tests for the ASR abstraction layer and the audio import / settings API.

The API-level tests run FastAPI in a child process with an isolated SQLite
database, so they never touch the real ``backend/data/grading.db`` and never
fight the engine binding that other test modules (e.g. test_bitable_sync)
depend on.
"""

import asyncio
import json
import os
import subprocess
import sys
import unittest

from asr import ASRClient, ASRError, MOCK_TRANSCRIPT


# backend/.env may be loaded into os.environ by other test modules
# (feishu.client's load_dotenv); keep the provider-default assertions
# deterministic instead of inheriting the live ASR_MODEL.
os.environ["ASR_MODEL"] = ""


class ASRClientUnitTests(unittest.TestCase):
    def test_provider_resolution_and_default_models(self):
        self.assertEqual(ASRClient(provider="mock").provider, "mock")
        self.assertEqual(ASRClient(provider="qwen_asr").model, "qwen3-asr-flash")
        self.assertEqual(ASRClient(provider="openai").model, "whisper-1")
        self.assertEqual(
            ASRClient(provider="dashscope").model, "paraformer-realtime-v2"
        )
        self.assertIn(ASRClient().provider, ASRClient.SUPPORTED_PROVIDERS)

    def test_unknown_provider_raises(self):
        with self.assertRaises(ASRError):
            ASRClient(provider="wat")

    def test_mock_transcribe_and_segments(self):
        client = ASRClient(provider="mock")
        self.assertEqual(asyncio.run(client.transcribe("x.m4a")), MOCK_TRANSCRIPT)
        segments = client._mock_segments()
        self.assertTrue(segments)
        self.assertTrue(all(s["end_ms"] > s["start_ms"] for s in segments))
        self.assertEqual("".join(s["text"] for s in segments), MOCK_TRANSCRIPT)


_CHILD_SCRIPT = r"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="weixue_asr_api_")
os.environ["WEIXUE_DB_PATH"] = os.path.join(_TMP, "test.db")
# Force a deterministic failure path: unknown provider -> ASRError at client build.
os.environ["ASR_PROVIDER"] = "broken"
# Prevent backend/.env from leaking real keys into the readiness assertions.
os.environ["LLM_API_KEY"] = ""
os.environ["ASR_API_KEY"] = ""
os.environ["ASR_MODEL"] = ""
sys.path.insert(0, os.getcwd())

import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
import main
from api import state
from fastapi.testclient import TestClient
from database import (
    AudioRecording,
    Course,
    DebateTopic,
    SessionLocal,
    Student,
    StudentResponse,
)

state.UPLOAD_DIR = os.path.join(_TMP, "uploads")
os.makedirs(state.UPLOAD_DIR, exist_ok=True)


def upload_count():
    return len(
        [
            f
            for f in os.listdir(state.UPLOAD_DIR)
            if os.path.isfile(os.path.join(state.UPLOAD_DIR, f))
        ]
    )


out = {}
with TestClient(main.app) as client:
    db = SessionLocal()
    course = Course(title="测试课", class_name="测试班", grade_level=4)
    db.add(course)
    db.flush()
    topic = DebateTopic(course_id=course.id, title="测试辩题", order=1)
    db.add(topic)
    db.flush()
    student = Student(course_id=course.id, name="测试生", grade=1)
    db.add(student)
    db.commit()
    cid, tid, sid = course.id, topic.id, student.id

    # 1) A failed transcription must not leave rows or files behind.
    resp = client.post(
        f"/api/courses/{cid}/audio/import",
        data={"student_id": sid, "topic_id": tid, "source": "audio"},
        files={"file": ("a.m4a", b"\x00" * 1024, "audio/mp4")},
    )
    out["failure_status"] = resp.status_code
    out["failure_uploads"] = upload_count()
    out["failure_recordings"] = db.query(AudioRecording).count()
    out["failure_responses"] = db.query(StudentResponse).count()

    # 2) Settings endpoint degrades to mock when the env value is invalid.
    settings = client.get("/api/settings/asr")
    assert settings.status_code == 200, settings.text
    body = settings.json()
    out["settings_provider"] = body["provider"]
    out["providers"] = [p["id"] for p in body["providers"]]
    out["openai_ready"] = next(
        p["ready"] for p in body["providers"] if p["id"] == "openai"
    )
    out["dashscope_ready"] = next(
        p["ready"] for p in body["providers"] if p["id"] == "dashscope"
    )
    out["qwen_asr_ready"] = next(
        p["ready"] for p in body["providers"] if p["id"] == "qwen_asr"
    )

    # 3) Provider switch persists and is read back.
    switched = client.post("/api/settings/asr", json={"provider": "openai"})
    assert switched.status_code == 200, switched.text
    out["set_provider"] = switched.json()["provider"]
    readback = client.get("/api/settings/asr")
    assert readback.status_code == 200 and readback.json()["provider"] == "openai"

    # 4) Invalid provider is rejected.
    invalid = client.post("/api/settings/asr", json={"provider": "wat"})
    out["invalid_provider_status"] = invalid.status_code

    # 5) Back to mock for the import lifecycle tests.
    client.post("/api/settings/asr", json={"provider": "mock"})

    # 6) Successful mock import creates one recording + one response + one file.
    ok = client.post(
        f"/api/courses/{cid}/audio/import",
        data={"student_id": sid, "topic_id": tid, "source": "audio"},
        files={"file": ("b.m4a", b"\x00" * 1024, "audio/mp4")},
    )
    out["import_status"] = ok.status_code
    out["import_transcript"] = ok.json().get("raw_text", "")
    out["import_recordings"] = db.query(AudioRecording).count()
    out["import_uploads"] = upload_count()
    rid = ok.json()["id"]

    # 7) Re-upload replaces the old recording (row + file) instead of leaking.
    ok2 = client.post(
        f"/api/courses/{cid}/audio/import",
        data={"student_id": sid, "topic_id": tid, "source": "audio"},
        files={"file": ("c.m4a", b"\x00" * 1024, "audio/mp4")},
    )
    assert ok2.status_code == 200, ok2.text
    out["reimport_recordings"] = db.query(AudioRecording).count()
    out["reimport_uploads"] = upload_count()

    # 8) Deleting the response removes the recording row and its physical file.
    deleted = client.delete(f"/api/responses/{rid}")
    out["delete_status"] = deleted.status_code
    out["delete_recordings"] = db.query(AudioRecording).count()
    out["delete_uploads"] = upload_count()

    # 9) Demo data must not survive a switch to a real provider (qwen_asr).
    import seed
    seed.seed(force=True)
    fresh = SessionLocal()
    out["demo_seeded_courses"] = fresh.query(Course).count()
    out["demo_seeded_responses"] = fresh.query(StudentResponse).count()
    switched_real = client.post("/api/settings/asr", json={"provider": "qwen_asr"})
    assert switched_real.status_code == 200, switched_real.text
    out["demo_after_real_switch"] = switched_real.json()["demo_data_present"]
    out["demo_after_real_courses"] = fresh.query(Course).count()
    out["demo_after_real_responses"] = fresh.query(StudentResponse).count()

    # 10) Switching back to mock with an empty DB re-seeds the demo course.
    switched_mock = client.post("/api/settings/asr", json={"provider": "mock"})
    assert switched_mock.status_code == 200, switched_mock.text
    out["demo_after_mock_switch"] = switched_mock.json()["demo_data_present"]
    out["demo_after_mock_courses"] = fresh.query(Course).count()
    out["demo_after_mock_responses"] = fresh.query(StudentResponse).count()
    fresh.close()

print("ASR_TEST_RESULT " + json.dumps(out, ensure_ascii=False))
"""


class AudioImportAPITests(unittest.TestCase):
    def test_failure_cleanup_settings_and_import_lifecycle(self):
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
            if line.startswith("ASR_TEST_RESULT "):
                result = json.loads(line[len("ASR_TEST_RESULT "):])
        self.assertIsNotNone(result, msg=proc.stdout)

        self.assertEqual(result["failure_status"], 502)
        self.assertEqual(result["failure_uploads"], 0)
        self.assertEqual(result["failure_recordings"], 0)
        self.assertEqual(result["failure_responses"], 0)

        self.assertEqual(result["settings_provider"], "mock")
        self.assertEqual(
            result["providers"], ["mock", "qwen_asr", "openai", "dashscope"]
        )
        self.assertFalse(result["openai_ready"])
        self.assertFalse(result["dashscope_ready"])
        self.assertFalse(result["qwen_asr_ready"])
        self.assertEqual(result["set_provider"], "openai")
        self.assertEqual(result["invalid_provider_status"], 400)

        self.assertEqual(result["import_status"], 200)
        self.assertEqual(result["import_transcript"], MOCK_TRANSCRIPT)
        self.assertEqual(result["import_recordings"], 1)
        self.assertEqual(result["import_uploads"], 1)
        self.assertEqual(result["reimport_recordings"], 1)
        self.assertEqual(result["reimport_uploads"], 1)

        self.assertEqual(result["delete_status"], 200)
        self.assertEqual(result["delete_recordings"], 0)
        self.assertEqual(result["delete_uploads"], 0)

        self.assertEqual(result["demo_seeded_courses"], 1)
        self.assertEqual(result["demo_seeded_responses"], 9)
        self.assertFalse(result["demo_after_real_switch"])
        self.assertEqual(result["demo_after_real_courses"], 0)
        self.assertEqual(result["demo_after_real_responses"], 0)
        self.assertTrue(result["demo_after_mock_switch"])
        self.assertEqual(result["demo_after_mock_courses"], 1)
        self.assertEqual(result["demo_after_mock_responses"], 9)


if __name__ == "__main__":
    unittest.main()
