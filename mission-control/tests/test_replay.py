"""Projections are a pure function of the event log: delete, replay, identical."""
from __future__ import annotations

import base64
import json

from mission_control.adapters.store.memory import MemoryStore
from mission_control.projections import NODES, RUNS
from mission_control.replay import check, rebuild, snapshot
from mission_control.writer import handle

from test_archon_engine import mapped


def push(e) -> dict:
    return {"message": {"data": base64.b64encode(json.dumps(e).encode()).decode(), "messageId": "m"}}


def written() -> MemoryStore:
    s = MemoryStore()
    for e in mapped()[0]:
        assert handle(push(e), s)[0] == 204
    return s


def test_writer_builds_projections():
    s = written()
    assert s.get(RUNS, "c9df0151-50ff-45b9-8b7d-88401e928fd5")["status"] == "completed"
    assert len(s.query(NODES)) == 7


def test_delete_replay_identical():
    s = written()
    before = snapshot(s)
    for d in s.query(RUNS):
        s.delete(RUNS, d["run"])
    assert s.query(RUNS) == []
    rebuild(s, s)
    assert snapshot(s) == before


def test_check_reports_drift():
    s = written()
    assert check(s) == []
    s.merge(RUNS, "c9df0151-50ff-45b9-8b7d-88401e928fd5", {"status": "failed"})   # tamper
    assert check(s) == ["runs/c9df0151-50ff-45b9-8b7d-88401e928fd5"]
