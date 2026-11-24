"""Read models for the console screens (UI-1): plain functions over the StateStore
and the products files, so they are testable without a web server.

Problems are derived here from the projections, with the rules below. The same rules
run on a schedule in the writer (`mission_control/alerts.py`, plan F7), which records
each opening and clearing in the `problems` collection and sends alerts.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
BASE_GRACE_S = 180             # a base-branch change reaches the engine's heartbeat within this
GIT_STALE_S = 900              # no successful poll of a repository for this long: stale
CHECK_FAILED = {"failure", "timed_out", "cancelled", "action_required", "stale"}
RULES_SILENT_S = 900           # the scheduled problem check has not run for this long
RULES_DOC = ("problem_rules", "latest")


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
    """Agent instances; `retired` = its container is no longer part of any deployment
    (removed, scaled down, or its host removed), so its silence is expected."""
    live: set[tuple[str, str]] = set()
    for d in store.query("deployments"):
        host = d.get("host") or str(d.get("deployment", "")).split("@")[-1]
        live |= {(host, i.get("name", "")) for i in d.get("instances") or []} if d.get("state") != "removed" else set()
    out = []
    for x in sorted(store.query("agents"), key=lambda x: x.get("agent_id", "")):
        a = age_s(x.get("last_heartbeat"), at)
        loc = str(x.get("location", ""))
        retired = False
        if loc.startswith("docker:"):
            host, _, name = loc[len("docker:"):].partition("/")
            retired = (host, name) not in live
        out.append({**x, "fresh": a is not None and a < SILENT_S, "ago": ago(x.get("last_heartbeat"), at),
                    "retired": retired})
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
def checks_summary(pr: dict) -> dict:
    """The checks on a PR's current head: counts and the failing names."""
    runs = ((pr.get("checks") or {}).get(pr.get("head", "")) or {})
    failed = sorted(n for n, r in runs.items() if r.get("conclusion") in CHECK_FAILED)
    passed = sum(1 for r in runs.values() if r.get("conclusion") == "success")
    state = "none" if not runs else "failing" if failed else "passing" if passed == len(runs) else "mixed"
    return {"total": len(runs), "passed": passed, "failed": failed, "state": state,
            "runs": [{"name": n, **r} for n, r in sorted(runs.items())]}


def git_stale(repo: dict, at: datetime) -> bool:
    interval = int(((repo.get("poll") or {}).get("interval_s")) or 60)
    a = age_s(repo.get("git_seen_at"), at)
    return a is None or a >= max(GIT_STALE_S, 3 * interval)


