"""Projections from real events: runs, nodes, engines; any order, any repetition, same state."""
from __future__ import annotations

import random

from mission_control.adapters.store.memory import MemoryStore
from mission_control.projections import ENGINES, NODES, RUNS, project

from test_archon_engine import mapped

EVENTS = mapped()[0]
RUN = "c9df0151-50ff-45b9-8b7d-88401e928fd5"


def state(store) -> dict:
    return {c: sorted((repr(sorted(d.items())) for d in store.query(c))) for c in (RUNS, NODES, ENGINES)}


def apply(events) -> MemoryStore:
    s = MemoryStore()
    for e in events:
        project(e, s)
    return s


def test_run_projection():
    run = apply(EVENTS).get(RUNS, RUN)
    assert run["status"] == "completed" and run["workflow"] == "mc-probe" and run["engine"] == "factory-1"
    assert run["started_at"] < run["ended_at"] and run["duration_ms"] == 6164
    assert len(run["graph"]["edges"]) == 8


def test_node_projection():
    s = apply(EVENTS)
    nodes = {d["node"]: d for d in s.query(NODES, where=[("run", "==", RUN)])}
    assert set(nodes) == {"pick", "build", "validate", "app-check", "holdout-verify", "review", "merge-gate"}
    assert all(n["status"] == "completed" for n in nodes.values())
    assert 3900 <= nodes["app-check"]["duration_ms"] <= 4500
    assert nodes["review"]["started_at"] >= nodes["app-check"]["ended_at"]


def test_running_run_shows_running_nodes():
    upto = [e for e in EVENTS if e["time"] <= "2026-11-15T12:51:27Z"]
    s = apply(upto)
    assert s.get(RUNS, RUN)["status"] == "running"
    running = {d["node"] for d in s.query(NODES, where=[("status", "==", "running")])}
    assert running == {"validate", "app-check", "holdout-verify"}          # the fan-out, live


def test_same_state_in_any_order_and_with_duplicates():
    want = state(apply(EVENTS))
    rnd = random.Random(7)
    for _ in range(25):
        shuffled = EVENTS + rnd.sample(EVENTS, 10)                          # duplicates too
        rnd.shuffle(shuffled)
        assert state(apply(shuffled)) == want


def test_failure_beats_later_running_noise():
    fail = {**EVENTS[-1], "id": "archon:x", "type": "dev.factory.run.failed", "time": "2026-11-15T12:51:31Z",
            "data": {"workflow": "mc-probe", "attempt": 1, "failure_class": "timeout", "failed_node": "app-check"}}
    late_start = {**EVENTS[0], "id": "archon:y", "time": "2026-11-15T12:59:00Z"}
    s = apply([fail, late_start])
    run = s.get(RUNS, RUN)
    assert run["status"] == "failed" and run["failure_class"] == "timeout"


def test_engine_heartbeat_keeps_latest():
    hb = {"specversion": "1.0", "id": "bridge:factory-1:t1", "type": "dev.factory.engine.heartbeat",
          "time": "2026-11-15T12:00:00Z", "factoryengine": "factory-1",
          "data": {"slots": {"total": 1, "busy": 1}, "halted": False, "paused": False}}
    older = {**hb, "id": "bridge:factory-1:t0", "time": "2026-11-15T11:00:00Z",
             "data": {"slots": {"total": 1, "busy": 0}, "halted": True, "paused": False}}
    s = MemoryStore()
    s.merge(ENGINES, "factory-1", {"bridge_cursor": "16"})                   # the bridge's field survives
    project(hb, s)
    project(older, s)
    eng = s.get(ENGINES, "factory-1")
    assert eng["last_heartbeat"] == "2026-11-15T12:00:00Z" and eng["halted"] is False
    assert eng["slots"]["busy"] == 1 and eng["bridge_cursor"] == "16"
