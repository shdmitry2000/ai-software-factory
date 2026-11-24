#!/usr/bin/env python3
"""Validate every example event: envelope, type list, and the type's data schema.

    uv run --with jsonschema --with rfc3339-validator python mission-control/schema/validate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # the mission_control package, without installing it

from mission_control.events import known_types, validate  # noqa: E402


def main() -> int:
    examples = sorted((HERE / "examples").glob("*.json"))
    failures = 0
    for path in examples:
        errors = validate(json.loads(path.read_text()))
        print(("ok   " if not errors else "FAIL ") + path.name)
        for e in errors:
            print("     " + e)
        failures += bool(errors)
    print(f"{len(examples) - failures}/{len(examples)} valid; {len(known_types())} types known")
    return 1 if failures or not examples else 0


if __name__ == "__main__":
    sys.exit(main())