def products(store: StateStore, at: datetime, products_dir: Path, prompts=None,
             problem_list: list | None = None) -> list[dict]:
    """Each product with its current settings (the file's values, overridden by console
    changes: products/{name}.effective) and its rollups: open PRs with checks and the
    compliance review of each head, open issues, branches, last run, problems, and when
    the git poller last read the repository (`git_seen_at`)."""
    try:
        items = list(products_mod.load_all(products_dir).values())
    except Exception:                                # a broken file must not take the page down
        items = []
    docs = {d.get("product"): d for d in store.query("products")}
    repos = {d.get("product"): d for d in store.query("repos")}
    all_pulls, all_items = store.query("pullrequests"), store.query("workitems")
    all_engines = store.query("engines")
    all_reports = reports(store, at)
    all_runs = runs(store, at, limit=RUN_LIMIT)
    out = []
    for p in sorted(items, key=lambda p: p.name):
        doc = docs.get(p.name) or {}
        p = products_mod.apply_effective(p, doc.get("effective"))
        overrides = {f: {**o, "ago": ago(o.get("time"), at)} for f, o in (doc.get("overrides") or {}).items()
                     if o.get("time", "") >= doc.get("seed_time", "")}
        history = [{**h, "ago": ago(h.get("time"), at)} for h in reversed(doc.get("history") or [])][:10]
        labels: list[str] = []
        if prompts is not None:
            try:
                labels = sorted(prompts.labels(p.compliance.rules))
            except Exception:
                labels = []
        if p.compliance.label not in labels:
            labels.append(p.compliance.label)
        rep = [r for r in all_reports if r.get("product") == p.name]
        by_pr = {int(r.get("pr") or 0): r for r in rep}
        rn = [r for r in all_runs if r.get("product") == p.name]
        repo = repos.get(p.name) or {}
        mine = [x for x in all_pulls if x.get("product") == p.name and x.get("state")]
        polled = bool(repo.get("git_seen_at") or mine)
        open_pulls, closed_pulls = [], []
        for x in sorted(mine, key=lambda x: -int(x.get("pr") or 0)):
            r = by_pr.get(int(x.get("pr") or 0))
            row = {**x, "short_head": str(x.get("head", ""))[:8], "ago": ago(x.get("opened_at"), at),
                   "checks": checks_summary(x), "report": r,
                   "review": "none" if not r else "current" if r.get("head") == x.get("head") else "outdated"}
            if x.get("state") == "open":
                open_pulls.append(row)
            else:
                closed_pulls.append({**row, "closed_ago": ago(x.get("closed_at"), at)})
        closed_pulls.sort(key=lambda x: x.get("closed_at") or "", reverse=True)
        if not polled:                               # no git data yet: the PRs the compliance agent reviewed
            open_pulls = [{"pr": r.get("pr"), "title": r.get("title"), "head": r.get("head"), "short_head": r["short_head"],
                           "base": r.get("base"), "checks": checks_summary({}), "report": r, "review": "current",
                           "ago": ""} for r in rep]
        open_items = sorted((w for w in all_items if w.get("product") == p.name and w.get("state") == "open"),
                            key=lambda w: -int(w.get("number") or 0))
        branches = {b: v for b, v in (repo.get("branches") or {}).items() if not v.get("deleted")}
        base = branches.get(p.base_branch) or {}
        open_rep = [row["report"] for row in open_pulls if row.get("report")]
        poll = repo.get("poll") or {}
        prob = [x for x in (problem_list or []) if getattr(x, "product", "") == p.name]
        engine_bases = sorted({(e.get("engine", ""), e.get("base_branch", "")) for e in all_engines
                               if e.get("product") == p.name and e.get("base_branch")})
        out.append({"name": p.name, "repo": p.repo, "base_branch": p.base_branch, "codehost": p.codehost,
                    "compliance": p.compliance, "reports": rep, "runs": rn[:5],
                    "pulls": open_pulls, "closed_pulls": closed_pulls[:5],
                    "open_prs": len(open_pulls), "findings": sum(r.get("n_findings", 0) for r in open_rep),
                    "failing_checks": sum(1 for row in open_pulls if row["checks"]["state"] == "failing"),
                    "unreviewed": sum(1 for row in open_pulls if row["review"] != "current"),
                    "issues": [{**w, "ago": ago(w.get("opened_at"), at)} for w in open_items[:10]],
                    "open_issues": len(open_items), "branches": len(branches),
                    "base_head": str(base.get("head", ""))[:8], "base_ago": ago(base.get("time"), at) if base else "",
                    "last_push_ago": ago(repo.get("last_push_at"), at) if repo.get("last_push_at") else "",
                    "polled": polled, "git_seen_ago": ago(repo.get("git_seen_at"), at),
                    "git_stale": polled and git_stale(repo, at), "git_error": "" if poll.get("ok", True) else poll.get("error", ""),
                    "rate": f"{poll['rate_remaining']}/{poll['rate_limit']}" if poll.get("rate_limit") else "",
                    "problems": len(prob), "engine_bases": [{"engine": n, "base": b, "ok": b == p.base_branch}
                                                            for n, b in engine_bases],
                    "last_run": rn[0] if rn else None, "overrides": overrides, "history": history,
                    "labels": labels, "modes": sorted(products_mod.MODES),
                    "registered": bool(doc.get("seed_hash"))})
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
    key: str = ""          # what it is about: engine, agent, deployment, run, product__pr
    acked_by: str = ""
    ack_reason: str = ""
    product: str = ""      # the product it concerns, when it concerns one (Projects rollup)

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.key}"


def _gone(h: dict) -> bool:
    """A VM host that was removed (or never had a desired state from a host command)."""
    return h.get("state") == "removed" or (h.get("host") != "local" and h.get("kind") == "vm" and not h.get("desired"))


