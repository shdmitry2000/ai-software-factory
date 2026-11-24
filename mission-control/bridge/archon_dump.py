#!/usr/bin/env python3
"""Read-only look into Archon's SQLite DB: run/event table schemas and the latest events.

    python3 archon_dump.py [--db ~/.archon/archon.db] [-n 10]

Opens the database read-only (SQLite URI mode=ro), so it can run beside a live engine.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def table(con: sqlite3.Connection, suffix: str) -> str:
    names = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    hits = [n for n in names if n.endswith(suffix)]
    if not hits:
        raise SystemExit(f"no table ending in {suffix!r}; tables: {', '.join(sorted(names))}")
    return hits[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("ARCHON_HOME", "~/.archon"), "archon.db"))
    ap.add_argument("-n", type=int, default=10)
    a = ap.parse_args()
    con = connect(Path(a.db).expanduser())
    for suffix in ("workflow_runs", "workflow_events"):
        t = table(con, suffix)
        cols = [f"{r['name']} {r['type']}" for r in con.execute(f"pragma table_info('{t}')")]
        count = con.execute(f"select count(*) from '{t}'").fetchone()[0]
        print(f"{t} ({count} rows): {', '.join(cols)}")
    ev = table(con, "workflow_events")
    print(f"\nlast {a.n} events:")
    for r in con.execute(f"select * from '{ev}' order by created_at desc limit ?", (a.n,)):
        d = dict(r)
        data = d.get("data")
        print(json.dumps({k: (v if k != "data" else (data[:160] + "…" if data and len(data) > 160 else data))
                          for k, v in d.items()}, default=str))


if __name__ == "__main__":
    main()
