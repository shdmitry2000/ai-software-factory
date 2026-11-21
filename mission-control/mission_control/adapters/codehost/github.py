"""CodeHost on GitHub's REST API, read-only.

Without a token it reads public repositories (60 requests an hour per IP, enough for a
10-minute poll). With one (later: a 1-hour GitHub App installation token minted by the
main node) the same calls work on private repositories.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from mission_control.ports import Change
from mission_control.registry import register

API = "https://api.github.com"


class GitHubCodeHost:
    def __init__(self, repo: str, token: str = "", api: str = API, timeout_s: float = 30.0) -> None:
        self.repo, self.api, self.timeout_s = repo, api.rstrip("/"), timeout_s
        self.token = token or os.environ.get("GITHUB_TOKEN", "")

    def _get(self, path: str, accept: str = "application/vnd.github+json", max_bytes: int = 0) -> tuple[bytes, bool]:
        req = urllib.request.Request(self.api + path, headers={
            "Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "factory-compliance-agent"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            if not max_bytes:
                return r.read(), False
            body = r.read(max_bytes + 1)
            return body[:max_bytes], len(body) > max_bytes

    def _change(self, pr: dict) -> Change:
        return Change(id=f"codehost:github/{self.repo}#{pr['number']}", title=pr.get("title", ""),
                      state="merged" if pr.get("merged_at") else pr.get("state", "open"),
                      head=pr["head"]["sha"], base=pr["base"]["ref"], url=pr.get("html_url", ""))

    def number(self, change_id: str) -> int:
        repo, _, n = change_id.removeprefix("codehost:github/").rpartition("#")
        if repo != self.repo or not n.isdigit():
            raise ValueError(f"{change_id!r} is not a change of {self.repo}")
        return int(n)

    def changes(self, *, state: str = "open") -> list[Change]:
        out, page = [], 1
        while True:
            body, _ = self._get(f"/repos/{self.repo}/pulls?state={state}&per_page=100&page={page}")
            prs = json.loads(body)
            out += [self._change(p) for p in prs]
            if len(prs) < 100:
                return out
            page += 1

    def get_change(self, change_id: str) -> Change | None:
        try:
            body, _ = self._get(f"/repos/{self.repo}/pulls/{self.number(change_id)}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return self._change(json.loads(body))

    def protected_paths(self) -> list[str]:
        return []

    def diff(self, change_id: str, *, max_bytes: int = 200_000) -> tuple[str, bool]:
        body, truncated = self._get(f"/repos/{self.repo}/pulls/{self.number(change_id)}",
                                    accept="application/vnd.github.diff", max_bytes=max_bytes)
        return body.decode("utf-8", "replace"), truncated


@register("codehost", "github")
def make(repo: str, token: str = "", api: str = API) -> GitHubCodeHost:
    return GitHubCodeHost(repo, token=token, api=api)