def rule_problems(store: StateStore, at: datetime, spec_hosts: set[str] | None = None) -> list[Problem]:
    """Every problem the rules see now, before operator acknowledgements and resolutions."""
    out: list[Problem] = []
    for e in engines(store, at):
        if not e["alive"]:
            out.append(Problem("critical", "engine.silent", f"Engine {e.get('engine')} is silent",
                               f"last heartbeat {e['ago']}", "/agents", e.get("last_heartbeat", ""), e.get("engine", "")))
        elif e.get("halted"):
            out.append(Problem("warning", "engine.halted", f"Engine {e.get('engine')} is halted",
                               f"no new laps start until it is unhalted{' · by ' + e['halt_by'] if e.get('halt_by') else ''}{': ' + e['halt_reason'] if e.get('halt_reason') else ''}",
                               "/agents", e.get("halt_changed_at", ""), e.get("engine", "")))
    for a in agents(store, at):
        if not a["fresh"] and a.get("state") != "stopped" and not a["retired"]:
            out.append(Problem("warning", "agent.silent", f"Agent {a.get('agent_id')} is silent",
                               f"last heartbeat {a['ago']}", "/agents", a.get("last_heartbeat", ""), a.get("agent_id", "")))
    for d in deployments(store, at):
        if d.get("state") not in ("running", "removed"):
            out.append(Problem("warning", "deployment.down", f"Deployment {d.get('deployment')} is {d.get('state')}",
                               f"last action {d.get('last_action')} {d['ago']}", "/agents", d.get("updated_at", ""), d.get("deployment", "")))
        if d.get("last_ok") is False:
            out.append(Problem("critical", "deployment.failed",
                               f"Last {d.get('last_action')} on {d.get('deployment')} failed",
                               str(d.get("error", ""))[:300], "/audit", d.get("updated_at", ""), d.get("deployment", "")))
    for h in store.query("hosts"):
        hid, a = h.get("host", ""), age_s(h.get("last_heartbeat"), at)
        if _gone(h) and not (spec_hosts and hid in spec_hosts):
            continue
        health = h.get("health") or {}
        if h.get("last_heartbeat") and (a is None or a >= SILENT_S):
            out.append(Problem("critical" if hid == "local" else "warning", "host.silent", f"Host {hid} is silent",
                               f"last heartbeat {ago(h.get('last_heartbeat'), at)}", "/hosts",
                               h.get("last_heartbeat", ""), hid))
        elif h.get("state") == "unreachable":
            out.append(Problem("warning", "host.unreachable", f"Host {hid} is unreachable",
                               str(health.get("error", ""))[:300], "/hosts", h.get("last_heartbeat", "")[:13], hid))
        if (health.get("disk_used_pct") or 0) >= HOST_DISK_WARN:
            out.append(Problem("warning", "host.disk", f"Host {hid} disk {health['disk_used_pct']:.0f}% full",
                               f"{health.get('disk_total_gb', '?')} GB disk", "/hosts",
                               h.get("last_heartbeat", "")[:10], hid))
        if h.get("last_ok") is False and h.get("last_action"):
            out.append(Problem("critical", "host.failed", f"Last {h.get('last_action')} on host {hid} failed",
                               str(h.get("error", ""))[:300], "/hosts", h.get("changed_at", ""), hid))
    for r in runs(store, at):
        last = r["chain"][-1] if r["chain"] else r       # only the latest attempt counts
        age = age_s(last.get("ended_at"), at)
        if last.get("status") == "failed" and age is not None and age < FAILED_WINDOW_S:
            out.append(Problem("warning", "run.failed", f"Run {last.get('workflow') or ''} failed",
                               f"{last.get('failure_class') or ''} at {last.get('failed_node') or '?'} · {last.get('product') or ''}",
                               f"/runs/{last.get('run')}", last.get("ended_at", ""), last.get("run", ""),
                               product=last.get("product") or ""))
        if last.get("stuck"):
            out.append(Problem("critical", "run.stuck", f"Run {last.get('workflow') or ''} looks stuck",
                               f"no event for over {STUCK_S // 60} min", f"/runs/{last.get('run')}",
                               last.get("last_event_at", ""), last.get("run", ""), product=last.get("product") or ""))
    pulls = {f"{x.get('product')}__{x.get('pr')}": x for x in store.query("pullrequests")}
    for rep in reports(store, at):
        key = f"{rep.get('product')}__{rep.get('pr')}"
        if (pulls.get(key) or {}).get("state") in ("merged", "closed"):
            continue                                   # the PR is gone; its last review no longer needs anyone
        if rep.get("status") in ("error", "capped"):
            out.append(Problem("warning", f"compliance.{rep['status']}",
                               f"Compliance {rep['status']} on {rep.get('product')} #{rep.get('pr')}",
                               str(rep.get("error", ""))[:300], "/compliance", rep.get("reported_at", ""),
                               key, product=rep.get("product", "")))
        elif rep.get("top_severity") == "high":
            out.append(Problem("info", "compliance.high", f"High-severity finding on {rep.get('product')} #{rep.get('pr')}",
                               rep["findings"][0].get("title", ""), "/compliance", rep.get("reported_at", ""),
                               key, product=rep.get("product", "")))
    for key, x in sorted(pulls.items()):
        if x.get("state") != "open":
            continue
        cs = checks_summary(x)
        if cs["failed"]:
            out.append(Problem("info", "pullrequest.checks_failed",
                               f"Checks failing on {x.get('product')} #{x.get('pr')}",
                               ", ".join(cs["failed"])[:300], "/projects", str(x.get("head", ""))[:12], key,
                               product=x.get("product", "")))
    wanted = {}
    for pdoc in store.query("products"):
        b = (pdoc.get("effective") or {}).get("base_branch")
        if b:
            wanted[pdoc.get("product")] = (b, max([o.get("time", "") for o in (pdoc.get("overrides") or {}).values()]
                                                  + [pdoc.get("seed_time", "")]))
    for e in engines(store, at):
        prod, base = e.get("product", ""), e.get("base_branch", "")
        if not (prod in wanted and base and e.get("alive")):
            continue
        want, since = wanted[prod]
        a = age_s(since, at)
        if base != want and (a is None or a >= BASE_GRACE_S):
            out.append(Problem("warning", "engine.base_mismatch",
                               f"Engine {e.get('engine')} starts laps from {base}, not {want}",
                               f"{prod}'s base branch is {want}; the command handler writes it to the engine's checkout",
                               "/projects", since, f"{e.get('engine')}:{prod}", product=prod))
    for repo in store.query("repos"):
        name, poll = repo.get("product", ""), repo.get("poll") or {}
        if not poll.get("ok", True):
            out.append(Problem("critical" if poll.get("error_kind") == "auth" else "warning", "git.error",
                               f"Reading {repo.get('repo') or name} failed",
                               str(poll.get("error", ""))[:300], "/projects", repo.get("error_at", "")[:13], name, product=name))
        elif repo.get("git_seen_at") and git_stale(repo, at):
            out.append(Problem("warning", "git.stale", f"Git data for {name} is stale",
                               f"last read {ago(repo.get('git_seen_at'), at)}; is mission-control-poller running?",
                               "/projects", repo.get("git_seen_at", ""), name, product=name))
    rules = store.get(*RULES_DOC)
    if rules and rules.get("at") and (age_s(rules["at"], at) or 0) >= RULES_SILENT_S:
        out.append(Problem("warning", "rules.silent", "The scheduled problem check has stopped",
                           f"last ran {ago(rules['at'], at)}; problems still show here, but no alerts go out "
                           "(Cloud Scheduler job factory-problem-rules → writer /rules)",
                           "/problems", rules["at"], "scheduler"))
    return out


