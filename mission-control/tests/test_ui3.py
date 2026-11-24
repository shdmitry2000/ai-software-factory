"""UI-3 part 1: desired state, upgrade / roll back, host heartbeats, logs, Deploy/Hosts/Chains."""
from __future__ import annotations

import asyncio
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mission_control import deployspec, events, projections, registry
from mission_control.builder import FakeBuilder
from mission_control.console import views
from mission_control.handler import CommandHandler, issue

REPO = Path(__file__).resolve().parents[2]
D1, D2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64


class Bus:
    def __init__(self):
        self.sent = []

    def publish(self, topic, ev, *, ordering_key=""):
        self.sent.append((topic, ev))
        return ev["id"]


def spec():
    return deployspec.load(REPO / "deploy", products_dir=REPO / "products")


@pytest.fixture
def env():
    store = registry.get("store", "memory")()
    bus = Bus()
    rt = registry.get("runtime", "fake")()
    builder = FakeBuilder(digest=D1)
    h = CommandHandler(runtime=rt, bus=bus, engine="factory-1", spec_loader=spec, vars={"PROJECT": "p"},
                       builder=builder, store=store, products_dir=REPO / "products")
    return h, store, rt, builder


def run(h, store, action, target="compliance@local", **args):
    cmd = issue(Bus(), "factory-commands", action, target, who="t", reason="ui3 test", args=args)
    out = h.handle(cmd)
    for ev in out:
        assert events.validate(ev) == [], ev["type"]
        projections.project(ev, store)
    return out, store.get("commands", cmd["data"]["command_id"])


# ------------------------------------------------------------------ desired state, upgrade, roll back
def test_apply_records_desired_state_and_digest_history(env):
    h, store, rt, builder = env
    _, c = run(h, store, "deployment.apply")
    assert c["status"] == "succeeded" and builder.built == ["compliance"]
    d = store.get("deployments", "compliance@local")
    assert d["desired"] == {"component": "compliance", "host": "local", "replicas": 1, "digest": D1,
                            "products": ["file-to-markdown-convertor"]}
    assert [x["digest"] for x in d["digests"]] == [D1]

    # a plain apply (restore, reapply) reuses the pinned digest: no build
    _, c = run(h, store, "deployment.apply")
    assert c["status"] == "succeeded" and builder.built == ["compliance"]

    # upgrade = build the checkout's HEAD
    builder.digest = D2
    _, c = run(h, store, "deployment.apply", build="1")
    assert builder.built == ["compliance", "compliance"]
    d = store.get("deployments", "compliance@local")
    assert d["digest"] == D2 and [x["digest"] for x in d["digests"]] == [D2, D1]

    # roll back = apply the previous digest
    _, c = run(h, store, "deployment.apply", digest=D1)
    d = store.get("deployments", "compliance@local")
    assert c["status"] == "succeeded" and d["digest"] == D1 and d["desired"]["digest"] == D1
    assert [x["digest"] for x in d["digests"]] == [D1, D2]
    assert rt.status()[0].digest == D1


def test_scale_and_overrides_change_the_desired_state(env):
    h, store, rt, _ = env
    run(h, store, "deployment.apply", replicas="2")
    assert store.get("deployments", "compliance@local")["desired"]["replicas"] == 2
    assert len(rt.status()) == 2
    run(h, store, "deployment.scale", replicas="1")
    assert store.get("deployments", "compliance@local")["desired"]["replicas"] == 1
    run(h, store, "deployment.apply")              # stored desired wins over git
    assert len(rt.status()) == 1
    run(h, store, "deployment.apply", replicas=3)
    run(h, store, "deployment.apply", reset="1")   # back to deploy/deployments.yaml
    assert store.get("deployments", "compliance@local")["desired"]["replicas"] == 1


def test_remove_clears_the_desired_state(env):
    h, store, _, _ = env
    run(h, store, "deployment.apply")
    run(h, store, "deployment.remove")
    d = store.get("deployments", "compliance@local")
    assert d["state"] == "removed" and "desired" not in d


@pytest.mark.parametrize("args, why", [
    ({"digest": "sha256:short"}, "sha256:<64 hex>"),
    ({"replicas": "many"}, "replicas"),
    ({"products": "no-such-product"}, "no product"),
])
def test_bad_apply_args_are_refused(env, args, why):
    h, store, _, _ = env
    out, c = run(h, store, "deployment.apply", **args)
    assert c["status"] == "refused" and why in c["refusal"]


