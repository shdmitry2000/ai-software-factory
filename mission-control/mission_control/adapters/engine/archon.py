"""Archon EngineAdapter: Archon's SQLite rows → factory CloudEvents.

`to_events` is a pure function (rows in, events out) so the mapping is unit-tested on real
rows (tests/fixtures/archon-probe-run.json). `ArchonEngine` adds the read-only database
access and the cursor; it never writes to Archon's database.

Mapping rules:
- event id `archon:<Archon event id>`, deterministic, so a replay rewrites the same document;
- the run id (a UUID) is the trace id, each node invocation a span (W3C traceparent);
- times come from the millisecond timestamps inside `data` when present, else the row's
  second-resolution `created_at`;
- `run.started` is followed by `graph.declared` when the run's captured workflow source can be
  read (nodes and `depends_on` edges);
- Archon event types without a factory schema yet are counted in `skipped`, never guessed.

Settings (mission-control.yaml):
    adapter: archon
    db: ~/.archon/archon.db
    engine: factory-1
    product: file-to-markdown-convertor
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from mission_control.ports import Event
from mission_control.registry import register

RUNS = "remote_agent_workflow_runs"
EVENTS = "remote_agent_workflow_events"

# Archon event_type → factory type (without the dev.factory. prefix)
TYPES = {
    "workflow_started": "run.started",
    "workflow_completed": "run.completed",
    "workflow_failed": "run.failed",
    "workflow_cancelled": "run.cancelled",
    "node_started": "node.started",
    "node_completed": "node.completed",
    "node_failed": "node.failed",
    "node_skipped": "node.skipped",
    "node_skipped_prior_success": "node.reused",
    "node_suspended": "node.suspended",
}
FAILURE_CLASSES = {"fatal", "transient", "unknown", "timeout", "exec_failed", "output_contract",
                   "max_iterations", "child_failed", "cancelled", "config", "infrastructure"}

GraphLoader = Callable[[dict], "dict | None"]


def _iso(created_at: str) -> str:
    """Archon's 'YYYY-MM-DD HH:MM:SS' (UTC) → RFC 3339."""
    return datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _plus_ms(iso: str, ms: int) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")) + timedelta(milliseconds=ms)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _hex(s: str, n: int) -> str:
    h = "".join(c for c in (s or "").lower() if c in "0123456789abcdef")
    return (h + "0" * n)[:n] if h.strip("0") else "0" * (n - 1) + "1"


def _traceparent(run_id: str, span_seed: str) -> str:
    return f"00-{_hex(run_id, 32)}-{_hex(span_seed, 16)}-01"


def graph_from_source(run: dict) -> dict | None:
    """Nodes and edges from the workflow YAML Archon captured for this run, if readable."""
    try:
        meta = json.loads(run.get("metadata") or "{}")
        root = Path(meta["workflow_source"]["root"])
    except (KeyError, TypeError, ValueError):
        return None
    name = run.get("workflow_name")
    for path in sorted(root.rglob("*.y*ml")) if root.is_dir() else []:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(doc, dict) and doc.get("name") == name and isinstance(doc.get("nodes"), list):
            nodes, edges = [], []
            for n in doc["nodes"]:
                if not isinstance(n, dict) or "id" not in n:
                    continue
                kind = next((k for k in ("prompt", "bash", "script", "loop", "approval", "workflow", "command") if k in n), "")
                nodes.append({"id": str(n["id"]), **({"kind": kind} if kind else {})})
                for dep in n.get("depends_on") or []:
                    edges.append({"from": str(dep), "to": str(n["id"])})
            return {"workflow": name, "nodes": nodes, "edges": edges}
    return None


