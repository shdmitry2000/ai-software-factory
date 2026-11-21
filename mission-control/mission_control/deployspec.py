"""Deploy files → AgentRuntime deployments (docs/deploy-and-hosts-plan.md §2).

    deploy/components/<name>.yaml   what can be deployed (image, command, LLM, events)
    deploy/hosts.yaml               where things can run
    deploy/deployments.yaml         what runs where (desired state, changed by PR)

`load()` checks each file against `schema/deploy.v1.json`, then the files against each
other (components and hosts exist, products exist, event types are known, no secret in
any env, only known ${PLACEHOLDERS}) and reports every problem at once. `resolve()`
turns one deployment into the `ports.Deployment` an AgentRuntime applies.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from mission_control.ports import Deployment, looks_secret

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = Path(__file__).resolve().parents[1] / "schema" / "deploy.v1.json"
PLACEHOLDER = re.compile(r"\$\{([A-Z_]+)\}")
VARS = {"PROJECT", "REGION", "ZONE", "HOST"}
"""Placeholders env values may use; filled from the host's facts at resolve time."""


class SpecError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("deploy files are invalid:\n  " + "\n  ".join(problems))


@dataclass(frozen=True)
class Component:
    name: str
    image: str
    description: str = ""
    build: dict = field(default_factory=dict)
    command: tuple[str, ...] = ()
    llm: str = "none"
    run_as: str = ""
    tmpfs: tuple[str, ...] = ("/tmp",)
    cpus: str = ""
    memory: str = ""
    env: dict[str, str] = field(default_factory=dict)
    per_product: bool = False
    consumes: tuple[str, ...] = ()
    emits: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostSpec:
    id: str
    kind: str
    address: str = ""
    machine_type: str = ""
    zone: str = ""
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeploymentSpec:
    component: str
    host: str
    digest: str = ""
    replicas: int = 1
    products: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.component}@{self.host}"


@dataclass(frozen=True)
class Spec:
    components: dict[str, Component]
    hosts: dict[str, HostSpec]
    deployments: dict[str, DeploymentSpec]

    def chains(self) -> list[tuple[str, str, str]]:
        """Edges of the agent chain graph: (from, kind, to). kind is `starts` (event →
        component), `emits` (component → event) or `feeds` (component → component,
        when one emits what another consumes)."""
        edges: list[tuple[str, str, str]] = []
        for c in sorted(self.components.values(), key=lambda c: c.name):
            edges += [(t, "starts", c.name) for t in c.consumes]
            edges += [(c.name, "emits", t) for t in c.emits]
        for a in self.components.values():
            for b in self.components.values():
                if a.name != b.name and set(a.emits) & set(b.consumes):
                    edges.append((a.name, "feeds", b.name))
        return edges


def _validator(defn: str) -> Draft202012Validator:
    schema = json.loads(SCHEMA.read_text())
    return Draft202012Validator({**schema, "$ref": f"#/$defs/{defn}"})


def _read(path: Path, defn: str, problems: list[str]) -> dict | None:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        problems.append(f"{path.name}: cannot read: {e}")
        return None
    errs = sorted(_validator(defn).iter_errors(raw), key=lambda e: list(e.path))
    for e in errs:
        where = "/".join(str(x) for x in e.path) or "(top)"
        problems.append(f"{path.name}: {where}: {e.message}")
    return None if errs else raw


def _check_env(where: str, env: dict[str, str], problems: list[str]) -> None:
    for k, v in env.items():
        if looks_secret(k):
            problems.append(f"{where}: env {k} looks like a secret; secrets never go in deploy files")
        for var in PLACEHOLDER.findall(v):
            if var not in VARS:
                problems.append(f"{where}: env {k} uses unknown ${{{var}}}; known: {', '.join(sorted(VARS))}")


