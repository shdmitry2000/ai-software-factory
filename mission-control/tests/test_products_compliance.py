"""Product registry, compliance/agents projections, and the repo's own seed files."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mission_control import products
from mission_control.adapters.prompts.files import FilePrompts
from mission_control.adapters.store.memory import MemoryStore
from mission_control.events import validate
from mission_control.projections import AGENTS, COMPLIANCE, project

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "mission-control" / "schema" / "examples"


def test_converter_product_seed():
    p = products.load(REPO / "products" / "file-to-markdown-convertor.yaml")
    assert p.repo == "shdmitry2000/file-to-markdown-convertor" and p.base_branch == "main"
    assert p.compliance.mode == "advisory" and p.compliance.rules == "compliance/rules"


@pytest.mark.parametrize("raw,msg", [
    ({"repo": "a/b"}, "base_branch"),
    ({"base_branch": "main"}, "repo"),
    ({"repo": "a/b", "base_branch": "main", "compliance": {"mode": "enforcing"}}, "mode"),
])
def test_product_errors(raw, msg):
    with pytest.raises(ValueError, match=msg):
        products.parse("x", raw)


def test_seed_rules_are_empty_and_labelled():
    p = FilePrompts(str(REPO / "prompts")).get("compliance/rules", label="production")
    assert p is not None and p.version == "1"
    import sys
    sys.path.insert(0, str(REPO))
    from agents.compliance.rules import parse
    assert parse(p.text) == []


def _report(time: str, head: str, eid: str) -> dict:
    ev = json.loads((EXAMPLES / "compliance.reported.json").read_text())
    ev = copy.deepcopy(ev)
    ev["time"], ev["id"], ev["data"]["head"] = time, eid, head
    return ev


def test_compliance_projection_latest_wins_any_order():
    old = _report("2026-11-17T12:00:00Z", "a" * 40, "compliance:x:3:old")
    new = _report("2026-11-17T13:00:00Z", "b" * 40, "compliance:x:3:new")
    for order in ([old, new], [new, old], [new, old, new, old]):
        s = MemoryStore()
        for e in order:
            assert not validate(e)
            project(e, s)
        doc = s.get(COMPLIANCE, "file-to-markdown-convertor__3")
        assert doc["head"] == "b" * 40 and doc["report_id"] == "compliance:x:3:new"
        assert doc["status"] == "no_rules" and doc["rules"]["count"] == 0


def test_agent_projection():
    hb = json.loads((EXAMPLES / "agent.heartbeat.json").read_text())
    s = MemoryStore()
    project(hb, s)
    doc = s.get(AGENTS, "compliance-7f9c")
    assert doc["location"].startswith("gke:") and doc["auth_method"] == "vertex"
    assert doc["last_heartbeat"] == hb["time"]