def problems(store: StateStore, at: datetime, spec_hosts: set[str] | None = None) -> list[Problem]:
    """What the Problems screen shows: the rules' problems with the operator's
    acknowledgements applied and this occurrence's resolutions hidden."""
    out = rule_problems(store, at, spec_hosts)
    states = {d.get("problem_id"): d for d in store.query("problem_states")}
    shown: list[Problem] = []
    for p in out:
        st = states.get(p.id) or {}
        res, ack = st.get("resolved") or {}, st.get("ack") or {}
        if res and res.get("since", "") == p.since:
            continue                                   # resolved by an operator for this occurrence
        if ack and ack.get("since", "") == p.since:
            p = replace(p, acked_by=ack.get("who", ""), ack_reason=ack.get("reason", ""))
        shown.append(p)
    return sorted(shown, key=lambda p: (bool(p.acked_by), SEVERITY_ORDER[p.severity], p.since or "~"))


def rule_log(store: StateStore, at: datetime, limit: int = 20) -> dict:
    """The scheduled rules (F7): when they last ran, and the problems they opened or
    cleared most recently (the `problems` collection)."""
    last = store.get(*RULES_DOC) or {}
    rows = sorted(store.query("problems"), key=lambda d: d.get("changed_at", ""), reverse=True)[:limit]
    for d in rows:
        d["ago"] = ago(d.get("changed_at"), at)
        d["opened_ago"] = ago(d.get("opened_at"), at)
    return {"ran": bool(last.get("at")), "ago": ago(last.get("at"), at), "last": last,
            "late": bool(last.get("at")) and (age_s(last.get("at"), at) or 0) >= RULES_SILENT_S,
            "recent": rows, "alerted": {d["problem_id"] for d in rows if d.get("state") == "open" and d.get("alerted")}}


