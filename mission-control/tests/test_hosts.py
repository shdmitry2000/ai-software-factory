"""UI-3 part 2 (R5, R8): VM hosts from the console, the fleet router, restore after the sweep."""
from __future__ import annotations

from pathlib import Path

import pytest

from mission_control import deployspec, events, projections, registry
from mission_control.adapters.runtime.docker_ssh import parse_health
from mission_control.adapters.runtime.fleet import Fleet
from mission_control.builder import FakeBuilder
from mission_control.console import views
from mission_control.handler import CommandHandler, issue
from mission_control.ports import Deployment, Host, Step

REPO = Path(__file__).resolve().parents[2]
D1 = "sha256:" + "1" * 64


class Bus:
    def __init__(self):
        self.sent = []

    def publish(self, topic, ev, *, ordering_key=""):
        self.sent.append((topic, ev))
        return ev["id"]


class FakeGce:
    zone, known_hosts = "me-west1-a", "/dev/null"

    def __init__(self):
        self.vms: dict[str, dict] = {}

    def describe(self, host_id):
        return self.vms.get(host_id)

    def create(self, host_id, machine_type="e2-small"):
        if host_id not in self.vms:
            self.vms[host_id] = {"name": host_id, "status": "RUNNING", "address": f"10.9.0.{len(self.vms) + 2}",
                                 "machine_type": machine_type}
        yield Step("create", True, host_id)

    def delete(self, host_id):
        self.vms.pop(host_id, None)
        yield Step("delete-vm", True, host_id)


def spec():
    return deployspec.load(REPO / "deploy", products_dir=REPO / "products")


@pytest.fixture
def env():
    store = registry.get("store", "memory")()
    gce = FakeGce()
    remotes = {}

    def remote(host_id, address):
        rt = registry.get("runtime", "fake")()
        rt._hosts = {host_id: Host(host_id, "vm", address, "installing")}
        remotes[host_id] = rt
        return rt
    fleet = Fleet(registry.get("runtime", "fake")(), gce=gce, store=store, remote=remote)
    builder = FakeBuilder(digest=D1)
    h = CommandHandler(runtime=fleet, bus=Bus(), engine="factory-1", spec_loader=spec, vars={"PROJECT": "p"},
                       builder=builder, store=store, products_dir=REPO / "products", max_vm_hosts=2)
    return h, store, fleet, gce, remotes, builder


def run(h, store, action, target, **args):
    cmd = issue(Bus(), "factory-commands", action, target, who="t", reason="hosts test", args=args)
    out = h.handle(cmd)
    for ev in out:
        assert events.validate(ev) == [], (ev["type"], events.validate(ev))
        projections.project(ev, store)
    return out, store.get("commands", cmd["data"]["command_id"])


def test_create_a_vm_host_deploy_to_it_and_remove_it(env):
    h, store, fleet, gce, remotes, _ = env
    out, c = run(h, store, "host.create", "agent-1", machine_type="e2-small")
    assert c["status"] == "succeeded", c
    assert [s["step"] for s in c["steps"].values()] == ["create", "install"]
    host = store.get("hosts", "agent-1")
    assert host["state"] == "healthy" and host["desired"] == {"kind": "vm", "machine_type": "e2-small", "zone": "me-west1-a"}
    assert host["address"] == "10.9.0.2"
    assert "agent-1" in fleet.vms()

    _, c = run(h, store, "deployment.apply", "compliance@agent-1")
    assert c["status"] == "succeeded", c
    assert [i.deployment for i in remotes["agent-1"].status()] == ["compliance@agent-1"]
    assert remotes["agent-1"].secret_files["compliance@agent-1"].startswith("CLAUDE_CODE_OAUTH_TOKEN=")
    assert fleet.local.status() == []                      # nothing landed on the main node

    for ev in h.beat_hosts():
        projections.project(ev, store)
    assert store.get("hosts", "agent-1")["health"]["containers"] == 1

    _, c = run(h, store, "host.remove", "agent-1")
    assert c["status"] == "succeeded"
    assert store.get("hosts", "agent-1")["state"] == "removed" and "desired" not in store.get("hosts", "agent-1")
    assert store.get("deployments", "compliance@agent-1")["state"] == "removed"
    assert gce.vms == {} and "agent-1" not in fleet.vms()


@pytest.mark.parametrize("target, args, why", [
    ("vm-1", {}, "look like agent-1"),
    ("agent-1", {"machine_type": "n2-highmem-96"}, "machine_type must be one of"),
])
def test_bad_host_create_is_refused(env, target, args, why):
    h, store, *_ = env
    _, c = run(h, store, "host.create", target, **args)
    assert c["status"] == "refused" and why in c["refusal"]


