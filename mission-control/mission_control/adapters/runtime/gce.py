"""Sandbox VMs for agent hosts, through the gcloud CLI on the main node (factory-vm SA).

A host VM: name = host id (`agent-…`, the only names the main node's IAM grant can
touch), no public IP (egress through Cloud NAT), service account `factory-agent`,
network tag `factory-agent` (SSH allowed only from VMs tagged `factory`), OS Login.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

from mission_control.ports import Step

HOST_ID = re.compile(r"^agent-[a-z0-9][a-z0-9-]{0,30}$")
MACHINE_TYPES = {"e2-small": "2 vCPU (shared), 2 GB, about $13/month",
                 "e2-medium": "2 vCPU (shared), 4 GB, about $26/month",
                 "e2-standard-2": "2 vCPU, 8 GB, about $52/month"}


class GceError(RuntimeError):
    pass


class Gce:
    def __init__(self, *, project: str, zone: str = "me-west1-a", service_account: str = "",
                 gcloud: str = "gcloud", key: str = "/root/.ssh/factory_hosts",
                 known_hosts: str = "/root/.ssh/factory_hosts_known", disk_gb: int = 20,
                 image_family: str = "ubuntu-2404-lts-amd64", image_project: str = "ubuntu-os-cloud"):
        self.project, self.zone, self.gcloud = project, zone, gcloud
        self.sa = service_account or f"factory-agent@{project}.iam.gserviceaccount.com"
        self.key, self.known_hosts, self.disk_gb = key, known_hosts, disk_gb
        self.image_family, self.image_project = image_family, image_project
        self._user = ""

    def _g(self, *args: str, timeout: float = 600) -> subprocess.CompletedProcess:
        return subprocess.run([self.gcloud, *args, "--project", self.project, "--quiet"],
                              capture_output=True, text=True, timeout=timeout)

    # ------------------------------------------------------------------ VMs
    def describe(self, host_id: str) -> dict | None:
        p = self._g("compute", "instances", "describe", host_id, "--zone", self.zone, "--format", "json", timeout=60)
        if p.returncode:
            if "was not found" in p.stderr or "notFound" in p.stderr:
                return None
            raise GceError(f"describe {host_id}: {p.stderr.strip()[-300:]}")
        raw = json.loads(p.stdout)
        nic = (raw.get("networkInterfaces") or [{}])[0]
        return {"name": raw["name"], "status": raw.get("status", ""), "address": nic.get("networkIP", ""),
                "machine_type": raw.get("machineType", "").rsplit("/", 1)[-1],
                "public": bool(nic.get("accessConfigs"))}

    def create(self, host_id: str, machine_type: str = "e2-small") -> Iterator[Step]:
        if not HOST_ID.match(host_id):
            raise ValueError(f"VM host ids start with agent- (got {host_id!r})")
        if machine_type not in MACHINE_TYPES:
            raise ValueError(f"machine type must be one of {', '.join(MACHINE_TYPES)}")
        cur = self.describe(host_id)
        if cur is None:
            p = self._g("compute", "instances", "create", host_id, "--zone", self.zone,
                        "--machine-type", machine_type, "--no-address",
                        "--image-family", self.image_family, "--image-project", self.image_project,
                        "--boot-disk-size", f"{self.disk_gb}GB", "--boot-disk-type", "pd-balanced",
                        "--service-account", self.sa, "--scopes", "cloud-platform",
                        "--tags", "factory-agent", "--metadata", "enable-oslogin=TRUE",
                        "--shielded-secure-boot", "--shielded-vtpm", "--shielded-integrity-monitoring",
                        "--labels", f"purpose=ai-software-factory,factory-host={host_id}", timeout=600)
            if p.returncode:
                yield Step("create", False, p.stderr.strip()[-500:])
                return
            cur = self.describe(host_id)
            self.forget_host_key(cur["address"] if cur else "")
            yield Step("create", True, f"{host_id} {machine_type} at {cur['address'] if cur else '?'} (no public IP)")
        else:
            yield Step("create", True, f"{host_id} exists ({cur['status']}, {cur['address']})")
            if cur["status"] in ("TERMINATED", "STOPPED"):
                p = self._g("compute", "instances", "start", host_id, "--zone", self.zone)
                yield Step("start", p.returncode == 0, p.stderr.strip()[-300:] or "started")

    def delete(self, host_id: str) -> Iterator[Step]:
        if not HOST_ID.match(host_id):
            raise ValueError(f"only agent-… VMs can be removed (got {host_id!r})")
        cur = self.describe(host_id)
        if cur is None:
            yield Step("delete-vm", True, f"{host_id} does not exist")
            return
        p = self._g("compute", "instances", "delete", host_id, "--zone", self.zone, timeout=600)
        if p.returncode == 0:
            self.forget_host_key(cur["address"])
        yield Step("delete-vm", p.returncode == 0, p.stderr.strip()[-300:] if p.returncode else f"{host_id} deleted")

    # ------------------------------------------------------------------ SSH identity
    def ssh_identity(self) -> tuple[str, str]:
        """(OS Login user, key path) of the main node's service account; makes the key
        and registers it once."""
        if self._user:
            return self._user, self.key
        key = Path(self.key)
        if not key.exists():
            key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "factory-main-node", "-f", str(key)],
                           check=True, capture_output=True)
        p = self._g("compute", "os-login", "ssh-keys", "add", f"--key-file={key}.pub", timeout=120)
        if p.returncode:
            raise GceError(f"OS Login key: {p.stderr.strip()[-300:]}")
        p = self._g("compute", "os-login", "describe-profile", "--format", "value(posixAccounts[0].username)", timeout=60)
        user = p.stdout.strip()
        if p.returncode or not user:
            raise GceError(f"OS Login profile: {p.stderr.strip()[-300:]}")
        self._user = user
        return user, self.key

    def forget_host_key(self, address: str) -> None:
        if address and os.path.exists(self.known_hosts):
            subprocess.run(["ssh-keygen", "-R", address, "-f", self.known_hosts], capture_output=True)
