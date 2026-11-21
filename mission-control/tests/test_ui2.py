"""UI-2: product settings, halt/unhalt, problem acks (handler + projections + agent + bridge)."""
from __future__ import annotations

from pathlib import Path

import pytest

from mission_control import products, projections, registry
from mission_control.adapters.engine.archon import ArchonEngine
from mission_control.handler import CommandHandler, issue

REPO = Path(__file__).resolve().parents[2]
P = "file-to-markdown-convertor"


class Recorder:
    def __init__(self):
        self.sent = []

    def publish(self, topic, ev, *, ordering_key=""):
        self.sent.append(ev)
        return ev["id"]


@pytest.fixture
def store():
    return registry.get("store", "memory")()


@pytest.fixture
def prompts(tmp_path):
    reg = registry.get("prompts", "files")(root=str(tmp_path / "prompts"))
    v = reg.put("compliance/rules", "", author="t")
    reg.set_label("compliance/rules", "production", v.version)
    reg.set_label("compliance/rules", "candidate", v.version)
    return reg


@pytest.fixture
def h(store, prompts, tmp_path):
    app = tmp_path / "app"
    (app / "factory").mkdir(parents=True)
    (app / "factory" / "consumer.py").write_text("# stand-in")
    calls = []

    def consumer(app_dir, verb):
        calls.append(verb)
        return True, f"Local launch brake {'set' if verb == 'halt' else 'cleared'}."
    hd = CommandHandler(runtime=registry.get("runtime", "fake")(), bus=Recorder(), engine="factory-1",
                        store=store, products_dir=REPO / "products", prompts=prompts, app_dir=app, consumer=consumer)
    hd.calls = calls
    return hd


def cmd(action, target, **args):
    return issue(Recorder(), "factory-commands", action, target, who="alice", reason="ui-2 test", args=args)


def run(h, store, ev):
    out = h.handle(ev)
    for e in out:
        projections.project(e, store)
    return out


def types(out):
    return [e["type"].removeprefix("dev.factory.") for e in out]


def test_sync_registers_products_once(h, store):
    first = h.sync_products()
    assert types(first) == ["product.registered"]
    for e in first:
        projections.project(e, store)
    assert h.sync_products() == []                       # same file: nothing new
    doc = store.get("products", P)
    assert doc["effective"]["base_branch"] == "main"


def test_product_update_with_the_value_seen(h, store):
    for e in h.sync_products():
        projections.project(e, store)
    out = run(h, store, cmd("product.update", P, field="compliance.daily_cap_usd", old="1.0", new="2.5"))
    assert types(out) == ["command.issued", "command.acknowledged", "product.updated", "command.completed"]
    upd = out[2]["data"]
    assert (upd["old"], upd["new"], upd["who"], upd["reason"]) == (1.0, 2.5, "alice", "ui-2 test")
    doc = store.get("products", P)
    assert doc["effective"]["compliance.daily_cap_usd"] == 2.5 and doc["history"][-1]["who"] == "alice"
    c = store.get("commands", out[0]["data"]["command_id"])
    assert c["status"] == "succeeded" and "1.0 → 2.5" in c["detail"]


@pytest.mark.parametrize("args,why", [
    ({"field": "compliance.daily_cap_usd", "old": "3.0", "new": "2.0"}, "stale"),
    ({"field": "compliance.daily_cap_usd", "old": "1.0", "new": "1.0"}, "already"),
    ({"field": "compliance.daily_cap_usd", "old": "1.0", "new": "500"}, "between 0 and 100"),
    ({"field": "base_branch", "old": "main", "new": "bad..name"}, "not a valid branch"),
    ({"field": "compliance.mode", "old": "advisory", "new": "enforcing"}, "mode must be"),
    ({"field": "compliance.label", "old": "production", "new": "nope"}, "no rules version has the label"),
    ({"field": "repo", "old": "a", "new": "b"}, "field must be"),
    ({"field": "base_branch", "new": "develop"}, "args.old"),
])
def test_product_update_refusals(h, store, args, why):
    out = run(h, store, cmd("product.update", P, **args))
    assert types(out) == ["command.issued", "command.acknowledged"]
    assert why in out[1]["data"]["refusal"]


def test_label_change_to_an_existing_label(h, store):
    out = run(h, store, cmd("product.update", P, field="compliance.label", old="production", new="candidate"))
    assert out[-1]["data"]["ok"] is True