def _node_data(etype: str, data: dict, run: dict) -> dict:
    node = (data.get("node") or {}).get("id") or ""
    out: dict[str, Any] = {"workflow": run.get("workflow_name") or "", "node": node}
    attempt = data.get("attemptNumber") or (data.get("attempt") or {}).get("number")
    if isinstance(attempt, int) and attempt >= 1:
        out["attempt"] = attempt
    timing = data.get("timing") or {}
    spend = (data.get("spend") or {}).get("tokens") or {}
    if etype == "node_started":
        provider = (data.get("binding") or {}).get("provider") or data.get("provider")
        model = (data.get("binding") or {}).get("model") or data.get("model")
        if provider:
            out["agent"] = {"provider": str(provider), **({"model": str(model)} if model else {})}
        out["sandbox"] = "worktree:" + str(run.get("working_path") or "") if run.get("working_path") else "none:"
    elif etype == "node_completed":
        out["duration_ms"] = int(timing.get("durationMs") or 0)
        if isinstance(spend.get("value"), int):
            out["tokens"] = spend["value"]
    elif etype == "node_failed":
        fc = str(data.get("failureClass") or data.get("failure_class") or "unknown")
        out["failure_class"] = fc if fc in FAILURE_CLASSES else "unknown"
        if data.get("error"):
            out["summary"] = str(data["error"])[:2000]
        if timing.get("durationMs") is not None:
            out["duration_ms"] = int(timing["durationMs"])
    elif etype == "node_skipped_prior_success":
        out["from_attempt"] = int(data.get("priorAttempt") or 1)
    elif etype in ("node_skipped", "node_suspended") and data.get("reason"):
        out["reason"] = str(data["reason"])
    return out


def _run_data(etype: str, data: dict, run: dict) -> dict:
    out: dict[str, Any] = {"workflow": run.get("workflow_name") or data.get("workflowName") or "", "attempt": 1}
    if etype == "workflow_started":
        out["trigger"] = {"cli": "manual", "web": "manual", "trigger": "schedule"}.get(str(data.get("origin")), "manual")
    elif etype == "workflow_completed":
        out["duration_ms"] = int(data.get("duration_ms") or 0)
        outcome = (data.get("terminal_record") or {}).get("outcome")
        if outcome in ("succeeded", "failed"):
            out["outcome"] = outcome
    elif etype == "workflow_failed":
        rec = data.get("terminal_record") or {}
        fc = str(data.get("failure_class") or rec.get("failure_class") or "unknown")
        out["failure_class"] = fc if fc in FAILURE_CLASSES else "unknown"
        if rec.get("first_failed_node"):
            out["failed_node"] = str(rec["first_failed_node"])
        if data.get("error") or rec.get("error"):
            out["summary"] = str(data.get("error") or rec.get("error"))[:2000]
        if data.get("duration_ms") is not None:
            out["duration_ms"] = int(data["duration_ms"])
    elif etype == "workflow_cancelled" and data.get("reason"):
        out["reason"] = str(data["reason"])[:2000]
    return out


def _time(etype: str, data: dict, created_at: str) -> str:
    timing = data.get("timing") or {}
    # Archon's durationMs counts from the invocation's start, not the attempt's (~25 ms later);
    # using the attempt start put a node's end after its successor's start.
    started = (data.get("invocation") or {}).get("startedAt") or timing.get("startedAt")
    if etype == "node_started" and started:
        return started
    if etype in ("node_completed", "node_failed") and started and timing.get("durationMs") is not None:
        return _plus_ms(started, int(timing["durationMs"]))
    return _iso(created_at)


