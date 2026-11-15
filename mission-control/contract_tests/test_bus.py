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


def drain(h, want: int, tries: int = 10) -> list:
    got = []
    for _ in range(tries):
        got += h.bus.pull(h.subscription, max_messages=want - len(got), timeout_s=2)
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
    assert bus_h.bus.pull(bus_h.subscription, timeout_s=1) == []


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
    sent = [ev(i, key="run-ordered") for i in range(5)]
    for e in sent:
        bus_h.bus.publish(bus_h.topic, e, ordering_key="run-ordered")
    got = drain(bus_h, 5)
    assert [d.event["id"] for d in got] == [e["id"] for e in sent]
    for d in got:
        d.ack()


def test_empty_pull_returns_empty_list(bus_h):
    assert bus_h.bus.pull(bus_h.subscription, timeout_s=1) == []
