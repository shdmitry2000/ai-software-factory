"""Read models for the console screens (UI-1): plain functions over the StateStore
and the products files, so they are testable without a web server.

Problems are derived here from the projections, with the rules below, until the
scheduled problem rules (plan F7) write a `problems` collection.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mission_control import products as products_mod
from mission_control.ports import StateStore

SILENT_S = 180                 # engine or agent heartbeat older than this: silent
STUCK_S = 3600                 # a running run with no event for this long: stuck
FAILED_WINDOW_S = 24 * 3600    # failed runs shown as problems for a day
RUN_LIMIT = 150
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
FINDING_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def now() -> datetime:
    return datetime.now(timezone.utc)


def age_s(t: str | None, at: datetime) -> float | None:
    try:
        return (at - datetime.fromisoformat(str(t).replace("Z", "+00:00"))).total_seconds()
    except (TypeError, ValueError):
        return None


def ago(t: str | None, at: datetime) -> str:
    s = age_s(t, at)
    if s is None:
        return "never"
    s = max(0, int(s))
    if s < 120:
        return f"{s}s ago"
    if s < 7200:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def duration(ms: int | float | None) -> str:
    if not ms:
        return ""
    s = int(ms) // 1000
    return f"{s}s" if s < 120 else f"{s // 60}m {s % 60:02d}s" if s < 7200 else f"{s // 3600}h {s % 3600 // 60:02d}m"


# ---------------------------------------------------------------------- engines and agents
def engines(store: StateStore, at: datetime) -> list[dict]:
    out = []
    for e in sorted(store.query("engines"), key=lambda e: e.get("engine", "")):
        a = age_s(e.get("last_heartbeat"), at)
        out.append({**e, "alive": a is not None and a < SILENT_S, "ago": ago(e.get("last_heartbeat"), at)})
    return out


def agents(store: StateStore, at: datetime) -> list[dict]:
    out = []
    for x in sorted(store.query("agents"), key=lambda x: x.get("agent_id", "")):
        a = age_s(x.get("last_heartbeat"), at)
        out.append({**x, "fresh": a is not None and a < SILENT_S, "ago": ago(x.get("last_heartbeat"), at)})
    return out


def deployments(store: StateStore, at: datetime) -> list[dict]:
    return [{**d, "ago": ago(d.get("updated_at"), at)}
            for d in sorted(store.query("deployments"), key=lambda d: d.get("deployment", ""))]


def commands(store: StateStore, at: datetime, limit: int = 5) -> list[dict]:
    return [{**c, "ago": ago(c.get("issued_at"), at)}
            for c in store.query("commands", order_by="issued_at", descending=True, limit=limit)]


# ---------------------------------------------------------------------- compliance
def reports(store: StateStore, at: datetime, *, product: str = "", status: str = "") -> list[dict]:
    rows = store.query("compliance")
    out = []
    for r in rows:
        if product and r.get("product") != product:
            continue
        if status and r.get("status") != status:
            continue
        findings = sorted(r.get("findings") or [], key=lambda f: FINDING_ORDER.get(f.get("severity"), 9))
        out.append({**r, "findings": findings, "n_findings": len(findings),
                    "top_severity": findings[0]["severity"] if findings else "",
                    "rules_version": (r.get("rules") or {}).get("version", ""),
                    "rules_count": (r.get("rules") or {}).get("count", 0),
                    "ago": ago(r.get("reported_at"), at), "short_head": str(r.get("head", ""))[:8]})
    return sorted(out, key=lambda r: (r.get("product", ""), -int(r.get("pr") or 0)))


# ---------------------------------------------------------------------- runs
def runs(store: StateStore, at: datetime, *, status: str = "", product: str = "", workflow: str = "",
         limit: int = RUN_LIMIT) -> list[dict]:
    """Newest first; retries grouped under their first attempt (`chain` = later attempts)."""
    rows = store.query("runs", order_by="started_at", descending=True, limit=limit)
    by_id = {r.get("run"): r for r in rows}

    def root(r: dict) -> str:
        seen = set()
        while r.get("parent_run") and r["parent_run"] in by_id and r["run"] not in seen:
            seen.add(r["run"])
            r = by_id[r["parent_run"]]
        return r.get("run", "")

    def shape(r: dict) -> dict:
        silent = age_s(r.get("last_event_at"), at)
        return {**r, "ago": ago(r.get("started_at"), at), "took": duration(r.get("duration_ms")),
                "stuck": r.get("status") == "running" and silent is not None and silent > STUCK_S}

    groups: dict[str, dict] = {}
    for r in rows:
        if status and r.get("status") != status:
            continue
        if product and r.get("product") != product:
            continue
        if workflow and r.get("workflow") != workflow:
            continue
        rid = root(r)
        g = groups.setdefault(rid, {"root": None, "chain": []})
        if r.get("run") == rid:
            g["root"] = shape(r)
        else:
            g["chain"].append(shape(r))
    out = []
    for rid, g in groups.items():
        head = g["root"] or g["chain"].pop()          # root filtered out or beyond the limit
        head["chain"] = sorted(g["chain"], key=lambda r: r.get("started_at", ""))
        out.append(head)
    return sorted(out, key=lambda r: r.get("started_at", ""), reverse=True)


def run_detail(store: StateStore, run_id: str, at: datetime) -> dict | None:
    r = store.get("runs", run_id)
    if r is None:
        return None
    nodes = store.query("nodes", where=[("run", "==", run_id)])
    nodes = sorted(nodes, key=lambda n: (n.get("started_at") or "~", n.get("node", "")))
    for n in nodes:
        n["took"] = duration(n.get("duration_ms"))
    return {**r, "ago": ago(r.get("started_at"), at), "took": duration(r.get("duration_ms")), "nodes": nodes}


def run_filters(store: StateStore) -> dict:
    rows = store.query("runs", order_by="started_at", descending=True, limit=RUN_LIMIT)
    return {k: sorted({r.get(k) for r in rows if r.get(k)}) for k in ("status", "product", "workflow")}


# ---------------------------------------------------------------------- products
def products(store: StateStore, at: datetime, products_dir: Path) -> list[dict]:
    try:
        items = list(products_mod.load_all(products_dir).values())
    except Exception:                                # a broken file must not take the page down
        items = []
    all_reports = reports(store, at)
    all_runs = runs(store, at, limit=RUN_LIMIT)
    out = []
    for p in sorted(items, key=lambda p: p.name):
        rep = [r for r in all_reports if r.get("product") == p.name]
        rn = [r for r in all_runs if r.get("product") == p.name]
        out.append({"name": p.name, "repo": p.repo, "base_branch": p.base_branch, "codehost": p.codehost,
                    "compliance": p.compliance, "reports": rep, "runs": rn[:5],
                    "open_prs": len(rep), "findings": sum(r["n_findings"] for r in rep),
                    "last_run": rn[0] if rn else None})
    return out


# ---------------------------------------------------------------------- problems
@dataclass(frozen=True)
class Problem:
    severity: str          # critical | warning | info
    kind: str
    title: str
    detail: str = ""
    link: str = ""
    since: str = ""


def problems(store: StateStore, at: datetime) -> list[Problem]:
    out: list[Problem] = []
    for e in engines(store, at):
        if not e["alive"]:
            out.append(Problem("critical", "engine.silent", f"Engine {e.get('engine')} is silent",
                               f"last heartbeat {e['ago']}", "/agents", e.get("last_heartbeat", "")))
        elif e.get("halted"):
            out.append(Problem("warning", "engine.halted", f"Engine {e.get('engine')} is halted",
                               "no new laps start until it is unhalted", "/agents"))
    for a in agents(store, at):
        if not a["fresh"] and a.get("state") != "stopped":
            out.append(Problem("warning", "agent.silent", f"Agent {a.get('agent_id')} is silent",
                               f"last heartbeat {a['ago']}", "/agents", a.get("last_heartbeat", "")))
    for d in deployments(store, at):
        if d.get("state") not in ("running", "removed"):
            out.append(Problem("warning", "deployment.down", f"Deployment {d.get('deployment')} is {d.get('state')}",
                               f"last action {d.get('last_action')} {d['ago']}", "/", d.get("updated_at", "")))
        if d.get("last_ok") is False:
            out.append(Problem("critical", "deployment.failed",
                               f"Last {d.get('last_action')} on {d.get('deployment')} failed",
                               str(d.get("error", ""))[:300], "/audit", d.get("updated_at", "")))
    for r in runs(store, at):
        last = r["chain"][-1] if r["chain"] else r       # only the latest attempt counts
        age = age_s(last.get("ended_at"), at)
        if last.get("status") == "failed" and age is not None and age < FAILED_WINDOW_S:
            out.append(Problem("warning", "run.failed", f"Run {last.get('workflow') or ''} failed",
                               f"{last.get('failure_class') or ''} at {last.get('failed_node') or '?'} · {last.get('product') or ''}",
                               f"/runs/{last.get('run')}", last.get("ended_at", "")))
        if last.get("stuck"):
            out.append(Problem("critical", "run.stuck", f"Run {last.get('workflow') or ''} looks stuck",
                               f"no event for over {STUCK_S // 60} min", f"/runs/{last.get('run')}",
                               last.get("last_event_at", "")))
    for rep in reports(store, at):
        if rep.get("status") in ("error", "capped"):
            out.append(Problem("warning", f"compliance.{rep['status']}",
                               f"Compliance {rep['status']} on {rep.get('product')} #{rep.get('pr')}",
                               str(rep.get("error", ""))[:300], "/compliance", rep.get("reported_at", "")))
        elif rep.get("top_severity") == "high":
            out.append(Problem("info", "compliance.high", f"High-severity finding on {rep.get('product')} #{rep.get('pr')}",
                               rep["findings"][0].get("title", ""), "/compliance", rep.get("reported_at", "")))
    return sorted(out, key=lambda p: (SEVERITY_ORDER[p.severity], p.since or "~"))
