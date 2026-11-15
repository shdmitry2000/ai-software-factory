"""Adapter registry: port name → adapter name → factory.

An adapter module registers itself with `@register("bus", "pubsub")`. The config loader
(`mission_control.config`) and the contract tests both look adapters up here, so adding
an adapter never touches core code.

A factory takes the adapter's settings as keyword arguments (from mission-control.yaml)
and returns the adapter.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

from mission_control.ports import PORTS

Factory = Callable[..., Any]
_ADAPTERS: dict[str, dict[str, Factory]] = {port: {} for port in PORTS}

# Built-in adapter modules, imported on first lookup so an adapter's cloud SDK is only
# needed when that adapter is actually used.
BUILTIN: dict[str, dict[str, str]] = {
    "bus": {"memory": "mission_control.adapters.bus.memory"},
    "store": {"memory": "mission_control.adapters.store.memory"},
}


def register(port: str, name: str) -> Callable[[Factory], Factory]:
    if port not in PORTS:
        raise KeyError(f"unknown port {port!r}; ports: {', '.join(PORTS)}")

    def deco(factory: Factory) -> Factory:
        _ADAPTERS[port][name] = factory
        return factory

    return deco


def get(port: str, name: str) -> Factory:
    if name not in _ADAPTERS.get(port, {}) and name in BUILTIN.get(port, {}):
        importlib.import_module(BUILTIN[port][name])
    try:
        return _ADAPTERS[port][name]
    except KeyError:
        known = sorted(set(_ADAPTERS.get(port, {})) | set(BUILTIN.get(port, {})))
        raise KeyError(f"no {port!r} adapter named {name!r}; known: {', '.join(known) or 'none'}") from None


def names(port: str) -> list[str]:
    return sorted(set(_ADAPTERS.get(port, {})) | set(BUILTIN.get(port, {})))
