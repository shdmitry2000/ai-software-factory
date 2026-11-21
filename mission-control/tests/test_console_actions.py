"""Console UI-2: actions send commands as the operator; unsafe requests are refused."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from mission_control import registry  # noqa: E402
from mission_control.console.app import create_app  # noqa: E402
from mission_control.console.peercred import Operator  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
ADMIN = Operator("alice", 1001, True)
HX = {"HX-Request": "true"}


class Recorder:
    def __init__(self):
        self.sent = []

    def publish(self, topic, ev, *, ordering_key=""):
        self.sent.append((topic, ev))
        return ev["id"]


@pytest.fixture
def setup():
    store = registry.get("store", "memory")()
    store.upsert("engines", "factory-1", {"engine": "factory-1", "last_heartbeat": "2026-11-21T10:00:00Z"})
    store.upsert("deployments", "compliance@local", {"deployment": "compliance@local", "state": "running"})
    bus = Recorder()
    app = create_app(store, resolve=lambda c: ADMIN, bus=bus, products_dir=REPO / "products",
                     prompts=registry.get("prompts", "files")(root=str(REPO / "prompts")),
                     allowed_hosts=("testserver", "localhost"))
    return store, bus, TestClient(app)


def test_act_sends_a_command_as_the_operator(setup):
    store, bus, c = setup
    r = c.post("/act", data={"action": "engine.halt", "target": "factory-1", "reason": "bad merge"}, headers=HX)
    assert r.status_code == 200 and "engine.halt" in r.text
    topic, ev = bus.sent[0]
    assert topic == "factory-commands" and ev["type"] == "dev.factory.command.issued"
    d = ev["data"]
    assert (d["action"], d["target"], d["who"], d["reason"], d["via"]) == ("engine.halt", "factory-1", "alice", "bad merge", "console")


def test_act_passes_args_and_replicas_as_int(setup):
    _, bus, c = setup
    c.post("/act", data={"action": "deployment.scale", "target": "compliance@local", "reason": "more",
                         "arg_replicas": "2", "arg_empty": ""}, headers=HX)
    assert bus.sent[0][1]["data"]["args"] == {"replicas": 2}


@pytest.mark.parametrize("headers,why", [
    ({}, "must come from the console page"),
    ({**HX, "Origin": "https://evil.example"}, "cross-origin"),
])
def test_unsafe_actions_refused(setup, headers, why):
    _, bus, c = setup
    r = c.post("/act", data={"action": "engine.halt", "target": "factory-1", "reason": "x y z"}, headers=headers)
    assert r.status_code == 403 and why in r.text and bus.sent == []


def test_reason_is_required(setup):
    _, bus, c = setup
    r = c.post("/act", data={"action": "engine.halt", "target": "factory-1", "reason": " "}, headers=HX)
    assert r.status_code == 400 and bus.sent == []


def test_unknown_host_refused_everywhere():
    app = create_app(registry.get("store", "memory")(), resolve=lambda c: ADMIN)
    c = TestClient(app, base_url="http://evil.example")
    assert c.get("/").status_code == 421 and c.get("/api/health").status_code == 421
    assert TestClient(app, base_url="http://localhost:8080").get("/api/health").status_code == 200


def test_read_only_console_has_no_buttons_and_refuses_actions():
    store = registry.get("store", "memory")()
    store.upsert("engines", "factory-1", {"engine": "factory-1", "last_heartbeat": "2026-11-21T10:00:00Z"})
    c = TestClient(create_app(store, resolve=lambda c: ADMIN, allowed_hosts=("testserver",)))
    assert 'hx-post="/act"' not in c.get("/agents").text
    r = c.post("/act", data={"action": "engine.halt", "target": "factory-1", "reason": "x y z"}, headers=HX)
    assert r.status_code == 403 and "read-only" in r.text


def test_settings_sends_one_update_per_changed_field_with_the_seen_value(setup):
    _, bus, c = setup
    page = c.get("/projects").text
    assert 'name="new:base_branch"' in page and 'name="old:compliance.daily_cap_usd"' in page
    form = {"new:base_branch": "main", "old:base_branch": "main",
            "new:compliance.mode": "advisory", "old:compliance.mode": "advisory",
            "new:compliance.label": "production", "old:compliance.label": "production",
            "new:compliance.daily_cap_usd": "2", "old:compliance.daily_cap_usd": "1.0", "reason": "busy week"}
    r = c.post("/settings/file-to-markdown-convertor", data=form, headers=HX)
    assert r.status_code == 200 and len(bus.sent) == 1
    d = bus.sent[0][1]["data"]
    assert d["action"] == "product.update" and d["args"] == {"field": "compliance.daily_cap_usd", "old": "1.0", "new": "2"}
    form["new:compliance.daily_cap_usd"] = "1"
    assert "nothing changed" in c.post("/settings/file-to-markdown-convertor", data=form, headers=HX).text


def test_buttons_audit_and_command_status(setup):
    store, _, c = setup
    agents = c.get("/agents").text
    assert "engine.halt" in agents and "deployment.restart" in agents and "deployment.stop" in agents
    store.upsert("commands", "abc12345", {"command_id": "abc12345", "action": "engine.halt", "target": "factory-1",
                 "who": "alice", "reason": "bad merge", "status": "succeeded", "issued_at": "2026-11-21T10:00:00Z",
                 "detail": "Local launch brake set.", "via": "console"})
    audit = c.get("/audit").text
    assert "bad merge" in audit and "Local launch brake set." in audit and "alice" in audit
    frag = c.get("/fragments/command/abc12345").text
    assert "succeeded" in frag and "Local launch brake set." in frag


def test_acknowledged_problem_leaves_needs_you(setup):
    store, _, c = setup
    store.merge("deployments", "compliance@local", {"state": "stopped", "updated_at": "2026-11-21T10:00:00Z"})
    assert "Deployment compliance@local is stopped" in c.get("/").text.split("Engines")[0]
    store.upsert("problem_states", "deployment.down:compliance@local", {
        "problem_id": "deployment.down:compliance@local",
        "ack": {"who": "alice", "reason": "rules change", "since": "2026-11-21T10:00:00Z"}})
    assert "Deployment compliance@local is stopped" not in c.get("/").text.split("Engines")[0]
    probs = c.get("/problems").text
    assert "acknowledged by alice" in probs