# ---------------------------------------------------------------------- audit
def audit(store: StateStore, at: datetime, *, who: str = "", action: str = "", status: str = "",
          limit: int = 200) -> list[dict]:
    rows = store.query("commands", order_by="issued_at", descending=True, limit=limit)
    out = []
    for c in rows:
        if who and c.get("who") != who:
            continue
        if action and not str(c.get("action", "")).startswith(action):
            continue
        if status and c.get("status") != status:
            continue
        steps = [v for _, v in sorted((c.get("steps") or {}).items())]
        out.append({**c, "ago": ago(c.get("issued_at"), at), "n_steps": len(steps),
                    "result": c.get("refusal") or c.get("error") or c.get("detail") or ""})
    return out


def audit_filters(store: StateStore) -> dict:
    rows = store.query("commands", order_by="issued_at", descending=True, limit=200)
    return {"who": sorted({r.get("who") for r in rows if r.get("who")}),
            "action": sorted({str(r.get("action", "")).split(".")[0] for r in rows if r.get("action")}),
            "status": sorted({r.get("status") for r in rows if r.get("status")})}


# ---------------------------------------------------------------------- hosts, deploy, chains (UI-3)
HOST_DISK_WARN = 85.0


def hosts(store: StateStore, at: datetime, spec=None) -> list[dict]:
    """Every host: from deploy/hosts.yaml and the hosts collection (heartbeats, console-created)."""
    docs = {h.get("host"): h for h in store.query("hosts")}
    ids = set(docs) | (set(spec.hosts) if spec else set())
    deps = deployments(store, at)
    out = []
    for hid in sorted(ids, key=lambda h: (h != "local", h)):
        doc = docs.get(hid) or {}
        if _gone(doc) and not (spec and hid in spec.hosts):
            continue
        hs = spec.hosts.get(hid) if spec else None
        a = age_s(doc.get("last_heartbeat"), at)
        health = doc.get("health") or {}
        out.append({"host": hid, "kind": doc.get("kind") or (hs.kind if hs else ""), "doc": doc,
                    "state": doc.get("state") or "unknown", "health": health,
                    "fresh": a is not None and a < SILENT_S, "ago": ago(doc.get("last_heartbeat"), at),
                    "in_git": hs is not None, "desired": doc.get("desired") or {},
                    "address": doc.get("address", ""),
                    "deployments": [d for d in deps if (d.get("host") or str(d.get("deployment", "")).split("@")[-1]) == hid and d.get("state") != "removed"]})
    return out


def deploy(store: StateStore, at: datetime, spec) -> dict:
    """Components and deployments: what runs (deployments collection) next to what git
    asks for (deploy/deployments.yaml), with the last digests for upgrade and roll back."""
    docs = {d.get("deployment"): d for d in deployments(store, at)}
    ids = set(docs) | (set(spec.deployments) if spec else set())
    rows = []
    for did in sorted(ids):
        doc = docs.get(did) or {"deployment": did, "state": "not deployed", "ago": ""}
        if doc.get("state") == "removed" and not (spec and did in spec.deployments):
            continue
        git = spec.deployments.get(did) if spec else None
        desired = doc.get("desired") or {}
        differs = []
        if git and desired:
            if int(desired.get("replicas", git.replicas)) != git.replicas:
                differs.append(f"replicas {desired.get('replicas')} (git {git.replicas})")
            if list(desired.get("products") or []) != list(git.products):
                differs.append(f"products {', '.join(desired.get('products') or [])} (git {', '.join(git.products)})")
            if git.digest and desired.get("digest") != git.digest:
                differs.append("digest differs from git's pin")
        elif desired and not git:
            differs.append("deployed from the console; not in deploy/deployments.yaml")
        cur = doc.get("digest", "")
        history = [h for h in doc.get("digests") or [] if h.get("digest") != cur]
        rows.append({**doc, "id": did, "component": did.split("@")[0], "host": did.split("@")[1],
                     "git": git, "desired": desired, "differs": differs, "rollback": history,
                     "replicas": desired.get("replicas", git.replicas if git else len(doc.get("instances") or []))})
    comps = []
    if spec:
        for c in sorted(spec.components.values(), key=lambda c: c.name):
            comps.append({"name": c.name, "description": c.description, "llm": c.llm, "per_product": c.per_product,
                          "image": c.image, "build": bool(c.build), "consumes": c.consumes, "emits": c.emits,
                          "deployed": [r["id"] for r in rows if r["component"] == c.name and r.get("state") not in ("not deployed", "removed")]})
    return {"deployments": rows, "components": comps}


