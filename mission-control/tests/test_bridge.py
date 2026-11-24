"""Bridge: publishes each engine event once per cursor, resumes after a restart, beats."""
from __future__ import annotations

from mission_control.adapters.bus.memory import MemoryBus
from mission_control.adapters.engine.archon import ArchonEngine
from mission_control.adapters.store.memory import MemoryStore
from mission_control.bridge import ENGINES, Bridge
from mission_control.events import validate

from test_archon_engine import _db


def setup(tmp_path):
    bus, store = MemoryBus(), MemoryStore()
    bus.create_subscription("sub", "factory-events")
    engine = ArchonEngine(str(_db(tmp_path)), engine="factory-1", graph_loader=lambda run: None)
    return bus, store, engine


def drain(bus) -> list:
    got = []
    while True:
        batch = bus.pull("sub", max_messages=100, timeout_s=0.05)
        if not batch:
            return got
        for d in batch:
            got.append(d.event)
            d.ack()


def test_publishes_every_event_then_nothing(tmp_path):
    bus, store, engine = setup(tmp_path)
    b = Bridge(engine, bus, store, name="factory-1", batch=10)
    assert b.step() == 10 and b.step() == 6 and b.step() == 0
    events = drain(bus)
    assert len(events) == 16 and len({e["id"] for e in events}) == 16
    assert store.get(ENGINES, "factory-1")["bridge_cursor"] == "16"


def test_restart_resumes_from_saved_cursor(tmp_path):
    bus, store, engine = setup(tmp_path)
    Bridge(engine, bus, store, name="factory-1", batch=10).step()          # 10 out, then "crash"
    again = Bridge(engine, bus, store, name="factory-1", batch=10)          # a rebuilt engine
    assert again.cursor == "10"
    while again.step():
        pass
    ids = [e["id"] for e in drain(bus)]
    assert len(ids) == 16 and len(set(ids)) == 16                            # no gap, no duplicate


def test_events_of_one_run_keep_their_order(tmp_path):
    bus, store, engine = setup(tmp_path)
    b = Bridge(engine, bus, store, name="factory-1")
    b.step()
    times = [e["time"] for e in drain(bus) if e["type"].startswith("dev.factory.node.started")]
    assert times == sorted(times)


def test_heartbeat_carries_cursor_and_is_valid(tmp_path):
    bus, store, engine = setup(tmp_path)
    b = Bridge(engine, bus, store, name="factory-1", heartbeat_s=3600)
    b.step()
    assert b.beat() is True and b.beat() is False                           # once per interval
    [hb] = [e for e in drain(bus) if e["type"] == "dev.factory.engine.heartbeat"]
    assert hb["data"]["bridge_cursor"] == "16" and validate(hb) == []


def test_invalid_event_is_dropped_not_blocking(tmp_path):
    bus, store, engine = setup(tmp_path)

    class Broken:
        def events_since(self, cursor, *, limit=500):
            good, cur = engine.events_since(cursor, limit=limit)
            return [{"type": "dev.factory.nonsense"}] + good, cur

        def heartbeat(self):
            return engine.heartbeat()

    b = Bridge(Broken(), bus, store, name="factory-1")
    assert b.step() == 16 and b.dropped == 1
