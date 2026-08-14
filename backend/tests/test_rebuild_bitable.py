"""Unit tests for the Bitable rebuild helper (paged clear loop)."""

import asyncio
import os


def _load_module():
    # feishu.client's module-level load_dotenv would leak real credentials into
    # the parent pytest env; snapshot and restore around the import.
    saved_env = dict(os.environ)
    try:
        from feishu.rebuild_bitable import _clear_table
        return _clear_table
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


class _FakeService:
    def __init__(self, pages):
        self.pages = list(pages)
        self.deleted = []

    async def search_records(self, table_id, page_size=500, page_token="", filter_spec=None):
        return self.pages.pop(0) if self.pages else {"items": [], "page_token": ""}

    async def batch_delete_records(self, table_id, record_ids):
        self.deleted.extend(record_ids)


def test_clear_table_pages_and_stops():
    clear_table = _load_module()
    fake = _FakeService(
        [
            {"items": [{"record_id": "r1"}, {"record_id": "r2"}], "page_token": "p2"},
            {"items": [{"record_id": "r3"}], "page_token": ""},
        ]
    )
    deleted = asyncio.run(clear_table(fake, "tbl001"))
    assert deleted == 3
    assert fake.deleted == ["r1", "r2", "r3"]


def test_clear_table_empty_stops():
    clear_table = _load_module()
    fake = _FakeService([{"items": [], "page_token": ""}])
    deleted = asyncio.run(clear_table(fake, "tbl001"))
    assert deleted == 0
    assert fake.deleted == []
