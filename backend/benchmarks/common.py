"""Shared helpers for the Weixue benchmark suite."""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
DEFAULT_ENV_FILE = BACKEND_DIR / ".env"
DEFAULT_DB = BACKEND_DIR / "data" / "benchmark.db"
SAMPLE_AUDIO = BACKEND_DIR / "data" / "sample_class_audio.wav"
DEFAULT_OUT_DIR = REPO_DIR / "tmp" / "benchmark"

HEALTH_URLS = ("/api/feishu/health", "/api/health")

DIMENSIONS = ["position", "material", "structure", "language", "perspective"]


def ensure_env() -> None:
    """Load backend/.env without overriding variables already set by the caller."""
    from dotenv import load_dotenv

    load_dotenv(DEFAULT_ENV_FILE, override=False)


def mask_key(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return value[:2] + "***"
    return value[:6] + "***"


def env_stamp() -> dict:
    base_url = (os.getenv("LLM_BASE_URL") or "").strip().rstrip("/")
    try:
        host = urlsplit(base_url).netloc if base_url else "(provider 默认)"
    except Exception:
        host = "(解析失败)"
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "llm_provider": os.getenv("LLM_PROVIDER") or "dashscope",
        "llm_model": os.getenv("LLM_MODEL") or "(provider 默认)",
        "llm_base_url": host,
        "llm_api_key_masked": mask_key(os.getenv("LLM_API_KEY")),
        "asr_provider_env": os.getenv("ASR_PROVIDER") or "mock",
        "asr_model_env": os.getenv("ASR_MODEL") or "(provider 默认)",
        "python": sys.version.split()[0],
        "git_commit": _git_short_head(),
    }


def _git_short_head() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def count_chars(text) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def stats(values) -> dict:
    vals = sorted(float(v) for v in values)
    n = len(vals)
    if n == 0:
        return {"n": 0, "min": None, "median": None, "mean": None, "max": None, "p95": None}

    def pct(p: float) -> float:
        idx = max(0, min(n - 1, math.ceil(p * n) - 1))
        return vals[idx]

    return {
        "n": n,
        "min": round(vals[0], 3),
        "median": round(pct(0.5), 3),
        "mean": round(statistics.mean(vals), 3),
        "max": round(vals[-1], 3),
        "p95": round(pct(0.95), 3),
    }


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


class BenchServer:
    """Temporary uvicorn server for the benchmark (own DB, own port)."""

    def __init__(self, port: int, db_path: Path, out_dir: Path, startup_timeout: float = 90.0):
        self.port = port
        self.db_path = Path(db_path)
        self.out_dir = Path(out_dir)
        self.startup_timeout = startup_timeout
        self.proc = None
        self.log_path = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "BenchServer":
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.out_dir / f"uvicorn-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        env = os.environ.copy()
        env["WEIXUE_DB_PATH"] = str(self.db_path)
        cmd = [
            sys.executable, "-m", "uvicorn", "main:app",
            "--app-dir", str(BACKEND_DIR),
            "--host", "127.0.0.1", "--port", str(self.port),
        ]
        flags = 0
        if os.name == "nt":
            flags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd, env=env, cwd=str(REPO_DIR),
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=flags, text=True,
        )
        deadline = time.monotonic() + self.startup_timeout
        with httpx.Client(timeout=3.0) as client:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"uvicorn 提前退出（退出码 {self.proc.returncode}）。日志：{self.log_path}\n{self.log_tail()}"
                    )
                for path in HEALTH_URLS:
                    try:
                        if client.get(f"{self.base_url}{path}").status_code == 200:
                            return self
                    except httpx.HTTPError:
                        pass
                time.sleep(0.5)
        raise RuntimeError(f"服务 {self.base_url} 启动超时。日志：{self.log_path}\n{self.log_tail()}")

    def log_tail(self, n: int = 40) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self.proc = None

    def __enter__(self) -> "BenchServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def seed_db(db_path: Path) -> None:
    """Seed a dedicated benchmark DB (wipes it first)."""
    env = os.environ.copy()
    env["WEIXUE_DB_PATH"] = str(db_path)
    proc = subprocess.run(
        [sys.executable, str(BACKEND_DIR / "seed.py"), "--force"],
        env=env, cwd=str(REPO_DIR),
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"seed.py 失败：\n{proc.stdout}\n{proc.stderr}")


def open_db(db_path: Path):
    """Open the benchmark DB with the project models (same process, separate env)."""
    os.environ["WEIXUE_DB_PATH"] = str(db_path)
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from database import SessionLocal

    return SessionLocal()


