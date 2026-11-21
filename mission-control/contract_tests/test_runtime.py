"""AgentRuntime contract: every adapter must pass.

The fake always runs. Real Docker joins with MC_CONTRACT_DOCKER=1 (it starts small
busybox containers named factory-ct-*, removed after each test).
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace

import pytest

from mission_control import registry
from mission_control.ports import Deployment, is_adapter_for

SECRET = "ct-secret-" + uuid.uuid4().hex[:8]
MARK = "hello-from-factory"


@dataclass
class RuntimeHarness:
    rt: object
    image: str
    inspect: Callable[[str], str]           # everything the runtime exposes about a deployment
    secret_seen: Callable[[str], str]       # the credential as the workload sees it
    close: Callable[[], None] = lambda: None


def _fake(tmp_path) -> RuntimeHarness:
    rt = registry.get("runtime", "fake")(credentials={"CLAUDE_CODE_OAUTH_TOKEN": SECRET})
    return RuntimeHarness(rt, "busybox:1.36.1",
                          inspect=lambda dep: repr(rt.status()) + repr(rt._deps.get(dep)),
                          secret_seen=lambda dep: rt.secret_files.get(dep, ""))


def _docker(tmp_path) -> RuntimeHarness:
    login = tmp_path / "factory-env"
    login.write_text(f"export CLAUDE_CODE_OAUTH_TOKEN={SECRET}\nexport OTHER=x\n")
    rt = registry.get("runtime", "docker-local")(env_dir=str(tmp_path / "env"), login_file=str(login),
                                                 stop_timeout=1)

    def names(dep):
        return [i.name for i in rt.status() if i.deployment == dep]

    def inspect(dep):
        ns = names(dep)
        return subprocess.run(["docker", "inspect", *ns], capture_output=True, text=True).stdout if ns else ""

    def secret_seen(dep):
        ns = names(dep)
        if not ns:
            return ""
        p = subprocess.run(["docker", "exec", ns[0], "cat", "/run/factory/llm.env"], capture_output=True, text=True)
        return p.stdout

    def close():
        ids = subprocess.run(["docker", "ps", "-aq", "--filter", "name=factory-ct-"],
                             capture_output=True, text=True).stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)

    return RuntimeHarness(rt, "busybox:1.36.1", inspect, secret_seen, close)


DOCKER_SKIP = "" if os.environ.get("MC_CONTRACT_DOCKER") == "1" else "set MC_CONTRACT_DOCKER=1 to run against Docker"
CANDIDATES = [("fake", _fake, ""), ("docker-local", _docker, DOCKER_SKIP)]


@pytest.fixture(params=[pytest.param(f, id=n, marks=[pytest.mark.skip(reason=s)] if s else [])
                        for n, f, s in CANDIDATES])
def h(request, tmp_path):
    harness = request.param(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def dep(h, **kw) -> Deployment:
    base = Deployment(component=f"ct-{uuid.uuid4().hex[:6]}", host="local", image=h.image,
                      command=("sh", "-c", f"echo {MARK}; sleep 300"), env={"MODE": "a"})
    return replace(base, **kw)


def steps(it):
    out = list(it)
    assert all(s.ok for s in out), out
    return out


def mine(h, d):
    return [i for i in h.rt.status() if i.deployment == d.id]


def test_is_an_agent_runtime(h):
    assert is_adapter_for("runtime", h.rt)
    assert any(x.id == "local" for x in h.rt.hosts())


def test_apply_runs_replicas_and_is_idempotent(h):
    d = dep(h, replicas=2)
    steps(h.rt.apply(d))
    first = mine(h, d)
    assert [i.state for i in first] == ["running", "running"]
    assert {i.name for i in first} == {f"factory-{d.component}-1", f"factory-{d.component}-2"}
    again = steps(h.rt.apply(d))
    assert all("unchanged" in s.detail for s in again if s.step == "run")
    assert [i.started_at for i in mine(h, d)] == [i.started_at for i in first]


def test_changed_spec_recreates(h):
    d = dep(h)
    steps(h.rt.apply(d))
    out = steps(h.rt.apply(replace(d, env={"MODE": "b"})))
    assert any("recreated" in s.detail for s in out)
    assert [i.state for i in mine(h, d)] == ["running"]


def test_stop_start_restart(h):
    d = dep(h)
    steps(h.rt.apply(d))
    steps(h.rt.stop(d.id))
    assert [i.state for i in mine(h, d)] == ["exited"]
    steps(h.rt.start(d.id))
    assert [i.state for i in mine(h, d)] == ["running"]
    steps(h.rt.restart(d.id))
    assert [i.state for i in mine(h, d)] == ["running"]


def test_scale_up_and_down(h):
    d = dep(h)
    steps(h.rt.apply(d))
    steps(h.rt.scale(d.id, 3))
    assert len(mine(h, d)) == 3
    steps(h.rt.scale(d.id, 1))
    assert [i.name for i in mine(h, d)] == [f"factory-{d.component}-1"]


def test_logs_and_remove(h):
    d = dep(h)
    steps(h.rt.apply(d))
    for _ in range(50):                       # the container may need a moment to print
        if any(MARK in line for line in h.rt.logs(d.id)):
            break
    assert any(MARK in line for line in h.rt.logs(d.id))
    steps(h.rt.remove(d.id))
    assert mine(h, d) == []
    for op in (h.rt.stop, h.rt.start, h.rt.restart, h.rt.remove):
        with pytest.raises(KeyError):
            list(op(d.id))
    with pytest.raises(KeyError):
        h.rt.logs(d.id)


def test_llm_credential_reaches_the_workload_but_not_the_runtime(h):
    d = dep(h, llm="project")
    steps(h.rt.apply(d))
    assert SECRET in h.secret_seen(d.id)
    assert "OTHER" not in h.secret_seen(d.id)            # only the allowed variable
    assert SECRET not in h.inspect(d.id)


def test_llm_without_credential_fails_and_runs_nothing(h):
    d = dep(h, llm="api_key")                            # the login holds a subscription token only
    out = list(h.rt.apply(d))
    assert out[-1].step == "credential" and not out[-1].ok
    assert "ANTHROPIC_API_KEY" in out[-1].detail and SECRET not in out[-1].detail
    assert mine(h, d) == []


def test_bad_deployments_are_refused(h):
    with pytest.raises(ValueError):
        list(h.rt.apply(dep(h, env={"GITHUB_TOKEN": "x"})))
    with pytest.raises(ValueError):
        list(h.rt.apply(dep(h, llm="vertex")))
    with pytest.raises(KeyError):
        list(h.rt.apply(dep(h, host="nowhere")))
