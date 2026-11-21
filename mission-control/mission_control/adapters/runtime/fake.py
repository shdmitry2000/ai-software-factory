"""In-memory AgentRuntime with the same rules as the real adapters (tests, console demos)."""
from __future__ import annotations

from collections.abc import Iterator

from mission_control.ports import Deployment, Host, Instance, Step
from mission_control.registry import register

from . import credential_lines, spec_hash, validate


class FakeRuntime:
    def __init__(self, credentials: dict[str, str] | None = None):
        self._hosts: dict[str, Host] = {"local": Host("local", "local", state="healthy")}
        self._deps: dict[str, Deployment] = {}
        self._spec: dict[str, str] = {}
        self._state: dict[str, str] = {}          # container name → state
        self._logs: dict[str, list[str]] = {}
        self.credentials = credentials if credentials is not None else {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token"}
        self.secret_files: dict[str, str] = {}    # deployment id → credential file text (inspection in tests)

    # hosts
    def hosts(self) -> list[Host]:
        return sorted(self._hosts.values(), key=lambda h: h.id)

    def create_host(self, host: Host) -> Iterator[Step]:
        if host.id in self._hosts:
            raise ValueError(f"host {host.id} exists")
        self._hosts[host.id] = Host(host.id, host.kind, host.address, "installing", dict(host.labels))
        yield Step("create", True, host.id)

    def install(self, host_id: str) -> Iterator[Step]:
        h = self._host(host_id)
        self._hosts[host_id] = Host(h.id, h.kind, h.address, "healthy", dict(h.labels))
        yield Step("install", True, "docker ready")

    def remove_host(self, host_id: str) -> Iterator[Step]:
        self._host(host_id)
        if host_id == "local":
            raise ValueError("the main node cannot be removed")
        for dep_id in [k for k, d in self._deps.items() if d.host == host_id]:
            yield from self.remove(dep_id)
        del self._hosts[host_id]
        yield Step("remove-host", True, host_id)

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
            name = _name(d, i)
            if name in self._state and not changed:
                yield Step("run", True, f"{name} unchanged")
                continue
            self._state[name] = "running"
            self._logs[name] = [f"{name} started: {' '.join(d.command)}"]
            yield Step("run", True, f"{name} {'recreated' if changed else 'started'}")
        for name in [n for n in self._names(d.id) if _index(n) > d.replicas]:
            del self._state[name]
            yield Step("remove", True, name)

    def remove(self, deployment_id: str) -> Iterator[Step]:
        self._dep(deployment_id)
        for name in self._names(deployment_id):
            del self._state[name]
            yield Step("remove", True, name)
        del self._deps[deployment_id], self._spec[deployment_id]
        self.secret_files.pop(deployment_id, None)

    def start(self, deployment_id: str) -> Iterator[Step]:
        yield from self._set(deployment_id, "running", "start")

    def stop(self, deployment_id: str) -> Iterator[Step]:
        yield from self._set(deployment_id, "exited", "stop")

    def restart(self, deployment_id: str) -> Iterator[Step]:
        yield from self._set(deployment_id, "running", "restart")

    def scale(self, deployment_id: str, replicas: int) -> Iterator[Step]:
        d = self._dep(deployment_id)
        yield from self.apply(Deployment(**{**d.__dict__, "replicas": replicas}))

    def status(self) -> list[Instance]:
        out = []
        for name, state in sorted(self._state.items()):
            d = next(x for k, x in self._deps.items() if name in self._names(k))
            out.append(Instance(d.id, name, d.host, state, d.image, d.digest))
        return out

    def logs(self, deployment_id: str, *, tail: int = 200) -> list[str]:
        self._dep(deployment_id)
        lines = [line for n in self._names(deployment_id) for line in self._logs.get(n, [])]
        return lines[-tail:] if tail else lines

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

    def _names(self, deployment_id: str) -> list[str]:
        d = self._deps.get(deployment_id)
        prefix = _name(d, 0)[:-1] if d else "\0"
        return sorted((n for n in self._state if n.startswith(prefix)), key=_index)

    def _set(self, deployment_id: str, state: str, step: str) -> Iterator[Step]:
        self._dep(deployment_id)
        for name in self._names(deployment_id):
            self._state[name] = state
            yield Step(step, True, name)


def _name(d: Deployment, i: int) -> str:
    return f"factory-{d.component}-{i}"


def _index(name: str) -> int:
    return int(name.rsplit("-", 1)[1])


@register("runtime", "fake")
def make(**kw) -> FakeRuntime:
    return FakeRuntime(**kw)
