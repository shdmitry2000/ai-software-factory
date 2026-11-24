"""R6: chain triggers. `consumes` → a filtered subscription per deployment, made at deploy."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mission_control import deployspec, events, projections, registry, triggers
from mission_control.adapters.bus.memory import MemoryBus
from mission_control.builder import FakeBuilder
from mission_control.console import views
from mission_control.handler import CommandHandler, issue

REPO = Path(__file__).resolve().parents[2]
D1 = "sha256:" + "1" * 64
PO, PU = "dev.factory.pullrequest.opened", "dev.factory.pullrequest.updated"


def ev(etype, product="file-to-markdown-convertor", n=1):
    e = events.make(etype, f"test:{etype}:{product}:{n}", "change/x", {}, source="factory://t/test", engine="t")
    if product:
        e["factoryproduct"] = product
    return e


# ---------------------------------------------------------------------- filters
def test_filter_matches_pubsub_syntax_and_the_memory_rule():
    t = triggers.trigger_for("compliance@local", (PO, PU), "file-to-markdown-convertor")
    assert t.subscription == "factory-trig-compliance--local"
    assert t.pubsub_filter() == ('(attributes.type = "dev.factory.pullrequest.opened" OR attributes.type = '
                                 '"dev.factory.pullrequest.updated") AND attributes.product = "file-to-markdown-convertor"')
    assert t.accepts(ev(PO)) and t.accepts(ev(PU))
    assert not t.accepts(ev(PO, product="other")) and not t.accepts(ev("dev.factory.check.completed"))
    w = triggers.trigger_for("x@local", ("dev.factory.pullrequest.*",))
    assert w.pubsub_filter() == 'hasPrefix(attributes.type, "dev.factory.pullrequest.")'
    assert w.accepts(ev(PO, product="")) and not w.accepts(ev("dev.factory.check.completed"))


def test_filters_are_checked():
    with pytest.raises(ValueError, match="256"):
        triggers.trigger_for("x@local", tuple(f"dev.factory.thing.verb_{c}" for c in "abcdefghi"))
    with pytest.raises(ValueError):
        triggers.trigger_for("x@local", ('dev.factory.a.b" OR "1',))
    with pytest.raises(ValueError):
        list(triggers.MemoryTriggers(MemoryBus()).remove("factory-commands-handler"))


def test_memory_bus_filters_and_deletes():
    bus = MemoryBus()
    t = triggers.trigger_for("compliance@local", (PO,), "file-to-markdown-convertor")
    mt = triggers.MemoryTriggers(bus)
    list(mt.ensure(t, "local"))
    bus.publish("factory-events", ev("dev.factory.check.completed"))
    bus.publish("factory-events", ev(PO))
    got = bus.pull(t.subscription, timeout_s=0.1)
    assert [d.event["type"] for d in got] == [PO]
    assert [s.detail for s in mt.remove(t.subscription)] and t.subscription not in mt.active
    assert list(mt.remove(t.subscription)) == []          # idempotent


# ---------------------------------------------------------------------- handler
class Bus:
    def __init__(self):
        self.sent = []

    def publish(self, topic, e, *, ordering_key=""):
        self.sent.append(e)
        return e["id"]


@pytest.fixture
def env():
    store, rt, mem = registry.get("store", "memory")(), registry.get("runtime", "fake")(), MemoryBus()
    tr = triggers.MemoryTriggers(mem)
    h = CommandHandler(runtime=rt, bus=Bus(), engine="factory-1", vars={"PROJECT": "p"}, builder=FakeBuilder(digest=D1),
                       store=store, products_dir=REPO / "products", triggers=tr,
                       spec_loader=lambda: deployspec.load(REPO / "deploy", products_dir=REPO / "products"))
    return h, store, rt, tr, mem


def run(h, store, action, target="compliance@local", **args):
    cmd = issue(Bus(), "factory-commands", action, target, who="t", reason="r6 test", args=args)
    out = h.handle(cmd)
    for e in out:
        assert events.validate(e) == [], (e["type"], events.validate(e))
        projections.project(e, store)
    return out, store.get("commands", cmd["data"]["command_id"])


def test_apply_wires_the_trigger_before_the_containers_start(env):
    h, store, rt, tr, mem = env
    out, c = run(h, store, "deployment.apply")
    assert c["status"] == "succeeded"
    steps = [e["data"]["step"] for e in out if e["type"].endswith("deployment.step")]
    assert steps.index("trigger") < steps.index("run")
    sub = "factory-trig-compliance--local"
    assert rt._deps["compliance@local"].env["MC_TRIGGER_SUBSCRIPTION"] == sub
    assert tr.active[sub].product == "file-to-markdown-convertor" and tr.members[sub] == "local"
    d = store.get("deployments", "compliance@local")
    assert d["trigger"] == {"subscription": sub, "types": [PO, PU], "product": "file-to-markdown-convertor"}
    mem.publish("factory-events", ev(PO))
    assert len(mem.pull(sub, timeout_s=0.1)) == 1
    out, c = run(h, store, "deployment.remove")
    assert c["status"] == "succeeded" and sub not in tr.active and "trigger" not in store.get("deployments", "compliance@local")


def test_a_failed_trigger_stops_the_apply(env):
    h, store, rt, tr, mem = env

    def broken(t, host):
        yield triggers.Step("trigger", False, "permission denied")
    tr.ensure = broken
    _, c = run(h, store, "deployment.apply")
    assert c["status"] == "failed" and "compliance@local" not in rt._deps


def test_host_remove_deletes_its_triggers(env):
    h, store, rt, tr, mem = env
    list(rt.create_host(registry.get("runtime", "fake")().hosts()[0].__class__("agent-1", "vm")))
    list(rt.install("agent-1"))
    store.upsert("hosts", "agent-1", {"host": "agent-1", "kind": "vm", "desired": {"kind": "vm"}})
    _, c = run(h, store, "deployment.apply", target="compliance@agent-1")
    assert c["status"] == "succeeded", c
    assert "factory-trig-compliance--agent-1" in tr.active and tr.members["factory-trig-compliance--agent-1"] == "agent-1"
    _, c = run(h, store, "host.remove", target="agent-1")
    assert c["status"] == "succeeded" and "factory-trig-compliance--agent-1" not in tr.active


def test_chains_view_marks_wired_edges(env):
    h, store, rt, tr, mem = env
    sp = deployspec.load(REPO / "deploy", products_dir=REPO / "products")
    at = views.now()
    assert not any(e["wired"] for e in views.chains(store, at, sp, count=lambda w: 0)["edges"])
    run(h, store, "deployment.apply")
    store.upsert("agents", "compliance-1-local", {"agent_id": "compliance-1-local", "last_trigger_at": "2026-11-21T10:00:00Z"})
    ch = views.chains(store, at, sp, count=lambda w: 0)
    assert {e["from"] for e in ch["edges"] if e["wired"]} == {PO, PU}
    assert ch["wired"]["compliance"][0]["subscription"] == "factory-trig-compliance--local"
    assert ch["last_trigger"]["compliance"] != "not yet"


# ---------------------------------------------------------------------- Pub/Sub admin (fake client)
class FakeSubscriber:
    def __init__(self):
        self.subs, self.policies, self.calls = {}, {}, []

    def subscription_path(self, project, name):
        return f"projects/{project}/subscriptions/{name}"

    def get_subscription(self, request):
        from google.api_core import exceptions as gexc
        s = self.subs.get(request["subscription"])
        if s is None:
            raise gexc.NotFound("no")
        return SimpleNamespace(filter=s["filter"])

    def create_subscription(self, request):
        self.calls.append("create")
        self.subs[request["name"]] = request

    def delete_subscription(self, request):
        from google.api_core import exceptions as gexc
        self.calls.append("delete")
        if self.subs.pop(request["subscription"], None) is None:
            raise gexc.NotFound("no")

    def get_iam_policy(self, request):
        from google.iam.v1 import policy_pb2
        return self.policies.get(request["resource"]) or policy_pb2.Policy()

    def set_iam_policy(self, request):
        self.calls.append("iam")
        self.policies[request["resource"]] = request["policy"]


def test_pubsub_triggers_create_recreate_and_grant_once():
    pytest.importorskip("google.cloud.pubsub_v1")
    fs = FakeSubscriber()
    pt = triggers.PubSubTriggers("p", subscriber=fs)
    t = triggers.trigger_for("compliance@agent-1", (PO,), "x")
    list(pt.ensure(t, "agent-1"))
    path = "projects/p/subscriptions/factory-trig-compliance--agent-1"
    assert fs.subs[path]["filter"] == t.pubsub_filter() and fs.subs[path]["topic"] == "projects/p/topics/factory-events"
    members = [m for b in fs.policies[path].bindings for m in b.members]
    assert members == ["serviceAccount:factory-agent@p.iam.gserviceaccount.com"]
    list(pt.ensure(t, "agent-1"))
    assert fs.calls == ["create", "iam"]                   # nothing to do the second time
    t2 = triggers.trigger_for("compliance@agent-1", (PO, PU), "x")
    list(pt.ensure(t2, "agent-1"))
    assert fs.calls[2:4] == ["delete", "create"]          # a filter cannot change in place
    assert [s.step for s in pt.remove(t.subscription)] == ["trigger"] and list(pt.remove(t.subscription)) == []
    with pytest.raises(ValueError):
        list(pt.remove("factory-events-writer"))
