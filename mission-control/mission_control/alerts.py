"""Scheduled problem rules (plan F7): open and clear problems, and alert on new ones.

Cloud Scheduler calls the writer's `POST /rules` every 2 minutes. `check()` runs the
console's rules (`views.rule_problems`) over the store, compares them with the open
problems in the `problems` collection and, for each difference, publishes an event:

    dev.factory.problem.opened     a rule sees a problem that was not open (or whose
                                   occurrence or severity changed)
    dev.factory.problem.resolved   an open problem is no longer seen (by_rule: true)

Each event is also projected into the store at once, so the next check never repeats
it even if the bus is slow. Event ids are deterministic, so a repeated check publishes
the same ids. A new problem at or above `alert_min` (and a worsened one) goes to the
Notifier; so does the clearing of a problem that was alerted. A problem an operator
acknowledged or resolved for this occurrence is not alerted.

Because this runs in Cloud Run, not on factory-1, a dead factory-1 is still noticed:
"engine silent" and "host silent" open and alert while the VM is gone.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from mission_control import events, projections
from mission_control.console import views
from mission_control.ports import StateStore

log = logging.getLogger("mission_control.alerts")
PROBLEMS = "problems"
SOURCE = "factory://mission-control/rules"
ENGINE = "mission-control"


def _eid(prefix: str, *parts: str) -> str:
    return f"{prefix}:{hashlib.sha1('|'.join(parts).encode()).hexdigest()[:24]}"


def _worse(a: str, b: str) -> bool:
    return views.SEVERITY_ORDER.get(a, 9) < views.SEVERITY_ORDER.get(b, 9)


def check(store: StateStore, bus, notifier, at: datetime | None = None, *, topic: str = "factory-events",
          alert_min: str = "warning") -> dict:
    at = at or views.now()
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    # rules.silent is about this check itself: only the console can see it stop
    seen = {p.id: p for p in views.rule_problems(store, at) if p.kind != "rules.silent"}
    open_ = {d["problem_id"]: d for d in store.query(PROBLEMS) if d.get("state") == "open" and d.get("problem_id")}
    states = {d.get("problem_id"): d for d in store.query("problem_states")}
    limit = views.SEVERITY_ORDER[alert_min]
    opened, cleared, alerts = [], [], 0

    def emit(etype: str, eid: str, pid: str, data: dict, product: str = "") -> None:
        ext = {"factoryproduct": product} if product else {}
        ev = events.make(etype, eid, f"problem/{pid}", data, source=SOURCE, engine=ENGINE, time=stamp, **ext)
        errs = events.validate(ev)
        if errs:
            raise ValueError(f"{etype} would be invalid: {errs}")
        bus.publish(topic, ev, ordering_key=f"problem/{pid}")
        projections.project(ev, store)

    def handled(pid: str, since: str) -> bool:
        st = states.get(pid) or {}
        return any((st.get(k) or {}).get("since", None) == since for k in ("ack", "resolved"))

    for pid, p in seen.items():
        prev = open_.get(pid)
        if prev and prev.get("since") == p.since and prev.get("severity") == p.severity:
            continue
        new = prev is None or _worse(p.severity, prev.get("severity", "info"))
        alert = new and views.SEVERITY_ORDER[p.severity] <= limit and not handled(pid, p.since)
        if alert:
            notifier.send(p.title, p.detail, severity=p.severity, link=p.link)
            alerts += 1
        data = {"problem_id": pid, "kind": p.kind, "severity": p.severity, "title": p.title[:300],
                "detail": p.detail[:600], "link": p.link, "since": p.since, "key": p.key,
                "alerted": bool(alert or (prev or {}).get("alerted"))}
        if p.product:
            data["product"] = p.product
        emit("dev.factory.problem.opened", _eid("problem-opened", pid, p.since, p.severity), pid, data, p.product)
        opened.append(pid)
    for pid, d in open_.items():
        if pid in seen:
            continue
        if d.get("alerted"):
            notifier.send(f"Cleared: {d.get('title', pid)}", f"open since {d.get('opened_at', '?')[:19]}",
                          severity="info", link=d.get("link", ""))
            alerts += 1
        emit("dev.factory.problem.resolved", _eid("problem-cleared", pid, d.get("since", ""), stamp), pid,
             {"problem_id": pid, "since": d.get("since", ""), "by_rule": True, "reason": "the rule no longer sees it"},
             d.get("product", ""))
        cleared.append(pid)
    summary = {"at": stamp, "open": len(seen), "opened": opened, "cleared": cleared, "alerts": alerts}
    store.upsert(*views.RULES_DOC, summary)
    if opened or cleared:
        log.info("problem rules: %d open, opened %s, cleared %s, %d alerts", len(seen), opened, cleared, alerts)
    return summary
