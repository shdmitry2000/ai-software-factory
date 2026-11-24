"""In-memory AgentRuntime with the same rules as the real adapters (tests, console demos).

Serves any number of hosts; containers are keyed by (host, name) like real hosts are.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

from mission_control.ports import Deployment, Host, Instance, Step
from mission_control.registry import register

from . import credential_lines, mask, spec_hash, validate


class FakeRuntime:
    def __init__(self, credentials: dict[str, str] | None = None):
        self._hosts: dict[str, Host] = {"local": Host("local", "local", state="healthy", labels={"docker": "fake"})}
        self._deps: dict[str, Deployment] = {}
        self._spec: dict[str, str] = {}
        self._state: dict[tuple[str, str], str] = {}     # (host, container name) → state
        self._logs: dict[tuple[str, str], list[str]] = {}
        self.credentials = credentials if credentials is not None else {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token"}
        self.secret_files: dict[str, str] = {}    # deployment id → credential file text (inspection in tests)
        self.unreachable: set[str] = set()        # tests: hosts whose health check fails

    # hosts
    def hosts(self) -> list[Host]:
        return sorted(self._hosts.values(), key=lambda h: h.id)

    def create_host(self, host: Host) -> Iterator[Step]:
        if host.kind != "vm":
            raise ValueError("only vm hosts can be created")
        if host.id in self._hosts:
            yield Step("create", True, f"{host.id} exists")
            return
        self._hosts[host.id] = Host(host.id, host.kind, host.address or f"10.0.0.{len(self._hosts) + 1}",
                                    "installing", dict(host.labels))
        yield Step("create", True, host.id)

    def install(self, host_id: str) -> Iterator[Step]:
        h = self._host(host_id)
        self._hosts[host_id] = replace(h, state="healthy", labels={**h.labels, "docker": "fake"})
        yield Step("install", True, "docker ready")

    def remove_host(self, host_id: str) -> Iterator[Step]:
        self._host(host_id)
        if host_id == "local":
            raise ValueError("the main node cannot be removed")
        for dep_id in [k for k, d in self._deps.items() if d.host == host_id]:
            yield from self.remove(dep_id)
        del self._hosts[host_id]
        yield Step("remove-host", True, host_id)

    def health(self, host_id: str) -> dict:
        h = self._host(host_id)
        if host_id in self.unreachable:
            return {"state": "unreachable", "error": "fake: unreachable"}
        n = sum(1 for (host, _), s in self._state.items() if host == host_id and s == "running")
        return {"state": "healthy" if h.state != "installing" else "installing", "docker": "fake",
                "cpus": 2, "load1": 0.1, "mem_total_mb": 4096, "mem_used_pct": 20.0,
                "disk_total_gb": 20.0, "disk_used_pct": 30.0, "containers": n}

    # deployments
    def apply(self, d: Deployment) -> Iterator[Step]:
        validate(d)
        self._host(d.host)
        try:
            lines = credential_lines(d.llm, self.credentials)
        except LookupError as e:
            yield Step("credential", False, str(e))
            return
        if lines:
            self.secret_files[d.id] = "\n".join(lines) + "\n"
            yield Step("credential", True, "file written")
        else:
            self.secret_files.pop(d.id, None)
        new_spec = spec_hash(d)
        changed = self._spec.get(d.id) != new_spec
        self._deps[d.id], self._spec[d.id] = d, new_spec
        for i in range(1, d.replicas + 1):
            key = (d.host, _name(d, i))
            if key in self._state and not changed:
                yield Step("run", True, f"{key[1]} unchanged")
                continue
            self._state[key] = "running"
            self._logs[key] = [f"{key[1]} started: {' '.join(d.command)}"]
            yield Step("run", True, f"{key[1]} {'recreated' if changed else 'started'}")
        for key in [k for k in self._keys(d.id) if _index(k[1]) > d.replicas]:
            del self._state[key]
            yield Step("remove", True, key[1])

    def remove(self, deployment_id: str) -> Iterator[Step]:
        self._dep(deployment_id)
        for key in self._keys(deployment_id):
            del self._state[key]
            yield Step("remove", True, key[1])
        del self._deps[deployment_id], self._spec[deployment_id]
        self.secret_files.pop(deployment_id, None)

    def start(self, deployment_id: str) -> Iterator[Step]:
        yield from self._set(deployment_id, "running", "start")

    def stop(self, deployment_id: str) -> Iterator[Step]:
        yield from self._set(deployment_id, "exited", "stop")

    def restart(self, deployment_id: str) -> Iterator[Step]:
        yield from self._set(deployment_id, "running", "restart")

    def scale(self, deployment_id: str, replicas: int) -> Iterator[Step]:
        yield from self.apply(replace(self._dep(deployment_id), replicas=replicas))

    def status(self) -> list[Instance]:
        out = []
        for dep_id, d in sorted(self._deps.items()):
            for key in self._keys(dep_id):
                out.append(Instance(dep_id, key[1], d.host, self._state[key], d.image, d.digest))
        return out

    def logs(self, deployment_id: str, *, tail: int = 200) -> list[str]:
        self._dep(deployment_id)
        lines = [mask(line, self.credentials) for k in self._keys(deployment_id) for line in self._logs.get(k, [])]
        return lines[-tail:] if tail else lines

    def follow_logs(self, deployment_id: str, *, tail: int = 200) -> Iterator[str]:
        yield from self.logs(deployment_id, tail=tail)

    def log(self, deployment_id: str, line: str) -> None:
        """Tests: append a log line to every replica."""
        for k in self._keys(deployment_id):
            self._logs.setdefault(k, []).append(line)

    # helpers
    def _host(self, host_id: str) -> Host:
        try:
            return self._hosts[host_id]
        except KeyError:
            raise KeyError(f"no host {host_id!r}") from None

    def _dep(self, deployment_id: str) -> Deployment:
        try:
            return self._deps[deployment_id]
        except KeyError:
            raise KeyError(f"no deployment {deployment_id!r}") from None

    def _keys(self, deployment_id: str) -> list[tuple[str, str]]:
        d = self._deps.get(deployment_id)
        if d is None:
            return []
        prefix = _name(d, 0)[:-1]
        return sorted((k for k in self._state if k[0] == d.host and k[1].startswith(prefix)),
                      key=lambda k: _index(k[1]))

    def _set(self, deployment_id: str, state: str, step: str) -> Iterator[Step]:
        self._dep(deployment_id)
        for key in self._keys(deployment_id):
            self._state[key] = state
            yield Step(step, True, key[1])


def _name(d: Deployment, i: int) -> str:
    return f"factory-{d.component}-{i}"


def _index(name: str) -> int:
    return int(name.rsplit("-", 1)[1])


@register("runtime", "fake")
def make(**kw) -> FakeRuntime:
    return FakeRuntime(**kw)
