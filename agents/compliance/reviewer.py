"""The one model call: review a diff against rules, return findings and cost.

`ClaudeReviewer` runs headless Claude Code with no tools, low effort, in an empty
directory, so the model sees only the rules and the diff. On GKE it authenticates to
Vertex AI through Workload Identity (CLAUDE_CODE_USE_VERTEX=1); there is no secret.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Protocol

from .rules import SEVERITIES, Rule


@dataclass
class Review:
    findings: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    model: str = ""


class Reviewer(Protocol):
    model: str

    def review(self, *, diff: str, rules: list[Rule], title: str) -> Review: ...


PROMPT = """You are an ADVISORY compliance reviewer. You never approve or block; you report.
Check the pull request diff below ONLY against these rules. Report a finding only when an
ADDED or CHANGED line clearly breaks a rule. No style advice, no rules of your own.

Rules:
{rules}

Pull request title: {title}

Answer with JSON only, no prose, exactly:
{{"findings": [{{"rule": "<rule id>", "severity": "<{sev}>", "title": "<short>",
  "file": "<path>", "line": <line in the new file or 0>, "detail": "<one or two sentences>"}}]}}
Use {{"findings": []}} when nothing breaks a rule.

Diff:
{diff}
"""


def render(rules: list[Rule], diff: str, title: str) -> str:
    text = "\n".join(f"- {r.id} ({r.severity}): {r.title}. {r.body}".strip() for r in rules)
    return PROMPT.format(rules=text, title=title[:300], diff=diff, sev="|".join(SEVERITIES))


def parse_findings(text: str, rules: list[Rule]) -> list[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("reviewer returned no JSON")
    raw = json.loads(m.group(0)).get("findings") or []
    known = {r.id: r for r in rules}
    out = []
    for f in raw[:50]:
        if not isinstance(f, dict) or f.get("rule") not in known:
            continue                     # findings must name a real rule
        sev = f.get("severity") if f.get("severity") in SEVERITIES else known[f["rule"]].severity
        item = {"rule": f["rule"], "severity": sev, "title": str(f.get("title", ""))[:300] or known[f["rule"]].title}
        if f.get("file"):
            item["file"] = str(f["file"])[:512]
        if isinstance(f.get("line"), int) and f["line"] >= 0:
            item["line"] = f["line"]
        if f.get("detail"):
            item["detail"] = str(f["detail"])[:2000]
        out.append(item)
    return out


class ClaudeReviewer:
    def __init__(self, model: str = "claude-haiku-5-5", effort: str = "low", claude: str = "claude",
                 timeout_s: float = 300.0) -> None:
        self.model, self.effort, self.claude, self.timeout_s = model, effort, claude, timeout_s

    def review(self, *, diff: str, rules: list[Rule], title: str) -> Review:
        cmd = [self.claude, "-p", "--output-format", "json", "--model", self.model, "--effort", self.effort,
               "--tools", "", "--no-session-persistence"]
        with tempfile.TemporaryDirectory(prefix="compliance-") as empty:
            r = subprocess.run(cmd, input=render(rules, diff, title), capture_output=True, text=True,
                               cwd=empty, timeout=self.timeout_s)
        if r.returncode != 0:
            raise RuntimeError(f"claude exited {r.returncode}: {(r.stderr or r.stdout)[-500:]}")
        out = json.loads(r.stdout)
        if out.get("is_error"):
            raise RuntimeError(f"claude error: {str(out.get('result'))[:500]}")
        return Review(findings=parse_findings(out.get("result", ""), rules),
                      cost_usd=float(out.get("total_cost_usd") or 0.0), model=self.model)


class FakeReviewer:
    """For tests and dry runs: records calls, returns canned findings."""

    def __init__(self, findings: list[dict] | None = None, cost_usd: float = 0.01, model: str = "fake") -> None:
        self.findings, self.cost_usd, self.model, self.calls = findings or [], cost_usd, model, []

    def review(self, *, diff: str, rules: list[Rule], title: str) -> Review:
        self.calls.append({"diff": diff, "rules": [r.id for r in rules], "title": title})
        return Review(findings=list(self.findings), cost_usd=self.cost_usd, model=self.model)