def to_events(runs: dict[str, dict], rows: Iterable[dict], *, engine: str, product: str = "",
              graph_loader: GraphLoader = graph_from_source) -> tuple[list[Event], dict[str, int]]:
    """Map Archon event rows (oldest first) to factory events; returns (events, skipped counts)."""
    events: list[Event] = []
    skipped: dict[str, int] = {}
    for row in rows:
        etype = row["event_type"]
        ftype = TYPES.get(etype)
        run = runs.get(row["workflow_run_id"], {})
        if ftype is None:
            skipped[etype] = skipped.get(etype, 0) + 1
            continue
        try:
            data = json.loads(row.get("data") or "{}")
        except ValueError:
            data = {}
        run_id = row["workflow_run_id"]
        is_node = ftype.startswith("node.")
        payload = _node_data(etype, data, run) if is_node else _run_data(etype, data, run)
        subject = f"run/{run_id}/node/{payload['node']}" if is_node else f"run/{run_id}"
        span_seed = ((data.get("invocation") or {}).get("id") or row["id"]) if is_node else row["id"]
        ev: Event = {
            "specversion": "1.0",
            "id": f"archon:{row['id']}",
            "source": f"factory://{engine}/archon",
            "type": f"dev.factory.{ftype}",
            "subject": subject,
            "time": _time(etype, data, row["created_at"]),
            "datacontenttype": "application/json",
            "factoryengine": engine,
            "factoryrun": run_id,
            "traceparent": _traceparent(run_id, span_seed),
            "data": payload,
        }
        if product:
            ev["factoryproduct"] = product
        if run.get("parent_run_id"):
            ev["factoryparentrun"] = run["parent_run_id"]
        events.append(ev)
        if etype == "workflow_started":
            graph = graph_loader(run) if run else None
            if graph:
                events.append({**{k: v for k, v in ev.items() if k != "data"},
                               "id": f"archon:graph-{run_id}", "type": "dev.factory.graph.declared",
                               "traceparent": _traceparent(run_id, "graph" + run_id), "data": graph})
    return events, skipped


class ArchonEngine:
    """Reads Archon's database read-only; the cursor is the events table's rowid."""

    def __init__(self, db: str, engine: str, product: str = "", slots: int = 1,
                 pin: str = "", graph_loader: GraphLoader = graph_from_source, app_dir: str = "") -> None:
        self.db = Path(os.path.expanduser(db))
        self.engine, self.product, self.slots, self.pin = engine, product, slots, pin
        # The product checkout: consumer.py halt/pause leave .factory/STOP and .factory/PAUSE there.
        self.app_dir = Path(os.path.expanduser(app_dir)) if app_dir else None
        self.graph_loader = graph_loader

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def events_since(self, cursor: str, *, limit: int = 500) -> tuple[list[Event], str]:
        after = int(cursor or 0)
        with self._connect() as con:
            rows = [dict(r) for r in con.execute(
                f"select rowid as _rowid, * from {EVENTS} where rowid > ? order by rowid limit ?", (after, limit))]
            if not rows:
                return [], str(after)
            ids = sorted({r["workflow_run_id"] for r in rows})
            marks = ",".join("?" * len(ids))
            runs = {r["id"]: dict(r) for r in con.execute(f"select * from {RUNS} where id in ({marks})", ids)}
        events, _ = to_events(runs, rows, engine=self.engine, product=self.product, graph_loader=self.graph_loader)
        return events, str(rows[-1]["_rowid"])

    def heartbeat(self) -> Event:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        busy = 0
        if self.db.exists():
            with self._connect() as con:
                busy = con.execute(f"select count(*) from {RUNS} where status in ('running','pending')").fetchone()[0]
        halted = paused = False
        if self.app_dir is not None:
            halted = (self.app_dir / ".factory" / "STOP").exists()
            paused = (self.app_dir / ".factory" / "PAUSE").exists()
        data: dict[str, Any] = {"slots": {"total": self.slots, "busy": int(busy)}, "halted": halted, "paused": paused}
        if self.pin:
            data["archon_pin"] = self.pin
        if self.product:
            data["product"] = self.product
        if self.app_dir is not None:                    # N1: the base the next lap starts from
            try:
                base = (self.app_dir / ".factory" / "base_branch").read_text(encoding="utf-8").strip()
            except OSError:
                base = ""
            if base:
                data["base_branch"] = base[:200]
        return {
            "specversion": "1.0", "id": f"bridge:{self.engine}:{now}", "source": f"factory://{self.engine}/bridge",
            "type": "dev.factory.engine.heartbeat", "subject": f"engine/{self.engine}", "time": now,
            "datacontenttype": "application/json", "factoryengine": self.engine, "data": data,
        }


@register("engine", "archon")
def make(db: str = "~/.archon/archon.db", engine: str = "", product: str = "", slots: int = 1,
         pin: str = "", app_dir: str = "", **_ignored) -> ArchonEngine:
    return ArchonEngine(db=db, engine=engine or os.uname().nodename.split(".")[0], product=product,
                        slots=int(slots), pin=pin, app_dir=app_dir)
