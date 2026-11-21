"""The bridge: engine events → the bus, on each engine machine.

Loop: read the engine's events after the saved cursor, publish each one (ordering key =
run), then save the new cursor. Publishing before saving makes delivery at-least-once: a
crash in between re-sends a few events, which the store absorbs because event ids are
deterministic. It can never skip one. A heartbeat goes out every `heartbeat_s`.

The cursor lives in the store (`engines/{engine}.bridge_cursor`), not on the machine, so an
engine rebuilt after the Sunday sweep resumes where the old one stopped.

    MISSION_CONTROL_CONFIG=mission-control.yaml python -m mission_control.bridge
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from mission_control.events import validate
from mission_control.ports import EngineAdapter, EventBus, StateStore

log = logging.getLogger("mission_control.bridge")
ENGINES = "engines"


class Bridge:
    def __init__(self, engine: EngineAdapter, bus: EventBus, store: StateStore, *, name: str,
                 topic: str = "factory-events", heartbeat_s: float = 60.0, batch: int = 200) -> None:
        self.engine, self.bus, self.store = engine, bus, store
        self.name, self.topic, self.heartbeat_s, self.batch = name, topic, heartbeat_s, batch
        self.cursor = (store.get(ENGINES, name) or {}).get("bridge_cursor", "")
        self._last_beat = 0.0
        self.published = self.dropped = 0

    def step(self) -> int:
        """Publish one batch; returns how many events went out."""
        events, cursor = self.engine.events_since(self.cursor, limit=self.batch)
        sent = 0
        for ev in events:
            errors = validate(ev)
            if errors:   # never block the stream on one bad mapping; count it and move on
                self.dropped += 1
                log.warning("dropping invalid event %s: %s", ev.get("id"), "; ".join(errors[:3]))
                continue
            self.bus.publish(self.topic, ev, ordering_key=ev.get("factoryrun", ""))
            sent += 1
        if cursor != self.cursor:
            self.store.merge(ENGINES, self.name, {
                "bridge_cursor": cursor,
                "bridge_saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            self.cursor = cursor
        self.published += sent
        return sent

    def beat(self, *, force: bool = False) -> bool:
        """Publish a heartbeat when one is due; returns True if it did."""
        now = time.monotonic()
        if not force and now - self._last_beat < self.heartbeat_s:
            return False
        hb = self.engine.heartbeat()
        hb["data"]["bridge_cursor"] = self.cursor
        self.bus.publish(self.topic, hb, ordering_key=f"engine-{self.name}")
        self._last_beat = now
        return True

    def run_forever(self, poll_s: float = 2.0) -> None:
        log.info("bridge %s starting from cursor %r", self.name, self.cursor)
        while True:
            try:
                while self.step() >= self.batch:   # catch up without sleeping
                    pass
                self.beat()
            except Exception:   # a bus or store hiccup must not kill the engine's reporting
                log.exception("bridge step failed; retrying")
            time.sleep(poll_s)


def main(argv: list[str] | None = None) -> None:
    """`--once`: catch up, send one heartbeat, print a summary and exit (setup and checks)."""
    import sys

    from mission_control import config

    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    engine = cfg.adapter("engine")
    name = getattr(engine, "engine", "") or os.uname().nodename.split(".")[0]
    bridge = Bridge(engine, cfg.adapter("bus"), cfg.adapter("store"), name=name,
                    topic=os.environ.get("MC_TOPIC", "factory-events"),
                    heartbeat_s=float(os.environ.get("MC_HEARTBEAT_S", "60")))
    if "--once" in argv:
        start = bridge.cursor
        while bridge.step():
            pass
        bridge.beat(force=True)
        print(f"bridge {name}: cursor {start!r} -> {bridge.cursor!r}, "
              f"published {bridge.published}, dropped {bridge.dropped}, heartbeat sent")
        return
    bridge.run_forever(poll_s=float(os.environ.get("MC_POLL_S", "2")))


if __name__ == "__main__":
    main()
