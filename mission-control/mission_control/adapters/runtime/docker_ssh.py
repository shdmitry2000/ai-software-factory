"""AgentRuntime for one VM host: the docker-local rules, with every docker command run
over SSH from the main node (`ssh user@ip sudo docker …`).

- SSH: the main node's own key (OS Login, the factory-vm service account), the VM's
  internal IP, a dedicated known_hosts file, BatchMode (never a prompt).
- The LLM credential is read on the main node and written on the VM through SSH stdin
  (mode 600, owned by the container user); it never appears on a command line.
- Images built on the main node are shipped with `docker save | ssh docker load`, so a
  VM needs no registry access.
"""
from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Generator, Iterator
from pathlib import Path

from mission_control.ports import Deployment, Host, Step

from .docker_local import DockerError, DockerLocal

INSTALL = r"""set -e
cloud-init status --wait >/dev/null 2>&1 || true
if ! command -v docker >/dev/null; then
  for i in 1 2 3 4 5; do apt-get update -qq && break || sleep 10; done
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1
mkdir -p -m 700 {env_dir}
echo "docker $(docker version --format '{{{{.Server.Version}}}}')"
df -Pk / | tail -1 | awk '{{print "disk", $2, $5}}'
"""

HEALTH = r"""nproc; cat /proc/loadavg; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
df -Pk / | tail -1
sudo docker version --format '{{.Server.Version}}' 2>/dev/null || echo none
sudo docker ps -q --filter label=dev.factory.managed=1 2>/dev/null | wc -l
"""


