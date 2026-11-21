"""GitActivity contract: every adapter answers the same questions the same way.

The fake always takes part. GitHub joins with MC_CONTRACT_LIVE=1 and MC_GIT_REPO=owner/name
(read-only calls against a repository with at least one branch; GITHUB_TOKEN is used when
set, otherwise anonymous calls, about 10 of them).
"""
from __future__ import annotations

import os
import re

import pytest

from mission_control import registry
from mission_control.ports import GitActivity, GitCheck, GitError, GitIssue, GitPull, is_adapter_for

SHA = re.compile(r"^[0-9a-f]{40}$")
LIVE_REPO = os.environ.get("MC_GIT_REPO", "")
LIVE = os.environ.get("MC_CONTRACT_LIVE") == "1" and bool(LIVE_REPO)


def _fake():
    g = registry.get("git", "fake")()
    repo = "acme/widget"
    g.set_branch(repo, "main", "a" * 40)
    g.put_pull(repo, GitPull(1, "x", "open", "b" * 40, "main"))
    g.set_checks(repo, "b" * 40, [GitCheck("1", "ci", "completed", "success")])
    g.put_issue(repo, GitIssue(2, "y", "open"))
    return g, repo


def _github():
    return registry.get("git", "github")(token=os.environ.get("GITHUB_TOKEN", "")), LIVE_REPO


CANDIDATES = [pytest.param(_fake, id="fake"),
              pytest.param(_github, id="github", marks=pytest.mark.skipif(
                  not LIVE, reason="live adapter: set MC_CONTRACT_LIVE=1 and MC_GIT_REPO"))]


@pytest.fixture(params=CANDIDATES)
def git(request):
    return request.param()


def test_is_a_git_activity(git):
    g, _ = git
    assert is_adapter_for("git", g) and isinstance(g, GitActivity)


def test_branches_are_heads(git):
    g, repo = git
    b = g.branches(repo)
    assert b and all(isinstance(k, str) and SHA.match(v) for k, v in b.items())


def test_open_pulls_and_their_checks(git):
    g, repo = git
    pulls = g.pulls(repo)
    assert all(p.state == "open" and SHA.match(p.head) and p.base for p in pulls)
    for p in pulls[:2]:
        same = g.pull(repo, p.number)
        assert same is not None and same.number == p.number
        for c in g.checks(repo, p.head):
            assert c.id and c.name and c.status in ("queued", "in_progress", "completed", "waiting", "requested", "pending")


def test_open_issues_exclude_pulls(git):
    g, repo = git
    prs = {p.number for p in g.pulls(repo)}
    issues = g.issues(repo)
    assert all(i.state == "open" and i.number not in prs for i in issues)


def test_missing_things_are_none(git):
    g, repo = git
    assert g.pull(repo, 999_999) is None
    assert g.issue(repo, 999_999) is None


def test_bad_input_is_refused(git):
    g, _ = git
    with pytest.raises((ValueError, GitError)):
        g.branches("not a repo")


def test_usage_counts_requests(git):
    g, repo = git
    g.branches(repo)
    assert g.usage()["requests"] >= 1
