"""Product registry: `products/<product>.yaml` in the factory repo.

The file seeds a product's settings; the console changes them later with a
`dev.factory.product.updated` event, and the `products` projection holds the current
values. A PR's base branch always comes from here, never from an assumption.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

MODES = {"off", "advisory"}          # "enforcing" is a later, explicit owner decision


@dataclass(frozen=True)
class Compliance:
    mode: str = "off"
    rules: str = "compliance/rules"   # PromptRegistry name
    label: str = "production"
    daily_cap_usd: float = 1.0


@dataclass(frozen=True)
class Product:
    name: str
    repo: str                         # owner/name on the code host
    base_branch: str
    codehost: str = "github"
    compliance: Compliance = field(default_factory=Compliance)


def parse(name: str, raw: dict) -> Product:
    if not isinstance(raw, dict):
        raise ValueError(f"product {name}: expected a mapping")
    for key in ("repo", "base_branch"):
        if not raw.get(key):
            raise ValueError(f"product {name}: '{key}' is required")
    c = raw.get("compliance") or {}
    comp = Compliance(mode=c.get("mode", "off"), rules=c.get("rules", "compliance/rules"),
                      label=c.get("label", "production"), daily_cap_usd=float(c.get("daily_cap_usd", 1.0)))
    if comp.mode not in MODES:
        raise ValueError(f"product {name}: compliance mode {comp.mode!r} not in {sorted(MODES)}")
    return Product(name=name, repo=raw["repo"], base_branch=raw["base_branch"],
                   codehost=raw.get("codehost", "github"), compliance=comp)


def load(path: str | Path) -> Product:
    p = Path(path)
    return parse(p.stem, yaml.safe_load(p.read_text()))


def load_all(directory: str | Path) -> dict[str, Product]:
    return {p.name: p for p in (load(f) for f in sorted(Path(directory).glob("*.yaml")))}