class DockerSSH(DockerLocal):
    kind = "vm"

    def __init__(self, host_id: str, address: str, *, user: str, key: str, known_hosts: str,
                 ssh: str = "ssh", docker: str = "docker", env_dir: str = "/etc/factory",
                 login_file: str = "/root/.factory-env", stop_timeout: int = 10, connect_timeout: int = 10):
        super().__init__(host_id=host_id, docker=docker, env_dir=env_dir, login_file=login_file,
                         stop_timeout=stop_timeout)
        self.address, self.user, self.key, self.known_hosts = address, user, key, known_hosts
        self.ssh, self.connect_timeout = ssh, connect_timeout

    # ------------------------------------------------------------------ ssh
    def ssh_argv(self) -> list[str]:
        return [self.ssh, "-i", self.key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", f"ConnectTimeout={self.connect_timeout}",
                "-o", "ServerAliveInterval=15", "-o", "LogLevel=ERROR", f"{self.user}@{self.address}"]

    def _cmd(self, *args: str) -> list[str]:
        return [*self.ssh_argv(), "sudo " + shlex.join([self.docker, *args])]

    def sh(self, script: str, *, sudo: bool = True, stdin: str | None = None, timeout: float = 600) -> subprocess.CompletedProcess:
        remote = ("sudo sh -c " if sudo else "sh -c ") + shlex.quote(script)
        return subprocess.run([*self.ssh_argv(), remote], input=stdin, text=True, capture_output=True, timeout=timeout)

    # ------------------------------------------------------------------ files on the VM
    def _write_secret(self, path: Path, text: str, uid: str) -> None:
        own = f"chown {shlex.quote(uid)} {shlex.quote(str(path))}; " if uid.isdigit() else ""
        script = (f"umask 077; mkdir -p -m 700 {shlex.quote(str(path.parent))}; "
                  f"cat > {shlex.quote(str(path))}; chmod 600 {shlex.quote(str(path))}; {own}")
        p = self.sh(script, stdin=text, timeout=60)
        if p.returncode:
            raise DockerError(f"writing the credential file on {self.host_id} failed: {p.stderr.strip()[-200:]}")

    def _delete_secret(self, path: Path) -> None:
        self.sh(f"rm -f {shlex.quote(str(path))}", timeout=60)

    # ------------------------------------------------------------------ images
    def _ensure_image(self, d: Deployment) -> Generator[Step, None, str]:
        ref = d.ref()
        if self._image_exists(ref):
            yield Step("image", True, f"{ref} present on {self.host_id}")
            return ref
        if d.digest and self._image_exists(d.digest):
            yield Step("image", True, f"{d.digest[:19]} present on {self.host_id}")
            return d.digest
        local = d.digest if d.digest and _local_has(self.docker, d.digest) else (ref if _local_has(self.docker, ref) else "")
        if local:
            ok, detail = self._ship(local)
            yield Step("ship", ok, detail)
            return (d.digest or ref) if ok else ""
        try:
            self._run("pull", "-q", ref, timeout=900)
        except DockerError as e:
            yield Step("pull", False, str(e))
            return ""
        yield Step("pull", True, ref)
        return ref

    def _ship(self, ref: str) -> tuple[bool, str]:
        """docker save on the main node | docker load on the VM."""
        save = subprocess.Popen([self.docker, "save", ref], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            load = subprocess.run([*self.ssh_argv(), "sudo " + shlex.join([self.docker, "load", "-q"])],
                                  stdin=save.stdout, capture_output=True, text=True, timeout=1800)
        finally:
            save.stdout.close()
            save.wait(timeout=60)
        if save.returncode or load.returncode:
            err = load.stderr.strip() or (save.stderr.read().decode(errors="replace").strip() if save.stderr else "")
            return False, f"shipping {ref[:19]} to {self.host_id} failed: {err[-300:]}"
        last = (load.stdout.strip().splitlines() or [""])[-1]
        return True, f"{ref[:19]} → {self.host_id} ({last[:100]})"

    # ------------------------------------------------------------------ hosts
    def hosts(self) -> list[Host]:
        return [Host(self.host_id, self.kind, self.address, "unknown")]

    def install(self, host_id: str) -> Iterator[Step]:
        self._check_host(host_id)
        p = None
        for _ in range(18):                              # a new VM: sshd and OS Login take a minute or two
            p = self.sh("true", sudo=False, timeout=30)
            if p.returncode == 0:
                break
            time.sleep(10)
        if p is None or p.returncode:
            yield Step("ssh", False, f"cannot reach {self.user}@{self.address}: {(p.stderr if p else '').strip()[-200:]}")
            return
        yield Step("ssh", True, f"{self.address}")
        p = self.sh(INSTALL.format(env_dir=shlex.quote(str(self.env_dir))), timeout=900)
        if p.returncode:
            yield Step("docker", False, (p.stderr or p.stdout).strip()[-400:])
            return
        facts = dict(line.split(" ", 1) for line in p.stdout.strip().splitlines() if " " in line)
        yield Step("docker", True, facts.get("docker", ""))
        yield Step("env-dir", True, str(self.env_dir))
        disk = facts.get("disk", "").split()
        free_ok = bool(disk) and int(disk[1].rstrip("%") or 0) < 90
        yield Step("disk", free_ok, f"{int(disk[0]) // 2**20 if disk else '?'} GB, {disk[1] if disk else '?'} used")

    def health(self, host_id: str) -> dict:
        self._check_host(host_id)
        try:
            p = self.sh(HEALTH, sudo=False, timeout=30)
        except subprocess.TimeoutExpired:
            return {"state": "unreachable", "error": "ssh timed out"}
        if p.returncode:
            return {"state": "unreachable", "error": (p.stderr.strip() or f"exit {p.returncode}")[-300:]}
        return parse_health(p.stdout)


def parse_health(text: str) -> dict:
    lines = [x.strip() for x in text.strip().splitlines()]
    out: dict = {"state": "healthy"}
    try:
        out["cpus"] = int(lines[0])
        out["load1"] = float(lines[1].split()[0])
        mem = {line.split(":")[0]: int(line.split()[1]) for line in lines[2:4]}
        total, avail = mem["MemTotal"], mem["MemAvailable"]
        out["mem_total_mb"], out["mem_used_pct"] = total // 1024, round(100 * (total - avail) / total, 1)
        df = lines[4].split()
        out["disk_total_gb"], out["disk_used_pct"] = round(int(df[1]) / 2**20, 1), float(df[4].rstrip("%"))
        out["docker"] = lines[5]
        out["containers"] = int(lines[6])
    except (IndexError, ValueError, KeyError, ZeroDivisionError):
        out["error"] = "could not read all health numbers"
    if out.get("docker") in ("none", ""):
        out["state"], out["error"] = "degraded", "docker is not running"
    return out


def _local_has(docker: str, ref: str) -> bool:
    return subprocess.run([docker, "image", "inspect", ref], capture_output=True).returncode == 0