def load(deploy_dir: str | Path = ROOT / "deploy", *, products_dir: str | Path | None = ROOT / "products",
         known_types: frozenset[str] | None = None) -> Spec:
    d = Path(deploy_dir)
    problems: list[str] = []
    if known_types is None:
        from mission_control import events
        known_types = events.known_types()

    components: dict[str, Component] = {}
    for path in sorted((d / "components").glob("*.yaml")):
        raw = _read(path, "component", problems)
        if raw is None:
            continue
        if raw["name"] != path.stem:
            problems.append(f"{path.name}: name {raw['name']!r} must match the file name")
            continue
        res = raw.get("resources") or {}
        c = Component(name=raw["name"], image=raw["image"], description=raw.get("description", ""),
                      build=raw.get("build") or {}, command=tuple(raw.get("command") or ()),
                      llm=raw.get("llm", "none"), run_as=raw.get("run_as", ""),
                      tmpfs=tuple(raw.get("tmpfs") or ("/tmp",)), cpus=res.get("cpus", ""),
                      memory=res.get("memory", ""), env=raw.get("env") or {},
                      per_product=bool(raw.get("per_product", False)),
                      consumes=tuple(x["type"] for x in raw.get("consumes") or ()),
                      emits=tuple(raw.get("emits") or ()))
        _check_env(path.name, c.env, problems)
        for t in (*c.consumes, *c.emits):
            if t not in known_types:
                problems.append(f"{path.name}: event type {t} is not in schema/types.md")
        components[c.name] = c

    hosts: dict[str, HostSpec] = {}
    hraw = _read(d / "hosts.yaml", "hosts", problems)
    for h in (hraw or {}).get("hosts", []):
        if h["id"] in hosts:
            problems.append(f"hosts.yaml: host {h['id']} listed twice")
        hosts[h["id"]] = HostSpec(h["id"], h["kind"], h.get("address", ""), h.get("machine_type", ""),
                                  h.get("zone", ""), h.get("labels") or {})
    locals_ = [h.id for h in hosts.values() if h.kind == "local"]
    if hraw is not None and locals_ != ["local"]:
        problems.append(f"hosts.yaml: exactly one host of kind local, with id 'local' (the main node); found {locals_}")

    product_names: set[str] | None = None
    if products_dir is not None:
        product_names = {p.stem for p in Path(products_dir).glob("*.yaml")}

    deployments: dict[str, DeploymentSpec] = {}
    dpath = d / "deployments.yaml"
    draw = _read(dpath, "deployments", problems) if dpath.exists() else {"deployments": []}
    for i, x in enumerate((draw or {}).get("deployments", [])):
        ds = DeploymentSpec(x["component"], x["host"], x.get("digest", ""), x.get("replicas", 1),
                            tuple(x.get("products") or ()), x.get("env") or {})
        where = f"deployments.yaml: {ds.id}"
        if ds.id in deployments:
            problems.append(f"{where}: listed twice (one deployment per component per host)")
        c = components.get(ds.component)
        if c is None:
            problems.append(f"{where}: no component {ds.component!r} in components/")
        if ds.host not in hosts:
            problems.append(f"{where}: no host {ds.host!r} in hosts.yaml")
        if c is not None and c.per_product and len(ds.products) != 1:
            problems.append(f"{where}: {c.name} works on one product per deployment; list exactly one")
        if c is not None and not c.per_product and ds.products:
            problems.append(f"{where}: {c.name} is not per product; remove products")
        if product_names is not None:
            for p in ds.products:
                if p not in product_names:
                    problems.append(f"{where}: no product {p!r} in products/")
        _check_env(where, ds.env, problems)
        deployments[ds.id] = ds

    if problems:
        raise SpecError(problems)
    return Spec(components, hosts, deployments)


def resolve(spec: Spec, deployment_id: str, *, vars: dict[str, str] | None = None, digest: str = "") -> Deployment:
    """The runtime Deployment for one entry. `vars` fill ${PLACEHOLDERS}; `digest`
    overrides the pinned one (a fresh build on the host)."""
    try:
        ds = spec.deployments[deployment_id]
    except KeyError:
        raise KeyError(f"no deployment {deployment_id!r}; known: {', '.join(sorted(spec.deployments)) or 'none'}") from None
    return resolve_entry(spec, ds, vars=vars, digest=digest)


def check_entry(spec: Spec, ds: DeploymentSpec, *, hosts: set[str] | None = None,
                product_names: set[str] | None = None) -> list[str]:
    """Problems with one deployment that may not be in deployments.yaml (a console
    deploy): the component exists, the host is known, the products fit."""
    problems = []
    c = spec.components.get(ds.component)
    if c is None:
        return [f"no component {ds.component!r} in deploy/components/"]
    if ds.host not in (hosts if hosts is not None else set(spec.hosts)):
        problems.append(f"no host {ds.host!r}")
    if c.per_product and len(ds.products) != 1:
        problems.append(f"{c.name} works on one product per deployment; choose exactly one")
    if not c.per_product and ds.products:
        problems.append(f"{c.name} is not per product; no products")
    if product_names is not None:
        problems += [f"no product {p!r} in products/" for p in ds.products if p not in product_names]
    if not 0 <= ds.replicas <= 20:
        problems.append("replicas must be 0..20")
    return problems


def resolve_entry(spec: Spec, ds: DeploymentSpec, *, vars: dict[str, str] | None = None,
                  digest: str = "") -> Deployment:
    c = spec.components[ds.component]
    values = {"HOST": ds.host, **(vars or {})}
    env = {**c.env, **ds.env}
    if c.per_product:
        env["FACTORY_PRODUCT"] = ds.products[0]
    missing = sorted({v for val in env.values() for v in PLACEHOLDER.findall(val) if v not in values})
    if missing:
        raise SpecError([f"{ds.id}: no value for {', '.join('${' + m + '}' for m in missing)} (pass --var)"])
    env = {k: PLACEHOLDER.sub(lambda m: values[m.group(1)], v) for k, v in env.items()}
    return Deployment(component=c.name, host=ds.host, image=c.image, digest=digest or ds.digest,
                      command=c.command, env=env, llm=c.llm, replicas=ds.replicas, run_as=c.run_as,
                      tmpfs=c.tmpfs, cpus=c.cpus, memory=c.memory)
