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
    close: Callable[[], None] = lambda: None


@dataclass
class StoreHarness:
    store: Any
    collection: str
    close: Callable[[], None] = lambda: None


def _memory_bus() -> BusHarness:
    bus = registry.get("bus", "memory")()
    topic = f"t-{uuid.uuid4().hex[:8]}"
    sub = f"s-{uuid.uuid4().hex[:8]}"
    bus.create_subscription(sub, topic)
    return BusHarness(bus, topic, sub)


def _memory_store() -> StoreHarness:
    return StoreHarness(registry.get("store", "memory")(), f"c_{uuid.uuid4().hex[:8]}")


LIVE = os.environ.get("MC_CONTRACT_LIVE") == "1"
GCP_PROJECT = os.environ.get("MC_GCP_PROJECT", "")
_LIVE_SKIP = "" if (LIVE and GCP_PROJECT) else "live adapter: set MC_CONTRACT_LIVE=1 and MC_GCP_PROJECT"


def _pubsub_bus() -> BusHarness:
    """Throwaway topic, dead-letter topic and subscription, deleted after the test."""
    bus = registry.get("bus", "pubsub")(project=GCP_PROJECT)
    tag = uuid.uuid4().hex[:8]
    topic, dlq, sub = f"test-mc-{tag}", f"test-mc-{tag}-dlq", f"test-mc-{tag}-sub"
    bus.create_topic(topic)
    bus.create_topic(dlq)
    # A dead-letter policy is what makes Pub/Sub count delivery attempts.
    bus.create_subscription(sub, topic, dead_letter_topic=dlq, max_delivery_attempts=5)
    # A new subscription can miss messages for its first seconds; wait until a probe
    # arrives so the test measures the adapter, not Pub/Sub's propagation delay.
    for _ in range(30):
        bus.publish(topic, {"specversion": "1.0", "id": f"probe:{uuid.uuid4().hex}", "probe": "1"})
        got = bus.pull(sub, max_messages=10, timeout_s=2)
        for d in got:
            d.ack()
        if got:
            break
    else:
        raise RuntimeError(f"subscription {sub} never became ready")
    for d in bus.pull(sub, max_messages=50, timeout_s=2):   # any late probes
        d.ack()

    def close() -> None:
        bus.delete_subscription(sub)
        bus.delete_topic(topic)
        bus.delete_topic(dlq)

    return BusHarness(bus, topic, sub, close)


def _firestore_store() -> StoreHarness:
    """A throwaway test_mc_* collection, emptied after the test."""
    store = registry.get("store", "firestore")(project=GCP_PROJECT)
    col = f"test_mc_{uuid.uuid4().hex[:8]}"
    return StoreHarness(store, col, lambda: store.drop_collection(col))


# port → [(adapter name, harness factory, skip reason or "")]
CANDIDATES: dict[str, list[tuple[str, Callable[[], Any], str]]] = {
    "bus": [("memory", _memory_bus, ""), ("pubsub", _pubsub_bus, _LIVE_SKIP)],
    "store": [("memory", _memory_store, ""), ("firestore", _firestore_store, _LIVE_SKIP)],
}


def _params(port: str):
    out = []
    for name, harness, skip in CANDIDATES.get(port, []):
        if not skip:
            try:
                registry.get(port, name)
            except KeyError:
                skip = f"adapter {port}/{name} not written yet"
            except ImportError as e:
                skip = f"adapter {port}/{name}: its SDK is not installed ({e.name or e})"
        marks = [pytest.mark.skip(reason=skip)] if skip else []
        out.append(pytest.param(harness, id=f"{port}-{name}", marks=marks))
    return out or [pytest.param(None, id=f"{port}-none", marks=pytest.mark.skip(reason=f"no {port} adapters"))]


@pytest.fixture(params=_params("bus"))
def bus_h(request):
    h = request.param()
    try:
        yield h
    finally:
        h.close()


@pytest.fixture(params=_params("store"))
def store_h(request):
    h = request.param()
    try:
        yield h
    finally:
        h.close()
