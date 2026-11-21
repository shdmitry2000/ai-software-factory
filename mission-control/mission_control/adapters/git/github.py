"""GitActivity on GitHub's REST API, by polling with ETags.

Every GET remembers the answer's ETag; the next identical GET sends `If-None-Match` and a
`304 Not Modified` reuses the remembered body. With a token, a 304 costs no rate limit, so
polling every 60 s is free while nothing changes. Without a token (public repositories
only) GitHub allows 60 requests an hour per IP, and 304s count; poll less often then.

The token is passed in (or a `token_source` callable that is asked again after a 401),
never taken from a command line. It is sent only to the API host.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone

from mission_control.ports import GitCheck, GitError, GitIssue, GitPull
from mission_control.registry import register

API = "https://api.github.com"
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubActivity:
    def __init__(self, token: str = "", *, token_source: Callable[[], str] | None = None, api: str = API,
                 timeout_s: float = 30.0, max_pages: int = 10, user_agent: str = "factory-git-poller") -> None:
        self.api, self.timeout_s, self.max_pages, self.user_agent = api.rstrip("/"), timeout_s, max_pages, user_agent
        self._token_source = token_source
        self._token = token or (token_source() if token_source else "")
        self._asked_at = time.monotonic()
        self._cache: dict[str, tuple[str, object, str]] = {}   # url → (etag, parsed body, next url)
        self._lock = threading.Lock()
        self.requests = self.not_modified = 0
        self.rate: dict = {}

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def _maybe_ask(self) -> None:
        """Without a token, ask the source again every 5 minutes (the gh login may arrive
        after the poller started, as it does in rebuild.sh)."""
        if not self._token and self._token_source is not None and time.monotonic() - self._asked_at >= 300:
            self._asked_at = time.monotonic()
            self._token = self._token_source()

    # ------------------------------------------------------------------ http
    def _request(self, url: str, *, retry_auth: bool = True) -> tuple[object, str]:
        """(parsed body, next page url) for one GET, through the ETag cache."""
        if not url.startswith(self.api + "/"):
            raise GitError("http", f"refusing a URL outside {self.api}")
        self._maybe_ask()
        cached = self._cache.get(url)
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "X-GitHub-Api-Version": "2022-11-28",
                                                   "User-Agent": self.user_agent})
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        if cached:
            req.add_header("If-None-Match", cached[0])
        with self._lock:
            self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                self._note_rate(r.headers)
                body = json.loads(r.read() or b"null")
                nxt = _next(r.headers.get("Link", ""))
                etag = r.headers.get("ETag", "")
                if etag:
                    self._cache[url] = (etag, body, nxt)
                return body, nxt
        except urllib.error.HTTPError as e:
            self._note_rate(e.headers)
            if e.code == 304 and cached:
                with self._lock:
                    self.not_modified += 1
                return cached[1], cached[2]
            if e.code == 401:
                if retry_auth and self._token_source is not None:
                    self._token = self._token_source()
                    return self._request(url, retry_auth=False)
                raise GitError("auth", "GitHub refused the token (401)") from None
            if e.code in (403, 429) and (e.headers.get("X-RateLimit-Remaining") == "0" or e.headers.get("Retry-After")):
                raise GitError("rate_limited", f"GitHub rate limit reached ({e.code})",
                               reset_at=self.rate.get("rate_reset", "")) from None
            if e.code == 404:
                raise GitError("not_found", f"not found: {url.removeprefix(self.api)}") from None
            raise GitError("http", f"GitHub answered {e.code} for {url.removeprefix(self.api)}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            raise GitError("network", f"GitHub unreachable: {getattr(e, 'reason', e)}") from None

    def _note_rate(self, headers) -> None:
        if headers is None or headers.get("X-RateLimit-Limit") is None:
            return
        try:
            reset = datetime.fromtimestamp(int(headers.get("X-RateLimit-Reset", "0")), timezone.utc)
            self.rate = {"rate_limit": int(headers["X-RateLimit-Limit"]),
                         "rate_remaining": int(headers.get("X-RateLimit-Remaining", "0")),
                         "rate_reset": reset.strftime("%Y-%m-%dT%H:%M:%SZ")}
        except (TypeError, ValueError):
            pass

    def _get(self, path: str) -> object:
        return self._request(self.api + path)[0]

    def _get_all(self, path: str) -> list:
        out: list = []
        url = self.api + path
        for _ in range(self.max_pages):
            body, url = self._request(url)
            out += body if isinstance(body, list) else []
            if not url:
                break
        return out

    def _get_one(self, path: str) -> dict | None:
        try:
            body = self._get(path)
        except GitError as e:
            if e.kind == "not_found":
                return None
            raise
        return body if isinstance(body, dict) else None

    # ------------------------------------------------------------------ GitActivity
    def pulls(self, repo: str) -> list[GitPull]:
        return [_pull(p) for p in self._get_all(f"/repos/{_repo(repo)}/pulls?state=open&per_page=100")]

    def pull(self, repo: str, number: int) -> GitPull | None:
        raw = self._get_one(f"/repos/{_repo(repo)}/pulls/{int(number)}")
        return _pull(raw) if raw else None

    def branches(self, repo: str) -> dict[str, str]:
        return {b["name"]: b["commit"]["sha"] for b in self._get_all(f"/repos/{_repo(repo)}/branches?per_page=100")}

    def checks(self, repo: str, sha: str) -> list[GitCheck]:
        if not _SHA.match(sha):
            raise ValueError(f"not a commit sha: {sha!r}")
        body = self._get(f"/repos/{_repo(repo)}/commits/{sha}/check-runs?per_page=100")
        runs = body.get("check_runs", []) if isinstance(body, dict) else []
        return [GitCheck(id=str(c["id"]), name=c.get("name", ""), status=c.get("status", ""),
                         conclusion=c.get("conclusion") or "", url=c.get("html_url") or "",
                         completed_at=c.get("completed_at") or "") for c in runs]

    def issues(self, repo: str) -> list[GitIssue]:
        raw = self._get_all(f"/repos/{_repo(repo)}/issues?state=open&per_page=100")
        return [_issue(i) for i in raw if "pull_request" not in i]

    def issue(self, repo: str, number: int) -> GitIssue | None:
        raw = self._get_one(f"/repos/{_repo(repo)}/issues/{int(number)}")
        return _issue(raw) if raw and "pull_request" not in raw else None

    def usage(self) -> dict:
        return {"requests": self.requests, "not_modified": self.not_modified,
                "authenticated": self.authenticated, **self.rate}


def _repo(repo: str) -> str:
    if not _REPO.match(repo):
        raise ValueError(f"not an owner/name repository: {repo!r}")
    return repo


def _next(link: str) -> str:
    m = _NEXT.search(link or "")
    return m.group(1) if m else ""


def _pull(p: dict) -> GitPull:
    return GitPull(number=int(p["number"]), title=p.get("title") or "",
                   state="merged" if p.get("merged_at") else p.get("state", "open"),
                   head=p["head"]["sha"], base=p["base"]["ref"], author=(p.get("user") or {}).get("login", ""),
                   url=p.get("html_url") or "", draft=bool(p.get("draft")), created_at=p.get("created_at") or "",
                   updated_at=p.get("updated_at") or "", closed_at=p.get("closed_at") or "",
                   merged_at=p.get("merged_at") or "")


def _issue(i: dict) -> GitIssue:
    return GitIssue(number=int(i["number"]), title=i.get("title") or "", state=i.get("state", "open"),
                    labels=tuple(sorted(lb["name"] if isinstance(lb, dict) else str(lb) for lb in i.get("labels") or [])),
                    author=(i.get("user") or {}).get("login", ""), url=i.get("html_url") or "",
                    created_at=i.get("created_at") or "", updated_at=i.get("updated_at") or "",
                    closed_at=i.get("closed_at") or "")


@register("git", "github")
def make(token: str = "", api: str = API, **kw) -> GitHubActivity:
    return GitHubActivity(token, api=api, **kw)
