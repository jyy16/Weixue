"""One-click backend launcher: FastAPI (uvicorn) + Feishu ws_listener.

Run from backend/:
    python start.py              # uvicorn http://127.0.0.1:8000 + ws_listener
    python start.py --port 8765
    python start.py --no-listener
    python start.py --reload     # uvicorn dev auto-reload (listener still starts once)

The ws_listener child is started in module form (``python -m feishu.ws_listener``,
required so backend-relative imports resolve) with its output appended to
ws_listener.log. If a listener is already running, the launcher skips starting
another one -- Feishu fans callbacks out to every active long connection, so
duplicate listeners make card clicks flaky.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
LISTENER_LOG = os.path.join(BACKEND_DIR, "ws_listener.log")
LISTENER_CMD = [sys.executable, "-m", "feishu.ws_listener"]


def _running_listener_pids() -> list[int]:
    """PIDs of existing feishu.ws_listener processes (excluding ourselves)."""
    try:
        if os.name == "nt":
            cmd = [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -match 'feishu\\.ws_listener' } | "
                "Select-Object -ExpandProperty ProcessId",
            ]
        else:
            cmd = ["pgrep", "-f", "feishu.ws_listener"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    pids = []
    for token in out.split():
        if token.strip().isdigit() and int(token) != os.getpid():
            pids.append(int(token))
    return pids


def _spawn_listener() -> subprocess.Popen:
    log_fh = open(LISTENER_LOG, "ab")
    proc = subprocess.Popen(
        LISTENER_CMD,
        cwd=BACKEND_DIR,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    # Give it a moment: if it exits immediately (e.g. missing Feishu config)
    # show the log tail so the failure is obvious instead of silent.
    time.sleep(1.5)
    if proc.poll() is not None:
        try:
            tail = open(LISTENER_LOG, "rb").read()[-1500:].decode("utf-8", "replace")
        except OSError:
            tail = ""
        print("note: ws_listener exited immediately; log tail:")
        print(tail or "(empty log)")
    else:
        print(f"ws_listener started (pid {proc.pid}); log -> ws_listener.log")
    return proc


def _stop_listener(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    print("stopping ws_listener ...")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _seed_demo_if_empty() -> None:
    from database import Course, SessionLocal, init_db
    init_db()
    db = SessionLocal()
    try:
        if db.query(Course).count() == 0:
            import seed
            seed.seed(force=False)
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="One-click Weixue backend launcher")
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument("--port", type=int, default=8000, help="bind port")
    parser.add_argument(
        "--reload", action="store_true", help="uvicorn dev auto-reload"
    )
    parser.add_argument(
        "--no-listener", action="store_true",
        help="do not start the Feishu ws_listener",
    )
    parser.add_argument(
        "--no-seed", action="store_true",
        help="do not seed demo data on an empty database",
    )
    args = parser.parse_args()

    if not os.path.isfile(os.path.join(BACKEND_DIR, "main.py")):
        print("start.py must be run from backend/ (cd backend && python start.py)")
        return 2

    os.chdir(BACKEND_DIR)

    if not args.no_seed:
        print("checking demo data ...")
        _seed_demo_if_empty()

    listener = None
    if not args.no_listener:
        existing = _running_listener_pids()
        if existing:
            print(f"ws_listener already running (pid {existing}); skipping start")
        else:
            listener = _spawn_listener()

    import uvicorn
    try:
        uvicorn.run("main:app", host=args.host, port=args.port, reload=args.reload)
    finally:
        _stop_listener(listener)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
