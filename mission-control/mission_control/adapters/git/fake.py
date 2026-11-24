"""In-memory GitActivity (tests, console demos): repositories you change by hand."""
from __future__ import annotations

from dataclasses import replace

from mission_control.ports import GitCheck, GitError, GitIssue, GitPull
from mission_control.registry import register


class FakeGitActivity:
    def __init__(self) -> None:
        self._pulls: dict[str, dict[int, GitPull]] = {}
        self._branches: dict[str, dict[str, str]] = {}
        self._checks: dict[tuple[str, str], list[GitCheck]] = {}
        self._issues: dict[str, dict[int, GitIssue]] = {}
        self.fail: GitError | None = None       # tests: every call raises this
        self.requests = 0

    def _call(self, repo: str) -> None:
        self.requests += 1
        if self.fail is not None:
            raise self.fail
        if "/" not in repo:
            raise ValueError(f"not an owner/name repository: {repo!r}")

    # ------------------------------------------------------------------ test setup
    def put_pull(self, repo: str, pr: GitPull) -> None:
        self._pulls.setdefault(repo, {})[pr.number] = pr

    def set_pull(self, repo: str, number: int, **changes) -> GitPull:
        pr = replace(self._pulls[repo][number], **changes)
        self._pulls[repo][number] = pr
        return pr

    def set_branch(self, repo: str, name: str, sha: str | None) -> None:
        b = self._branches.setdefault(repo, {})
        if sha is None:
            b.pop(name, None)
        else:
            b[name] = sha

    def set_checks(self, repo: str, sha: str, checks: list[GitCheck]) -> None:
        self._checks[(repo, sha)] = list(checks)

    def put_issue(self, repo: str, issue: GitIssue) -> None:
        self._issues.setdefault(repo, {})[issue.number] = issue

    def set_issue(self, repo: str, number: int, **changes) -> GitIssue:
        i = replace(self._issues[repo][number], **changes)
        self._issues[repo][number] = i
        return i

    # ------------------------------------------------------------------ GitActivity
    def pulls(self, repo: str) -> list[GitPull]:
        self._call(repo)
        return sorted((p for p in self._pulls.get(repo, {}).values() if p.state == "open"), key=lambda p: -p.number)

    def pull(self, repo: str, number: int) -> GitPull | None:
        self._call(repo)
        return self._pulls.get(repo, {}).get(number)

    def branches(self, repo: str) -> dict[str, str]:
        self._call(repo)
        return dict(self._branches.get(repo, {}))

    def checks(self, repo: str, sha: str) -> list[GitCheck]:
        self._call(repo)
        return list(self._checks.get((repo, sha), []))

    def issues(self, repo: str) -> list[GitIssue]:
        self._call(repo)
        return sorted((i for i in self._issues.get(repo, {}).values() if i.state == "open"), key=lambda i: -i.number)

    def issue(self, repo: str, number: int) -> GitIssue | None:
        self._call(repo)
        return self._issues.get(repo, {}).get(number)

    def usage(self) -> dict:
        return {"requests": self.requests, "not_modified": 0, "authenticated": True,
                "rate_limit": 5000, "rate_remaining": 5000 - self.requests}


@register("git", "fake")
def make(**kw) -> FakeGitActivity:
    return FakeGitActivity(**kw)
