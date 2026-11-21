"""Console UI-1 read models and screens: runs and retries, problems rules, products."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from mission_control import registry  # noqa: E402
from mission_control.console import views  # noqa: E402
from mission_control.console.app import create_app  # noqa: E402
from mission_control.console.peercred import Operator  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
AT = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
ADMIN = Operator("alice", 1001, True)


def t(seconds_ago: float, at: datetime = AT) -> str:
    return (at - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def store():
    s = registry.get("store", "memory")()
    s.upsert("engines", "factory-1", {"engine": "factory-1", "last_heartbeat": t(30)})
    s.upsert("agents", "a1", {"agent_id": "a1", "state": "idle", "last_heartbeat": t(20), "products": ["p"]})
    s.upsert("agents", "a2", {"agent_id": "a2", "state": "idle", "last_heartbeat": t(4000)})
    s.upsert("agents", "a3", {"agent_id": "a3", "state": "stopped", "last_heartbeat": t(9000)})
    s.upsert("deployments", "compliance@local", {"deployment": "compliance@local", "state": "stopped",
             "last_action": "deployment.stop", "last_ok": True, "updated_at": t(60)})
    # a failed run retried successfully (no problem), a failed run not retried (problem), a stuck run
    s.upsert("runs", "r1", {"run": "r1", "workflow": "archon-loop", "product": "file-to-markdown-convertor",
             "status": "failed", "started_at": t(900), "ended_at": t(800), "failure_class": "test", "failed_node": "validate"})
    s.upsert("runs", "r1b", {"run": "r1b", "parent_run": "r1", "workflow": "archon-loop", "status": "completed",
             "started_at": t(700), "ended_at": t(600), "duration_ms": 100000})
    s.upsert("runs", "r2", {"run": "r2", "workflow": "archon-ship", "product": "file-to-markdown-convertor",
             "status": "failed", "started_at": t(500), "ended_at": t(400), "failure_class": "review", "failed_node": "review"})
    s.upsert("runs", "r3", {"run": "r3", "workflow": "archon-loop", "status": "running", "started_at": t(9000),
             "last_event_at": t(7200)})
    s.upsert("nodes", "r2:review", {"run": "r2", "node": "review", "status": "failed", "started_at": t(450)})
    s.upsert("compliance", "file-to-markdown-convertor__3", {"product": "file-to-markdown-convertor", "pr": 3,
             "status": "findings", "head": "f9b6bff520c8", "rules": {"version": "2", "count": 1},
             "findings": [{"rule": "C-001", "severity": "low", "title": "minor"},
                          {"rule": "C-002", "severity": "high", "title": "secret in code"}],
             "reported_at": t(100), "cost_usd": 0.002})
    s.upsert("compliance", "file-to-markdown-convertor__4", {"product": "file-to-markdown-convertor", "pr": 4,
             "status": "error", "error": "diff fetch failed", "reported_at": t(50)})
    return s


def test_runs_group_retries_under_the_first_attempt(store):
    rows = views.runs(store, AT)
    by = {r["run"]: r for r in rows}
    assert set(by) == {"r1", "r2", "r3"}
    assert [c["run"] for c in by["r1"]["chain"]] == ["r1b"]
    assert by["r3"]["stuck"] is True and by["r2"]["stuck"] is False
    assert [r["run"] for r in views.runs(store, AT, status="failed")] == ["r2", "r1"]


def test_problems_rules(store):
    kinds = {(p.kind, p.title) for p in views.problems(store, AT)}
    titles = " | ".join(t for _, t in kinds)
    assert ("agent.silent", "Agent a2 is silent") in kinds
    assert "Agent a3" not in titles                                   # stopped on purpose
    assert ("deployment.down", "Deployment compliance@local is stopped") in kinds
    assert any(k == "run.failed" and "archon-ship" in t for k, t in kinds)     # r2
    assert not any(k == "run.failed" and "archon-loop" in t for k, t in kinds)  # r1 retried fine
    assert any(k == "run.stuck" for k, _ in kinds)
    assert any(k == "compliance.error" for k, _ in kinds)
    assert any(k == "compliance.high" for k, _ in kinds)
    sev = [p.severity for p in views.problems(store, AT)]
    assert sev == sorted(sev, key=views.SEVERITY_ORDER.get)


def test_a_cleared_problem_disappears(store):
    store.merge("deployments", "compliance@local", {"state": "running", "last_action": "deployment.start"})
    assert not any(p.kind == "deployment.down" for p in views.problems(store, AT))


def test_reports_sorted_and_filtered(store):
    rows = views.reports(store, AT)
    assert [r["pr"] for r in rows] == [4, 3]
    three = rows[1]
    assert three["top_severity"] == "high" and three["findings"][0]["rule"] == "C-002"
    assert three["rules_version"] == "2" and three["short_head"] == "f9b6bff5"
    assert [r["pr"] for r in views.reports(store, AT, status="error")] == [4]


def test_products_join_files_reports_and_runs(store):
    ps = views.products(store, AT, REPO / "products")
    p = next(x for x in ps if x["name"] == "file-to-markdown-convertor")
    assert p["base_branch"] == "main" and p["compliance"].mode == "advisory"
    assert p["open_prs"] == 2 and p["findings"] == 2 and p["last_run"]["run"] == "r2"


def test_run_detail(store):
    d = views.run_detail(store, "r2", AT)
    assert d["nodes"][0]["node"] == "review"
    assert views.run_detail(store, "nope", AT) is None


@pytest.fixture
def web(store):
    return TestClient(create_app(store, resolve=lambda c: ADMIN, products_dir=REPO / "products"))


@pytest.mark.parametrize("path,expect", [
    ("/agents", "a2"), ("/projects", "file-to-markdown-convertor"), ("/compliance", "secret in code"),
    ("/runs", "retry 1"), ("/problems", "is stopped"), ("/runs/r2", "review"),
    ("/compliance?status=error", "diff fetch failed"), ("/runs?status=completed", "r1b"), ("/runs?status=lost", "No runs"),
])
def test_screens_render(web, path, expect):
    r = web.get(path)
    assert r.status_code == 200 and expect in r.text, path


def test_fragments_refresh_on_changed(web):
    page = web.get("/runs?status=failed").text
    assert 'hx-get="/fragments/runs?status=failed"' in page and 'sse:changed' in page
    frag = web.get("/fragments/runs?status=failed")
    assert frag.status_code == 200 and "<html" not in frag.text and "r2" in frag.text


def test_unknown_run_is_404(web):
    assert web.get("/runs/nope").status_code == 404
