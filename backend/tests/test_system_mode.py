"""Tests for /api/settings/mode (capability matrix + demo data actions).

Run in a child process with an isolated SQLite DB and purged Feishu/LLM
credentials so the capability flags are deterministic and no network is hit.
"""

import json
import os
import subprocess
import sys
import unittest


class SystemModeAPITests(unittest.TestCase):
    def test_capabilities_and_demo_actions(self):
        script = r"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="weixue_mode_api_")
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
from fastapi.testclient import TestClient
from database import Course, SessionLocal

_cfg = main.feishu_client.config
_cfg.app_id = ""
_cfg.app_secret = ""
_cfg.teacher_open_id = ""
_cfg.bitable_app_token = ""
_cfg.bitable_table_ids = {}

out = {}
with TestClient(main.app) as client:
    caps = client.get("/api/settings/mode").json()
    out["empty_caps"] = caps

    # enter_demo on an empty DB seeds the demo course
    demo = client.post("/api/settings/mode", json={"action": "enter_demo"}).json()
    out["enter_demo"] = demo
    out["course_count_after_demo"] = SessionLocal().query(Course).count()

    caps2 = client.get("/api/settings/mode").json()
    out["caps_after_demo"] = caps2

    # enter_real purges only the marked demo course
    real = client.post("/api/settings/mode", json={"action": "enter_real"}).json()
    out["enter_real"] = real
    out["course_count_after_real"] = SessionLocal().query(Course).count()

    # invalid action rejected
    bad = client.post("/api/settings/mode", json={"action": "nope"})
    out["bad_status"] = bad.status_code

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
        self.assertFalse(out["empty_caps"]["demo_course_present"])
        self.assertEqual(out["empty_caps"]["asr_provider"], "mock")
        self.assertTrue(out["empty_caps"]["asr_ready"])
        self.assertFalse(out["empty_caps"]["llm_configured"])
        self.assertFalse(out["empty_caps"]["feishu_ready"])
        self.assertFalse(out["empty_caps"]["bitable_ready"])
        self.assertTrue(out["enter_demo"]["seeded"])
        self.assertEqual(out["course_count_after_demo"], 1)
        self.assertTrue(out["caps_after_demo"]["demo_course_present"])
        self.assertTrue(out["enter_real"]["purged"])
        self.assertEqual(out["course_count_after_real"], 0)
        self.assertEqual(out["bad_status"], 400)


if __name__ == "__main__":
    unittest.main()
