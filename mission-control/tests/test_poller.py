"""Stage Q: the git poller (Q2–Q3), the git projections and the product rollups (Q4, Q6)."""
from __future__ import annotations

import itertools
import json
import random
import threading
from pathlib import Path
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mission_control import events, projections, registry
from mission_control.adapters.git.fake import FakeGitActivity
from mission_control.adapters.git.github import GitHubActivity
from mission_control.console import views
from mission_control.poller import Poller
from mission_control.ports import GitCheck, GitError, GitIssue, GitPull

REPO = "acme/widget"
A, B, C, M = "a" * 40, "b" * 40, "c" * 40, "d" * 40


class Bus:
    def __init__(self):
        self.sent = []

    def publish(self, topic, ev, *, ordering_key=""):
        self.sent.append(ev)
        return ev["id"]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    pdir = tmp_path / "products"
    pdir.mkdir()
    (pdir / "widget.yaml").write_text(f"repo: {REPO}\nbase_branch: main\n")
    git, bus, store, clock = FakeGitActivity(), Bus(), registry.get("store", "memory")(), Clock()
    git.set_branch(REPO, "main", M)
    git.put_pull(REPO, GitPull(7, "Add parser", "open", A, "main", "bot", created_at="2026-11-20T10:00:00Z",
                               updated_at="2026-11-20T11:00:00Z"))
    git.put_issue(REPO, GitIssue(3, "Parser crashes", "open", ("bug",), created_at="2026-11-19T09:00:00Z",
                                 updated_at="2026-11-19T09:00:00Z"))
    poller = Poller(git, bus, store, products_dir=pdir, engine="factory-1", clock=clock)
    return poller, git, bus, store, clock, pdir


def kinds(evs):
    return [e["type"].removeprefix("dev.factory.") for e in evs]


def project_all(store, evs):
    for ev in evs:
        projections.project(ev, store)


# ---------------------------------------------------------------------- the poller
def test_first_poll_announces_existing_state_as_initial(env):
    poller, git, bus, store, clock, _ = env
    sent = poller.step()
    assert sorted(kinds(sent)) == ["branch.pushed", "codehost.polled", "pullrequest.opened", "workitem.opened"]
    assert all(not events.validate(e) for e in sent)
    by = {e["type"]: e for e in sent}
    pr = by["dev.factory.pullrequest.opened"]
    assert pr["data"]["initial"] is True and pr["subject"] == f"change/codehost:github/{REPO}#7"
    assert pr["time"].startswith("2026-11-20T10:00:00") and pr["factoryproduct"] == "widget"
    assert by["dev.factory.codehost.polled"]["data"]["base_head"] == M
    assert poller.step() == []                           # nothing changed, polled marker not due


def test_new_head_title_merge_close_and_reopen(env):
    poller, git, bus, store, clock, _ = env
    poller.step()
    git.set_pull(REPO, 7, head=B, updated_at="2026-11-20T12:00:00Z")
    git.put_pull(REPO, GitPull(8, "Fix docs", "open", C, "main", created_at="2026-11-20T12:30:00Z"))
    sent = poller.step()
    upd = next(e for e in sent if e["type"].endswith("pullrequest.updated"))
    assert upd["data"]["previous_head"] == A and upd["data"]["head"] == B and "initial" not in upd["data"]
    opened = next(e for e in sent if e["type"].endswith("pullrequest.opened"))
    assert opened["data"]["pr"] == 8 and "initial" not in opened["data"]
    git.set_pull(REPO, 8, title="Fix the docs", updated_at="2026-11-20T12:40:00Z")
    upd2 = poller.step()
    assert kinds(upd2) == ["pullrequest.updated", "codehost.polled"] or kinds(upd2) == ["pullrequest.updated"]
    assert "previous_head" not in upd2[0]["data"]
    git.set_pull(REPO, 7, state="merged", merged_at="2026-11-20T13:00:00Z", closed_at="2026-11-20T13:00:00Z")
    git.set_pull(REPO, 8, state="closed", closed_at="2026-11-20T13:05:00Z")
    gone = [e for e in poller.step() if e["type"].startswith("dev.factory.pullrequest")]
    assert sorted(kinds(gone)) == ["pullrequest.closed", "pullrequest.merged"]
    git.set_pull(REPO, 8, state="open", closed_at="", updated_at="2026-11-20T14:00:00Z")
    re = [e for e in poller.step() if e["type"].endswith("pullrequest.opened")]
    assert re and re[0]["data"]["reopened"] is True and re[0]["id"] != opened["id"]
    project_all(store, bus.sent)
    docs = {d["pr"]: d for d in store.query("pullrequests")}
    assert docs[7]["state"] == "merged" and docs[7]["head"] == B
    assert docs[8]["state"] == "open" and docs[8]["title"] == "Fix the docs"


