"""Projections: the readable state built from the event log.

    runs/{run}              one per run: workflow, engine, status, times, failure, graph
    nodes/{run}:{node}      one per node of a run: status, times, agent, sandbox, attempts
    engines/{engine}        one per engine: last heartbeat, slots, halted/paused, pin
    agents/{agent_id}       one per agent instance (VM, pod, laptop): last heartbeat and state
    compliance/{product}__{pr}  the latest advisory compliance report of a PR

Every rule is order-independent and idempotent, because the bus delivers late and twice:
- a terminal status always beats a non-terminal one; among non-terminal statuses the event
  with the later time wins; among terminal ones the later time wins too;
- start times keep the minimum, end and "last seen" times the maximum;
- nothing is counted incrementally (counts are computed when read).
So any order of the same events, applied any number of times, yields the same documents.
Collections are flat (no subcollections) so the model maps 1:1 onto a relational store.
"""
from __future__ import annotations

from typing import Any

from mission_control.ports import Event, StateStore

RUNS, NODES, ENGINES = "runs", "nodes", "engines"
AGENTS, COMPLIANCE = "agents", "compliance"
COMMANDS, DEPLOYMENTS = "commands", "deployments"
REPORT_FIELDS = ("product", "pr", "change", "head", "base", "title", "mode", "status", "rules",
                 "findings", "model", "effort", "cost_usd", "diff_truncated", "error")
TERMINAL_RUN = {"completed", "failed", "cancelled", "abandoned", "lost"}
TERMINAL_NODE = {"completed", "failed", "skipped", "reused"}

RUN_STATUS = {
    "run.started": "running", "run.resumed": "running", "run.paused": "paused",
    "run.completed": "completed", "run.failed": "failed", "run.cancelled": "cancelled",
    "run.abandoned": "abandoned", "run.lost": "lost",
}
NODE_STATUS = {
    "node.started": "running", "node.suspended": "suspended", "node.completed": "completed",
    "node.failed": "failed", "node.skipped": "skipped", "node.reused": "reused",
}


def _min(a: Any, b: Any) -> Any:
    return b if a is None else a if b is None else min(a, b)


def _max(a: Any, b: Any) -> Any:
    return b if a is None else a if b is None else max(a, b)


def _status(doc: dict, status: str, t: str, terminal: set[str]) -> None:
    """Set doc['status'] if this (status, time) beats the current one."""
    cur, cur_t = doc.get("status"), doc.get("status_time")
    new_key = (status in terminal, t, status)
    cur_key = (cur in terminal, cur_t or "", cur or "")
    if cur is None or new_key > cur_key:
        doc["status"], doc["status_time"] = status, t


def _base(doc: dict | None, **ids: Any) -> dict:
    out = dict(doc or {})
    for k, v in ids.items():
        if v:
            out[k] = v
    return out


def _apply_run(doc: dict | None, ev: Event, kind: str) -> dict:
    d, t, data = _base(doc, run=ev.get("factoryrun"), engine=ev.get("factoryengine"),
                       product=ev.get("factoryproduct"), work_item=ev.get("factoryworkitem"),
                       parent_run=ev.get("factoryparentrun")), ev["time"], ev.get("data", {})
    if data.get("workflow"):
        d["workflow"] = data["workflow"]
    d["last_event_at"] = _max(d.get("last_event_at"), t)
    if kind == "graph.declared":
        d["graph"] = {"nodes": data.get("nodes", []), "edges": data.get("edges", [])}
        return d
    status = RUN_STATUS[kind]
    _status(d, status, t, TERMINAL_RUN)
    d["attempt"] = _max(d.get("attempt"), data.get("attempt"))
    if kind == "run.started":
        d["started_at"] = _min(d.get("started_at"), t)
        if data.get("trigger"):
            d["trigger"] = data["trigger"]
    if status in TERMINAL_RUN:
        d["ended_at"] = _max(d.get("ended_at"), t)
        for k in ("duration_ms", "outcome", "failure_class", "failed_node", "summary", "tokens"):
            if data.get(k) is not None and d.get("status") == status:
                d[k] = data[k]
    if kind == "run.paused" and d.get("status") == "paused":
        d["waiting_for"] = data.get("waiting_for", "other")
    return d


def _apply_node(doc: dict | None, ev: Event, kind: str) -> dict:
    data = ev.get("data", {})
    d, t = _base(doc, run=ev.get("factoryrun"), node=data.get("node"), engine=ev.get("factoryengine"),
                 workflow=data.get("workflow")), ev["time"]
    status = NODE_STATUS[kind]
    _status(d, status, t, TERMINAL_NODE)
    d["attempts"] = _max(d.get("attempts"), data.get("attempt"))
    d["last_event_at"] = _max(d.get("last_event_at"), t)
    if kind == "node.started":
        d["started_at"] = _min(d.get("started_at"), t)
        for k in ("agent", "sandbox"):
            if data.get(k):
                d[k] = data[k]
    if status in TERMINAL_NODE or status == "suspended":
        d["ended_at"] = _max(d.get("ended_at"), t)
        for k in ("duration_ms", "tokens", "failure_class", "summary", "reason", "from_attempt"):
            if data.get(k) is not None and d.get("status") == status:
                d[k] = data[k]
    return d


def _apply_engine(doc: dict | None, ev: Event) -> dict:
    d, t, data = _base(doc, engine=ev.get("factoryengine")), ev["time"], ev.get("data", {})
    if t >= (d.get("last_heartbeat") or ""):
        d["last_heartbeat"] = t
        for k in ("slots", "halted", "paused", "archon_pin", "sandbox"):
            if k in data:
                d[k] = data[k]
    return d


