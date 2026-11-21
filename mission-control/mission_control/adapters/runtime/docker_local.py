"""AgentRuntime on the main node itself: Docker on factory-1 (host id `local`).

Each replica is one container named `factory-<component>-<n>`, labelled with its
deployment, replica number, spec hash and the full (secret-free) deployment, so the
runtime's own state is all this adapter needs: nothing is kept in memory.

Hardening for every container: read-only root filesystem, writable tmpfs only where the
deployment says, all capabilities dropped, no-new-privileges, no published ports,
restart unless stopped.

The LLM credential (llm: project | api_key) is copied from the main node's login file
(`/root/.factory-env`, written by bin/gcp/push-logins.sh) into
`<env_dir>/<component>.env`, mode 600, owned by the container user, and mounted
read-only at /run/factory/llm.env. The container gets only FACTORY_ENV_FILE pointing at
it, so `docker inspect` never shows the value.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from mission_control.ports import Deployment, Host, Instance, Step
from mission_control.registry import register

from . import credential_lines, from_json, parse_env_file, spec_hash, to_json, validate

L = "dev.factory."
SECRET_MOUNT = "/run/factory/llm.env"


class DockerError(RuntimeError):
    pass


class DockerLocal:
    kind = "local"

    def __init__(self, *, host_id: str = "local", docker: str = "docker",
                 env_dir: str = "/etc/factory", login_file: str = "/root/.factory-env",
                 stop_timeout: int = 10):
        self.host_id, self.docker = host_id, docker
        self.env_dir, self.login_file = Path(env_dir), Path(login_file)
        self.stop_timeout = stop_timeout

    # ------------------------------------------------------------------ docker and files
    def _run(self, *args: str, check: bool = True, merge: bool = False) -> str:
        streams = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT if merge else subprocess.PIPE}
        p = subprocess.run([self.docker, *args], text=True, **streams)
        if check and p.returncode:
            err = (p.stdout if merge else p.stderr or "").strip()
            raise DockerError(f"docker {args[0]} failed: {err[-400:]}")
        return p.stdout or ""

    def _read_login(self) -> dict[str, str]:
        return parse_env_file(self.login_file.read_text())

    def _write_secret(self, path: Path, text: str, uid: str) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(path, 0o600)
        if uid.isdigit() and hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(path, int(uid), -1)       # readable by the container user alone

    def _delete_secret(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def _secret_path(self, d: Deployment) -> Path:
        return self.env_dir / f"{d.component}.env"

    def _image_exists(self, ref: str) -> bool:
        return subprocess.run([self.docker, "image", "inspect", ref], capture_output=True).returncode == 0

    def _containers(self, deployment_id: str = "") -> list[dict]:
        args = ["ps", "-aq", "--filter", f"label={L}managed=1"]
        if deployment_id:
            args += ["--filter", f"label={L}deployment={deployment_id}"]
        ids = self._run(*args).split()
        if not ids:
            return []
        items = json.loads(self._run("inspect", *ids))
        return sorted(items, key=lambda c: int(c["Config"]["Labels"].get(f"{L}replica", "0")))

    def _require(self, deployment_id: str) -> list[dict]:
        found = self._containers(deployment_id)
        if not found:
            raise KeyError(f"no deployment {deployment_id!r} on {self.host_id}")
        return found

    # ------------------------------------------------------------------ hosts
    def hosts(self) -> list[Host]:
        try:
            version = self._run("version", "--format", "{{.Server.Version}}").strip()
            return [Host(self.host_id, self.kind, state="healthy", labels={"docker": version})]
        except (DockerError, FileNotFoundError):
            return [Host(self.host_id, self.kind, state="unreachable")]

    def create_host(self, host: Host) -> Iterator[Step]:
        raise ValueError("docker-local runs only on the main node; new hosts come from the gce-vm adapter")

    def install(self, host_id: str) -> Iterator[Step]:
        self._check_host(host_id)
        h = self.hosts()[0]
        yield Step("docker", h.state == "healthy", h.labels.get("docker", "docker not reachable"))
        self.env_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        yield Step("env-dir", True, str(self.env_dir))

    def remove_host(self, host_id: str) -> Iterator[Step]:
        raise ValueError("the main node cannot be removed")

    # ------------------------------------------------------------------ deployments
    def apply(self, d: Deployment) -> Iterator[Step]:
        validate(d)
        self._check_host(d.host)

        ref = d.ref()
        if self._image_exists(ref):
            yield Step("image", True, f"{ref} present")
        elif d.digest and self._image_exists(d.digest):       # a locally built image, by id
            ref = d.digest
            yield Step("image", True, f"local image {d.digest[:19]}")
        else:
            try:
                self._run("pull", "-q", ref)
            except DockerError as e:
                yield Step("pull", False, str(e))
                return
            yield Step("pull", True, ref)

        secret = self._secret_path(d)
        if d.llm != "none":
            try:
                lines = credential_lines(d.llm, self._read_login())
            except (OSError, LookupError) as e:
                msg = str(e) if isinstance(e, LookupError) else f"cannot read {self.login_file}"
                yield Step("credential", False, msg)
                return
            self._write_secret(secret, "\n".join(lines) + "\n", d.run_as)
            yield Step("credential", True, f"{secret} (mode 600)")
        else:
            self._delete_secret(secret)

        want = spec_hash(d)
        have = {int(c["Config"]["Labels"][f"{L}replica"]): c for c in self._containers(d.id)}
        for i in range(1, d.replicas + 1):
            c = have.get(i)
            name = _name(d, i)
            if c and c["Config"]["Labels"].get(f"{L}spec") == want:
                if c["State"]["Status"] != "running":
                    self._run("start", name)
                    yield Step("run", True, f"{name} started")
                else:
                    yield Step("run", True, f"{name} unchanged")
                continue
            if c:
                self._run("rm", "-f", name)
            try:
                self._run(*self._run_args(d, i, ref, want, secret))
            except DockerError as e:
                yield Step("run", False, str(e))
                return
            yield Step("run", True, f"{name} {'recreated' if c else 'created'}")
        for i, c in sorted(have.items()):
            if i > d.replicas:
                self._run("rm", "-f", c["Name"].lstrip("/"))
                yield Step("remove", True, c["Name"].lstrip("/"))

        states = {c["Name"].lstrip("/"): c["State"]["Status"] for c in self._containers(d.id)}
        bad = {n: s for n, s in states.items() if s != "running"}
        yield Step("verify", not bad, "all running" if not bad else f"not running: {bad}")

    def _run_args(self, d: Deployment, i: int, ref: str, spec: str, secret: Path) -> list[str]:
        name = _name(d, i)
        labels = {"managed": "1", "deployment": d.id, "component": d.component, "replica": str(i),
                  "spec": spec, "digest": d.digest, "deployment-json": to_json(d)}
        args = ["run", "-d", "--name", name, "--hostname", name, "--restart", "unless-stopped",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        for k, v in labels.items():
            args += ["--label", f"{L}{k}={v}"]
        for path in d.tmpfs:
            args += ["--tmpfs", f"{path}:rw,mode=1777"]
        if d.run_as:
            args += ["--user", d.run_as]
        if d.cpus:
            args += ["--cpus", d.cpus]
        if d.memory:
            args += ["--memory", d.memory]
        env = {"AGENT_ID": f"{d.component}-{i}@{d.host}", "AGENT_LOCATION": f"docker:{d.host}/{name}",
               "FACTORY_DEPLOYMENT": d.id, **d.env}
        if d.digest:
            env.setdefault("AGENT_VERSION", d.digest[:19])
        if d.llm != "none":
            args += ["-v", f"{secret}:{SECRET_MOUNT}:ro"]
            env["FACTORY_ENV_FILE"] = SECRET_MOUNT
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        return [*args, ref, *d.command]

    def remove(self, deployment_id: str) -> Iterator[Step]:
        found = self._require(deployment_id)
        d = from_json(found[0]["Config"]["Labels"][f"{L}deployment-json"])
        for c in found:
            self._run("rm", "-f", c["Name"].lstrip("/"))
            yield Step("remove", True, c["Name"].lstrip("/"))
        self._delete_secret(self._secret_path(d))

    def start(self, deployment_id: str) -> Iterator[Step]:
        yield from self._each(deployment_id, "start")

    def stop(self, deployment_id: str) -> Iterator[Step]:
        yield from self._each(deployment_id, "stop", "-t", str(self.stop_timeout))

    def restart(self, deployment_id: str) -> Iterator[Step]:
        yield from self._each(deployment_id, "restart", "-t", str(self.stop_timeout))

    def scale(self, deployment_id: str, replicas: int) -> Iterator[Step]:
        found = self._require(deployment_id)
        d = from_json(found[0]["Config"]["Labels"][f"{L}deployment-json"])
        yield from self.apply(replace(d, replicas=replicas))

    def status(self) -> list[Instance]:
        out = []
        for c in self._containers():
            lb = c["Config"]["Labels"]
            out.append(Instance(lb.get(f"{L}deployment", ""), c["Name"].lstrip("/"), self.host_id,
                                c["State"]["Status"], c["Config"]["Image"], lb.get(f"{L}digest", ""),
                                c["State"].get("StartedAt", "")))
        return out

    def logs(self, deployment_id: str, *, tail: int = 200) -> list[str]:
        found = self._require(deployment_id)
        lines: list[str] = []
        for c in found:
            name = c["Name"].lstrip("/")
            text = self._run("logs", "--tail", str(tail), name, merge=True, check=False)
            prefix = f"[{name}] " if len(found) > 1 else ""
            lines += [prefix + line for line in text.splitlines()]
        return lines[-tail:] if tail else lines

    # ------------------------------------------------------------------ helpers
    def _check_host(self, host_id: str) -> None:
        if host_id != self.host_id:
            raise KeyError(f"no host {host_id!r}; this adapter serves {self.host_id!r}")

    def _each(self, deployment_id: str, verb: str, *extra: str) -> Iterator[Step]:
        for c in self._require(deployment_id):
            name = c["Name"].lstrip("/")
            self._run(verb, *extra, name)
            yield Step(verb, True, name)


def _name(d: Deployment, i: int) -> str:
    return f"factory-{d.component}-{i}"


@register("runtime", "docker-local")
def make(**kw) -> DockerLocal:
    return DockerLocal(**kw)
