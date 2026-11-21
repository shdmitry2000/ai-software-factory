"""AgentRuntime for the whole fleet: the main node (`local`) plus every VM host.

Routes each call by host: a deployment id is `component@host`. VM hosts are known from
the hosts collection (created from the console: `desired.kind == vm`, not removed) and
from what this process created itself; their adapters (docker over SSH) are made on
first use. VMs are created and deleted through `Gce`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator

from mission_control.ports import AgentRuntime, Deployment, Host, Instance, StateStore, Step
from mission_control.registry import register


class Fleet:
    def __init__(self, local: AgentRuntime, *, gce=None, store: StateStore | None = None,
                 remote: Callable[[str, str], AgentRuntime] | None = None, extra_vms: dict[str, str] | None = None):
        self.local, self.gce, self.store = local, gce, store
        self._remote_factory = remote or self._ssh_adapter
        self._remotes: dict[tuple[str, str], AgentRuntime] = {}
        self._known: dict[str, str] = dict(extra_vms or {})      # host id → internal address

    # ------------------------------------------------------------------ routing
    def _ssh_adapter(self, host_id: str, address: str) -> AgentRuntime:
        from .docker_ssh import DockerSSH
        if self.gce is None:
            raise KeyError(f"no VM support configured for host {host_id!r}")
        user, key = self.gce.ssh_identity()
        return DockerSSH(host_id, address, user=user, key=key, known_hosts=self.gce.known_hosts)

    def vms(self) -> dict[str, str]:
        out = dict(self._known)
        if self.store is not None:
            try:
                for h in self.store.query("hosts"):
                    hid = h.get("host", "")
                    if hid and hid != "local" and (h.get("desired") or {}).get("kind") == "vm" and h.get("state") != "removed":
                        out.setdefault(hid, h.get("address", ""))
            except Exception:
                pass
        return out

    def _address(self, host_id: str) -> str:
        addr = self.vms().get(host_id, "")
        if not addr and self.gce is not None:
            cur = self.gce.describe(host_id)
            addr = cur["address"] if cur else ""
        if not addr:
            raise KeyError(f"no host {host_id!r}")
        self._known[host_id] = addr
        return addr

    def _for(self, host_id: str) -> AgentRuntime:
        if host_id == "local":
            return self.local
        addr = self._address(host_id)
        key = (host_id, addr)
        if key not in self._remotes:
            self._remotes[key] = self._remote_factory(host_id, addr)
        return self._remotes[key]

    @staticmethod
    def _host_of(deployment_id: str) -> str:
        return deployment_id.split("@", 1)[1] if "@" in deployment_id else ""

    # ------------------------------------------------------------------ hosts
    def hosts(self) -> list[Host]:
        return [*self.local.hosts(), *(Host(h, "vm", a) for h, a in sorted(self.vms().items()))]

    def create_host(self, host: Host) -> Iterator[Step]:
        if host.kind != "vm" or self.gce is None:
            raise ValueError("only vm hosts can be created, and only where VM support is configured")
        yield from self.gce.create(host.id, host.labels.get("machine_type", "e2-small"))
        cur = self.gce.describe(host.id)
        if cur and cur.get("address"):
            self._known[host.id] = cur["address"]

    def install(self, host_id: str) -> Iterator[Step]:
        yield from self._for(host_id).install(host_id)

    def remove_host(self, host_id: str) -> Iterator[Step]:
        if host_id == "local":
            raise ValueError("the main node cannot be removed")
        try:
            rt = self._for(host_id)
            for dep in sorted({i.deployment for i in rt.status()}):
                yield from rt.remove(dep)
        except Exception as e:                          # an unreachable VM is deleted anyway
            yield Step("remove", True, f"containers not removed ({type(e).__name__}); the VM goes with them")
        if self.gce is not None:
            yield from self.gce.delete(host_id)
        self._known.pop(host_id, None)
        for key in [k for k in self._remotes if k[0] == host_id]:
            del self._remotes[key]

    def health(self, host_id: str) -> dict:
        try:
            return self._for(host_id).health(host_id)
        except KeyError:
            raise
        except Exception as e:
            return {"state": "unreachable", "error": f"{type(e).__name__}: {e}"[:300]}

    # ------------------------------------------------------------------ deployments
    def apply(self, d: Deployment) -> Iterator[Step]:
        yield from self._for(d.host).apply(d)

    def remove(self, deployment_id: str) -> Iterator[Step]:
        yield from self._for(self._host_of(deployment_id)).remove(deployment_id)

    def start(self, deployment_id: str) -> Iterator[Step]:
        yield from self._for(self._host_of(deployment_id)).start(deployment_id)

    def stop(self, deployment_id: str) -> Iterator[Step]:
        yield from self._for(self._host_of(deployment_id)).stop(deployment_id)

    def restart(self, deployment_id: str) -> Iterator[Step]:
        yield from self._for(self._host_of(deployment_id)).restart(deployment_id)

    def scale(self, deployment_id: str, replicas: int) -> Iterator[Step]:
        yield from self._for(self._host_of(deployment_id)).scale(deployment_id, replicas)

    def status(self) -> list[Instance]:
        out = list(self.local.status())
        for h in self.vms():
            try:
                out += self._for(h).status()
            except Exception:
                pass
        return out

    def logs(self, deployment_id: str, *, tail: int = 200) -> list[str]:
        return self._for(self._host_of(deployment_id)).logs(deployment_id, tail=tail)

    def follow_logs(self, deployment_id: str, *, tail: int = 200) -> Iterator[str]:
        yield from self._for(self._host_of(deployment_id)).follow_logs(deployment_id, tail=tail)


@register("runtime", "fleet")
def make(**kw) -> Fleet:
    return Fleet(**kw)
