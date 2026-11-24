"""Advisory compliance agent: every open PR head of a product, against one rules version.

One tick: load the rules (PromptRegistry, by label) → list open PRs (CodeHost) → skip a
head already reported against the same rules → review → publish
`dev.factory.compliance.reported`. Empty rules ⇒ `no_rules`, and no model call at all.
It never writes to the code host and never blocks anything; it only publishes events.
"""
from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from mission_control import products
from mission_control.ports import Change, CodeHost, Event, EventBus, PromptRegistry, StateStore
from mission_control.products import Product
from mission_control.projections import COMPLIANCE, compliance_key

from . import rules as rules_mod
from .reviewer import Reviewer

FINAL = {"no_rules", "clean", "findings"}     # a head with one of these is not reviewed again


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Identity:
    agent_id: str                     # unique per instance (pod name)
    pool: str = "compliance"
    location: str = "host:local"      # gke:{cluster}/{pod} | gce:{zone}/{vm} | host:{name}
    auth_method: str = "none"         # vertex | api_key | subscription | anthropic_wif | none
    version: str = "dev"


@dataclass
class ComplianceAgent:
    me: Identity
    product: Product
    codehost: CodeHost
    prompts: PromptRegistry
    reviewer: Reviewer
    bus: EventBus
    store: StateStore
    topic: str = "factory-events"
    clock: Callable[[], str] = now_iso
    spent_usd: float = 0.0            # today's spend (TODO OC4: persist across restarts)
    reports: int = 0
    day: str = ""
    _done: dict[str, tuple[str, str]] = field(default_factory=dict)   # key → (head, rules hash)
    trigger: str = ""                 # subscription that starts ticks (R6); "" = timer only
    triggered_today: int = 0
    last_trigger_at: str = ""

    def triggered(self, ev: Event) -> None:
        """An event arrived on the trigger subscription (counted in the heartbeat)."""
        self._roll_day()
        self.triggered_today += 1
        self.last_trigger_at = self.clock()

    # ---------------------------------------------------------------- events
    def _event(self, etype: str, eid: str, subject: str, data: dict, **ext: str) -> Event:
        return {"specversion": "1.0", "id": eid, "source": f"factory://{self.me.agent_id}/compliance-agent",
                "type": etype, "subject": subject, "time": self.clock(),
                "datacontenttype": "application/json", "factoryengine": self.me.agent_id, **ext, "data": data}

    def heartbeat(self, state: str = "idle", stopped_reason: str = "") -> Event:
        data = {"agent_id": self.me.agent_id, "pool": self.me.pool, "kind": "compliance",
                "location": self.me.location, "state": state, "products": [self.product.name],
                "model": self.reviewer.model, "tier": "small", "auth_method": self.me.auth_method,
                "version": self.me.version, "spent_usd_today": round(self.spent_usd, 4),
                "reports_today": self.reports}
        if stopped_reason:
            data["stopped_reason"] = stopped_reason
        if self.trigger:
            data.update(trigger=self.trigger, triggered_today=self.triggered_today)
            if self.last_trigger_at:
                data["last_trigger_at"] = self.last_trigger_at
        t = self.clock()
        ev = self._event("dev.factory.agent.heartbeat", f"agent:{self.me.agent_id}:{t}",
                         f"agent/{self.me.agent_id}", data)
        self.bus.publish(self.topic, ev, ordering_key=f"agent-{self.me.agent_id}")
        return ev

    # ---------------------------------------------------------------- one tick
    def _roll_day(self) -> None:
        today = self.clock()[:10]
        if today != self.day:
            self.day, self.spent_usd, self.reports, self.triggered_today = today, 0.0, 0, 0

    def _already(self, key: str, head: str, rules_hash: str) -> bool:
        if self._done.get(key) == (head, rules_hash):
            return True
        doc = self.store.get(COMPLIANCE, key) or {}
        return (doc.get("head") == head and (doc.get("rules") or {}).get("hash") == rules_hash
                and doc.get("status") in FINAL)

    def _refresh_settings(self) -> None:
        """Take the console-editable settings from products/{name}.effective (the console
        changes them with product.updated); keep the file's values when there is none."""
        try:
            doc = self.store.get("products", self.product.name)
        except Exception:
            return
        if doc and doc.get("effective"):
            self.product = products.apply_effective(self.product, doc["effective"])

    def tick(self) -> list[Event]:
        self._roll_day()
        self._refresh_settings()
        c = self.product.compliance
        if c.mode != "advisory":
            return []
        prompt = self.prompts.get(c.rules, label=c.label)
        if prompt is None:
            raise RuntimeError(f"no {c.rules!r} prompt with label {c.label!r}")
        rules = rules_mod.parse(prompt.text)
        rules_ref = {"name": prompt.name, "version": prompt.version, "label": c.label,
                     "hash": prompt.hash, "count": len(rules)}
        out = []
        for ch in self.codehost.changes(state="open"):
            pr = int(ch.id.rpartition("#")[2])
            key = compliance_key(self.product.name, pr)
            if self._already(key, ch.head, prompt.hash):
                continue
            out.append(self._report(ch, pr, key, rules, rules_ref))
        return out

    def _report(self, ch: Change, pr: int, key: str, rules: list, rules_ref: dict) -> Event:
        c = self.product.compliance
        data = {"product": self.product.name, "change": ch.id, "pr": pr, "head": ch.head, "base": ch.base,
                "title": ch.title[:512], "mode": "advisory", "rules": rules_ref, "findings": [], "cost_usd": 0.0}
        start = time.monotonic()
        if not rules:
            data["status"] = "no_rules"                      # zero cost: no diff, no model
        elif self.spent_usd >= c.daily_cap_usd:
            data["status"] = "capped"
        else:
            try:
                diff, truncated = self.codehost.diff(ch.id)
                data.update(diff_bytes=len(diff.encode()), diff_truncated=truncated)
                rv = self.reviewer.review(diff=diff, rules=rules, title=ch.title)
                self.spent_usd += rv.cost_usd
                data.update(findings=rv.findings, cost_usd=round(rv.cost_usd, 6), model=rv.model, effort="low",
                            status="findings" if rv.findings else "clean")
            except Exception as e:                        # reported, retried next tick
                data.update(status="error", error=f"{type(e).__name__}: {e}"[:2000])
        data["duration_ms"] = int((time.monotonic() - start) * 1000)
        eid = f"compliance:{self.product.name}:{pr}:{ch.head[:12]}:{rules_ref['hash'][7:19]}"
        if data["status"] not in FINAL:
            eid += f":{data['status']}:{self.clock()}"          # a retry is a new event
        ev = self._event("dev.factory.compliance.reported", eid, f"change/{ch.id}", data,
                         factoryproduct=self.product.name)
        self.bus.publish(self.topic, ev, ordering_key=f"compliance-{key}")
        self.reports += 1
        if data["status"] in FINAL:
            self._done[key] = (ch.head, rules_ref["hash"])
        return ev
