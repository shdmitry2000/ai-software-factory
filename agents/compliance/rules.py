"""Compliance rules as written in the prompt manager.

    ## C-001: No secrets in code
    severity: high
    Flag added lines that contain credentials, tokens, private keys or passwords.

Anything inside <!-- --> is ignored, so the empty rule set can carry its own instructions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SEVERITIES = ("info", "low", "medium", "high")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HEAD = re.compile(r"^##\s+([A-Z][A-Z0-9]*-\d+)\s*:\s*(.+?)\s*$")
_SEV = re.compile(r"^severity\s*:\s*(\w+)\s*$", re.I)


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: str
    body: str


def parse(text: str) -> list[Rule]:
    rules: list[Rule] = []
    cur: dict | None = None

    def close() -> None:
        if cur:
            rules.append(Rule(cur["id"], cur["title"], cur["severity"], "\n".join(cur["body"]).strip()))

    for line in _COMMENT.sub("", text).splitlines():
        if m := _HEAD.match(line):
            close()
            cur = {"id": m.group(1), "title": m.group(2), "severity": "medium", "body": []}
        elif cur is not None and (m := _SEV.match(line.strip())) and not cur["body"]:
            sev = m.group(1).lower()
            if sev not in SEVERITIES:
                raise ValueError(f"rule {cur['id']}: severity {sev!r} not in {SEVERITIES}")
            cur["severity"] = sev
        elif cur is not None:
            cur["body"].append(line)
    close()
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate rule ids: {sorted({i for i in ids if ids.count(i) > 1})}")
    return rules
