"""One-shot Bitable rebuild: clear remote rows + local bindings, then full sync.

Usage from backend/:
    python -m feishu.rebuild_bitable --yes

Steps:
1. Verify Bitable is configured.
2. Delete EVERY row in the configured tables (班级/辩题/学生/评估记录/讲评计划)
   -- rows only; the base and table schema stay, so an Aily connection to
   this base keeps working.
3. Clear the local ``feishu_bindings`` table. Without this the next sync would
   try to batch_update record_ids that no longer exist and fail to recreate.
4. Re-sync every local course from SQLite (the single source of truth).
5. Print a per-table verification: remote row count vs local entity count.

Why a rebuild is ever needed: local entity deletion never deletes remote rows
by design (Bitable is the teacher's review surface), so repeated seed/reset
cycles leave stale rows behind. Run this AFTER the demo data is finalized,
before recording/submission -- re-importing data afterwards will need another
rebuild.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Optional

from database import (
    Course,
    DebateTopic,
    FeishuBinding,
    PrepPlan,
    SessionLocal,
    Student,
    StudentResponse,
)

from .bitable import BitableService
from .client import FeishuClient, FeishuConfig
from .sync import BitableSyncer, TABLE_KEYS

TABLE_LABELS = {
    "courses": "班级",
    "topics": "辩题",
    "students": "学生",
    "responses": "评估记录",
    "prep_plans": "讲评计划",
}


async def _clear_table(service, table_id: str) -> int:
    """Delete every row in one table (paged); return rows deleted."""
    deleted = 0
    page_token = ""
    while True:
        data = await service.search_records(
            table_id, page_size=500, page_token=page_token
        )
        items = (data or {}).get("items") or []
        ids = [str(r["record_id"]) for r in items if r.get("record_id")]
        if ids:
            await service.batch_delete_records(table_id, ids)
            deleted += len(ids)
        page_token = (data or {}).get("page_token") or ""
        if not page_token or not ids:
            return deleted


def _clear_local_bindings() -> int:
    db = SessionLocal()
    try:
        count = db.query(FeishuBinding).delete()
        db.commit()
        return count
    finally:
        db.close()


def _local_counts() -> dict:
    db = SessionLocal()
    try:
        return {
            "courses": db.query(Course).count(),
            "topics": db.query(DebateTopic).count(),
            "students": db.query(Student).count(),
            "responses": db.query(StudentResponse).count(),
            "prep_plans": db.query(PrepPlan).count(),
        }
    finally:
        db.close()


async def _remote_counts(service, config) -> dict:
    counts = {}
    for key in TABLE_KEYS:
        table_id = (config.bitable_table_ids or {}).get(key) or ""
        if not table_id:
            counts[key] = None
            continue
        total = 0
        page_token = ""
        while True:
            data = await service.search_records(
                table_id, page_size=500, page_token=page_token
            )
            total += len((data or {}).get("items") or [])
            page_token = (data or {}).get("page_token") or ""
            if not page_token:
                break
        counts[key] = total
    return counts


async def _full_sync(client, config) -> None:
    db = SessionLocal()
    syncer = BitableSyncer(client, config)
    try:
        for course in db.query(Course).all():
            await syncer.sync_course(db, course.id)
    finally:
        db.close()


async def _run(args) -> int:
    config = FeishuConfig()
    if not config.bitable_app_token or not config.bitable_table_ids:
        print(
            "未配置多维表格（FEISHU_BITABLE_APP_TOKEN / FEISHU_BITABLE_TABLE_IDS）。"
            "先运行 python -m feishu.bootstrap_base 或填写 backend/.env。"
        )
        return 1

    print("思辨星 · 多维表格一键重建（不可恢复，建议先备份本地库）")
    print("  1) 删除多维表格所有记录（保留 base 与表结构，Aily 连接不断）")
    print("  2) 清空本地 feishu_bindings 映射")
    print("  3) 从本地 SQLite 全量重建同步")
    if not args.yes:
        answer = input("确认执行请输入 yes：").strip().lower()
        if answer != "yes":
            print("已取消。")
            return 2

    client = FeishuClient(config)
    try:
        service = BitableService(client)

        cleared = {}
        for key in TABLE_KEYS:
            table_id = (config.bitable_table_ids or {}).get(key) or ""
            if not table_id:
                cleared[key] = None
                continue
            cleared[key] = await _clear_table(service, table_id)
        print(
            "已清空远端记录："
            + "、".join(
                f"{TABLE_LABELS.get(k, k)}={v}"
                for k, v in cleared.items()
                if v is not None
            )
        )

        removed = _clear_local_bindings()
        print(f"已清空本地 feishu_bindings：{removed} 条")

        if not args.skip_sync:
            await _full_sync(client, config)
            local = _local_counts()
            remote = await _remote_counts(service, config)
            print("重建完成，核对：")
            for key in TABLE_KEYS:
                lc = local.get(key, 0)
                rc = remote.get(key)
                if rc is None:
                    mark = "未配置"
                elif rc == lc:
                    mark = "OK"
                else:
                    mark = "不一致!"
                print(f"  {TABLE_LABELS.get(key, key):<6} 本地 {lc} / 远端 {rc}  {mark}")
        else:
            print("已跳过同步（--skip-sync）。")
        return 0
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="多维表格一键重建（清空远端 + 重建同步）")
    parser.add_argument("--yes", action="store_true", help="跳过确认提示")
    parser.add_argument("--skip-sync", action="store_true", help="只清空，不重建同步")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
