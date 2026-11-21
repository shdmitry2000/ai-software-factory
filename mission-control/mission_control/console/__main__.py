"""Run the console.

    # factory-1 (systemd unit factory-console): unix socket, Firestore, peer identity
    python -m mission_control.console --uds /run/factory-console/sock --project P

    # a laptop: loopback TCP, in-memory demo data, a dev operator name
    python -m mission_control.console --port 8080 --demo --dev-operator me
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from datetime import datetime, timedelta, timezone

import uvicorn

from mission_control import registry
from mission_control.console.app import create_app
from mission_control.console.peercred import PeerCredH11


def _seed(store) -> None:
    """Example rows for a local run, clearly marked as demo data on the page."""
    now = datetime.now(timezone.utc)
    def t(s):
        return (now - timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    store.upsert("engines", "factory-1", {"engine": "factory-1", "last_heartbeat": t(20), "halted": False})
    store.upsert("agents", "compliance-1-local", {"agent_id": "compliance-1-local", "state": "idle",
                 "model": "claude-haiku-5-5", "last_heartbeat": t(35)})
    store.upsert("deployments", "compliance@local", {"deployment": "compliance@local", "state": "running",
                 "digest": "sha256:d566817f18438c724c5f", "last_action": "deployment.apply", "last_ok": True})
    store.upsert("hosts", "local", {"host": "local", "kind": "local", "state": "healthy", "last_heartbeat": t(25),
                 "health": {"state": "healthy", "docker": "27.5.1", "cpus": 4, "load1": 0.31, "mem_total_mb": 16008,
                            "mem_used_pct": 41.2, "disk_total_gb": 48.4, "disk_used_pct": 62.0, "containers": 1}})
    store.update("deployments", "compliance@local", lambda d: {**d, "desired": {"component": "compliance", "host": "local",
                 "replicas": 1, "digest": "sha256:" + "d5" * 32, "products": ["file-to-markdown-convertor"]},
                 "digests": [{"digest": "sha256:" + "d5" * 32, "at": t(400)}, {"digest": "sha256:" + "a8" * 32, "at": t(90000)}],
                 "digest": "sha256:" + "d5" * 32})
    store.upsert("commands", "demo-1", {"command_id": "demo-1", "action": "deployment.restart",
                 "target": "compliance@local", "who": "demo", "reason": "example", "status": "succeeded",
                 "issued_at": t(300)})
    for i, (typ, subj) in enumerate([("agent.heartbeat", "agent/compliance-1-local"),
                                     ("compliance.reported", "change/codehost:github/demo#3"),
                                     ("engine.heartbeat", "engine/factory-1")]):
        store.upsert("events", f"demo:{i}", {"type": f"dev.factory.{typ}", "subject": subj,
                     "factoryengine": "factory-1", "received_at": t(60 * (i + 1)), "time": t(60 * (i + 1))})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mission_control.console")
    ap.add_argument("--uds", default="", help="unix socket path (production)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--store", choices=["memory", "firestore"], default="")
    ap.add_argument("--project", default=os.environ.get("GCP_PROJECT", ""))
    ap.add_argument("--engine", default=os.environ.get("MC_ENGINE") or socket.gethostname().split(".")[0].lower())
    ap.add_argument("--archon-url", default="http://localhost:3090/")
    ap.add_argument("--dev-operator", default="", help="local TCP runs only: let this name in")
    ap.add_argument("--demo", action="store_true", help="in-memory store with example rows")
    ap.add_argument("--read-only", action="store_true", help="no command bus: buttons are hidden")
    ap.add_argument("--commands-topic", default="factory-commands")
    o = ap.parse_args(argv)

    kind = o.store or ("memory" if o.demo else "firestore")
    if kind == "firestore" and not o.project:
        print("--project (or GCP_PROJECT) is required for the firestore store", file=sys.stderr)
        return 2
    store = registry.get("store", kind)(**({"project": o.project} if kind == "firestore" else {}))
    if o.demo:
        _seed(store)
    if o.uds and o.dev_operator:
        print("--dev-operator is for local TCP runs only; the socket uses peer identity", file=sys.stderr)
        return 2
    if not o.uds and o.host not in ("127.0.0.1", "localhost", "::1"):
        print("refusing to listen on a non-loopback address; production uses --uds", file=sys.stderr)
        return 2

    bus = None
    if kind == "firestore" and not o.read_only:
        bus = registry.get("bus", "pubsub")(project=o.project)
    from mission_control.deployspec import ROOT
    prompts = registry.get("prompts", "files")(root=str(ROOT / "prompts"))
    app = create_app(store, dev_operator=o.dev_operator, archon_url=o.archon_url, engine=o.engine, demo=o.demo,
                     bus=bus, commands_topic=o.commands_topic, prompts=prompts)
    config = uvicorn.Config(app, http=PeerCredH11, log_level="info", access_log=False,
                            proxy_headers=False, server_header=False)
    server = uvicorn.Server(config)
    if o.uds:
        try:
            os.unlink(o.uds)
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(o.uds)
        # Anyone local may connect; the app admits only root and factory-operators (SO_PEERCRED).
        os.chmod(o.uds, 0o666)
        server.run(sockets=[sock])
    else:
        config.host, config.port = o.host, o.port
        server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
