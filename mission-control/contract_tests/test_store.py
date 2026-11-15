"""StateStore contract: what every store adapter must do."""
from __future__ import annotations

from mission_control.ports import StateStore


def test_is_a_state_store(store_h):
    assert isinstance(store_h.store, StateStore)


def test_missing_doc_is_none(store_h):
    assert store_h.store.get(store_h.collection, "nope") is None


def test_upsert_then_get(store_h):
    s, c = store_h.store, store_h.collection
    s.upsert(c, "a", {"status": "running", "n": 1})
    assert s.get(c, "a") == {"status": "running", "n": 1}


def test_upsert_twice_is_one_document(store_h):
    s, c = store_h.store, store_h.collection
    s.upsert(c, "a", {"n": 1})
    s.upsert(c, "a", {"n": 1})
    assert len(s.query(c)) == 1


def test_upsert_replaces(store_h):
    s, c = store_h.store, store_h.collection
    s.upsert(c, "a", {"x": 1, "y": 2})
    s.upsert(c, "a", {"x": 3})
    assert s.get(c, "a") == {"x": 3}


def test_merge_updates_only_given_fields(store_h):
    s, c = store_h.store, store_h.collection
    s.merge(c, "a", {"x": 1})
    s.merge(c, "a", {"y": 2})
    assert s.get(c, "a") == {"x": 1, "y": 2}


def test_update_read_modify_write(store_h):
    s, c = store_h.store, store_h.collection
    for _ in range(20):
        s.update(c, "counter", lambda d: {"n": (d or {}).get("n", 0) + 1})
    assert s.get(c, "counter") == {"n": 20}


def test_update_returning_none_deletes(store_h):
    s, c = store_h.store, store_h.collection
    s.upsert(c, "a", {"x": 1})
    s.update(c, "a", lambda d: None)
    assert s.get(c, "a") is None


def test_query_where_order_limit(store_h):
    s, c = store_h.store, store_h.collection
    for i, status in enumerate(["failed", "running", "completed", "failed"]):
        s.upsert(c, f"r{i}", {"status": status, "started": i})
    failed = s.query(c, where=[("status", "==", "failed")], order_by="started", descending=True)
    assert [d["started"] for d in failed] == [3, 0]
    assert len(s.query(c, order_by="started", limit=2)) == 2
    assert {d["status"] for d in s.query(c, where=[("status", "in", ["running", "completed"])])} == {"running", "completed"}


def test_neutral_ids_with_slashes_and_hashes(store_h):
    s, c = store_h.store, store_h.collection
    wid = "tracker:github/shdmitry2000/file-to-markdown-convertor#14"
    s.upsert(c, wid, {"stage": "building"})
    s.merge(c, wid, {"laps": 2})
    assert s.get(c, wid) == {"stage": "building", "laps": 2}
    s.delete(c, wid)
    assert s.get(c, wid) is None


def test_delete(store_h):
    s, c = store_h.store, store_h.collection
    s.upsert(c, "a", {"x": 1})
    s.delete(c, "a")
    assert s.get(c, "a") is None
