"""Archon rows → factory events, on a real probe run (1 run, 16 events, fan-out and join)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from mission_control.adapters.engine.archon import ArchonEngine, graph_from_source, to_events
from mission_control.events import validate
from mission_control.ports import EngineAdapter

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "archon-probe-run.json").read_text())
RUNS = {r["id"]: r for r in FIXTURE["runs"]}
ROWS = FIXTURE["events"]
GRAPH = {"workflow": "mc-probe",
         "nodes": [{"id": n} for n in ["pick", "build", "validate", "app-check", "holdout-verify", "review", "merge-gate"]],
         "edges": [{"from": "pick", "to": "build"}, {"from": "build", "to": "validate"},
                   {"from": "build", "to": "app-check"}, {"from": "build", "to": "holdout-verify"},
                   {"from": "validate", "to": "review"}, {"from": "app-check", "to": "review"},
                   {"from": "holdout-verify", "to": "review"}, {"from": "review", "to": "merge-gate"}]}


def mapped(graph=lambda run: GRAPH):
    return to_events(RUNS, ROWS, engine="factory-1", product="file-to-markdown-convertor", graph_loader=graph)


def test_every_event_is_valid():
    events, skipped = mapped()
    assert skipped == {}
    bad = {e["id"]: validate(e) for e in events if validate(e)}
    assert bad == {}


def test_one_event_per_row_plus_the_graph():
    events, _ = mapped()
    assert len(events) == len(ROWS) + 1
    assert [e["type"] for e in events[:2]] == ["dev.factory.run.started", "dev.factory.graph.declared"]
    assert events[-1]["type"] == "dev.factory.run.completed"


def test_no_graph_when_source_unreadable():
    events, _ = mapped(graph=lambda run: None)
    assert "dev.factory.graph.declared" not in {e["type"] for e in events}


def test_ids_are_deterministic():
    assert [e["id"] for e in mapped()[0]] == [e["id"] for e in mapped()[0]]
    assert all(e["id"].startswith("archon:") for e in mapped()[0])


def test_one_trace_per_run():
    traces = {e["traceparent"].split("-")[1] for e in mapped()[0]}
    assert traces == {"c9df015150ff45b98b7d88401e928fd5"}


def test_fan_out_runs_in_parallel():
    events, _ = mapped()
    starts = {e["data"]["node"]: e["time"] for e in events if e["type"] == "dev.factory.node.started"}
    ends = {e["data"]["node"]: e["time"] for e in events if e["type"] == "dev.factory.node.completed"}
    branch = ["validate", "app-check", "holdout-verify"]
    assert max(starts[n] for n in branch) < min(ends[n] for n in branch)  # all running at once
    assert all(starts["review"] >= ends[n] for n in branch)            # review waited for all three


def test_node_durations_and_run_duration():
    events, _ = mapped()
    durations = {e["data"]["node"]: e["data"]["duration_ms"] for e in events if e["type"] == "dev.factory.node.completed"}
    assert 1900 <= durations["build"] <= 2500 and 3900 <= durations["app-check"] <= 4500
    done = events[-1]["data"]
    assert done["workflow"] == "mc-probe" and done["duration_ms"] == 6164


def test_graph_from_captured_source(tmp_path):
    src = tmp_path / "source"
    (src / ".archon" / "workflows").mkdir(parents=True)
    (src / ".archon" / "workflows" / "mc-probe.yaml").write_text(
        "name: mc-probe\nnodes:\n  - id: a\n    bash: echo\n  - id: b\n    depends_on: [a]\n    prompt: hi\n")
    run = {"workflow_name": "mc-probe", "metadata": json.dumps({"workflow_source": {"root": str(src)}})}
    g = graph_from_source(run)
    assert g == {"workflow": "mc-probe", "nodes": [{"id": "a", "kind": "bash"}, {"id": "b", "kind": "prompt"}],
                 "edges": [{"from": "a", "to": "b"}]}


def _db(tmp_path) -> Path:
    db = tmp_path / "archon.db"
    con = sqlite3.connect(db)
    run_cols = [k for k in FIXTURE["runs"][0] if k != "_rowid"]
    ev_cols = [k for k in ROWS[0] if k != "_rowid"]
    con.execute(f"create table remote_agent_workflow_runs ({', '.join(run_cols)})")
    con.execute(f"create table remote_agent_workflow_events ({', '.join(ev_cols)})")
    for r in FIXTURE["runs"]:
        con.execute(f"insert into remote_agent_workflow_runs values ({','.join('?' * len(run_cols))})", [r[c] for c in run_cols])
    for r in ROWS:
        con.execute(f"insert into remote_agent_workflow_events values ({','.join('?' * len(ev_cols))})", [r[c] for c in ev_cols])
    con.commit()
    con.close()
    return db


def test_engine_reads_with_a_cursor(tmp_path):
    eng = ArchonEngine(str(_db(tmp_path)), engine="factory-1", graph_loader=lambda run: None)
    assert isinstance(eng, EngineAdapter)
    first, cur = eng.events_since("", limit=10)
    rest, cur2 = eng.events_since(cur, limit=100)
    none, cur3 = eng.events_since(cur2)
    assert len(first) == 10 and len(rest) == 6 and none == [] and cur3 == cur2 == "16"
    assert len({e["id"] for e in first + rest}) == 16


def test_heartbeat_is_valid(tmp_path):
    hb = ArchonEngine(str(_db(tmp_path)), engine="factory-1", pin="1cfb8fc5").heartbeat()
    assert validate(hb) == [] and hb["data"]["slots"] == {"total": 1, "busy": 0}
