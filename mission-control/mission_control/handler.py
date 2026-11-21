"""Command handler on the main node: executes console/CLI commands (Stages R4, UI-2).

    command.issued on factory-commands
      → copied to factory-events (the audit trail has every command, refused or not)
      → command.acknowledged (accepted, or refused with the reason)
      → deployment.*: one deployment.step per runtime step, then deployment.changed
        product.update: product.updated (old value must still match), command.completed
        engine.halt/unhalt: consumer.py halt/unhalt in the app, engine.halted/unhalted,
                            command.completed
        problem.ack/resolve: problem.acknowledged/resolved, command.completed

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
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

from mission_control import deployspec, events
from mission_control import products as products_mod
from mission_control import triggers as triggers_mod
from mission_control.ports import AgentRuntime, Event, EventBus, PromptRegistry, StateStore, Step

log = logging.getLogger("mission_control.handler")

DEPLOYMENT_ID = re.compile(r"^[a-z][a-z0-9-]*@[a-z][a-z0-9-]*$")
PRODUCT_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PROBLEM_ID = re.compile(r"^[a-z_]+\.[a-z_]+:[^/\s]{1,180}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
HOST_STATES = {"healthy", "unreachable", "installing", "degraded"}
HOST_FACTS = {"docker": str, "cpus": int, "load1": float, "mem_total_mb": int, "mem_used_pct": float,
              "disk_total_gb": float, "disk_used_pct": float, "containers": int, "error": str}
LOGS_SOCKET = "/run/mission-control-handler/logs.sock"
DEPLOYMENT_ACTIONS = {"deployment.apply", "deployment.start", "deployment.stop", "deployment.restart",
                      "deployment.remove", "deployment.scale"}
HOST_ACTIONS = {"host.create", "host.install", "host.remove"}
ACTIONS = DEPLOYMENT_ACTIONS | HOST_ACTIONS | {"product.update", "engine.halt", "engine.unhalt", "problem.ack",
                                               "problem.resolve", "fleet.restore"}


class Seq:
    """Step numbers of one command (host and deployment steps share them)."""
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def run_consumer(app_dir: Path, verb: str) -> tuple[bool, str]:
    """`python3 factory/consumer.py <verb>` in the product checkout (halt / unhalt)."""
    p = subprocess.run(["python3", "factory/consumer.py", verb], cwd=app_dir,
                       capture_output=True, text=True, timeout=120)
    out = (p.stdout or p.stderr).strip().splitlines()
    return p.returncode == 0, (out[-1] if out else f"exit {p.returncode}")[:500]


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
    products_dir: Path | None = None
    prompts: PromptRegistry | None = None
    app_dir: Path | None = None
    consumer: Callable[[Path, str], tuple[bool, str]] = run_consumer
    max_vm_hosts: int = 3
    triggers: object | None = None                  # mission_control.triggers (R6); None = timer only
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
        if action in HOST_ACTIONS or action == "fleet.restore":
            out += self._run_hosts(cid, action, target, args, key)
            return out
        if action not in DEPLOYMENT_ACTIONS:
            out += self._run_other(cid, action, target, args, data, key)
            return out
        evs, _, _ = self._deploy(cid, action, target, args, key, Seq())
        return out + evs

    def _deploy(self, cid: str, action: str, target: str, args: dict, key: str, seq: Seq,
                report_as: str = "") -> tuple[list[Event], bool, str]:
        """Run one deployment action; a deployment.step per step, then deployment.changed."""
        out: list[Event] = []
        host = target.split("@", 1)[1]
        subject = f"deployment/{target}"
        ok, err, extra = True, "", {}
        try:
            for step in self._run(action, target, args, extra):
                ok &= step.ok
                out.append(self._step(cid, seq, step, deployment=target, host=host, key=key))
                if not step.ok:
                    err = f"{step.step}: {step.detail}"[:600]
        except Exception as e:                          # report, never crash the loop
            ok, err = False, f"{type(e).__name__}: {e}"[:600]
            out.append(self._step(cid, seq, Step("error", False, err), deployment=target, host=host, key=key))
        try:
            inst = [i for i in self.runtime.status() if i.deployment == target]
        except Exception as e:
            inst, err = [], err or f"status: {e}"
        changed = {"command_id": cid, "deployment": target, "host": host, "action": report_as or action, "ok": ok,
                   "instances": [{"name": i.name, "state": i.state, "digest": i.digest[:71],
                                  "started_at": i.started_at} for i in inst][:50], **extra}
        if err:
            changed["error"] = err
        eid = f"deployment-changed:{cid}" + (f":{target}" if report_as else "")
        out.append(self._publish("dev.factory.deployment.changed", eid, subject, changed, key))
        return out, ok, err

    def _step(self, cid: str, seq: Seq, step: Step, *, key: str, deployment: str = "", host: str = "") -> Event:
        n = seq.next()
        if deployment:
            return self._publish("dev.factory.deployment.step", f"deployment-step:{cid}:{n:03d}", f"deployment/{deployment}",
                                 {"command_id": cid, "deployment": deployment, "host": host, "seq": n,
                                  "step": step.step[:40], "ok": step.ok, "detail": step.detail[:600]}, key)
        return self._publish("dev.factory.host.step", f"host-step:{cid}:{n:03d}", f"host/{host}",
                             {"command_id": cid, "host": host, "seq": n, "step": step.step[:40], "ok": step.ok,
                              "detail": step.detail[:600]}, key)

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
        if action == "product.update":
            return self._check_product(target, args)
        if action in ("engine.halt", "engine.unhalt"):
            if target != self.engine:
                return f"this handler controls engine {self.engine!r}, not {target!r}"
            if self.app_dir is None or not (self.app_dir / "factory" / "consumer.py").exists():
                return "no product checkout with factory/consumer.py is configured on this engine"
            return ""
        if action in ("problem.ack", "problem.resolve"):
            return "" if PROBLEM_ID.match(target) else f"not a problem id: {target!r}"
        if action in HOST_ACTIONS:
            return self._check_host(action, target, args)
        if action == "fleet.restore":
            return "" if target == self.engine else f"this handler restores engine {self.engine!r}'s fleet, not {target!r}"
        if not DEPLOYMENT_ID.match(target):
            return f"target must be a deployment id like compliance@local, not {target!r}"
        if action == "deployment.scale":
            r = _int(args.get("replicas"))
            if r is None or not 0 <= r <= 20:
                return "deployment.scale needs args.replicas, an integer 0..20"
            args["replicas"] = r
        if action == "deployment.apply":
            try:
                self._plan(target, args)
            except deployspec.SpecError as e:
                return f"deploy files are invalid: {'; '.join(e.problems)[:400]}"
            except ValueError as e:
                return str(e)[:500]
        return ""

    def _check_host(self, action: str, target: str, args: dict) -> str:
        from mission_control.adapters.runtime.gce import HOST_ID, MACHINE_TYPES
        if action == "host.install" and target == "local":
            return ""
        if not HOST_ID.match(target):
            return f"VM host ids look like agent-1 (agent- then letters, digits, dashes), not {target!r}"
        known = {h.id for h in self.runtime.hosts()}
        if action == "host.create":
            mt = str(args.get("machine_type") or "e2-small")
            if mt not in MACHINE_TYPES:
                return f"machine_type must be one of {', '.join(MACHINE_TYPES)}"
            args["machine_type"] = mt
            vms = {h for h in known if h != "local"}
            if target not in vms and len(vms) >= self.max_vm_hosts:
                return f"at most {self.max_vm_hosts} VM hosts (now: {', '.join(sorted(vms))}); remove one first"
            return ""
        if target not in known:
            return f"no host {target!r}"
        return ""

    def _run_hosts(self, cid: str, action: str, target: str, args: dict, key: str) -> list[Event]:
        """host.create (create + install), host.install, host.remove, fleet.restore."""
        from mission_control.ports import Host
        out: list[Event] = []
        seq = Seq()

        def steps(it, host: str) -> tuple[bool, str]:
            ok, err = True, ""
            try:
                for st in it:
                    ok &= st.ok
                    out.append(self._step(cid, seq, st, host=host, key=key))
                    if not st.ok:
                        err = f"{st.step}: {st.detail}"[:600]
                        break
            except Exception as e:
                ok, err = False, f"{type(e).__name__}: {e}"[:600]
                out.append(self._step(cid, seq, Step("error", False, err), host=host, key=key))
            return ok, err

        def changed(host: str, ok: bool, err: str, state: str, desired: dict | None, report_as: str = "") -> None:
            addr = next((h.address for h in self.runtime.hosts() if h.id == host), "")
            data = {"command_id": cid, "host": host, "action": report_as or action, "ok": ok,
                    "kind": "local" if host == "local" else "vm", "state": state}
            if addr:
                data["address"] = addr
            if desired:
                data["desired"] = desired
            if err:
                data["error"] = err
            eid = f"host-changed:{cid}" + (f":{host}" if report_as else "")
            out.append(self._publish("dev.factory.host.changed", eid, f"host/{host}", data, key))

        def bring_up(host: str, desired: dict, report_as: str = "") -> bool:
            ok, err = steps(self.runtime.create_host(Host(host, "vm", labels={"machine_type": desired.get("machine_type", "e2-small")})), host)
            if ok:
                ok, err = steps(self.runtime.install(host), host)
            changed(host, ok, err, "healthy" if ok else "degraded", desired, report_as)
            return ok

        zone = getattr(getattr(self.runtime, "gce", None), "zone", "")
        try:
            if action == "host.create":
                desired = {"kind": "vm", "machine_type": args["machine_type"]}
                if zone:
                    desired["zone"] = zone
                ok = bring_up(target, desired)
                out.append(self._completed(cid, action, target, ok, f"{target} is ready" if ok else "see the steps", key))
            elif action == "host.install":
                ok, err = steps(self.runtime.install(target), target)
                stored = self._stored("hosts", target).get("desired")
                changed(target, ok, err, "healthy" if ok else "degraded", stored)
                out.append(self._completed(cid, action, target, ok, "installed" if ok else err or "failed", key))
            elif action == "host.remove":
                on_host = [d["deployment"] for d in (self.store.query("deployments") if self.store else [])
                           if str(d.get("deployment", "")).endswith(f"@{target}") and d.get("state") != "removed"]
                ok, err = steps(self.runtime.remove_host(target), target)
                if ok and self.triggers is not None:
                    for dep in on_host:
                        steps(self.triggers.remove(triggers_mod.subscription_name(dep)), target)
                if ok:
                    for dep in on_host:
                        out.append(self._publish("dev.factory.deployment.changed", f"deployment-changed:{cid}:{dep}",
                                                 f"deployment/{dep}", {"command_id": cid, "deployment": dep, "host": target,
                                                                       "action": "deployment.remove", "ok": True,
                                                                       "instances": []}, key))
                changed(target, ok, err, "removed" if ok else "degraded", None)
                out.append(self._completed(cid, action, target, ok, f"{target} removed" if ok else err or "failed", key))
            else:
                out += self._restore(cid, key, seq, bring_up)
        except Exception as e:                          # report, never crash the loop
            out.append(self._completed(cid, action, target, False, f"{type(e).__name__}: {e}", key))
        return out

    def _restore(self, cid: str, key: str, seq: Seq, bring_up) -> list[Event]:
        """fleet.restore: recreate VM hosts, then reapply every deployment's desired state.
        A pinned image the main node no longer has (rebuilt after the sweep) is rebuilt."""
        out: list[Event] = []
        spec = self.spec_loader()
        host_docs = {h.get("host"): h for h in (self.store.query("hosts") if self.store else [])}
        vms: dict[str, dict] = {}
        for hid, hs in spec.hosts.items():
            if hs.kind == "vm" and (host_docs.get(hid) or {}).get("state") != "removed":
                vms[hid] = {"kind": "vm", "machine_type": hs.machine_type or "e2-small"}
        for hid, h in host_docs.items():
            if (h.get("desired") or {}).get("kind") == "vm" and h.get("state") != "removed":
                vms[hid] = h["desired"]
        up = {"local"}
        for hid, desired in sorted(vms.items()):
            if bring_up(hid, desired, report_as="fleet.restore"):
                up.add(hid)
        dep_docs = {d.get("deployment"): d for d in (self.store.query("deployments") if self.store else [])}
        targets = sorted({did for did, d in dep_docs.items() if d.get("desired") and d.get("state") != "removed"} |
                         {did for did in spec.deployments if (dep_docs.get(did) or {}).get("state") != "removed"})
        done, failed = [], []
        for did in targets:
            host = did.split("@", 1)[1]
            if host not in up:
                failed.append(f"{did} (host {host} is not up)")
                continue
            args: dict = {}
            pinned = (dep_docs.get(did) or {}).get("desired", {}).get("digest") or getattr(spec.deployments.get(did), "digest", "")
            has = getattr(self.builder, "has", None)
            if pinned and has is not None and not has(pinned):
                args["build"] = "1"
            refusal = ""
            try:
                self._plan(did, dict(args))
            except Exception as e:
                refusal = str(e)
            if refusal:
                failed.append(f"{did}: {refusal}"[:200])
                continue
            evs, ok, err = self._deploy(cid, "deployment.apply", did, args, key, seq, report_as="fleet.restore")
            out += evs
            (done if ok else failed).append(did if ok else f"{did}: {err}"[:200])
        ok = not failed and len(up) == len(vms) + 1
        detail = (f"hosts up: {', '.join(sorted(up))}; deployments: {', '.join(done) or 'none'}"
                  + (f"; failed: {'; '.join(failed)}" if failed else ""))
        out.append(self._completed(cid, "fleet.restore", self.engine, ok, detail, key))
        return out

    # ---------------------------------------------------------------- desired state
    def _stored(self, collection: str, doc_id: str) -> dict:
        if self.store is None:
            return {}
        try:
            return self.store.get(collection, doc_id) or {}
        except Exception:
            return {}

    def _known_hosts(self, spec: deployspec.Spec) -> set[str]:
        known = set(spec.hosts)
        if self.store is not None:
            try:
                known |= {h["host"] for h in self.store.query("hosts")
                          if h.get("host") and h.get("desired") and h.get("state") != "removed"}
            except Exception:
                pass
        return known

    def _plan(self, target: str, args: dict) -> tuple[deployspec.Spec, deployspec.DeploymentSpec, str]:
        """What deployment.apply will run: (spec, entry, digest; "" = build first).

        The entry starts from the last applied desired state (deployments/{id}.desired),
        else deploy/deployments.yaml, else the target itself (a new deployment from the
        console); then args override it: replicas, products, digest; `build` builds the
        checkout's HEAD instead of the pinned digest; `reset` ignores the stored state."""
        spec = self.spec_loader()
        component, host = target.split("@", 1)
        doc = {} if args.get("reset") else self._stored("deployments", target)
        stored = doc.get("desired") or {}
        ds = spec.deployments.get(target) or deployspec.DeploymentSpec(component, host)
        if stored:
            ds = replace(ds, replicas=int(stored.get("replicas", ds.replicas)),
                         products=tuple(stored.get("products") or ds.products), digest=stored.get("digest", ds.digest))
        elif not ds.digest and doc.get("digest") and doc.get("state") != "removed":
            ds = replace(ds, digest=doc["digest"])       # running before desired state was kept: keep its image
        if "replicas" in args:
            r = _int(args["replicas"])
            if r is None or not 0 <= r <= 20:
                raise ValueError("replicas must be an integer 0..20")
            args["replicas"] = r
            ds = replace(ds, replicas=r)
        if args.get("products"):
            ps = args["products"]
            ds = replace(ds, products=tuple(x.strip() for x in (ps.split(",") if isinstance(ps, str) else ps) if x.strip()))
        if not ds.products and component in spec.components and spec.components[component].per_product:
            names = self._product_names()
            if names and len(names) == 1:
                ds = replace(ds, products=tuple(names))
        problems = deployspec.check_entry(spec, ds, hosts=self._known_hosts(spec), product_names=self._product_names())
        if problems:
            raise ValueError(f"{target}: " + "; ".join(problems))
        digest = str(args.get("digest") or "")
        if digest and not DIGEST.match(digest):
            raise ValueError("digest must look like sha256:<64 hex>")
        if not digest and not _truthy(args.get("build")):
            digest = ds.digest
        c = spec.components[component]
        if not digest and not (c.build and self.builder is not None):
            raise ValueError(f"{target}: no image digest given or pinned, and nothing can build {c.name}")
        return spec, replace(ds, digest=digest), digest

    def _product_names(self) -> set[str] | None:
        if self.products_dir is None:
            return None
        return {p.stem for p in Path(self.products_dir).glob("*.yaml")}

    def _run(self, action: str, target: str, args: dict, extra: dict) -> Iterator[Step]:
        if action == "deployment.apply":
            spec, ds, digest = self._plan(target, args)
            dep = deployspec.resolve_entry(spec, ds, vars=self.vars, digest=digest)
            c = spec.components[dep.component]
            if not dep.digest:
                digest = yield from self.builder.build(c)
                if not digest:
                    return
                dep = replace(dep, digest=digest)
            extra.update(digest=dep.digest, image=dep.image)
            if self.triggers is not None:
                name = triggers_mod.subscription_name(dep.id)
                if c.consumes:
                    t = triggers_mod.trigger_for(dep.id, c.consumes, ds.products[0] if c.per_product else "")
                    for st in self.triggers.ensure(t, dep.host):
                        yield st
                        if not st.ok:
                            return
                    dep = replace(dep, env={**dep.env, "MC_TRIGGER_SUBSCRIPTION": t.subscription})
                    extra["trigger"] = {"subscription": t.subscription, "types": list(t.types),
                                        **({"product": t.product} if t.product else {})}
                else:                                   # consumes removed since the last apply
                    yield from self.triggers.remove(name)
            steps_ok = True
            for st in self.runtime.apply(dep):
                steps_ok &= st.ok
                yield st
            if steps_ok:
                extra["desired"] = _desired(replace(ds, digest=dep.digest))
        elif action == "deployment.scale":
            ok = True
            for st in self.runtime.scale(target, args["replicas"]):
                ok &= st.ok
                yield st
            stored = self._stored("deployments", target).get("desired")
            if ok and stored:
                extra["desired"] = {**stored, "replicas": args["replicas"]}
        else:
            ok = True
            for st in getattr(self.runtime, action.split(".", 1)[1])(target):
                ok &= st.ok
                yield st
            if action == "deployment.remove" and ok and self.triggers is not None:
                yield from self.triggers.remove(triggers_mod.subscription_name(target))

    # ---------------------------------------------------------------- products, engine, problems
    def _product_file(self, name: str) -> products_mod.Product | None:
        if self.products_dir is None:
            return None
        path = self.products_dir / f"{name}.yaml"
        return products_mod.load(path) if path.exists() else None

    def _effective(self, name: str) -> dict | None:
        doc = None
        if self.store is not None:
            try:
                doc = self.store.get("products", name)
            except Exception:
                doc = None
        if doc and doc.get("effective"):
            return doc["effective"]
        p = self._product_file(name)
        return products_mod.flatten(p) if p else None

    def _check_product(self, name: str, args: dict) -> str:
        if not PRODUCT_ID.match(name):
            return f"not a product name: {name!r}"
        eff = self._effective(name)
        if eff is None:
            return f"no product {name!r} (products/{name}.yaml)"
        f = args.get("field")
        if f not in products_mod.EDITABLE:
            return f"field must be one of {', '.join(products_mod.EDITABLE)}"
        if "old" not in args or "new" not in args:
            return "product.update needs args.old (the value you saw) and args.new"
        labels = None
        if f == "compliance.label" and self.prompts is not None:
            labels = sorted(self.prompts.labels(eff.get("compliance.rules", "compliance/rules")))
        try:
            new = products_mod.check_value(f, args["new"], labels=labels)
            old = products_mod.check_value(f, args["old"]) if f != "compliance.label" else str(args["old"])
        except ValueError as e:
            return str(e)
        cur = eff.get(f)
        same = (float(cur) == float(old)) if f == "compliance.daily_cap_usd" else (str(cur) == str(old))
        if not same:
            return f"stale: {f} is now {cur!r}, not {args['old']!r}; reload and try again"
        if (float(cur) == new) if f == "compliance.daily_cap_usd" else (str(cur) == new):
            return f"{f} is already {cur!r}"
        args["new"], args["old"] = new, cur
        return ""

    def _completed(self, cid: str, action: str, target: str, ok: bool, detail: str, key: str) -> Event:
        data = {"command_id": cid, "action": action, "target": target, "ok": ok}
        data["detail" if ok else "error"] = detail[:600]
        return self._publish("dev.factory.command.completed", f"command-completed:{cid}",
                             f"engine/{self.engine}/command/{cid}", data, key)

    def _run_other(self, cid: str, action: str, target: str, args: dict, cmd: dict, key: str) -> list[Event]:
        who, reason = cmd.get("who", ""), cmd.get("reason", "")
        out: list[Event] = []
        try:
            if action == "product.update":
                out.append(self._publish("dev.factory.product.updated", f"product-updated:{cid}", f"product/{target}",
                                         {"product": target, "field": args["field"], "old": args["old"],
                                          "new": args["new"], "who": who, "reason": reason, "command_id": cid}, key))
                out.append(self._completed(cid, action, target, True,
                                           f"{args['field']}: {args['old']} → {args['new']}", key))
            elif action in ("engine.halt", "engine.unhalt"):
                verb = action.split(".", 1)[1]
                ok, msg = self.consumer(self.app_dir, verb)
                if ok:
                    out.append(self._publish(f"dev.factory.engine.{verb}ed", f"engine-{verb}ed:{cid}",
                                             f"engine/{target}", {"engine": target, "who": who, "reason": reason,
                                                                  "command_id": cid, "via": cmd.get("via", "cli")}, key))
                out.append(self._completed(cid, action, target, ok, msg, key))
            else:
                kind = "acknowledged" if action == "problem.ack" else "resolved"
                eid = f"problem-{'ack' if kind == 'acknowledged' else 'resolved'}:{cid}"
                data = {"problem_id": target, "who": who, "reason": reason, "command_id": cid}
                if args.get("since"):
                    data["since"] = str(args["since"])[:64]
                out.append(self._publish(f"dev.factory.problem.{kind}", eid, f"problem/{target}", data, key))
                out.append(self._completed(cid, action, target, True, f"problem {kind}", key))
        except Exception as e:                          # report, never crash the loop
            out.append(self._completed(cid, action, target, False, f"{type(e).__name__}: {e}", key))
        return out

    def sync_products(self) -> list[Event]:
        """Send product.registered for every product file whose settings changed."""
        out: list[Event] = []
        if self.products_dir is None:
            return out
        for name, p in sorted(products_mod.load_all(self.products_dir).items()):
            h = products_mod.seed_hash(p)
            doc = None
            if self.store is not None:
                try:
                    doc = self.store.get("products", name)
                except Exception:
                    doc = None
            if doc and doc.get("seed_hash") == h:
                continue
            out.append(self._publish("dev.factory.product.registered", f"product-registered:{name}:{h[7:19]}",
                                     f"product/{name}", {"product": name, "seed": products_mod.flatten(p),
                                                         "seed_hash": h, "source": f"products/{name}.yaml"},
                                     f"product-{name}"))
        return out

    # ---------------------------------------------------------------- hosts and logs
    def beat_hosts(self) -> list[Event]:
        """One host.heartbeat per host the runtime serves."""
        out: list[Event] = []
        minute = time.strftime("%Y%m%dT%H%M", time.gmtime())
        for h in self.runtime.hosts():
            try:
                facts = self.runtime.health(h.id)
            except Exception as e:                      # report, never crash the loop
                facts = {"state": "unreachable", "error": f"{type(e).__name__}: {e}"}
            data = {"host": h.id, "kind": h.kind if h.kind in ("local", "vm") else "vm",
                    "state": facts.get("state") if facts.get("state") in HOST_STATES else "degraded"}
            if h.address:
                data["address"] = h.address
            for k, conv in HOST_FACTS.items():
                if facts.get(k) not in (None, ""):
                    try:
                        data[k] = conv(facts[k])
                    except (TypeError, ValueError):
                        pass
            if "error" in data:
                data["error"] = data["error"][:300]
            out.append(self._publish("dev.factory.host.heartbeat", f"host-heartbeat:{h.id}:{minute}",
                                     f"host/{h.id}", data, f"host-{h.id}"))
        return out

    def logs_server(self, path: str):
        """Serve deployment logs on a unix socket (root only) for the console.

        Request: one JSON line {"deployment": id, "tail": n, "follow": bool}.
        Reply: "ok" or "error: …", then log lines (masked by the runtime) until the
        client hangs up (follow) or the lines end."""
        import json as json_mod
        import socketserver
        import threading
        runtime = self.runtime

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    req = json_mod.loads(self.rfile.readline(4096) or b"{}")
                    dep = str(req.get("deployment", ""))
                    tail = max(0, min(int(req.get("tail", 200)), 2000))
                    follow = bool(req.get("follow"))
                    if not DEPLOYMENT_ID.match(dep):
                        raise ValueError(f"not a deployment id: {dep!r}")
                    lines = runtime.follow_logs(dep, tail=tail) if follow else iter(runtime.logs(dep, tail=tail))
                except Exception as e:
                    self.wfile.write(f"error: {type(e).__name__}: {e}\n".encode()[:500])
                    return
                try:
                    self.wfile.write(b"ok\n")
                    for line in lines:
                        self.wfile.write((line[:4000] + "\n").encode("utf-8", "replace"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    close = getattr(lines, "close", None)
                    if close:
                        close()

        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        old = os.umask(0o177)
        try:
            srv = socketserver.ThreadingUnixStreamServer(path, Handler)
        finally:
            os.umask(old)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, name="logs", daemon=True).start()
        log.info("logs on %s", path)
        return srv

    # ---------------------------------------------------------------- loop
    def serve(self, subscription: str, *, max_messages: int = 5, timeout_s: float = 10.0,
              stop: Callable[[], bool] = lambda: False, beat_s: float = 60.0) -> None:
        log.info("handling commands from %s as %s", subscription, self.engine)
        try:
            for ev in self.sync_products():
                log.info("registered %s", ev["subject"])
        except Exception:
            log.exception("product sync failed; commands are still handled")
        import threading

        def beats():
            while not stop():
                try:
                    self.beat_hosts()
                except Exception:
                    log.exception("host heartbeat failed")
                for _ in range(int(beat_s * 10)):
                    if stop():
                        return
                    time.sleep(0.1)
        if beat_s > 0:
            threading.Thread(target=beats, name="host-heartbeats", daemon=True).start()
        while not stop():
            for d in self.bus.pull(subscription, max_messages=max_messages, timeout_s=timeout_s):
                d.ack()                                  # at most once; the audit shows what happened
                try:
                    self.handle(d.event)
                except Exception:
                    log.exception("command %s failed outside its own reporting", d.event.get("id"))


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _truthy(v) -> bool:
    return v is True or str(v).lower() in ("1", "true", "yes", "on")


def _desired(ds: deployspec.DeploymentSpec) -> dict:
    d = {"component": ds.component, "host": ds.host, "replicas": ds.replicas}
    if ds.digest:
        d["digest"] = ds.digest
    if ds.products:
        d["products"] = list(ds.products)
    return d


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
    sv.add_argument("--repo", default=str(deployspec.ROOT), help="checkout used for deploy/, products/, prompts/ and builds")
    sv.add_argument("--app-dir", default="", help="product checkout with factory/consumer.py (halt/unhalt)")
    sv.add_argument("--zone", default=os.environ.get("MC_ZONE", "me-west1-a"), help="zone for VM hosts")
    sv.add_argument("--logs-socket", default=LOGS_SOCKET, help="unix socket the console reads logs from ('' = off)")
    sd = sub.add_parser("send")
    sd.add_argument("action")
    sd.add_argument("target")
    sd.add_argument("--reason", required=True)
    sd.add_argument("--replicas", type=int)
    sd.add_argument("--digest", default="")
    sd.add_argument("--arg", action="append", default=[], help="KEY=VALUE for args (e.g. field=..., old=..., new=...)")
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
        from mission_control.adapters.runtime.gce import Gce
        runtime = registry.get("runtime", "fleet")(local=registry.get("runtime", "docker-local")(),
                                                   gce=Gce(project=o.project, zone=o.zone), store=store)
        h = CommandHandler(runtime=runtime, bus=bus, topic=o.events_topic,
                           engine=o.engine, spec_loader=lambda: deployspec.load(repo / "deploy", products_dir=repo / "products"),
                           vars={"PROJECT": o.project}, builder=DockerBuilder(repo), store=store,
                           products_dir=repo / "products",
                           prompts=registry.get("prompts", "files")(root=str(repo / "prompts")),
                           app_dir=Path(o.app_dir) if o.app_dir else None,
                           triggers=triggers_mod.PubSubTriggers(o.project, topic=o.events_topic))
        if o.logs_socket:
            h.logs_server(o.logs_socket)
        h.serve(o.subscription)
        return 0

    args: dict = {}
    if o.replicas is not None:
        args["replicas"] = o.replicas
    if o.digest:
        args["digest"] = o.digest
    for pair in o.arg:
        k, sep, v = pair.partition("=")
        if not sep:
            print(f"--arg needs KEY=VALUE, got {pair!r}", file=sys.stderr)
            return 2
        args[k] = v
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
