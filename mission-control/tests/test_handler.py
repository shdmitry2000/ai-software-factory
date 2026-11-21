"""Command handler on the fake runtime: events, refusals, failures, projections."""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from mission_control import deployspec, events, projections, registry
from mission_control.builder import FakeBuilder
from mission_control.handler import CommandHandler, issue

REPO = Path(__file__).resolve().parents[2]


class Recorder:
    def __init__(self):
        self.sent = []

    def publish(self, topic, ev, *, ordering_key=""):
        self.sent.append((topic, ev))
        return ev["id"]

    def pull(self, *a, **k):
        return []


def spec():
    return deployspec.load(REPO / "deploy", products_dir=REPO / "products")


@pytest.fixture
def h():
    return CommandHandler(runtime=registry.get("runtime", "fake")(), bus=Recorder(), engine="factory-1",
                          spec_loader=spec, vars={"PROJECT": "proj"}, builder=FakeBuilder())


def command(action, target="compliance@local", **args):
    bus = Recorder()
    return issue(bus, "factory-commands", action, target, who="tester", reason="unit test", args=args)


def project_all(evs):
    store = registry.get("store", "memory")()
    for ev in evs:
        projections.project(ev, store)
    return store


def types(evs):
    return [e["type"].removeprefix("dev.factory.") for e in evs]


def test_apply_builds_deploys_and_reports_every_step(h):
    cmd = command("deployment.apply")
    out = h.handle(cmd)
    assert types(out)[:2] == ["command.issued", "command.acknowledged"]
    assert types(out)[-1] == "deployment.changed"
    steps = [e["data"]["step"] for e in out if e["type"].endswith("deployment.step")]
    assert steps[0] == "build" and "run" in steps and "credential" in steps
    assert all(not events.validate(e) for e in out)
    assert all(t == "factory-events" for t, _ in h.bus.sent)
    changed = out[-1]["data"]
    assert changed["ok"] and changed["digest"].startswith("sha256:b") and changed["instances"][0]["state"] == "running"
    assert h.builder.built == ["compliance"]

    store = project_all(out)
    c = store.get("commands", cmd["data"]["command_id"])
    assert c["status"] == "succeeded" and c["who"] == "tester" and c["accepted"] is True
    assert [s["step"] for _, s in sorted(c["steps"].items())] == steps
    d = store.get("deployments", "compliance@local")
    assert d["state"] == "running" and d["last_action"] == "deployment.apply" and d["digest"] == changed["digest"]


def test_projections_do_not_depend_on_arrival_order(h):
    out = h.handle(command("deployment.apply"))
    a = project_all(out)
    shuffled = out[:]
    random.Random(7).shuffle(shuffled)
    b = project_all(shuffled)
    cid = out[0]["data"]["command_id"]
    assert a.get("commands", cid) == b.get("commands", cid)
    assert a.get("deployments", "compliance@local") == b.get("deployments", "compliance@local")


@pytest.mark.parametrize("action,target,args,why", [
    ("deployment.explode", "compliance@local", {}, "unknown action"),
    ("deployment.restart", "compliance", {}, "deployment id"),
    ("deployment.scale", "compliance@local", {}, "args.replicas"),
    ("deployment.scale", "compliance@local", {"replicas": 99}, "args.replicas"),
    ("deployment.apply", "ghost@local", {}, "no component"),
])
def test_refusals_are_acknowledged_with_the_reason(h, action, target, args, why):
    cmd = command(action, target, **args)
    out = h.handle(cmd)
    assert types(out) == ["command.issued", "command.acknowledged"]
    ack = out[1]["data"]
    assert ack["accepted"] is False and why in ack["refusal"]
    assert project_all(out).get("commands", cmd["data"]["command_id"])["status"] == "refused"


def test_apply_without_any_way_to_get_an_image_is_refused(h):
    h.builder = None
    out = h.handle(command("deployment.apply"))
    assert "no image digest" in out[1]["data"]["refusal"]


def test_lifecycle_and_scale(h):
    h.handle(command("deployment.apply"))
    for action, state in [("deployment.stop", "exited"), ("deployment.start", "running"),
                          ("deployment.restart", "running")]:
        out = h.handle(command(action))
        assert out[-1]["data"]["ok"] and {i["state"] for i in out[-1]["data"]["instances"]} == {state}
    out = h.handle(command("deployment.scale", replicas=2))
    assert len(out[-1]["data"]["instances"]) == 2
    out = h.handle(command("deployment.remove"))
    assert out[-1]["data"]["ok"] and out[-1]["data"]["instances"] == []
    assert project_all(out).get("deployments", "compliance@local")["state"] == "removed"


def test_runtime_error_becomes_a_failed_step_not_a_crash(h):
    out = h.handle(command("deployment.stop"))            # nothing deployed yet: KeyError
    last_step = [e for e in out if e["type"].endswith("deployment.step")][-1]["data"]
    assert last_step["step"] == "error" and not last_step["ok"]
    assert out[-1]["data"]["ok"] is False and "KeyError" in out[-1]["data"]["error"]
    assert project_all(out).get("commands", out[0]["data"]["command_id"])["status"] == "failed"


def test_build_failure_deploys_nothing(h):
    h.builder = FakeBuilder(fail=True)
    out = h.handle(command("deployment.apply"))
    assert out[-1]["data"]["ok"] is False and "build" in out[-1]["data"]["error"]
    assert h.runtime.status() == []


def test_redelivered_and_invalid_commands_are_skipped(h):
    cmd = command("deployment.apply")
    assert h.handle(cmd)
    assert h.handle(cmd) == []
    bad = command("deployment.restart")
    bad["data"]["reason"] = ""
    assert h.handle(bad) == []


def test_serve_pulls_from_the_commands_subscription():
    bus = registry.get("bus", "memory")()
    bus.create_subscription("handler", "factory-commands")
    bus.create_subscription("writer", "factory-events")
    h = CommandHandler(runtime=registry.get("runtime", "fake")(), bus=bus, spec_loader=spec,
                       vars={"PROJECT": "p"}, builder=FakeBuilder())
    issue(bus, "factory-commands", "deployment.apply", "compliance@local", who="t", reason="serve test")
    calls = iter([False, True])
    h.serve("handler", timeout_s=0.1, stop=lambda: next(calls), beat_s=0)
    got = [d.event["type"] for d in bus.pull("writer", max_messages=50, timeout_s=0.1)]
    got = [t for t in got if t != "dev.factory.host.heartbeat"]   # hosts report from their own thread
    assert got[0].endswith("command.issued") and got[-1].endswith("deployment.changed")
