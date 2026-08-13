"""Command-line bootstrap for the five Bitable tables (fields + options).

Usage from backend/:
    python -m feishu.schema --apply
    python -m feishu.schema            # dry run? Feishu has no dry-run fields
                                       # API, so without --apply it only prints
                                       # the expected schemas and configuration.

The OpenAPI field-management endpoints used here are real Feishu APIs, but this
tool has not been exercised against a live app yet; run it during 联调 and keep
the per-table report from the output.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from .bitable import (
    BitableService,
    TABLE_COURSES,
    TABLE_PREP_PLANS,
    TABLE_RESPONSES,
    TABLE_STUDENTS,
    TABLE_TOPICS,
)
from .client import FeishuClient, FeishuConfig


async def _run(apply: bool) -> int:
    config = FeishuConfig()
    if not config.bitable_app_token or not config.bitable_table_ids:
        print(json.dumps({
            "status": "not_configured",
            "hint": "Set FEISHU_BITABLE_APP_TOKEN and FEISHU_BITABLE_TABLE_IDS",
        }, ensure_ascii=False, indent=2))
        return 1
    if not apply:
        print(json.dumps({
            "status": "dry_run",
            "schemas": {
                "courses": {k: v for k, v in TABLE_COURSES.items()},
                "topics": {k: v for k, v in TABLE_TOPICS.items()},
                "students": {k: v for k, v in TABLE_STUDENTS.items()},
                "responses": {k: v for k, v in TABLE_RESPONSES.items()},
                "prep_plans": {k: v for k, v in TABLE_PREP_PLANS.items()},
            },
            "hint": "Run with --apply to create missing fields/options",
        }, ensure_ascii=False, indent=2))
        return 0
    client = FeishuClient(config=config)
    try:
        report = await BitableService(client).ensure_schema()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if all(v.get("status") in {"ok", "skipped"} for v in report.values()) else 1
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Bitable table schemas")
    parser.add_argument("--apply", action="store_true", help="Actually create missing fields/options")
    args = parser.parse_args()
    return asyncio.run(_run(args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