def day_start(at: datetime) -> str:
    return at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def chains(store: StateStore, at: datetime, spec, count=None) -> dict:
    """The chain graph from the component files, with today's (UTC) event counts per edge:
    `starts` = events of that type stored today; `emits` = that component's instances' events."""
    count = count or (lambda where: store.count("events", where=where))
    since = day_start(at)
    agents_ = store.query("agents")
    agent_ids = [a.get("agent_id", "") for a in agents_]
    wired: dict[str, list[dict]] = {}               # component → its deployments' triggers (R6)
    for d in store.query("deployments"):
        if d.get("trigger") and d.get("state") != "removed":
            wired.setdefault(str(d.get("deployment", "")).split("@")[0], []).append(
                {"deployment": d.get("deployment"), **d["trigger"]})
    edges = []
    if spec is None:
        return {"edges": [], "graph": None, "since": since, "wired": wired}
    for frm, kind, to in spec.chains():
        n = None
        try:
            if kind == "starts":
                n = count([("type", "==", frm), ("received_at", ">=", since)])
            elif kind == "emits":
                ids = [a for a in agent_ids if a.startswith(f"{frm}-")]
                n = sum(count([("type", "==", to), ("factoryengine", "==", a), ("received_at", ">=", since)]) for a in ids)
        except Exception:
            n = None
        live = kind == "starts" and any(frm in w["types"] or any(x.endswith(".*") and frm.startswith(x[:-1])
                                                                   for x in w["types"]) for w in wired.get(to, []))
        edges.append({"from": frm, "kind": kind, "to": to, "count": n, "wired": live})
    last = {}
    for c in spec.components:
        ts = [a["last_trigger_at"] for a in agents_ if a.get("last_trigger_at") and str(a.get("agent_id", "")).startswith(f"{c}-")]
        if ts:
            last[c] = ago(max(ts), at)
    return {"edges": edges, "graph": layout(spec, edges), "since": since, "wired": wired, "last_trigger": last}


def layout(spec, edges: list[dict]) -> dict:
    """Three columns for an SVG: trigger events → components → emitted events."""
    comps = sorted(spec.components)
    emitted = sorted({e["to"] for e in edges if e["kind"] == "emits"})
    triggers = sorted({e["from"] for e in edges if e["kind"] == "starts"} - set(emitted))
    col_x, row_h, top = (20, 330, 560), 46, 40
    w_type, w_comp = 250, 170
    nodes: dict[str, dict] = {}
    for i, t in enumerate(triggers):
        nodes["t:" + t] = {"x": col_x[0], "y": top + i * row_h, "w": w_type, "label": t.removeprefix("dev.factory."), "kind": "event"}
    for i, t in enumerate(emitted):
        nodes["t:" + t] = {"x": col_x[2], "y": top + i * row_h, "w": w_type, "label": t.removeprefix("dev.factory."), "kind": "event"}
    rows = max(len(triggers), len(emitted), 1)
    for i, c in enumerate(comps):
        y = top + (rows - len(comps)) * row_h / 2 + i * row_h * 1.5
        nodes["c:" + c] = {"x": col_x[1], "y": y, "w": w_comp, "label": c, "kind": "component"}
    lines = []
    for e in edges:
        if e["kind"] == "starts":
            a, b = nodes.get("t:" + e["from"]), nodes.get("c:" + e["to"])
        elif e["kind"] == "emits":
            a, b = nodes.get("c:" + e["from"]), nodes.get("t:" + e["to"])
        else:
            continue
        if not a or not b:
            continue
        back = a["x"] > b["x"]                      # an emitted event that starts another component
        x1, y1 = (a["x"] + a["w"], a["y"] + 14) if not back else (a["x"], a["y"] + 14)
        x2, y2 = (b["x"], b["y"] + 14) if not back else (b["x"] + b["w"], b["y"] + 14)
        lines.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "mx": (x1 + x2) / 2, "my": (y1 + y2) / 2 - 4,
                      "count": e["count"], "kind": e["kind"] + (" wired" if e.get("wired") else "")})
    height = top + max(rows, len(comps) * 1.5) * row_h + 10
    return {"nodes": list(nodes.values()), "lines": lines, "width": col_x[2] + w_type + 20, "height": int(height)}
