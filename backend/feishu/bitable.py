"""Feishu Bitable (多维表格) integration: schema constants + record batch operations.

APIs (verified 2026-08):
- List tables:     GET  /bitable/v1/apps/{app_token}/tables
- Search records:  POST /bitable/v1/apps/{app_token}/tables/{table_id}/records/search
- Batch create:    POST /bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create
- Batch update:    POST /bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update

Field type codes (write format):
- text = 1, number = 2, single select = 3, multi select = 4,
  date = 5 (ms timestamp), checkbox = 7, person = 11 (open_id)
- single select write value: "选项名" (plain string); multi select: ["选项一", "选项二"].
  The {"text": "..."} object forms are READ shapes and fail on write with
  SingleSelectFieldConvFail (verified against live API 2026-08).
"""

from typing import Any, Optional

from .client import FeishuClient

FIELD_TEXT = 1
FIELD_NUMBER = 2
FIELD_SINGLE_SELECT = 3
FIELD_MULTI_SELECT = 4
FIELD_DATE = 5
FIELD_CHECKBOX = 7
FIELD_PERSON = 11

# Suggested table schemas (field name -> field type). Build these in the Feishu
# console first, then fill FEISHU_BITABLE_TABLE_IDS in .env.
TABLE_COURSES = {
    "班级名": FIELD_TEXT,
    "年级": FIELD_NUMBER,
    "创建时间": FIELD_DATE,
}

TABLE_TOPICS = {
    "标题": FIELD_TEXT,
    "类型": FIELD_SINGLE_SELECT,
    "认知梯段": FIELD_SINGLE_SELECT,
    "引导材料": FIELD_TEXT,
    "参考论据": FIELD_TEXT,
    "顺序": FIELD_NUMBER,
}

TABLE_STUDENTS = {
    "姓名": FIELD_TEXT,
    "年级": FIELD_NUMBER,
    "认知梯段": FIELD_SINGLE_SELECT,
    "班级": FIELD_TEXT,
    "评语草稿": FIELD_TEXT,
}

TABLE_RESPONSES = {
    "学生": FIELD_TEXT,
    "辩题": FIELD_TEXT,
    # Course identifier so pull can filter rows per course (review issue 1);
    # without it a per-course pull would scan the whole shared table.
    "班级": FIELD_TEXT,
    "来源": FIELD_SINGLE_SELECT,
    "原始文本": FIELD_TEXT,
    "清洗文本": FIELD_TEXT,
    "AI评分摘要": FIELD_TEXT,
    "AI置信度": FIELD_SINGLE_SELECT,
    "AI建议标签": FIELD_MULTI_SELECT,
    "加分项": FIELD_MULTI_SELECT,
    "教师评分": FIELD_TEXT,
    "教师标签": FIELD_MULTI_SELECT,
    "教师批注": FIELD_TEXT,
    "状态": FIELD_SINGLE_SELECT,
    "更新时间": FIELD_DATE,
}

TABLE_PREP_PLANS = {
    "班级": FIELD_TEXT,
    "计划状态": FIELD_SINGLE_SELECT,
    "讲评顺序": FIELD_TEXT,
    "备注": FIELD_TEXT,
    "AI总结": FIELD_TEXT,
    "更新时间": FIELD_DATE,
}

# Single-select options that must exist for the sync builders to succeed.
# Keyed by field name; merged across all tables that use the field.
SINGLE_SELECT_OPTIONS: dict[str, list[str]] = {
    "类型": ["两难", "事实观点", "因果"],
    "认知梯段": ["基础层", "发展层", "进阶层"],
    "来源": ["手动录入", "音频转写"],
    "AI置信度": ["高", "低", "不确定"],
    "加分项": ["有自己", "有新意"],
    "状态": ["待评估", "AI已评", "教师已审"],
    "计划状态": ["草稿", "已确认"],
}


