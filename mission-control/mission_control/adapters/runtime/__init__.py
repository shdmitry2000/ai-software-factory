"""AgentRuntime adapters: where deployments run (docs/deploy-and-hosts-plan.md)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace

from mission_control.ports import LLM_METHODS, Deployment, looks_secret

CREDENTIAL_KEYS = {"project": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
                   "api_key": ("ANTHROPIC_API_KEY",)}
"""Which variables of the main node's login file a deployment may receive."""


def validate(d: Deployment) -> None:
    if not d.component or not d.host or not d.image:
        raise ValueError("deployment needs component, host and image")
    if d.llm not in LLM_METHODS:
        raise ValueError(f"llm must be one of {', '.join(LLM_METHODS)}, not {d.llm!r}")
    if d.replicas < 0:
        raise ValueError("replicas must be >= 0")
    for k in d.env:
        if looks_secret(k):
            raise ValueError(f"env {k!r} looks like a secret; secrets never go in a deployment")


def spec_hash(d: Deployment) -> str:
    """Identity of everything but the replica count: a change means recreate."""
    body = asdict(replace(d, replicas=0))
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def to_json(d: Deployment) -> str:
    return json.dumps(asdict(d), sort_keys=True)


def from_json(text: str) -> Deployment:
    raw = json.loads(text)
    raw["command"] = tuple(raw.get("command", ()))
    raw["tmpfs"] = tuple(raw.get("tmpfs", ()))
    return Deployment(**raw)


def parse_env_file(text: str) -> dict[str, str]:
    """`export K=V` or `K=V` lines; quotes stripped; comments and blanks ignored."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip():
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            out[key.strip()] = value
    return out


def credential_lines(llm: str, source: dict[str, str]) -> list[str]:
    """The `K=V` lines a deployment gets, or [] when it needs none. Raises when needed
    and missing (the message names the variables, never a value)."""
    if llm == "none":
        return []
    keys = CREDENTIAL_KEYS[llm]
    got = [f"{k}={source[k]}" for k in keys if source.get(k)]
    if not got:
        raise LookupError(f"llm={llm} needs one of {', '.join(keys)} in the main node's login file")
    return got[:1]
