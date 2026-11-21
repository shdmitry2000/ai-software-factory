"""Validate factory events against the schemas in `schema/` (envelope, type list, data).

The schema directory is `$MC_SCHEMA_DIR`, or `schema/` next to this package in a checkout.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from mission_control.ports import Event


def schema_dir() -> Path:
    return Path(os.environ.get("MC_SCHEMA_DIR") or Path(__file__).resolve().parent.parent / "schema")


@lru_cache(maxsize=1)
def _load() -> tuple[Draft202012Validator, frozenset[str], dict]:
    d = schema_dir()
    envelope = Draft202012Validator(json.loads((d / "envelope.v1.json").read_text()), format_checker=FormatChecker())
    types = frozenset(line.split("`")[1] for line in (d / "types.md").read_text().splitlines()
                      if line.startswith("| `dev.factory."))
    data = json.loads((d / "data.v1.json").read_text())
    return envelope, types, data


def known_types() -> frozenset[str]:
    return _load()[1]


def validate(event: Event) -> list[str]:
    """Problems with `event`; empty when it is a valid, publishable factory event."""
    envelope, types, data = _load()
    errors = [e.message for e in envelope.iter_errors(event)]
    etype = event.get("type") if isinstance(event, dict) else None
    if etype not in types:
        errors.append(f"type {etype!r} is not in types.md")
        return errors
    key = etype.removeprefix("dev.factory.")
    if key not in data["$defs"]:
        errors.append(f"no data schema for {key!r} in data.v1.json")
    else:
        v = Draft202012Validator({**data, "$ref": f"#/$defs/{key}"}, format_checker=FormatChecker())
        errors += [f"data: {e.message}" for e in v.iter_errors(event.get("data", {}))]
    return errors


def now() -> str:
    """UTC now, RFC 3339 with microseconds, as every factory source writes it."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def make(etype: str, eid: str, subject: str, data: dict, *, source: str, engine: str,
         time: str = "", **ext: str) -> Event:
    """A factory event envelope (CloudEvents 1.0, structured JSON)."""
    return {"specversion": "1.0", "id": eid, "source": source, "type": etype, "subject": subject,
            "time": time or now(), "datacontenttype": "application/json", "factoryengine": engine,
            **ext, "data": data}