def test_unknown_host_is_refused(env):
    h, store, _, _ = env
    _, c = run(h, store, "deployment.apply", "compliance@vm-nowhere")
    assert c["status"] == "refused" and "no host 'vm-nowhere'" in c["refusal"]


# ------------------------------------------------------------------ hosts
def test_host_heartbeats_are_valid_and_projected(env):
    h, store, rt, _ = env
    run(h, store, "deployment.apply")
    for ev in h.beat_hosts():
        assert events.validate(ev) == []
        projections.project(ev, store)
    doc = store.get("hosts", "local")
    assert doc["state"] == "healthy" and doc["health"]["containers"] == 1 and doc["health"]["docker"] == "fake"
    rt.unreachable.add("local")
    for ev in h.beat_hosts():
        projections.project(ev, store)
    assert store.get("hosts", "local")["state"] == "unreachable"


def test_host_facts_on_this_machine():
    from mission_control.adapters.runtime import host_facts
    f = host_facts()
    assert f.get("disk_total_gb", 0) > 0 and 0 <= f["disk_used_pct"] <= 100


def test_host_problems():
    store = registry.get("store", "memory")()
    at = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    store.upsert("hosts", "local", {"host": "local", "state": "healthy", "last_heartbeat": "2026-11-21T11:59:30+00:00",
                                    "health": {"disk_used_pct": 91.0, "disk_total_gb": 10}})
    store.upsert("hosts", "vm-a", {"host": "vm-a", "state": "healthy", "last_heartbeat": "2026-11-21T11:00:00+00:00"})
    kinds = {(p.kind, p.key) for p in views.problems(store, at)}
    assert ("host.disk", "local") in kinds and ("host.silent", "vm-a") in kinds


# ------------------------------------------------------------------ logs
def test_logs_socket_serves_masked_lines(env):
    h, store, rt, _ = env
    run(h, store, "deployment.apply")
    rt.log("compliance@local", "token is fake-token here")
    path = tempfile.mktemp(prefix="mc", suffix=".sock", dir="/tmp")
    srv = h.logs_server(path)
    try:
        from mission_control.console.app import socket_logs

        async def read(dep):
            return [line async for line in socket_logs(path, dep, 50, False)]
        lines = asyncio.run(read("compliance@local"))
        assert lines[0].startswith("factory-compliance-1 started")
        assert lines[-1] == "token is [CLAUDE_CODE_OAUTH_TOKEN masked] here"
        assert asyncio.run(read("ghost@local"))[0].startswith("[handler] error: KeyError")
        s = socket.socket(socket.AF_UNIX)
        s.connect(path)
        s.sendall(b'{"deployment": "../etc"}\n')
        assert s.recv(200).startswith(b"error: ValueError")
        s.close()
    finally:
        srv.shutdown()
        Path(path).unlink(missing_ok=True)


# ------------------------------------------------------------------ console views
def test_deploy_view_shows_drift_and_rollback_targets():
    store = registry.get("store", "memory")()
    store.upsert("deployments", "compliance@local", {
        "deployment": "compliance@local", "host": "local", "state": "running", "digest": D2,
        "desired": {"component": "compliance", "host": "local", "replicas": 2, "digest": D2,
                    "products": ["file-to-markdown-convertor"]},
        "digests": [{"digest": D2}, {"digest": D1}]})
    v = views.deploy(store, datetime.now(timezone.utc), spec())
    row = v["deployments"][0]
    assert row["differs"] == ["replicas 2 (git 1)"]
    assert [r["digest"] for r in row["rollback"]] == [D1]
    assert v["components"][0]["deployed"] == ["compliance@local"]


def test_chains_counts_today_per_edge():
    store = registry.get("store", "memory")()
    at = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    store.upsert("agents", "compliance-1-local", {"agent_id": "compliance-1-local"})
    for i in range(3):
        store.upsert("events", f"r{i}", {"type": "dev.factory.compliance.reported", "factoryengine": "compliance-1-local",
                                         "received_at": f"2026-11-21T0{i}:00:00+00:00"})
    store.upsert("events", "old", {"type": "dev.factory.compliance.reported", "factoryengine": "compliance-1-local",
                                   "received_at": "2026-11-20T23:00:00+00:00"})
    store.upsert("events", "other", {"type": "dev.factory.agent.heartbeat", "factoryengine": "factory-1",
                                     "received_at": "2026-11-21T01:00:00+00:00"})
    v = views.chains(store, at, spec())
    got = {(e["from"], e["to"]): e["count"] for e in v["edges"]}
    assert got[("compliance", "dev.factory.compliance.reported")] == 3
    assert got[("compliance", "dev.factory.agent.heartbeat")] == 0       # someone else's heartbeat
    assert got[("dev.factory.pullrequest.opened", "compliance")] == 0
    assert len(v["graph"]["lines"]) == 4


