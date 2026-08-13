"""One-shot Bitable bootstrap: create base + 5 tables, write .env, ensure schema, sync.

Usage from backend/:
    python -m feishu.bootstrap_base [--share-email you@tenant.com] [--no-sync]

Steps (all idempotent, safe to re-run):
1. Create a Bitable app (base) if FEISHU_BITABLE_APP_TOKEN is empty.
2. Create any missing tables among courses/topics/students/responses/prep_plans.
3. Write FEISHU_BITABLE_APP_TOKEN / FEISHU_BITABLE_TABLE_IDS back into
   backend/.env (a timestamped backup of .env is saved first).
4. Run ensure_schema() to build fields + single-select options.
5. Sync every local course from SQLite as an end-to-end verification
   (disable with --no-sync).

The base is created by the project's own app (tenant_access_token), so the
app is the owner and needs no extra permission dance. Use --share-email to
grant a tenant user full access (best effort; requires a drive permission
scope on the app).

Prereq: FEISHU_APP_ID / FEISHU_APP_SECRET set in backend/.env and the app
has the `bitable:app` scope enabled in the Feishu developer console.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time

from .bitable import BitableService
from .client import FeishuClient, FeishuConfig
from .sync import BitableSyncer

BASE_NAME = "维学思辨星·评估数据"
TABLE_NAMES = {
    "courses": "班级",
    "topics": "辩题",
    "students": "学生",
    "responses": "评估记录",
    "prep_plans": "讲评计划",
}

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_BACKEND_DIR, ".env")


async def _ensure_base_and_tables(client: FeishuClient, config: FeishuConfig) -> dict:
    app_token = config.bitable_app_token
    base_url = ""
    created_app = False

    if not app_token:
        data = await client.request("POST", "/bitable/v1/apps", json_body={"name": BASE_NAME})
        app = (data or {}).get("app") or {}
        app_token = str(app.get("app_token") or data.get("app_token") or "")
        base_url = str(app.get("url") or "")
        if not app_token:
            raise RuntimeError(f"bitable app creation returned no app_token: {data!r}")
        created_app = True

    listed = await client.request(
        "GET", f"/bitable/v1/apps/{app_token}/tables", params={"page_size": 100}
    )
    items = (listed or {}).get("items") or []
    by_name = {str(t.get("name", "")): str(t.get("table_id", "")) for t in items}

    table_ids = dict(config.bitable_table_ids or {})
    created_tables: list[str] = []
    for key, name in TABLE_NAMES.items():
        if table_ids.get(key):
            continue
        if name in by_name:
            table_ids[key] = by_name[name]
            continue
        data = await client.request(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables",
            json_body={"table": {"name": name}},
        )
        tid = str((data or {}).get("table_id") or ((data or {}).get("table") or {}).get("table_id") or "")
        if not tid:
            raise RuntimeError(f"table creation for {name} returned no table_id: {data!r}")
        table_ids[key] = tid
        created_tables.append(name)

    # Freshly created apps ship with a default "数据表"; drop it once ours exist.
    removed_default = False
    if created_app:
        ours = set(table_ids.values())
        for t in items:
            tid = str(t.get("table_id", ""))
            if tid and tid not in ours:
                try:
                    await client.request(
                        "DELETE", f"/bitable/v1/apps/{app_token}/tables/{tid}"
                    )
                    removed_default = True
                except Exception:  # noqa: BLE001 - cosmetic cleanup only
                    pass

    return {
        "app_token": app_token,
        "base_url": base_url or f"https://feishu.cn/base/{app_token}",
        "table_ids": table_ids,
        "created_app": created_app,
        "created_tables": created_tables,
        "removed_default_table": removed_default,
    }


def _write_env(app_token: str, table_ids: dict) -> str:
    """Write bitable config back into backend/.env; returns the backup path."""
    backup = f"{_ENV_PATH}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    if os.path.exists(_ENV_PATH):
        shutil.copy2(_ENV_PATH, backup)
    token_line = f"FEISHU_BITABLE_APP_TOKEN={app_token}"
    ids_line = "FEISHU_BITABLE_TABLE_IDS=" + json.dumps(
        {k: table_ids.get(k, "") for k in TABLE_NAMES}, ensure_ascii=False, separators=(",", ":")
    )
    lines = open(_ENV_PATH, encoding="utf-8").read().splitlines()
    out, seen = [], set()
    for line in lines:
        if line.startswith("FEISHU_BITABLE_APP_TOKEN="):
            line = token_line
            seen.add("token")
        elif line.startswith("FEISHU_BITABLE_TABLE_IDS="):
            line = ids_line
            seen.add("ids")
        out.append(line)
    if "token" not in seen:
        out.append(token_line)
    if "ids" not in seen:
        out.append(ids_line)
    open(_ENV_PATH, "w", encoding="utf-8").write("\n".join(out) + "\n")
    return backup


async def _sync_all_courses(client: FeishuClient, config: FeishuConfig) -> list[dict]:
    from database import Course, SessionLocal  # local import: keeps CLI usable w/o DB

    syncer = BitableSyncer(client, config=config)
    if not syncer.available:
        return [{"error": "syncer not available after bootstrap (unexpected)"}]
    db = SessionLocal()
    results = []
    try:
        for course in db.query(Course).order_by(Course.id).all():
            summary = await syncer.sync_course(db, course.id)
            results.append({"course_id": course.id, "class_name": course.class_name, "summary": summary})
    finally:
        db.close()
    return results


async def _share_base(client: FeishuClient, app_token: str, email: str) -> dict:
    try:
        data = await client.request(
            "POST",
            f"/drive/v1/permissions/{app_token}/members",
            params={"type": "bitable", "need_notification": "false"},
            json_body={"member_type": "email", "member_id": email, "perm": "full_access"},
        )
        return {"shared": True, "member": (data or {}).get("member", {})}
    except Exception as exc:  # noqa: BLE001 - best effort
        return {"shared": False, "error": str(exc)}


async def _run(share_email: str, sync: bool) -> int:
    config = FeishuConfig()
    if not config.is_configured:
        print(json.dumps({
            "status": "not_configured",
            "hint": "Set FEISHU_APP_ID and FEISHU_APP_SECRET in backend/.env first",
        }, ensure_ascii=False, indent=2))
        return 1

    client = FeishuClient(config=config)
    try:
        boot = await _ensure_base_and_tables(client, config)
        config.bitable_app_token = boot["app_token"]
        config.bitable_table_ids = boot["table_ids"]
        client.config = config
        backup = _write_env(boot["app_token"], boot["table_ids"])

        service = BitableService(
            client, app_token=boot["app_token"], table_ids=boot["table_ids"]
        )
        schema_report = await service.ensure_schema()

        share_report = {}
        if share_email:
            share_report = await _share_base(client, boot["app_token"], share_email)

        sync_report = []
        if sync:
            sync_report = await _sync_all_courses(client, config)

        print(json.dumps({
            "status": "ok",
            "base": boot,
            "env_backup": backup,
            "schema": schema_report,
            "share": share_report,
            "sync": sync_report,
        }, ensure_ascii=False, indent=2))
        ok_schema = all(v.get("status") in {"ok", "skipped"} for v in schema_report.values())
        sync_errors = [
            err
            for item in sync_report
            for counters in (item.get("summary", {}).get("tables") or {}).values()
            if isinstance(counters, dict) and counters.get("errors")
            for err in [f"course {item.get('course_id')}: {counters}"]
        ]
        return 0 if ok_schema and not sync_errors else 1
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Bitable base + tables for Weixue")
    parser.add_argument("--share-email", default="", help="grant a tenant user full access by email")
    parser.add_argument("--no-sync", action="store_true", help="skip the end-to-end course sync")
    args = parser.parse_args()
    return asyncio.run(_run(args.share_email.strip(), not args.no_sync))


if __name__ == "__main__":
    raise SystemExit(main())