def read_responses(db_path: Path) -> list[dict]:
    """Read every response (AI + teacher fields) directly from the benchmark DB."""
    db = open_db(db_path)
    try:
        from database import Student, StudentResponse

        rows = (
            db.query(StudentResponse, Student)
            .join(Student, StudentResponse.student_id == Student.id)
            .all()
        )
        return [
            {
                "response_id": r.id,
                "student_id": st.id,
                "student_name": st.name,
                "grade": st.grade,
                "topic_id": r.topic_id,
                "source": r.source or "",
                "raw_text": r.raw_text or "",
                "cleaned_text": r.cleaned_text or "",
                "ai_dimension_scores": dict(r.ai_dimension_scores or {}),
                "ai_confidence": r.ai_confidence or "",
                "ai_reasoning": dict(r.ai_reasoning or {}),
                "ai_note": r.ai_note or "",
                "ai_suggested_tags": list(r.ai_suggested_tags or []),
                "teacher_reviewed": bool(r.teacher_reviewed),
                "teacher_dimension_scores": dict(r.teacher_dimension_scores or {}),
                "teacher_tags": list(r.teacher_tags or []),
            }
            for r, st in rows
        ]
    finally:
        db.close()
        db.get_bind().dispose()


def export_teacher_references(db_path: Path) -> list[dict]:
    """Teacher-reviewed responses (AI-vs-teacher quality reference)."""
    return [
        r for r in read_responses(db_path)
        if r["teacher_reviewed"] and r["teacher_dimension_scores"]
    ]


def recording_paths(db_path: Path) -> list[str]:
    db = open_db(db_path)
    try:
        from database import AudioRecording

        return [rec.file_path for rec in db.query(AudioRecording).all() if rec.file_path]
    finally:
        db.close()
        db.get_bind().dispose()


def cleanup_run(db_path: Path) -> None:
    """Remove the benchmark DB and any audio files this run uploaded."""
    uploads_dir = (BACKEND_DIR / "uploads").resolve()
    for path in recording_paths(db_path):
        try:
            p = Path(path)
            if p.is_absolute() and p.resolve().parent == uploads_dir:
                p.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        Path(db_path).unlink(missing_ok=True)
    except Exception:
        pass


def write_report(parts: dict, out_dir: Path, name: str) -> Path:
    """Write JSON + Markdown report; returns the shared base path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"{name}-{ts}"
    (out_dir / f"{base.name}.json").write_text(
        json.dumps(parts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / f"{base.name}.md").write_text(render_markdown(parts), encoding="utf-8")
    return base


def render_markdown(parts: dict) -> str:
    lines = ["# 维学思辨星 · 性能基准报告", ""]
    meta = parts.get("meta", {})
    lines.append(f"> 生成时间：{meta.get('generated_at', '')}")
    lines.append(
        f"> LLM：`{meta.get('llm_provider', '')} / {meta.get('llm_model', '')}`"
        f"（{meta.get('llm_base_url', '')}）"
    )
    if meta.get("llm_api_key_masked"):
        lines.append(f"> LLM Key：`{meta['llm_api_key_masked']}`")
    lines.append(
        f"> ASR（env）：`{meta.get('asr_provider_env', '')} / {meta.get('asr_model_env', '')}`"
    )
    if meta.get("git_commit"):
        lines.append(f"> 代码版本：`{meta['git_commit']}`")
    lines.append("")
    lines.append(
        "> 口径：耗时为本机实测墙钟时间；token 为字符粗估（中文≈1 token），"
        "实际成本以测试 API 控制台为准。"
    )
    lines.append("")
    for key, section in parts.get("results", {}).items():
        lines.append(f"## {section.get('title', key)}")
        lines.append("")
        for note in section.get("notes", []):
            lines.append(f"- {note}")
        if section.get("notes"):
            lines.append("")
        for tbl in section.get("tables", []):
            lines.append(f"### {tbl.get('caption', '')}")
            lines.append("")
            headers = [str(h) for h in tbl.get("headers", [])]
            rows = tbl.get("rows", [])
            lines.append("| " + " | ".join(headers) + " |")
            lines.append("|" + "|".join("---" for _ in headers) + "|")
            for row in rows:
                lines.append("| " + " | ".join(str(c) for c in row) + " |")
            lines.append("")
    return "\n".join(lines)


def estimate_assess_tokens(rows: list[dict]) -> dict:
    """Rough token estimate for the batch assessment (中文≈1 token)."""
    in_total = 0
    out_total = 0
    for row in rows:
        in_total += 3000 + count_chars(row.get("raw_text"))
        out_json = json.dumps(
            {
                "ai_dimension_scores": row.get("ai_dimension_scores"),
                "ai_reasoning": row.get("ai_reasoning"),
                "ai_note": row.get("ai_note"),
                "ai_suggested_tags": row.get("ai_suggested_tags"),
            },
            ensure_ascii=False,
        )
        out_total += count_chars(out_json)
    return {
        "input_chars_est": in_total,
        "output_chars_est": out_total,
        "tokens_est": in_total + out_total,
        "note": "评估输入按量规+校准记录+任务说明粗估约 3000 字符/条；中文≈1 token，实际以 API 用量为准",
    }


def estimate_comment_tokens(drafts: list[str]) -> dict:
    """Rough token estimate for comment drafts (中文≈1 token)."""
    in_total = len(drafts) * 1500
    out_total = sum(count_chars(d) for d in drafts)
    return {
        "input_chars_est": in_total,
        "output_chars_est": out_total,
        "tokens_est": in_total + out_total,
        "note": "评语输入 prompt 粗估约 1500 字符/条；中文≈1 token，实际以 API 用量为准",
    }
