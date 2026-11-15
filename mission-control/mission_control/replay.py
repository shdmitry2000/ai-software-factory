"""Rebuild the projections from the event log.

    MISSION_CONTROL_CONFIG=... python -m mission_control.replay [--check]

`runs` and `nodes` are dropped and rebuilt from `events`; `engines` documents are re-applied
in place (they also hold the bridge's cursor, which is not an event). `--check` rebuilds
into an in-memory store and compares it with the live projections without touching them:
the way to prove the projections are a pure function of the log, and the way to move to a
new store (point a new adapter at it, replay, compare, switch).
"""
from __future__ import annotations

import sys

from mission_control.adapters.store.memory import MemoryStore
from mission_control.ports import StateStore
from mission_control.projections import ENGINES, NODES, RUNS, project
from mission_control.writer import EVENTS

PROJECTED = (RUNS, NODES)
HEARTBEAT_FIELDS = ("engine", "last_heartbeat", "slots", "halted", "paused", "archon_pin", "sandbox")


def _key(c: str, d: dict) -> str:
    return d.get("run", "") + ":" + d.get("node", "") if c == NODES else d.get("run") or d.get("engine", "")


def snapshot(store: StateStore) -> dict:
    """The projections as comparable data (engines: heartbeat fields only)."""
    out = {c: {_key(c, d): d for d in store.query(c)} for c in PROJECTED}
    out[ENGINES] = {d.get("engine", ""): {k: d[k] for k in HEARTBEAT_FIELDS if k in d}
                    for d in store.query(ENGINES) if "last_heartbeat" in d}
    return out


def rebuild(source: StateStore, target: StateStore, *, drop: bool = True) -> int:
    if drop:
        for c in PROJECTED:
            for d in target.query(c):
                target.delete(c, d["run"] if c == RUNS else f"{d['run']}:{d['node']}")
    events = sorted(source.query(EVENTS), key=lambda e: (e.get("time", ""), e.get("id", "")))
    for e in events:
        project({k: v for k, v in e.items() if k not in ("received_at", "bus_message_id")}, target)
    return len(events)


def check(store: StateStore) -> list[str]:
    """Differences between live projections and a fresh rebuild (empty = identical)."""
    fresh = MemoryStore()
    rebuild(store, fresh, drop=False)
    live, new = snapshot(store), snapshot(fresh)
    diffs = []
    for c in (*PROJECTED, ENGINES):
        for k in sorted(set(live[c]) | set(new[c])):
            if live[c].get(k) != new[c].get(k):
                diffs.append(f"{c}/{k}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    from mission_control import config

    argv = sys.argv[1:] if argv is None else argv
    store = config.load().adapter("store")
    if "--check" in argv:
        diffs = check(store)
        print("projections match a rebuild from the log" if not diffs else "differ: " + ", ".join(diffs[:20]))
        return 1 if diffs else 0
    n = rebuild(store, store)
    print(f"rebuilt {', '.join(PROJECTED)} from {n} events")
    return 0


if __name__ == "__main__":
    sys.exit(main())
