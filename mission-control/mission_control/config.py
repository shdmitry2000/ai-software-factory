"""Load mission-control.yaml and build each port's adapter by name.

    ports:
      bus:
        adapter: pubsub
        project: ${GCP_PROJECT}
      store:
        adapter: firestore

`${VAR}` and `${VAR:-default}` are taken from the environment, so secrets and
per-machine values never live in the file. Switching a port to another adapter is an edit
to this file, nothing else.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mission_control import registry
from mission_control.ports import PORTS, is_adapter_for

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise KeyError(f"environment variable {name} is not set (used in mission-control.yaml)")
        return _VAR.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass
class Config:
    ports: dict[str, dict[str, Any]] = field(default_factory=dict)
    _built: dict[str, Any] = field(default_factory=dict, repr=False)

    def adapter_name(self, port: str) -> str:
        return self.ports[port]["adapter"]

    def adapter(self, port: str) -> Any:
        """The adapter for `port`, built once from its settings."""
        if port not in self._built:
            if port not in PORTS:
                raise KeyError(f"unknown port {port!r}")
            if port not in self.ports:
                raise KeyError(f"port {port!r} is not configured in mission-control.yaml")
            settings = dict(self.ports[port])
            name = settings.pop("adapter")
            obj = registry.get(port, name)(**_expand(settings))
            if not is_adapter_for(port, obj):
                raise TypeError(f"{port}/{name} does not implement the {port} port")
            self._built[port] = obj
        return self._built[port]


def loads(text: str) -> Config:
    raw = yaml.safe_load(text) or {}
    ports = raw.get("ports") or {}
    for port, settings in ports.items():
        if port not in PORTS:
            raise KeyError(f"unknown port {port!r} in mission-control.yaml; ports: {', '.join(PORTS)}")
        if not isinstance(settings, dict) or "adapter" not in settings:
            raise ValueError(f"port {port!r} needs an 'adapter' name")
    return Config(ports=ports)


def load(path: str | os.PathLike | None = None) -> Config:
    """Read the file named by `path`, $MISSION_CONTROL_CONFIG, or ./mission-control.yaml."""
    p = Path(path or os.environ.get("MISSION_CONTROL_CONFIG", "mission-control.yaml"))
    return loads(p.read_text(encoding="utf-8"))
