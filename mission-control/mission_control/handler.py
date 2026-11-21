"""Command handler on the main node: executes console/CLI commands (Stage R4).

    command.issued on factory-commands
      → copied to factory-events (the audit trail has every command, refused or not)
      → command.acknowledged (accepted, or refused with the reason)
      → one deployment.step per step the AgentRuntime reports
      → deployment.changed (outcome + instances)

Commands run one at a time. A delivery is acked when received (a command is never run
twice by redelivery); a redelivered command id is also skipped.

    # on factory-1 (systemd unit mission-control-handler)
    python -m mission_control.handler serve --project P
    # send a command (main node only) and watch it run
    python -m mission_control.handler send deployment.restart compliance@local \\
        --reason "pick up new rules" --wait --project P
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import socket
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace

from mission_control import deployspec, events
from mission_control.ports import AgentRuntime, Event, EventBus, StateStore, Step

log = logging.getLogger("mission_control.handler")

ACTIONS = {"deployment.apply", "deployment.start", "deployment.stop", "deployment.restart",
           "deployment.remove", "deployment.scale"}
DEPLOYMENT_ID = re.compile(r"^[a-z][a-z0-9-]*@[a-z][a-z0-9-]*$")


@dataclass
class CommandHandler:
    runtime: AgentRuntime
    bus: EventBus
    topic: str = "factory-events"
    engine: str = "factory-1"
    spec_loader: Callable[[], deployspec.Spec] = deployspec.load
    vars: dict[str, str] = field(default_factory=dict)
    builder: object | None = None
    store: StateStore | None = None
    _seen: set[str] = field(default_factory=set)

    # ---------------------------------------------------------------- events
    @property
    def source(self) -> str:
        return f"factory://{self.engine}/command-handler"

    def _publish(self, etype: str, eid: str, subject: str, data: dict, key: str) -> Event:
        ev = events.make(etype, eid, subject, data, source=self.source, engine=self.engine)
        problems = events.validate(ev)
        if problems:                                   # a handler bug: never publish junk
            raise ValueError(f"{etype} would be invalid: {problems}")
        self.bus.publish(self.topic, ev, ordering_key=key)
        return ev

    # ---------------------------------------------------------------- one command
    def handle(self, cmd: Event) -> list[Event]:
        out: list[Event] = []
        data = cmd.get("data") or {}
        cid = str(data.get("command_id", ""))
        if cmd.get("type") != "dev.factory.command.issued" or events.validate(cmd):
            log.warning("dropped invalid command %s: %s", cmd.get("id"), events.validate(cmd))
            return out
        if self._handled(cid):
            log.info("command %s already handled; skipped", cid)
            return out
        self._seen.add(cid)
        key = f"command-{cid}"
        self.bus.publish(self.topic, cmd, ordering_key=key)    # audit copy (same id: deduped)
        out.append(cmd)

        action, target, args = data["action"], data["target"], data.get("args") or {}
        refusal = self._check(action, target, args)
        ack = {"command_id": cid, "action": action, "target": target, "accepted": not refusal,
               "who": data.get("who", ""), "handler": self.engine}
        if refusal:
            ack["refusal"] = refusal
        out.append(self._publish("dev.factory.command.acknowledged", f"command-ack:{cid}",
                                 f"engine/{self.engine}/command/{cid}", ack, key))
        if refusal:
            return out

        host = target.split("@", 1)[1]
        subject = f"deployment/{target}"
        ok, err, seq, extra = True, "", 0, {}
        try:
            for step in self._run(action, target, args, extra):
                seq += 1
                ok &= step.ok
                out.append(self._publish("dev.factory.deployment.step", f"deployment-step:{cid}:{seq:03d}", subject,
                                         {"command_id": cid, "deployment": target, "host": host, "seq": seq,
                                          "step": step.step, "ok": step.ok, "detail": step.detail[:600]}, key))
                if not step.ok:
                    err = f"{step.step}: {step.detail}"[:600]
        except Exception as e:                          # report, never crash the loop
            ok, err = False, f"{type(e).__name__}: {e}"[:600]
            seq += 1
            out.append(self._publish("dev.factory.deployment.step", f"deployment-step:{cid}:{seq:03d}", subject,
                                     {"command_id": cid, "deployment": target, "host": host, "seq": seq,
                                      "step": "error", "ok": False, "detail": err}, key))
        try:
            inst = [i for i in self.runtime.status() if i.deployment == target]
        except Exception as e:
            inst, err = [], err or f"status: {e}"
        changed = {"command_id": cid, "deployment": target, "host": host, "action": action, "ok": ok,
                   "instances": [{"name": i.name, "state": i.state, "digest": i.digest[:71],
                                  "started_at": i.started_at} for i in inst][:50], **extra}
        if err:
            changed["error"] = err
        out.append(self._publish("dev.factory.deployment.changed", f"deployment-changed:{cid}", subject, changed, key))
        return out

    def _handled(self, cid: str) -> bool:
        if cid in self._seen:
            return True
        if self.store is not None:
            try:
                return self.store.get("events", f"command-ack:{cid}") is not None
            except Exception:                          # the store is a hint, not a gate
                return False
        return False

    def _check(self, action: str, target: str, args: dict) -> str:
        if action not in ACTIONS:
            return f"unknown action {action!r}; known: {', '.join(sorted(ACTIONS))}"
        if not DEPLOYMENT_ID.match(target):
            return f"target must be a deployment id like compliance@local, not {target!r}"
        if action == "deployment.scale":
            r = args.get("replicas")
            if not isinstance(r, int) or isinstance(r, bool) or not 0 <= r <= 20:
                return "deployment.scale needs args.replicas, an integer 0..20"
        if action == "deployment.apply":
            try:
                spec = self.spec_loader()
            except deployspec.SpecError as e:
                return f"deploy files are invalid: {'; '.join(e.problems)[:400]}"
            ds = spec.deployments.get(target)
            if ds is None:
                return f"{target} is not in deploy/deployments.yaml"
            c = spec.components[ds.component]
            if not (args.get("digest") or ds.digest or (c.build and self.builder is not None)):
                return f"{target}: no image digest given or pinned, and nothing can build {c.name}"
        return ""

    def _run(self, action: str, target: str, args: dict, extra: dict) -> Iterator[Step]:
        if action == "deployment.apply":
            spec = self.spec_loader()
            dep = deployspec.resolve(spec, target, vars=self.vars, digest=str(args.get("digest", "")))
            c = spec.components[dep.component]
            if not dep.digest:
                digest = yield from self.builder.build(c)
                if not digest:
                    return
                dep = replace(dep, digest=digest)
            extra.update(digest=dep.digest, image=dep.image)
            yield from self.runtime.apply(dep)
        elif action == "deployment.scale":
            yield from self.runtime.scale(target, args["replicas"])
        else:
            yield from getattr(self.runtime, action.split(".", 1)[1])(target)

    # ---------------------------------------------------------------- loop
    def serve(self, subscription: str, *, max_messages: int = 5, timeout_s: float = 10.0,
              stop: Callable[[], bool] = lambda: False) -> None:
        log.info("handling commands from %s as %s", subscription, self.engine)
        while not stop():
            for d in self.bus.pull(subscription, max_messages=max_messages, timeout_s=timeout_s):
                d.ack()                                  # at most once; the audit shows what happened
                try:
                    self.handle(d.event)
                except Exception:
                    log.exception("command %s failed outside its own reporting", d.event.get("id"))


def issue(bus: EventBus, topic: str, action: str, target: str, *, who: str, reason: str,
          args: dict | None = None, engine: str = "factory-1", via: str = "cli") -> Event:
    """Publish a command.issued on the commands topic; returns it."""
    cid = uuid.uuid4().hex[:16]
    ev = events.make("dev.factory.command.issued", f"command:{cid}", f"engine/{engine}/command/{cid}",
                     {"command_id": cid, "action": action, "target": target, "args": args or {},
                      "who": who, "reason": reason, "via": via},
                     source=f"factory://{engine}/{via}", engine=engine)
    problems = events.validate(ev)
    if problems:
        raise ValueError(f"invalid command: {problems}")
    bus.publish(topic, ev, ordering_key=f"command-{cid}")
    return ev


def _who() -> str:
    """The operator: the real user behind sudo, else the login name."""
    return os.environ.get("SUDO_USER") or getpass.getuser()


def main(argv: list[str] | None = None) -> int:
    from mission_control import registry
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m mission_control.handler")
    ap.add_argument("--project", default=os.environ.get("GCP_PROJECT", ""))
    ap.add_argument("--engine", default=os.environ.get("MC_ENGINE") or socket.gethostname().split(".")[0].lower())
    ap.add_argument("--events-topic", default="factory-events")
    ap.add_argument("--commands-topic", default="factory-commands")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--subscription", default="factory-commands-handler")
    sv.add_argument("--repo", default=str(deployspec.ROOT), help="checkout used for deploy/ and builds")
    sd = sub.add_parser("send")
    sd.add_argument("action")
    sd.add_argument("target")
    sd.add_argument("--reason", required=True)
    sd.add_argument("--replicas", type=int)
    sd.add_argument("--digest", default="")
    sd.add_argument("--wait", action="store_true", help="follow commands/{id} until it finishes")
    sd.add_argument("--timeout", type=float, default=900)
    o = ap.parse_args(argv)
    if not o.project:
        print("--project or GCP_PROJECT is required", file=sys.stderr)
        return 2
    bus = registry.get("bus", "pubsub")(project=o.project)
    store = registry.get("store", "firestore")(project=o.project)

    if o.cmd == "serve":
        from pathlib import Path

        from mission_control.builder import DockerBuilder
        repo = Path(o.repo)
        h = CommandHandler(runtime=registry.get("runtime", "docker-local")(), bus=bus, topic=o.events_topic,
                           engine=o.engine, spec_loader=lambda: deployspec.load(repo / "deploy", products_dir=repo / "products"),
                           vars={"PROJECT": o.project}, builder=DockerBuilder(repo), store=store)
        h.serve(o.subscription)
        return 0

    args: dict = {}
    if o.replicas is not None:
        args["replicas"] = o.replicas
    if o.digest:
        args["digest"] = o.digest
    ev = issue(bus, o.commands_topic, o.action, o.target, who=_who(), reason=o.reason, args=args, engine=o.engine)
    cid = ev["data"]["command_id"]
    print(f"issued {cid}: {o.action} {o.target}")
    if not o.wait:
        return 0
    shown, deadline = set(), time.monotonic() + o.timeout
    while time.monotonic() < deadline:
        doc = store.get("commands", cid) or {}
        for k in sorted(doc.get("steps") or {}):
            if k not in shown:
                shown.add(k)
                s = doc["steps"][k]
                print(f"  {'ok ' if s.get('ok') else 'ERR'} {s.get('step'):<10} {s.get('detail', '')}")
        if doc.get("status") in ("succeeded", "failed", "refused"):
            print(f"{doc['status']}" + (f": {doc.get('refusal') or doc.get('error')}" if doc.get("status") != "succeeded" else ""))
            return 0 if doc["status"] == "succeeded" else 1
        time.sleep(2)
    print(f"no outcome within {o.timeout:.0f}s; see commands/{cid}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
