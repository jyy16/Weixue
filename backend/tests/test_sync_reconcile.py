"""Unit tests for the Bitable full-sync reconcile step (stale-row cleanup)."""

import asyncio
import os


def _load_syncer_class():
    # Importing feishu.sync triggers feishu.client's load_dotenv, which would
    # leak real credentials into the parent pytest env; snapshot and restore.
    saved_env = dict(os.environ)
    try:
        from feishu.sync import BitableSyncer
        return BitableSyncer
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


class _FakeConfig:
    bitable_table_ids = {"responses": "tbl_resp", "topics": ""}


class _FakeService:
    def __init__(self):
        self.pages = [
            {
                "items": [{"record_id": "r1"}, {"record_id": "r2"}],
                "has_more": True,
                "page_token": "p2",
            },
            {"items": [{"record_id": "r3"}], "has_more": False, "page_token": ""},
        ]
        self.deleted = []

    async def search_records(self, table_id, page_size=500, page_token="", filter_spec=None):
        return self.pages.pop(0)

    async def batch_delete_records(self, table_id, record_ids):
        self.deleted.append((table_id, list(record_ids)))


class _FakeBinding:
    def __init__(self, remote_record_id):
        self.remote_record_id = remote_record_id


class _FakeDB:
    def __init__(self, bound_ids):
        self._bound_ids = bound_ids

    def query(self, model):
        return self

    def filter(self, *args, **kwargs):
        return [_FakeBinding(rid) for rid in self._bound_ids]


def test_reconcile_deletes_unbound_rows_only():
    BitableSyncer = _load_syncer_class()
    syncer = BitableSyncer.__new__(BitableSyncer)
    syncer.config = _FakeConfig()
    syncer.service = _FakeService()

    result = asyncio.run(syncer._reconcile_tables(_FakeDB(bound_ids=["r2"])))

    # responses: r2 is bound -> kept; r1/r3 stale -> deleted.
    assert result == {"responses": 2}
    assert syncer.service.deleted == [("tbl_resp", ["r1", "r3"])]


def test_reconcile_all_bound_nothing_deleted():
    BitableSyncer = _load_syncer_class()
    syncer = BitableSyncer.__new__(BitableSyncer)
    syncer.config = _FakeConfig()
    syncer.service = _FakeService()

    result = asyncio.run(syncer._reconcile_tables(_FakeDB(bound_ids=["r1", "r2", "r3"])))

    assert result == {"responses": 0}
    assert syncer.service.deleted == []