# ------------------------------------------------------------------ console endpoints
pytest.importorskip("fastapi")


def console(store, bus=None, logs_source=None):
    from fastapi.testclient import TestClient

    from mission_control.console.app import create_app
    from mission_control.console.peercred import Operator
    app = create_app(store, resolve=lambda c: Operator("alice", 1001, True), bus=bus,
                     products_dir=REPO / "products", allowed_hosts=("localhost",), logs_source=logs_source)
    return TestClient(app, base_url="http://localhost")


def test_deploy_dialog_sends_apply_with_choices():
    store, bus = registry.get("store", "memory")(), Bus()
    c = console(store, bus)
    assert "build the main node" in c.get("/panels/deploy?component=compliance").text
    r = c.post("/deploy", data={"component": "compliance", "host": "local", "replicas": "2", "image": D1,
                                "product": "file-to-markdown-convertor", "reason": "try two"},
               headers={"HX-Request": "true"})
    assert r.status_code == 200
    ev = bus.sent[-1][1]["data"]
    assert ev["action"] == "deployment.apply" and ev["target"] == "compliance@local" and ev["who"] == "alice"
    assert ev["args"] == {"replicas": 2, "digest": D1, "products": "file-to-markdown-convertor"}
    r = c.post("/deploy", data={"component": "compliance", "host": "local", "image": "build", "reason": "upgrade"},
               headers={"HX-Request": "true"})
    assert r.status_code == 200, r.text
    assert bus.sent[-1][1]["data"]["args"] == {"build": "1"}
    assert c.post("/deploy", data={"component": "compliance", "host": "local", "reason": "x y"}).status_code == 403
    assert c.post("/deploy", data={"component": "Bad!", "host": "local", "reason": "xyz"},
                  headers={"HX-Request": "true"}).status_code == 400


def test_new_screens_render():
    store = registry.get("store", "memory")()
    store.upsert("hosts", "local", {"host": "local", "kind": "local", "state": "healthy",
                                    "health": {"docker": "27", "disk_used_pct": 50.0}})
    c = console(store, Bus())
    for path in ("hosts", "deploy", "chains"):
        r = c.get(f"/{path}")
        assert r.status_code == 200, path
    assert "compliance.reported" in c.get("/chains").text
    assert "Deploy to local" in c.get("/hosts").text


def test_logs_stream_escapes_and_ends():
    async def source(dep, tail, follow):
        yield "<script>x</script>"
        yield "second"
    c = console(registry.get("store", "memory")(), Bus(), logs_source=source)
    body = c.get("/panels/logs/compliance@local/stream?follow=0").text
    assert "&lt;script&gt;x&lt;/script&gt;" in body and "<script>" not in body
    assert body.strip().endswith("— end of log —</div>")
    assert c.get("/panels/logs/..%2Fetc/stream").status_code in (400, 404)


def test_a_deployment_without_desired_state_keeps_its_running_image(env):
    h, store, rt, builder = env
    store.upsert("deployments", "compliance@local", {"deployment": "compliance@local", "state": "running", "digest": D2})
    builder.images.add(D2)
    _, c = run(h, store, "deployment.apply")
    assert c["status"] == "succeeded" and builder.built == []
    assert store.get("deployments", "compliance@local")["desired"]["digest"] == D2


def test_retired_agents_are_not_silent_problems():
    store = registry.get("store", "memory")()
    at = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    old = "2026-11-21T10:00:00+00:00"
    store.upsert("agents", "compliance-1-agent-1", {"agent_id": "compliance-1-agent-1", "last_heartbeat": old,
                                                    "location": "docker:agent-1/factory-compliance-1"})
    store.upsert("agents", "compliance-1-local", {"agent_id": "compliance-1-local", "last_heartbeat": old,
                                                  "location": "docker:local/factory-compliance-1"})
    store.upsert("deployments", "compliance@agent-1", {"deployment": "compliance@agent-1", "state": "removed", "instances": []})
    store.upsert("deployments", "compliance@local", {"deployment": "compliance@local", "host": "local", "state": "running",
                                                     "instances": [{"name": "factory-compliance-1", "state": "running"}]})
    silent = {p.key for p in views.problems(store, at) if p.kind == "agent.silent"}
    assert silent == {"compliance-1-local"}
