"""Product registry: `products/<product>.yaml` in the factory repo.

The file seeds a product's settings; the console changes them later with a
`dev.factory.product.updated` event, and the `products` projection holds the current
values. A PR's base branch always comes from here, never from an assumption.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
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


# ---------------------------------------------------------------------- console-editable settings
# The file seeds a product; the console changes these fields with product.updated
# events; Firestore products/{name}.effective holds the current values, which the engine
# and agents read at the start of each lap or tick.
EDITABLE = ("base_branch", "compliance.mode", "compliance.label", "compliance.daily_cap_usd")
_BRANCH = re.compile(r"^(?!/)(?!.*//)(?!.*\.\.)(?!.*@\{)[A-Za-z0-9._/-]{1,200}(?<![./])$")


def flatten(p: Product) -> dict:
    """The product's settings as flat field → value (the seed sent in product.registered)."""
    return {"repo": p.repo, "codehost": p.codehost, "base_branch": p.base_branch,
            "compliance.mode": p.compliance.mode, "compliance.rules": p.compliance.rules,
            "compliance.label": p.compliance.label, "compliance.daily_cap_usd": float(p.compliance.daily_cap_usd)}


def seed_hash(p: Product) -> str:
    body = json.dumps(flatten(p), sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def apply_effective(p: Product, effective: dict | None) -> Product:
    """`p` with the console-editable fields taken from `effective` where present."""
    if not effective:
        return p
    c = p.compliance
    comp = replace(c, mode=effective.get("compliance.mode", c.mode),
                   label=effective.get("compliance.label", c.label),
                   daily_cap_usd=float(effective.get("compliance.daily_cap_usd", c.daily_cap_usd)))
    return replace(p, base_branch=effective.get("base_branch", p.base_branch), compliance=comp)


def check_value(field: str, value, *, labels: list[str] | None = None):
    """Normalized value, or raise ValueError with a reason a person can act on."""
    if field not in EDITABLE:
        raise ValueError(f"{field} cannot be changed from the console; editable: {', '.join(EDITABLE)}")
    if field == "base_branch":
        v = str(value).strip()
        if not _BRANCH.match(v):
            raise ValueError(f"{v!r} is not a valid branch name")
        return v
    if field == "compliance.mode":
        v = str(value).strip()
        if v not in MODES:
            raise ValueError(f"mode must be one of {', '.join(sorted(MODES))}")
        return v
    if field == "compliance.label":
        v = str(value).strip()
        if not v:
            raise ValueError("label cannot be empty")
        if labels is not None and v not in labels:
            raise ValueError(f"no rules version has the label {v!r}; labels: {', '.join(labels) or 'none'}")
        return v
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError("daily cap must be a number of dollars") from None
    if not 0 <= v <= 100:
        raise ValueError("daily cap must be between 0 and 100 dollars")
    return v
