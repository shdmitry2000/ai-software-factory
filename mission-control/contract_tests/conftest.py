"""Run every port's contract suite against every adapter that can run here.

An adapter takes part when it is listed in CANDIDATES for its port. Cloud adapters need
real resources, so they join only when MC_CONTRACT_LIVE=1 and their settings are in the
environment; otherwise they are skipped with the reason shown.

Each candidate is a "harness": a function returning the adapter plus whatever the suite
needs alongside it (a topic and subscription for a bus).
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from mission_control import registry


@dataclass
class BusHarness:
    bus: Any
    topic: str
    subscription: str


@dataclass
class StoreHarness:
    store: Any
    collection: str


def _memory_bus() -> BusHarness:
    bus = registry.get("bus", "memory")()
    topic = f"t-{uuid.uuid4().hex[:8]}"
    sub = f"s-{uuid.uuid4().hex[:8]}"
    bus.create_subscription(sub, topic)
    return BusHarness(bus, topic, sub)


def _memory_store() -> StoreHarness:
    return StoreHarness(registry.get("store", "memory")(), f"c_{uuid.uuid4().hex[:8]}")


LIVE = os.environ.get("MC_CONTRACT_LIVE") == "1"

# port → [(adapter name, harness factory, skip reason or "")]
CANDIDATES: dict[str, list[tuple[str, Callable[[], Any], str]]] = {
    "bus": [("memory", _memory_bus, "")],
    "store": [("memory", _memory_store, "")],
}


def _params(port: str):
    out = []
    for name, harness, skip in CANDIDATES.get(port, []):
        if not skip:
            try:
                registry.get(port, name)
            except (KeyError, ModuleNotFoundError):
                skip = f"adapter {port}/{name} not written yet"
        marks = [pytest.mark.skip(reason=skip)] if skip else []
        out.append(pytest.param(harness, id=f"{port}-{name}", marks=marks))
    return out or [pytest.param(None, id=f"{port}-none", marks=pytest.mark.skip(reason=f"no {port} adapters"))]


@pytest.fixture(params=_params("bus"))
def bus_h(request) -> BusHarness:
    return request.param()


@pytest.fixture(params=_params("store"))
def store_h(request) -> StoreHarness:
    return request.param()
