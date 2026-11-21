"""Drive the AgentRuntime port from a shell on the main node (until the console's
command handler does it, Stage R4). `spec` and `apply-spec` read the deploy/ files
(need pyyaml and jsonschema: run through uv); the other commands are standard library.

    python3 -m mission_control.runtime spec                       # check deploy/, show the plan
    python3 -m mission_control.runtime apply-spec compliance@local --var PROJECT=p --digest sha256:…

    python3 -m mission_control.runtime apply --component compliance --image factory/agent:abc \\
        --digest sha256:… --llm project --run-as 10001 --tmpfs /tmp --tmpfs /home/agent \\
        --env GCP_PROJECT=my-project -- --bus pubsub --store firestore
    python3 -m mission_control.runtime status
    python3 -m mission_control.runtime logs compliance@local --tail 50
    python3 -m mission_control.runtime stop|start|restart|remove compliance@local
    python3 -m mission_control.runtime scale compliance@local 2
"""
from __future__ import annotations

import argparse
import sys

from mission_control import registry
from mission_control.ports import LLM_METHODS, Deployment


def _env(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        k, sep, v = p.partition("=")
        if not sep:
            raise SystemExit(f"expected KEY=VALUE, got {p!r}")
        out[k] = v
    return out


def _show(steps) -> int:
    ok = True
    for s in steps:
        ok &= s.ok
        print(f"{'ok ' if s.ok else 'ERR'} {s.step:<10} {s.detail}", flush=True)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m mission_control.runtime")
    ap.add_argument("--adapter", default="docker-local")
    ap.add_argument("--env-dir", default="", help="where credential files go (default /etc/factory)")
    ap.add_argument("--login-file", default="", help="the main node's login file (default /root/.factory-env)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--component", required=True)
    a.add_argument("--host", default="local")
    a.add_argument("--image", required=True)
    a.add_argument("--digest", default="")
    a.add_argument("--llm", choices=LLM_METHODS, default="none")
    a.add_argument("--replicas", type=int, default=1)
    a.add_argument("--run-as", default="")
    a.add_argument("--tmpfs", action="append", default=[])
    a.add_argument("--env", action="append", default=[])
    a.add_argument("--cpus", default="")
    a.add_argument("--memory", default="")
    a.add_argument("command", nargs="*")
    sp = sub.add_parser("spec")
    sp.add_argument("--deploy-dir", default="")
    ap_ = sub.add_parser("apply-spec")
    ap_.add_argument("deployment")
    ap_.add_argument("--deploy-dir", default="")
    ap_.add_argument("--digest", default="")
    ap_.add_argument("--var", action="append", default=[], help="PLACEHOLDER=value, e.g. PROJECT=my-project")
    sub.add_parser("status")
    sub.add_parser("hosts")
    lg = sub.add_parser("logs")
    lg.add_argument("deployment")
    lg.add_argument("--tail", type=int, default=200)
    for verb in ("start", "stop", "restart", "remove"):
        sub.add_parser(verb).add_argument("deployment")
    sc = sub.add_parser("scale")
    sc.add_argument("deployment")
    sc.add_argument("replicas", type=int)
    o = ap.parse_args(argv)

    if o.cmd in ("spec", "apply-spec"):
        from mission_control import deployspec
        try:
            spec = deployspec.load(o.deploy_dir) if o.deploy_dir else deployspec.load()
        except deployspec.SpecError as e:
            print(e, file=sys.stderr)
            return 2
        if o.cmd == "spec":
            print("hosts:       " + ", ".join(f"{h.id} ({h.kind})" for h in spec.hosts.values()))
            print("components:  " + ", ".join(f"{c.name} (llm {c.llm})" for c in spec.components.values()))
            for ds in spec.deployments.values():
                print(f"deployment:  {ds.id}  replicas {ds.replicas}  products {', '.join(ds.products) or '-'}"
                      f"  digest {ds.digest[:19] or '(fresh build)'}")
            for a, kind, b in spec.chains():
                print(f"chain:       {a} --{kind}--> {b}")
            return 0
        try:
            d = deployspec.resolve(spec, o.deployment, vars=_env(o.var), digest=o.digest)
        except (KeyError, deployspec.SpecError) as e:
            print(f"error: {e.args[0] if isinstance(e, KeyError) else e}", file=sys.stderr)
            return 2

    settings = {k: v for k, v in (("env_dir", o.env_dir), ("login_file", o.login_file)) if v}
    rt = registry.get("runtime", o.adapter)(**settings)
    try:
        if o.cmd == "apply-spec":
            return _show(rt.apply(d))
        if o.cmd == "apply":
            d = Deployment(component=o.component, host=o.host, image=o.image, digest=o.digest,
                           command=tuple(o.command), env=_env(o.env), llm=o.llm, replicas=o.replicas,
                           run_as=o.run_as, tmpfs=tuple(o.tmpfs or ["/tmp"]), cpus=o.cpus, memory=o.memory)
            return _show(rt.apply(d))
        if o.cmd == "status":
            for i in rt.status():
                print(f"{i.deployment:<32} {i.name:<28} {i.state:<10} {i.digest[:19] or i.image}  {i.started_at}")
            return 0
        if o.cmd == "hosts":
            for h in rt.hosts():
                print(f"{h.id:<16} {h.kind:<6} {h.state:<12} {h.labels}")
            return 0
        if o.cmd == "logs":
            print("\n".join(rt.logs(o.deployment, tail=o.tail)))
            return 0
        if o.cmd == "scale":
            return _show(rt.scale(o.deployment, o.replicas))
        return _show(getattr(rt, o.cmd)(o.deployment))
    except (KeyError, ValueError) as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