def _apply_agent(doc: dict | None, ev: Event) -> dict:
    d, t, data = dict(doc or {}), ev["time"], ev.get("data", {})
    if t >= (d.get("last_heartbeat") or ""):
        d.update({k: v for k, v in data.items()})
        d["last_heartbeat"] = t
    return d


def _apply_report(doc: dict | None, ev: Event) -> dict:
    """The latest report wins (by time, then id), whatever order reports arrive in."""
    d = dict(doc or {})
    if (ev["time"], ev["id"]) >= (d.get("reported_at", ""), d.get("report_id", "")):
        data = ev.get("data", {})
        d = {k: data[k] for k in REPORT_FIELDS if k in data}
        d.update(reported_at=ev["time"], report_id=ev["id"], agent=ev.get("factoryengine", ""))
    return d


def _apply_command(doc: dict | None, ev: Event, kind: str) -> dict:
    """commands/{id}: what was asked, by whom, accepted or not, each step, the outcome.
    Order-independent: each event fills its own fields; steps are keyed by seq."""
    d, data, t = dict(doc or {}), ev.get("data", {}), ev["time"]
    d["command_id"] = data.get("command_id")
    if kind == "command.issued":
        d.update({k: data[k] for k in ("action", "target", "args", "who", "reason", "via") if k in data})
        d["issued_at"] = t
        d.setdefault("status", "issued")
    elif kind == "command.acknowledged":
        d.update(accepted=data.get("accepted"), acked_at=t, handler=data.get("handler", ""))
        if data.get("refusal"):
            d["refusal"] = data["refusal"]
        if d.get("status") in (None, "issued"):
            d["status"] = "running" if data.get("accepted") else "refused"
    elif kind == "deployment.step":
        steps = dict(d.get("steps") or {})
        steps[f"{data['seq']:03d}"] = {"step": data.get("step"), "ok": data.get("ok"),
                                       "detail": data.get("detail", ""), "time": t}
        d["steps"] = steps
    elif kind == "deployment.changed":
        d.update(finished_at=t, ok=data.get("ok"), status="succeeded" if data.get("ok") else "failed")
        if data.get("error"):
            d["error"] = data["error"]
    return d


def _apply_deployment(doc: dict | None, ev: Event) -> dict:
    """deployments/{id}: the latest outcome wins (by time, then id)."""
    d, data = dict(doc or {}), ev.get("data", {})
    if (ev["time"], ev["id"]) < (d.get("updated_at", ""), d.get("event_id", "")):
        return d
    removed = data.get("action") == "deployment.remove" and data.get("ok")
    d.update(deployment=data["deployment"], host=data.get("host", ""), last_action=data.get("action"),
             last_ok=data.get("ok"), last_command=data.get("command_id"), instances=data.get("instances", []),
             state="removed" if removed else _rollup(data.get("instances", [])),
             updated_at=ev["time"], event_id=ev["id"])
    for k in ("digest", "image", "error"):
        if k in data:
            d[k] = data[k]
        elif k == "error":
            d.pop("error", None)
    return d


def _rollup(instances: list[dict]) -> str:
    states = {i.get("state") for i in instances}
    if not instances:
        return "none"
    return "running" if states == {"running"} else "stopped" if "running" not in states else "partial"


def compliance_key(product: str, pr: int) -> str:
    return f"{product}__{pr}"


def project(ev: Event, store: StateStore) -> list[str]:
    """Apply one event to the projections; returns the documents touched (collection/id)."""
    kind = str(ev.get("type", "")).removeprefix("dev.factory.")
    run = ev.get("factoryrun", "")
    if kind in RUN_STATUS or kind == "graph.declared":
        if not run:
            return []
        store.update(RUNS, run, lambda doc: _apply_run(doc, ev, kind))
        return [f"{RUNS}/{run}"]
    if kind in NODE_STATUS:
        node = (ev.get("data") or {}).get("node")
        if not (run and node):
            return []
        key = f"{run}:{node}"
        store.update(NODES, key, lambda doc: _apply_node(doc, ev, kind))
        return [f"{NODES}/{key}"]
    if kind == "engine.heartbeat" and ev.get("factoryengine"):
        store.update(ENGINES, ev["factoryengine"], lambda doc: _apply_engine(doc, ev))
        return [f"{ENGINES}/{ev['factoryengine']}"]
    if kind == "agent.heartbeat":
        aid = (ev.get("data") or {}).get("agent_id")
        if not aid:
            return []
        store.update(AGENTS, aid, lambda doc: _apply_agent(doc, ev))
        return [f"{AGENTS}/{aid}"]
    if kind == "compliance.reported":
        data = ev.get("data") or {}
        if not (data.get("product") and data.get("pr")):
            return []
        key = compliance_key(data["product"], data["pr"])
        store.update(COMPLIANCE, key, lambda doc: _apply_report(doc, ev))
        return [f"{COMPLIANCE}/{key}"]
    if kind in ("command.issued", "command.acknowledged", "deployment.step", "deployment.changed"):
        data = ev.get("data") or {}
        touched = []
        if data.get("command_id"):
            store.update(COMMANDS, data["command_id"], lambda doc: _apply_command(doc, ev, kind))
            touched.append(f"{COMMANDS}/{data['command_id']}")
        if kind == "deployment.changed" and data.get("deployment"):
            store.update(DEPLOYMENTS, data["deployment"], lambda doc: _apply_deployment(doc, ev))
            touched.append(f"{DEPLOYMENTS}/{data['deployment']}")
        return touched
    return []