def test_unknown_product_refused(h, store):
    assert "no product" in run(h, store, cmd("product.update", "ghost", field="base_branch", old="a", new="b"))[1]["data"]["refusal"]


def test_newer_file_seed_beats_older_console_override(store):
    def ev(kind, eid, t, data):
        return {"type": f"dev.factory.{kind}", "id": eid, "time": t, "data": data}
    def seed(h, base):
        return {"product": P, "seed": {"base_branch": base}, "seed_hash": "sha256:" + h * 64}
    projections.project(ev("product.updated", "u1", "2026-11-21T10:00:00Z",
                           {"product": P, "field": "base_branch", "old": "main", "new": "dev"}), store)
    projections.project(ev("product.registered", "r1", "2026-11-21T09:00:00Z", seed("a", "main")), store)
    assert store.get("products", P)["effective"]["base_branch"] == "dev"          # override is newer
    projections.project(ev("product.registered", "r2", "2026-11-21T11:00:00Z", seed("b", "release")), store)
    assert store.get("products", P)["effective"]["base_branch"] == "release"      # file changed later


def test_halt_and_unhalt(h, store):
    out = run(h, store, cmd("engine.halt", "factory-1"))
    assert types(out)[2:] == ["engine.halted", "command.completed"] and h.calls == ["halt"]
    e = store.get("engines", "factory-1")
    assert e["halted"] is True and e["halt_by"] == "alice" and e["halt_reason"] == "ui-2 test"
    out = run(h, store, cmd("engine.unhalt", "factory-1"))
    assert types(out)[2:] == ["engine.unhalted", "command.completed"]
    assert store.get("engines", "factory-1")["halted"] is False


def test_halt_refusals_and_failure(h, store):
    assert "controls engine" in run(h, store, cmd("engine.halt", "other-1"))[1]["data"]["refusal"]
    h.consumer = lambda app_dir, verb: (False, "git error")
    out = run(h, store, cmd("engine.halt", "factory-1"))
    assert types(out)[2:] == ["command.completed"] and out[-1]["data"]["error"] == "git error"
    h.app_dir = None
    assert "no product checkout" in run(h, store, cmd("engine.halt", "factory-1"))[1]["data"]["refusal"]


def test_problem_ack_and_resolve(h, store):
    pid = "deployment.down:compliance@local"
    out = run(h, store, cmd("problem.ack", pid, since="2026-11-21T10:00:00Z"))
    assert types(out)[2:] == ["problem.acknowledged", "command.completed"]
    st = store.get("problem_states", pid)
    assert st["ack"]["who"] == "alice" and st["ack"]["since"] == "2026-11-21T10:00:00Z"
    run(h, store, cmd("problem.resolve", pid, since="2026-11-21T10:00:00Z"))
    assert store.get("problem_states", pid)["resolved"]["since"] == "2026-11-21T10:00:00Z"
    assert "not a problem id" in run(h, store, cmd("problem.ack", "bad/id"))[1]["data"]["refusal"]


def test_agent_takes_settings_from_the_store(store):
    from agents.compliance.agent import ComplianceAgent, Identity
    from agents.compliance.reviewer import FakeReviewer

    class NoPRs:
        def changes(self, state="open"):
            raise AssertionError("mode off must not look at PRs")
    p = products.load(REPO / "products" / f"{P}.yaml")
    store.upsert("products", P, {"product": P, "effective": {"compliance.mode": "off"}})
    agent = ComplianceAgent(me=Identity(agent_id="t"), product=p, codehost=NoPRs(),
                            prompts=registry.get("prompts", "files")(root=str(REPO / "prompts")),
                            reviewer=FakeReviewer(), bus=Recorder(), store=store, topic="t")
    assert agent.tick() == [] and agent.product.compliance.mode == "off"


def test_bridge_reports_halted_and_paused_from_the_app(tmp_path):
    app = tmp_path / "app"
    eng = ArchonEngine(db=str(tmp_path / "none.db"), engine="factory-1", app_dir=str(app))
    assert eng.heartbeat()["data"]["halted"] is False
    (app / ".factory").mkdir(parents=True)
    (app / ".factory" / "STOP").write_text("x")
    (app / ".factory" / "PAUSE").write_text("x")
    d = eng.heartbeat()["data"]
    assert d["halted"] is True and d["paused"] is True