def test_checks_are_announced_once_per_run(env):
    poller, git, bus, store, clock, _ = env
    poller.step()
    git.set_checks(REPO, A, [GitCheck("11", "test", "in_progress")])
    assert [e for e in poller.step() if "check" in e["type"]] == []
    git.set_checks(REPO, A, [GitCheck("11", "test", "completed", "failure", completed_at="2026-11-20T11:10:00Z")])
    first = [e for e in poller.step() if "check" in e["type"]]
    assert len(first) == 1 and first[0]["data"]["conclusion"] == "failure"
    assert [e for e in poller.step() if "check" in e["type"]] == []
    git.set_checks(REPO, A, [GitCheck("11", "test", "completed", "failure", completed_at="2026-11-20T11:10:00Z"),
                             GitCheck("12", "test", "completed", "success", completed_at="2026-11-20T11:20:00Z")])
    rerun = [e for e in poller.step() if "check" in e["type"]]
    assert len(rerun) == 1 and rerun[0]["data"]["check_id"] == "12"
    project_all(store, bus.sent)
    pr = store.get("pullrequests", "widget__7")
    assert views.checks_summary(pr)["state"] == "passing"   # the rerun's later result wins
    git.set_pull(REPO, 7, head=B, updated_at="2026-11-20T12:00:00Z")
    poller.step()
    assert list(store.get("git_poll", "widget")["checks"]) == [B]   # only open heads are remembered


def test_branches_and_issues(env):
    poller, git, bus, store, clock, _ = env
    poller.step()
    git.set_branch(REPO, "main", A)
    git.set_branch(REPO, "feature/x", B)
    git.set_issue(REPO, 3, labels=("bug", "factory"), updated_at="2026-11-20T08:00:00Z")
    sent = poller.step()
    pushed = {e["data"]["branch"]: e["data"] for e in sent if e["type"].endswith("branch.pushed")}
    assert pushed["main"]["previous"] == M and pushed["feature/x"]["head"] == B
    assert any(e["type"].endswith("workitem.updated") and e["data"]["labels"] == ["bug", "factory"] for e in sent)
    git.set_branch(REPO, "feature/x", None)
    git.set_issue(REPO, 3, state="closed", closed_at="2026-11-20T09:00:00Z")
    sent = poller.step()
    assert "branch.deleted" in kinds(sent) and "workitem.closed" in kinds(sent)
    project_all(store, bus.sent)
    repo = store.get("repos", "widget")
    assert repo["branches"]["feature/x"]["deleted"] is True and repo["branches"]["main"]["head"] == A
    assert repo["last_push_at"]                          # the non-initial push counts
    assert store.get("workitems", "widget__3")["state"] == "closed"


def test_a_failed_poll_reports_and_keeps_what_was_seen(env):
    poller, git, bus, store, clock, _ = env
    poller.step()
    git.fail = GitError("auth", "GitHub refused the token (401)")
    clock.t += 60
    sent = poller.step()
    assert kinds(sent) == ["codehost.polled"] and sent[0]["data"]["ok"] is False
    assert sent[0]["data"]["error_kind"] == "auth"
    assert store.get("git_poll", "widget")["pulls"]          # nothing forgotten
    git.fail = None
    clock.t += 60
    back = poller.step()
    assert kinds(back) == ["codehost.polled"] and back[0]["data"]["ok"] is True   # no old news re-sent
    project_all(store, bus.sent)
    assert store.get("repos", "widget")["poll"]["ok"] is True


def test_polled_marker_every_five_minutes(env):
    poller, git, bus, store, clock, _ = env
    poller.step()
    clock.t += 120
    assert poller.step() == []
    clock.t += 200
    assert kinds(poller.step()) == ["codehost.polled"]


def test_restart_resumes_from_the_store(env):
    poller, git, bus, store, clock, pdir = env
    poller.step()
    again = Poller(git, bus, store, products_dir=pdir, engine="factory-1", clock=clock)
    assert again.step() == []


