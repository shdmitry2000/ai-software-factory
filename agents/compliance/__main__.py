"""Run the compliance agent.

    # dry run on a laptop: live GitHub reads, events printed, nothing written anywhere
    PYTHONPATH=mission-control python -m agents.compliance --once --bus memory --print

    # in the GKE pod (Workload Identity: Vertex, Pub/Sub, Firestore; no secrets)
    python -m agents.compliance --bus pubsub --store firestore --interval 600

Settings come from flags or env: GCP_PROJECT, MC_TOPIC, AGENT_ID (pod name),
AGENT_LOCATION, COMPLIANCE_MODEL, FACTORY_PRODUCT.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

from mission_control import products, registry

from .agent import ComplianceAgent, Identity
from .reviewer import ClaudeReviewer, FakeReviewer

ROOT = Path(__file__).resolve().parents[2]


class PrintingBus:
    """Wraps a bus and prints each event (dry runs)."""

    def __init__(self, inner):
        self.inner = inner

    def publish(self, topic, event, *, ordering_key=""):
        print(json.dumps(event, indent=2), flush=True)
        return self.inner.publish(topic, event, ordering_key=ordering_key)

    def pull(self, *a, **kw):
        return self.inner.pull(*a, **kw)


def auth_method() -> str:
    if os.environ.get("CLAUDE_CODE_USE_VERTEX") == "1":
        return "vertex"
    if os.environ.get("ANTHROPIC_FEDERATION_RULE_ID"):
        return "anthropic_wif"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api_key"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "subscription"
    return "none"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agents.compliance")
    ap.add_argument("--product", default=os.environ.get("FACTORY_PRODUCT", "file-to-markdown-convertor"))
    ap.add_argument("--products-dir", default=str(ROOT / "products"))
    ap.add_argument("--prompts-dir", default=str(ROOT / "prompts"))
    ap.add_argument("--bus", choices=["memory", "pubsub"], default="memory")
    ap.add_argument("--store", choices=["memory", "firestore"], default="memory")
    ap.add_argument("--project", default=os.environ.get("GCP_PROJECT", ""))
    ap.add_argument("--topic", default=os.environ.get("MC_TOPIC", "factory-events"))
    ap.add_argument("--reviewer", choices=["claude", "fake"], default="claude")
    ap.add_argument("--model", default=os.environ.get("COMPLIANCE_MODEL", "claude-haiku-5-5"))
    ap.add_argument("--interval", type=float, default=600.0, help="seconds between polls")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--print", action="store_true", help="print every published event")
    a = ap.parse_args(argv)

    product = products.load(Path(a.products_dir) / f"{a.product}.yaml")
    cloud = {"project": a.project} if a.project else {}
    bus = registry.get("bus", a.bus)(**(cloud if a.bus == "pubsub" else {}))
    store = registry.get("store", a.store)(**(cloud if a.store == "firestore" else {}))
    if a.print or a.bus == "memory":
        bus = PrintingBus(bus)
    me = Identity(agent_id=os.environ.get("AGENT_ID") or socket.gethostname().split(".")[0].lower(),
                  location=os.environ.get("AGENT_LOCATION", f"host:{socket.gethostname().split('.')[0]}"),
                  auth_method=auth_method(), version=os.environ.get("AGENT_VERSION", "dev"))
    agent = ComplianceAgent(
        me=me, product=product,
        codehost=registry.get("codehost", product.codehost)(repo=product.repo),
        prompts=registry.get("prompts", "files")(root=a.prompts_dir),
        reviewer=ClaudeReviewer(model=a.model) if a.reviewer == "claude" else FakeReviewer(),
        bus=bus, store=store, topic=a.topic)

    agent.heartbeat("starting")
    next_beat = 0.0
    while True:
        try:
            reports = agent.tick()
            print(f"tick: {len(reports)} report(s), spent today ${agent.spent_usd:.4f}", file=sys.stderr, flush=True)
        except Exception as e:                      # keep running; the heartbeat shows it's alive
            print(f"tick failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if a.once:
            agent.heartbeat("stopped", stopped_reason="--once")
            return 0
        deadline = time.monotonic() + a.interval
        while time.monotonic() < deadline:
            if time.monotonic() >= next_beat:
                agent.heartbeat("idle")
                next_beat = time.monotonic() + 60
            time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    sys.exit(main())