class BitableService:
    def __init__(
        self,
        client: FeishuClient,
        app_token: str = "",
        table_ids: Optional[dict] = None,
    ) -> None:
        self.client = client
        self.app_token = app_token or client.config.bitable_app_token
        self.table_ids = table_ids or client.config.bitable_table_ids

    def _base(self, table_id: str) -> str:
        return f"/bitable/v1/apps/{self.app_token}/tables/{table_id}"

    async def list_tables(self) -> Any:
        """List tables of the configured app (useful to look up table_ids)."""
        return await self.client.request(
            "GET", f"/bitable/v1/apps/{self.app_token}/tables", params={"page_size": 100}
        )

    async def search_records(
        self,
        table_id: str,
        page_size: int = 500,
        page_token: str = "",
        filter_spec: Optional[dict] = None,
    ) -> Any:
        body: dict = {"page_size": page_size}
        if page_token:
            body["page_token"] = page_token
        if filter_spec:
            body["filter"] = filter_spec
        return await self.client.request(
            "POST", f"{self._base(table_id)}/records/search", json_body=body
        )

    async def batch_create_records(self, table_id: str, records: list[dict]) -> Any:
        """records: [{"fields": {...}}, ...] (max 1000 per call)."""
        return await self.client.request(
            "POST", f"{self._base(table_id)}/records/batch_create", json_body={"records": records}
        )

    async def batch_update_records(self, table_id: str, records: list[dict]) -> Any:
        """records: [{"record_id": "...", "fields": {...}}, ...] (max 500 per call)."""
        return await self.client.request(
            "POST", f"{self._base(table_id)}/records/batch_update", json_body={"records": records}
        )

    # ── Field management (schema bootstrap, best effort) ────────────────

    async def list_fields(self, table_id: str) -> list[dict]:
        """List existing fields of a table (used to make ensure_schema idempotent)."""
        payload = await self.client.request(
            "GET", f"{self._base(table_id)}/fields", params={"page_size": 100}
        )
        return payload.get("items", []) if isinstance(payload, dict) else []

    async def create_field(
        self,
        table_id: str,
        field_name: str,
        field_type: int,
        options: Optional[list[str]] = None,
    ) -> Any:
        """Create a field; single/multi-select fields get their option list."""
        body: dict = {"field_name": field_name, "type": field_type}
        if options:
            body["property"] = {"options": [{"name": name} for name in options]}
        return await self.client.request(
            "POST", f"{self._base(table_id)}/fields", json_body=body
        )

    async def update_field_options(
        self,
        table_id: str,
        field_id: str,
        options: list[str],
        field_name: str = "",
        field_type: int = 0,
    ) -> Any:
        """Set a select field's FULL option list (PUT is a replace, not append).

        Callers must merge any pre-existing options into ``options`` first —
        anything omitted here is dropped by the API, including custom options
        teachers added in the console. The update endpoint requires
        field_name and type in the body (official docs), so pass them through
        from the list_fields payload whenever available.
        """
        body: dict = {
            "property": {"options": [{"name": name} for name in options]},
        }
        if field_name:
            body["field_name"] = field_name
        if field_type:
            body["type"] = field_type
        return await self.client.request(
            "PUT", f"{self._base(table_id)}/fields/{field_id}", json_body=body
        )

    async def ensure_schema(self, schemas: Optional[dict] = None) -> dict:
        """Idempotently create missing fields and single-select options.

        schemas maps table_key -> {field_name: field_type}; defaults to the four
        built-in table schemas. Options come from SINGLE_SELECT_OPTIONS.
        Returns a per-table report; API failures are captured per table instead
        of aborting the whole run.
        """
        schemas = schemas or {
            "courses": TABLE_COURSES,
            "topics": TABLE_TOPICS,
            "students": TABLE_STUDENTS,
            "responses": TABLE_RESPONSES,
            # prep_plans must not be missed: the table would be created empty
            # and prep-plan pushes would fail with missing-field errors.
            "prep_plans": TABLE_PREP_PLANS,
        }
        report: dict[str, dict] = {}
        for table_key, schema in schemas.items():
            table_id = (self.table_ids or {}).get(table_key, "")
            if not table_id:
                report[table_key] = {"status": "skipped", "reason": "table_id not configured"}
                continue
            created: list[str] = []
            updated_options: list[str] = []
            failed: list[str] = []
            try:
                existing = {f.get("field_name", ""): f for f in await self.list_fields(table_id)}
                for field_name, field_type in schema.items():
                    try:
                        field = existing.get(field_name)
                        if field is None:
                            options = SINGLE_SELECT_OPTIONS.get(field_name)
                            await self.create_field(table_id, field_name, field_type, options)
                            created.append(field_name)
                            continue
                        # Idempotent option bootstrap for select fields.
                        want = SINGLE_SELECT_OPTIONS.get(field_name)
                        if not want:
                            continue
                        # Keep existing options in their current order: the PUT
                        # below is a full replace, so anything we don't send
                        # back is dropped — including options teachers added
                        # by hand in the console.
                        have_names = [
                            str(opt.get("name") or "")
                            for opt in ((field.get("property") or {}).get("options") or [])
                        ]
                        have = {name for name in have_names if name}
                        missing = [name for name in want if name not in have]
                        if missing:
                            await self.update_field_options(
                                table_id,
                                field.get("field_id", ""),
                                have_names + missing,
                                field_name=field.get("field_name", ""),
                                field_type=field.get("type", 0),
                            )
                            updated_options.append(field_name)
                    except Exception as exc:  # noqa: BLE001 - report per field
                        failed.append(f"{field_name}: {exc}")
                report[table_key] = {
                    "status": "ok",
                    "created_fields": created,
                    "updated_options": updated_options,
                    "failed": failed,
                }
            except Exception as exc:  # noqa: BLE001 - report per table
                report[table_key] = {"status": "error", "error": str(exc)}
        return report