# ---------------------------------------------------------------------- projections
def test_git_projections_are_order_independent(env):
    poller, git, bus, store, clock, _ = env
    poller.step()
    git.set_checks(REPO, A, [GitCheck("11", "ci", "completed", "failure", completed_at="2026-11-20T11:10:00Z")])
    git.set_pull(REPO, 7, head=B, updated_at="2026-11-20T12:00:00Z")
    poller.step()
    git.set_checks(REPO, B, [GitCheck("12", "ci", "completed", "success", completed_at="2026-11-20T12:10:00Z")])
    poller.step()
    git.set_pull(REPO, 7, state="merged", merged_at="2026-11-20T13:00:00Z", closed_at="2026-11-20T13:00:00Z")
    git.set_branch(REPO, "main", B)
    git.set_issue(REPO, 3, state="closed", closed_at="2026-11-20T13:00:00Z")
    poller.step()
    evs = [e for e in bus.sent if not e["type"].endswith("codehost.polled")]
    want = None
    for seed in range(5):
        s = registry.get("store", "memory")()
        order = evs[:]
        random.Random(seed).shuffle(order)
        project_all(s, order + order[:3])
        got = {c: sorted((json.dumps(d, sort_keys=True) for d in s.query(c)))
               for c in ("pullrequests", "workitems", "repos")}
        want = want or got
        assert got == want
    pr = json.loads(want["pullrequests"][0])
    assert pr["state"] == "merged" and pr["head"] == B


# ---------------------------------------------------------------------- views
def test_rollups_and_problems(env):
    poller, git, bus, store, clock, pdir = env
    git.set_checks(REPO, A, [GitCheck("11", "ci", "completed", "failure", completed_at="2026-11-20T11:10:00Z")])
    poller.step()
    project_all(store, bus.sent)
    store.upsert("compliance", "widget__7", {"product": "widget", "pr": 7, "head": "f" * 40, "status": "error",
                                             "error": "boom", "reported_at": "2026-11-20T11:00:00Z"})
    at = views.now()
    probs = views.problems(store, at)
    assert {p.kind for p in probs} >= {"pullrequest.checks_failed", "compliance.error"}
    p = views.products(store, at, pdir, problem_list=probs)[0]
    assert p["open_prs"] == 1 and p["failing_checks"] == 1 and p["unreviewed"] == 1
    assert p["pulls"][0]["review"] == "outdated" and p["open_issues"] == 1 and p["base_head"] == M[:8]
    assert p["polled"] and not p["git_stale"] and p["problems"] >= 2
    git.set_pull(REPO, 7, state="merged", merged_at="2026-11-20T13:00:00Z")
    project_all(store, poller.step())
    assert "compliance.error" not in {p.kind for p in views.problems(store, at)}   # PR gone: not a problem


def test_git_error_and_stale_problems(env):
    poller, git, bus, store, clock, pdir = env
    poller.step()
    project_all(store, bus.sent)
    seen = store.get("repos", "widget")["git_seen_at"]
    later = datetime.fromisoformat(seen.replace("Z", "+00:00")) + timedelta(hours=1)
    assert "git.stale" in {p.kind for p in views.problems(store, later)}
    git.fail = GitError("rate_limited", "GitHub rate limit reached (403)")
    clock.t += 60
    project_all(store, poller.step())
    probs = {p.kind: p for p in views.problems(store, later)}
    assert "git.error" in probs and probs["git.error"].product == "widget" and "git.stale" not in probs


def test_projects_page_renders_rollups(env):
    from fastapi.testclient import TestClient

    from mission_control.console.app import create_app
    poller, git, bus, store, clock, pdir = env
    git.set_checks(REPO, A, [GitCheck("11", "ci", "completed", "failure", completed_at="2026-11-20T11:10:00Z")])
    project_all(store, poller.step())
    client = TestClient(create_app(store, dev_operator="tester", products_dir=pdir))
    html = client.get("/projects", headers={"host": "localhost"}).text
    assert "Add parser" in html and "1 failing" in html and "Parser crashes" in html and "fresh" in html


