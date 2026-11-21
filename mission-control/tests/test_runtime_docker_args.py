"""docker-local builds valid ids and secret-free run arguments (no Docker needed)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from mission_control.adapters.runtime.docker_local import DockerLocal, agent_id
from mission_control.ports import Deployment

SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "envelope.v1.json"


def _engine_pattern() -> str:
    return json.loads(SCHEMA.read_text())["properties"]["factoryengine"]["pattern"]


def test_agent_id_is_a_valid_factoryengine():
    pat = re.compile(_engine_pattern())
    for comp, host in [("compliance", "local"), ("Compliance_X", "vm-agents-1"), ("a.b", "h@1")]:
        d = Deployment(component=comp, host=host, image="img")
        assert pat.match(agent_id(d, 1)), agent_id(d, 1)
    assert agent_id(Deployment("compliance", "local", "img"), 1) == "compliance-1-local"


def test_run_args_carry_no_credential_and_are_hardened(tmp_path):
    rt = DockerLocal(env_dir=str(tmp_path))
    d = Deployment("compliance", "local", "img:1", llm="project", run_as="10001", tmpfs=("/tmp", "/home/agent"))
    args = rt._run_args(d, 1, "img:1", "spec", tmp_path / "compliance.env")
    joined = " ".join(args)
    assert "--read-only" in args and "ALL" in args and "no-new-privileges" in args
    assert "-p" not in args and "--publish" not in args
    assert "FACTORY_ENV_FILE=/run/factory/llm.env" in joined
    assert "AGENT_ID=compliance-1-local" in joined
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in joined and "ANTHROPIC_API_KEY" not in joined
