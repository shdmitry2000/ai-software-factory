"""Console UI-0: access control, shell, health, overview data, live stream framing."""
from __future__ import annotations

import os
import socket
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from mission_control import registry  # noqa: E402
from mission_control.console.app import BUILT, SCREENS, create_app  # noqa: E402
from mission_control.console.peercred import Operator, operator_for, peer_of  # noqa: E402

ADMIN = Operator("alice", 1001, True, "peercred", ("factory-operators",))
STRANGER = Operator("mallory", 1002, False, "peercred", ())


def ts(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def store():
    s = registry.get("store", "memory")()
    s.upsert("engines", "factory-1", {"engine": "factory-1", "last_heartbeat": ts(10)})
    s.upsert("engines", "old-1", {"engine": "old-1", "last_heartbeat": ts(3600)})
    s.upsert("agents", "compliance-1-local", {"agent_id": "compliance-1-local", "state": "idle", "last_heartbeat": ts(20)})
    s.upsert("deployments", "compliance@local", {"deployment": "compliance@local", "state": "running", "last_ok": True})
    s.upsert("events", "e1", {"type": "dev.factory.agent.heartbeat", "subject": "agent/compliance-1-local",
                              "factoryengine": "compliance-1-local", "received_at": ts(5),
                              "data": {"note": "PAYLOAD-MUST-NOT-RENDER"}})
    return s


def client(store, op):
    return TestClient(create_app(store, resolve=lambda creds: op, poll_s=0.05))


def test_health_is_open_and_checks_the_store(store):
    r = client(store, None).get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_no_identity_is_refused(store):
    c = client(store, None)
    assert c.get("/").status_code == 403
    assert c.get("/api/whoami").json()["error"] == "forbidden"


def test_non_operator_is_refused_with_how_to_get_in(store):
    r = client(store, STRANGER).get("/")
    assert r.status_code == 403 and "mallory" in r.text and "factory-operators" in r.text


def test_operator_sees_the_shell_and_overview(store):
    r = client(store, ADMIN).get("/")
    assert r.status_code == 200
    for _, label, _ in SCREENS:
        assert f">{label}</a>" in r.text
    assert "alice" in r.text and "compliance-1-local" in r.text and "compliance@local" in r.text
    assert "Engine old-1 is silent" in r.text                    # needs-you list
    assert "PAYLOAD-MUST-NOT-RENDER" not in r.text               # payloads never reach the page
    assert 'sse-connect="/stream"' in r.text


def test_every_screen_answers_and_later_ones_name_their_step(store):
    c = client(store, ADMIN)
    for path, label, stage in SCREENS[1:]:
        r = c.get(f"/{path}")
        assert r.status_code == 200, path
        if stage not in BUILT:
            assert stage in r.text, path


def test_whoami_and_overview_api(store):
    c = client(store, ADMIN)
    assert c.get("/api/whoami").json()["name"] == "alice"
    ov = c.get("/api/overview").json()
    assert {e["engine"]: e["alive"] for e in ov["engines"]} == {"factory-1": True, "old-1": False}
    assert any(p["kind"] == "engine.silent" for p in ov["needs"])


def test_dev_operator_only_when_no_peer_credentials(store):
    c = TestClient(create_app(store, dev_operator="dev-me"))
    assert c.get("/api/whoami").json() == {"name": "dev-me", "uid": None, "how": "dev", "groups": []}


def test_stream_signals_changed_when_a_new_event_lands(store):
    with client(store, ADMIN).stream("GET", "/stream") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = r.iter_lines()
        for line in lines:
            if line.startswith(": connected"):
                break
        store.upsert("events", "e2", {"type": "dev.factory.engine.heartbeat", "subject": "engine/factory-1",
                                      "received_at": ts(0)})
        for line in lines:
            if line.startswith("event: "):
                assert line == "event: changed"
                break


def test_operator_for_this_process_and_root():
    me = operator_for(os.getuid())
    assert me.name and me.uid == os.getuid()
    assert operator_for(0).allowed is True
    assert peer_of(None) is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SO_PEERCRED is Linux-only")
def test_peer_of_reads_the_kernel_credentials():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        pid, uid, gid = peer_of(a)
        assert (pid, uid) == (os.getpid(), os.getuid())
    finally:
        a.close(), b.close()
