"""AgentRuntime adapters: where deployments run (docs/deploy-and-hosts-plan.md)."""
from __future__ import annotations

import hashlib
import os
import json
from dataclasses import asdict, replace
from pathlib import Path

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


def mask(line: str, source: dict[str, str]) -> str:
    """Replace any credential value of the login file that appears in a log line."""
    for k in {k for keys in CREDENTIAL_KEYS.values() for k in keys}:
        v = source.get(k, "")
        if len(v) >= 8 and v in line:
            line = line.replace(v, f"[{k} masked]")
    return line


def host_facts(proc: str = "/proc", root: str = "/") -> dict:
    """Health numbers of the machine this runs on (Linux /proc); partial on other systems."""
    import shutil
    out: dict = {}
    try:
        out["cpus"] = os.cpu_count() or 0
        out["load1"] = round(float(Path(proc, "loadavg").read_text().split()[0]), 2)
    except (OSError, ValueError, IndexError):
        pass
    try:
        mem = {}
        for line in Path(proc, "meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            mem[k] = int(v.split()[0])
        total, avail = mem["MemTotal"], mem.get("MemAvailable", mem.get("MemFree", 0))
        out["mem_total_mb"] = total // 1024
        out["mem_used_pct"] = round(100 * (total - avail) / total, 1) if total else 0.0
    except (OSError, KeyError, ValueError, IndexError):
        pass
    try:
        du = shutil.disk_usage(root)
        out["disk_total_gb"] = round(du.total / 2**30, 1)
        out["disk_used_pct"] = round(100 * du.used / du.total, 1) if du.total else 0.0
    except OSError:
        pass
    return out
