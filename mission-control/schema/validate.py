#!/usr/bin/env python3
"""Validate example events against the envelope (and, once they exist, their data schemas).

    uv run --with jsonschema --with rfc3339-validator python mission-control/schema/validate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

HERE = Path(__file__).resolve().parent
TYPES = {line.split("`")[1] for line in (HERE / "types.md").read_text().splitlines()
         if line.startswith("| `dev.factory.")}


def main() -> int:
    envelope = Draft202012Validator(json.loads((HERE / "envelope.v1.json").read_text()),
                                    format_checker=FormatChecker())
    failures = 0
    examples = sorted((HERE / "examples").glob("*.json"))
    for path in examples:
        event = json.loads(path.read_text())
        errors = [e.message for e in envelope.iter_errors(event)]
        if event.get("type") not in TYPES:
            errors.append(f"type {event.get('type')!r} is not in types.md")
        data_schema = HERE / "data" / f"{event.get('type', '').removeprefix('dev.factory.')}.json"
        if data_schema.exists():
            v = Draft202012Validator(json.loads(data_schema.read_text()), format_checker=FormatChecker())
            errors += [f"data: {e.message}" for e in v.iter_errors(event.get("data", {}))]
        print(("ok   " if not errors else "FAIL ") + path.name)
        for e in errors:
            print("     " + e)
        failures += bool(errors)
    print(f"{len(examples) - failures}/{len(examples)} valid; {len(TYPES)} types known")
    return 1 if failures or not examples else 0


if __name__ == "__main__":
    sys.exit(main())
