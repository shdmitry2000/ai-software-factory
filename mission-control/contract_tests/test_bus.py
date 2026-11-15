"""EventBus contract: what every bus adapter must do."""
from __future__ import annotations

import uuid

from mission_control.ports import EventBus


def ev(n: int, key: str = "run-1") -> dict:
    return {
        "specversion": "1.0", "id": f"test:{uuid.uuid4().hex}", "source": "factory://test/contract",
        "type": "dev.factory.node.started", "time": "2026-11-15T12:00:00Z",
        "datacontenttype": "application/json", "factoryengine": "test", "factoryrun": key,
        "data": {"workflow": "w", "node": f"n{n}"},
    }


def pull(h, max_messages: int = 10, timeout_s: float = 2) -> list:
    """Pull, dropping the harness's readiness probes (at-least-once delivery may repeat one)."""
    out = []
    for d in h.bus.pull(h.subscription, max_messages=max_messages, timeout_s=timeout_s):
        if d.event.get("probe"):
            d.ack()
        else:
            out.append(d)
    return out


def drain(h, want: int, tries: int = 10) -> list:
    got = []
    for _ in range(tries):
        got += pull(h, max_messages=want - len(got))
        if len(got) >= want:
            break
    return got


def test_is_an_event_bus(bus_h):
    assert isinstance(bus_h.bus, EventBus)


def test_publish_returns_an_id(bus_h):
    assert bus_h.bus.publish(bus_h.topic, ev(1))


def test_published_event_arrives_unchanged(bus_h):
    e = ev(1)
    bus_h.bus.publish(bus_h.topic, e)
    [d] = drain(bus_h, 1)
    assert d.event == e
    d.ack()


def test_acked_is_not_redelivered(bus_h):
    bus_h.bus.publish(bus_h.topic, ev(1))
    [d] = drain(bus_h, 1)
    d.ack()
    assert pull(bus_h, timeout_s=1) == []


def test_nacked_is_redelivered(bus_h):
    e = ev(1)
    bus_h.bus.publish(bus_h.topic, e)
    [d] = drain(bus_h, 1)
    d.nack()
    [again] = drain(bus_h, 1)
    assert again.event["id"] == e["id"]
    assert again.attempt >= 2
    again.ack()


def test_same_ordering_key_keeps_order(bus_h):
    """An ordered consumer acks as it goes: a key's next message may wait for that ack."""
    sent = [ev(i, key="run-ordered") for i in range(5)]
    for e in sent:
        bus_h.bus.publish(bus_h.topic, e, ordering_key="run-ordered")
    got: list[str] = []
    for _ in range(30):
        for d in pull(bus_h, max_messages=5):
            got.append(d.event["id"])
            d.ack()
        if len(got) >= 5:
            break
    assert got == [e["id"] for e in sent]


def test_empty_pull_returns_empty_list(bus_h):
    assert pull(bus_h, timeout_s=1) == []