def test_vm_host_cap(env):
    h, store, *_ = env
    run(h, store, "host.create", "agent-1")
    run(h, store, "host.create", "agent-2")
    _, c = run(h, store, "host.create", "agent-3")
    assert c["status"] == "refused" and "at most 2 VM hosts" in c["refusal"]
    _, c = run(h, store, "host.remove", "agent-9")
    assert c["status"] == "refused" and "no host" in c["refusal"]


def test_restore_after_the_sweep_recreates_hosts_and_deployments(env):
    h, store, fleet, gce, remotes, builder = env
    run(h, store, "host.create", "agent-1", machine_type="e2-medium")
    run(h, store, "deployment.apply", "compliance@local")
    run(h, store, "deployment.apply", "compliance@agent-1", replicas=2)

    # the sweep: every VM and container gone, the main node rebuilt (no local images)
    gce.vms.clear()
    remotes.clear()
    fleet._remotes.clear()
    fleet._known.clear()
    fleet.local = registry.get("runtime", "fake")()
    builder.images.clear()
    builder.built.clear()

    out, c = run(h, store, "fleet.restore", "factory-1")
    assert c["status"] == "succeeded", c.get("error")
    assert gce.vms["agent-1"]["machine_type"] == "e2-medium"
    assert [i.deployment for i in fleet.local.status()] == ["compliance@local"]
    assert len(remotes["agent-1"].status()) == 2              # desired replicas came back
    assert builder.built == ["compliance"]      # the old image was gone: rebuilt once, then reused
    assert store.get("deployments", "compliance@agent-1")["last_action"] == "fleet.restore"
    assert "hosts up: agent-1, local" in c["detail"]


def test_restore_skips_removed(env):
    h, store, fleet, gce, remotes, _ = env
    run(h, store, "host.create", "agent-1")
    run(h, store, "host.remove", "agent-1")
    run(h, store, "deployment.apply", "compliance@local")
    run(h, store, "deployment.remove", "compliance@local")
    _, c = run(h, store, "fleet.restore", "factory-1")
    assert c["status"] == "succeeded" and gce.vms == {} and fleet.local.status() == []


def test_fleet_routes_by_host(env):
    _, store, fleet, gce, remotes, _ = env
    list(fleet.create_host(Host("agent-1", "vm")))
    d = Deployment("compliance", "agent-1", "img", digest=D1)
    list(fleet.apply(d))
    assert fleet.logs("compliance@agent-1")[0].startswith("factory-compliance-1 started")
    assert {i.host for i in fleet.status()} == {"agent-1"}
    with pytest.raises(KeyError):
        list(fleet.apply(Deployment("compliance", "agent-zz", "img")))
    with pytest.raises(ValueError):
        list(fleet.remove_host("local"))


def test_parse_remote_health():
    out = parse_health("2\n0.15 0.10 0.05 1/100 123\nMemTotal: 2000000 kB\nMemAvailable: 1500000 kB\n"
                       "/dev/root 20000000 5000000 15000000 25% /\n27.5.1\n1\n")
    assert out == {"state": "healthy", "cpus": 2, "load1": 0.15, "mem_total_mb": 1953, "mem_used_pct": 25.0,
                   "disk_total_gb": 19.1, "disk_used_pct": 25.0, "docker": "27.5.1", "containers": 1}
    assert parse_health("garbage")["error"]
    assert parse_health("2\n0.1\nMemTotal: 1 kB\nMemAvailable: 1 kB\n/ 1 1 1 1% /\nnone\n0\n")["state"] == "degraded"


def test_ssh_adapter_quotes_docker_commands():
    from mission_control.adapters.runtime.docker_ssh import DockerSSH
    rt = DockerSSH("agent-1", "10.9.0.2", user="sa_1", key="/k", known_hosts="/kh")
    argv = rt._cmd("run", "--label", 'dev.factory.deployment-json={"a": "b c"}', "img")
    assert argv[:3] == ["ssh", "-i", "/k"] and "sa_1@10.9.0.2" in argv
    assert argv[-1] == "sudo docker run --label 'dev.factory.deployment-json={\"a\": \"b c\"}' img"
    assert "BatchMode=yes" in argv and "UserKnownHostsFile=/kh" in argv


def test_hosts_view_lists_console_vms():
    store = registry.get("store", "memory")()
    store.upsert("hosts", "agent-1", {"host": "agent-1", "kind": "vm", "state": "healthy", "address": "10.9.0.2",
                                      "desired": {"kind": "vm", "machine_type": "e2-small"}})
    from datetime import datetime, timezone
    rows = views.hosts(store, datetime.now(timezone.utc), spec())
    assert [r["host"] for r in rows] == ["local", "agent-1"]
    assert rows[1]["in_git"] is False and rows[1]["desired"]["machine_type"] == "e2-small"