# ---------------------------------------------------------------------- GitHub adapter (local HTTP server)
class FakeGitHub(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeGitHub.seen.append((self.path, self.headers.get("If-None-Match"), self.headers.get("Authorization")))
        status, body, headers = FakeGitHub.routes.get(self.path, (404, {"message": "Not Found"}, {}))
        if callable(status):
            status, body, headers = status(self)
        etag = headers.get("ETag")
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(status)
        for k, v in {"Content-Type": "application/json", "X-RateLimit-Limit": "5000",
                     "X-RateLimit-Remaining": "4990", "X-RateLimit-Reset": "1800000000", **headers}.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def gh():
    srv = HTTPServer(("127.0.0.1", 0), FakeGitHub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    FakeGitHub.routes, FakeGitHub.seen = {}, []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _pr(n, sha):
    return {"number": n, "title": f"PR {n}", "state": "open", "head": {"sha": sha}, "base": {"ref": "main"},
            "user": {"login": "bot"}, "html_url": f"https://github.com/{REPO}/pull/{n}", "draft": False,
            "created_at": "2026-11-20T10:00:00Z", "updated_at": "2026-11-20T10:00:00Z", "merged_at": None}


def test_github_etag_pagination_and_rate(gh):
    page1 = f"/repos/{REPO}/pulls?state=open&per_page=100"
    page2 = f"/repos/{REPO}/pulls?state=open&per_page=100&page=2"
    FakeGitHub.routes = {
        page1: (200, [_pr(2, A)], {"ETag": '"p1"', "Link": f'<{gh}{page2}>; rel="next"'}),
        page2: (200, [_pr(1, B)], {"ETag": '"p2"'}),
    }
    g = GitHubActivity("t0k3n", api=gh)
    assert [p.number for p in g.pulls(REPO)] == [2, 1]
    assert [p.number for p in g.pulls(REPO)] == [2, 1]          # answered from the cache on 304
    assert g.usage()["not_modified"] == 2 and g.usage()["rate_remaining"] == 4990
    assert FakeGitHub.seen[2][1] == '"p1"' and FakeGitHub.seen[0][2] == "Bearer t0k3n"


def test_github_401_asks_for_a_new_token_once(gh):
    path = f"/repos/{REPO}/branches?per_page=100"

    def route(req):
        if req.headers.get("Authorization") == "Bearer new":
            return 200, [{"name": "main", "commit": {"sha": M}}], {}
        return 401, {"message": "Bad credentials"}, {}
    FakeGitHub.routes = {path: (route, None, None)}
    tokens = itertools.chain(["old", "new"], itertools.repeat("new"))
    g = GitHubActivity(token_source=lambda: next(tokens), api=gh)
    assert g.branches(REPO) == {"main": M}
    g2 = GitHubActivity("bad", api=gh)
    with pytest.raises(GitError) as e:
        g2.branches(REPO)
    assert e.value.kind == "auth"


def test_github_rate_limit_and_not_found(gh):
    FakeGitHub.routes = {f"/repos/{REPO}/issues?state=open&per_page=100":
                         (403, {"message": "rate limit"}, {"X-RateLimit-Remaining": "0"})}
    g = GitHubActivity(api=gh)
    with pytest.raises(GitError) as e:
        g.issues(REPO)
    assert e.value.kind == "rate_limited" and e.value.reset_at
    assert g.pull(REPO, 99) is None
    with pytest.raises(ValueError):
        g.checks(REPO, "not-a-sha")
    with pytest.raises(ValueError):
        g.pulls("../../etc")


def test_github_maps_checks_and_skips_prs_in_issues(gh):
    FakeGitHub.routes = {
        f"/repos/{REPO}/commits/{A}/check-runs?per_page=100": (200, {"total_count": 1, "check_runs": [
            {"id": 5, "name": "ci", "status": "completed", "conclusion": "success", "html_url": "u",
             "completed_at": "2026-11-20T10:05:00Z"}]}, {}),
        f"/repos/{REPO}/issues?state=open&per_page=100": (200, [
            {"number": 4, "title": "Bug", "state": "open", "labels": [{"name": "bug"}], "user": {"login": "x"}},
            {"number": 2, "title": "a PR", "state": "open", "pull_request": {}, "labels": []}], {}),
    }
    g = GitHubActivity(api=gh)
    assert g.checks(REPO, A)[0].conclusion == "success"
    assert [(i.number, i.labels) for i in g.issues(REPO)] == [(4, ("bug",))]


def test_poller_event_ids_fit_the_envelope(env):
    poller, git, bus, store, clock, _ = env
    long = "feature/" + "x" * 240
    git.set_branch(REPO, long, A)
    sent = poller.step()
    assert all(len(e["id"]) <= 256 and not events.validate(e) for e in sent)


def test_without_a_token_poll_slowly_until_the_login_arrives(gh, monkeypatch):
    from mission_control.adapters.git import github as mod
    FakeGitHub.routes = {f"/repos/{REPO}/branches?per_page=100": (200, [], {})}
    tokens = iter(["", "late"])
    g = GitHubActivity(token_source=lambda: next(tokens), api=gh)
    p = Poller(g, Bus(), registry.get("store", "memory")(), products_dir=Path("/nonexistent"), engine="factory-1")
    assert not g.authenticated and p.interval_s == 600
    now = [mod.time.monotonic() + 301]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    g.branches(REPO)
    assert g.authenticated and p.interval_s == 60 and FakeGitHub.seen[-1][2] == "Bearer late"
