"""Compliance agent: empty rules cost nothing; reports carry the rules version; no repeats.

    PYTHONPATH=mission-control:. uv run --project mission-control --extra test pytest agents -q
"""
from __future__ import annotations

import pytest

from mission_control.adapters.bus.memory import MemoryBus
from mission_control.adapters.prompts.files import FilePrompts
from mission_control.adapters.store.memory import MemoryStore
from mission_control.events import validate
from mission_control.ports import Change
from mission_control.products import Compliance, Product
from mission_control.projections import project

from agents.compliance.agent import ComplianceAgent, Identity
from agents.compliance.reviewer import FakeReviewer, parse_findings
from agents.compliance.rules import parse

REPO = "shdmitry2000/file-to-markdown-convertor"
RULE = "# Compliance rules\n\n## C-001: No secrets in code\nseverity: high\nNo tokens in added lines.\n"


class FakeCodeHost:
    def __init__(self, heads: dict[int, str]):
        self.heads, self.diffs = heads, 0

    def changes(self, *, state="open"):
        return [Change(id=f"codehost:github/{REPO}#{n}", title=f"PR {n}", state="open", head=h, base="main")
                for n, h in self.heads.items()]

    def get_change(self, change_id):
        return None

    def protected_paths(self):
        return []

    def diff(self, change_id, *, max_bytes=200_000):
        self.diffs += 1
        return "+token = 'abc'\n", False


@pytest.fixture
def world(tmp_path):
    prompts = FilePrompts(str(tmp_path))
    empty = prompts.put("compliance/rules", "# Compliance rules\n<!-- ## C-999: ignored\n -->\n", author="t")
    prompts.set_label("compliance/rules", "production", empty.version)
    bus, store = MemoryBus(), MemoryStore()
    clock = iter(f"2026-11-17T12:{m:02d}:00Z" for m in range(60))
    reviewer = FakeReviewer(findings=[{"rule": "C-001", "severity": "high", "title": "token added", "file": "a.py", "line": 1}])
    host = FakeCodeHost({3: "a" * 40, 4: "b" * 40})
    agent = ComplianceAgent(
        me=Identity(agent_id="compliance-test", location="host:test"),
        product=Product(name="file-to-markdown-convertor", repo=REPO, base_branch="main",
                        compliance=Compliance(mode="advisory", daily_cap_usd=0.015)),
        codehost=host, prompts=prompts, reviewer=reviewer, bus=bus, store=store, clock=lambda: next(clock))
    return agent, prompts, reviewer, host, store


def apply(store, events):
    for e in events:
        assert validate(e) == [], validate(e)
        project(e, store)


def test_rules_parser():
    assert parse("# x\n<!--\n## C-001: hidden\n-->\n") == []
    (r,) = parse(RULE)
    assert (r.id, r.severity, r.body) == ("C-001", "high", "No tokens in added lines.")
    with pytest.raises(ValueError):
        parse("## C-1: a\n## C-1: b\n")


def test_empty_rules_make_no_model_call(world):
    agent, _, reviewer, host, store = world
    events = agent.tick()
    apply(store, events)
    assert [e["data"]["status"] for e in events] == ["no_rules", "no_rules"]
    assert reviewer.calls == [] and host.diffs == 0
    assert all(e["data"]["rules"]["count"] == 0 and e["data"]["cost_usd"] == 0 for e in events)
    assert agent.tick() == []                          # same heads, same rules: nothing new


def test_new_rules_version_reviews_again_and_records_it(world):
    agent, prompts, reviewer, host, store = world
    apply(store, agent.tick())
    v2 = prompts.put("compliance/rules", RULE, author="owner")
    prompts.set_label("compliance/rules", "production", v2.version)
    events = agent.tick()
    apply(store, events)
    assert len(events) == 2 and len(reviewer.calls) == 2 and reviewer.calls[0]["rules"] == ["C-001"]
    d = events[0]["data"]
    assert d["status"] == "findings" and d["rules"]["version"] == v2.version and d["findings"][0]["rule"] == "C-001"
    assert agent.tick() == []


def test_new_head_is_reviewed(world):
    agent, _, _, host, store = world
    apply(store, agent.tick())
    host.heads[3] = "c" * 40
    events = agent.tick()
    assert [(e["data"]["pr"], e["data"]["head"]) for e in events] == [(3, "c" * 40)]


def test_daily_cap_stops_reviews(world):
    agent, prompts, reviewer, host, store = world
    v2 = prompts.put("compliance/rules", RULE, author="owner")
    prompts.set_label("compliance/rules", "production", v2.version)
    events = agent.tick()                               # first review costs 0.01, cap is 0.015
    assert [e["data"]["status"] for e in events] == ["findings", "findings"]
    host.heads[3] = "d" * 40
    host.heads[4] = "e" * 40
    events = agent.tick()                               # 0.02 spent ≥ cap → capped, no call
    assert [e["data"]["status"] for e in events] == ["capped", "capped"] and len(reviewer.calls) == 2
    apply(store, events)


def test_heartbeat_is_valid(world):
    agent = world[0]
    assert validate(agent.heartbeat("idle")) == []


def test_findings_must_name_a_real_rule():
    rules = parse(RULE)
    got = parse_findings('```json\n{"findings":[{"rule":"C-001","severity":"weird","title":"t"},'
                         '{"rule":"X-9","severity":"high","title":"made up"}]}\n```', rules)
    assert got == [{"rule": "C-001", "severity": "high", "title": "t"}]
