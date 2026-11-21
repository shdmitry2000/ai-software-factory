"""The git poller: what happens in every product's repository → the bus, on the main node.

No public webhook is needed. Every `interval_s` the poller reads each product's open pull
requests, branches, open issues and the checks on each open PR head (GitActivity; on
GitHub every unchanged answer is a free 304), compares them with what it saw last time and
publishes one event per difference:

    pullrequest.opened / updated / merged / closed     change/{id}
    check.completed                                    change/{id}/check/{name}
    branch.pushed / deleted                            product/{name}/branch/{branch}
    workitem.opened / updated / closed                 workitem/{id}
    codehost.polled                                    product/{name}   (on change, every 5 min)

What it saw lives in the store (`git_poll/{product}`), like the bridge's cursor, so a main
node rebuilt after the sweep carries on without re-announcing old news. Publishing happens
before saving: a crash in between re-sends a few events, which the store absorbs because
their ids are deterministic. The first poll of a repository marks its events `initial`
(state that already existed), so chain triggers can tell them from new happenings.

    python -m mission_control.poller --project P [--once]
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mission_control import events
from mission_control import products as products_mod
from mission_control.ports import Event, EventBus, GitActivity, GitError, GitIssue, GitPull, StateStore

log = logging.getLogger("mission_control.poller")
STATE = "git_poll"
POLLED_EVERY_S = 300
KEEP_CLOSED = 200
CONCLUSIONS = {"success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required", "stale"}


def utc(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def change_id(codehost: str, repo: str, n: int) -> str:
    return f"codehost:{codehost}/{repo}#{n}"


def work_item_id(codehost: str, repo: str, n: int) -> str:
    return f"tracker:{codehost}/{repo}#{n}"


class Poller:
    def __init__(self, git: GitActivity, bus: EventBus, store: StateStore, *, products_dir: Path,
                 engine: str, topic: str = "factory-events", interval_s: int = 60,
                 clock=time.time) -> None:
        self.git, self.bus, self.store = git, bus, store
        self.products_dir, self.engine, self.topic = Path(products_dir), engine, topic
        self.interval_s, self.clock = interval_s, clock
        self.source = f"factory://{engine}/git-poller"
        self.published = self.dropped = 0

    # ------------------------------------------------------------------ products
    def products(self) -> list[products_mod.Product]:
        out = []
        for name, p in sorted(products_mod.load_all(self.products_dir).items()):
            try:
                eff = (self.store.get("products", name) or {}).get("effective")
            except Exception:
                eff = None
            out.append(products_mod.apply_effective(p, eff))
        return out

    # ------------------------------------------------------------------ one cycle
    def step(self) -> list[Event]:
        out: list[Event] = []
        for p in self.products():
            try:
                out += self.poll(p)
            except Exception:                            # one product must not stop the others
                log.exception("polling %s failed", p.name)
        return out

    def poll(self, p: products_mod.Product) -> list[Event]:
        state = self.store.get(STATE, p.name) or {}
        initial = not state.get("seen_at")
        now = utc(self.clock())
        new: dict = {"product": p.name, "repo": p.repo, "seen_at": now, "pulls": {}, "closed_pulls": {},
                     "issues": {}, "closed_issues": {}, "branches": {}, "checks": {},
                     "polled_at": state.get("polled_at", ""), "polled": state.get("polled") or {}}
        out: list[Event] = []
        try:
            pulls = self.git.pulls(p.repo)
            branches = self.git.branches(p.repo)
            issues = self.git.issues(p.repo)
            out += self._pulls(p, state, new, pulls, initial, now)
            out += self._checks(p, state, new, pulls, initial)
            out += self._branches(p, state, new, branches, initial)
            out += self._issues(p, state, new, issues, initial, now)
        except GitError as e:
            log.warning("%s: %s", p.name, e)
            summary = {"ok": False, "error": str(e)[:300], "error_kind": e.kind}
            out += self._polled(p, state, {**state, "seen_at": state.get("seen_at", "")}, summary, now, force=True)
            return out
        summary = {"ok": True, "open_prs": len(pulls), "open_issues": len(issues), "branches": len(branches),
                   "base_branch": p.base_branch}
        if branches.get(p.base_branch):
            summary["base_head"] = branches[p.base_branch]
        out += self._polled(p, state, new, summary, now, force=bool(out))
        self.store.upsert(STATE, p.name, new)
        return out

    # ------------------------------------------------------------------ publishing
    def _emit(self, etype: str, eid: str, subject: str, data: dict, *, product: str, at: str = "") -> Event | None:
        ev = events.make(f"dev.factory.{etype}", eid[:256], subject, data, source=self.source, engine=self.engine,
                         time=_rfc3339(at) if at else "", factoryproduct=product)
        problems = events.validate(ev)
        if problems:                                     # never block the stream on one bad mapping
            self.dropped += 1
            log.warning("dropping invalid %s %s: %s", etype, eid, "; ".join(problems[:3]))
            return None
        self.bus.publish(self.topic, ev, ordering_key=f"git-{product}")
        self.published += 1
        return ev

    def _polled(self, p, state: dict, new: dict, summary: dict, now: str, *, force: bool) -> list[Event]:
        usage = {k: v for k, v in self.git.usage().items()
                 if k in ("requests", "not_modified", "rate_limit", "rate_remaining", "rate_reset", "authenticated")}
        last = state.get("polled") or {}
        due = (self.clock() - _epoch(state.get("polled_at"))) >= POLLED_EVERY_S
        changed = {k: summary.get(k) for k in ("ok", "error_kind", "open_prs", "open_issues", "base_head")} != \
                  {k: last.get(k) for k in ("ok", "error_kind", "open_prs", "open_issues", "base_head")}
        if not (force or due or changed):
            return []
        data = {"product": p.name, "repo": p.repo, **summary, "interval_s": int(self.interval_s), **usage}
        ev = self._emit("codehost.polled", f"git:polled:{p.name}:{now}", f"product/{p.name}", data, product=p.name, at=now)
        new["polled_at"], new["polled"] = now, summary
        if not summary.get("ok"):                        # a failed poll keeps what was seen; save the polled marker only
            self.store.merge(STATE, p.name, {"polled_at": now, "polled": summary})
        return [ev] if ev else []

    # ------------------------------------------------------------------ pull requests
    def _pull_data(self, p, pr: GitPull, **extra) -> dict:
        d = {"product": p.name, "repo": p.repo, "change": change_id(p.codehost, p.repo, pr.number), "pr": pr.number,
             "title": pr.title[:512], "state": pr.state, "head": pr.head, "base": pr.base, "draft": pr.draft}
        for k in ("author", "url", "created_at", "updated_at", "closed_at", "merged_at"):
            v = getattr(pr, k)
            if v:
                d[k] = _rfc3339(v) if k.endswith("_at") else v[:512]
        return {**d, **{k: v for k, v in extra.items() if v not in (None, "", False)}}

    def _pulls(self, p, state: dict, new: dict, pulls: list[GitPull], initial: bool, now: str) -> list[Event]:
        out: list[Event] = []
        seen, closed = state.get("pulls") or {}, dict(state.get("closed_pulls") or {})
        for pr in pulls:
            key, cid = str(pr.number), change_id(p.codehost, p.repo, pr.number)
            subj = f"change/{cid}"
            prev = seen.get(key)
            snap = {"head": pr.head, "title": pr.title[:512], "base": pr.base, "draft": pr.draft}
            new["pulls"][key] = snap
            if prev is None:
                reopened = key in closed
                at = (pr.updated_at or now) if reopened else (pr.created_at or now)
                eid = f"github:pr-opened:{p.repo}#{pr.number}" + (f":reopened:{at}" if reopened else "")
                ev = self._emit("pullrequest.opened", eid, subj, self._pull_data(p, pr, initial=initial, reopened=reopened),
                                product=p.name, at=at)
                closed.pop(key, None)
            elif prev != snap:
                at = pr.updated_at or now
                ev = self._emit("pullrequest.updated", f"github:pr-updated:{p.repo}#{pr.number}:{pr.head}:{at}", subj,
                                self._pull_data(p, pr, previous_head=prev.get("head") if prev.get("head") != pr.head else ""),
                                product=p.name, at=at)
            else:
                continue
            if ev:
                out.append(ev)
        for key in sorted(set(seen) - {str(pr.number) for pr in pulls}, key=int):
            pr = self.git.pull(p.repo, int(key))
            cid = change_id(p.codehost, p.repo, int(key))
            if pr is None:                               # gone (repository moved or PR deleted): closed now
                prev = seen[key]
                pr = GitPull(number=int(key), title=prev.get("title", ""), state="closed", head=prev["head"],
                             base=prev.get("base", ""), closed_at=now)
            if pr.state == "open":                       # listed late (pagination edge): keep it as seen
                new["pulls"][key] = seen[key]
                continue
            if pr.state == "merged":
                ev = self._emit("pullrequest.merged", f"github:pr-merged:{p.repo}#{pr.number}", f"change/{cid}",
                                self._pull_data(p, pr), product=p.name, at=pr.merged_at or now)
            else:
                at = pr.closed_at or now
                ev = self._emit("pullrequest.closed", f"github:pr-closed:{p.repo}#{pr.number}:{at}", f"change/{cid}",
                                self._pull_data(p, pr), product=p.name, at=at)
            closed[key] = pr.closed_at or pr.merged_at or now
            if ev:
                out.append(ev)
        new["closed_pulls"] = dict(sorted(closed.items(), key=lambda kv: kv[1])[-KEEP_CLOSED:])
        return out

    def _checks(self, p, state: dict, new: dict, pulls: list[GitPull], initial: bool) -> list[Event]:
        out: list[Event] = []
        seen = state.get("checks") or {}
        for pr in pulls:
            done = dict(seen.get(pr.head) or {})
            cid = change_id(p.codehost, p.repo, pr.number)
            for c in self.git.checks(p.repo, pr.head):
                if c.status != "completed" or done.get(c.id) == (c.conclusion or "unknown"):
                    continue
                conclusion = c.conclusion if c.conclusion in CONCLUSIONS else "unknown"
                data = {"product": p.name, "repo": p.repo, "change": cid, "pr": pr.number, "head": pr.head,
                        "name": c.name[:200], "check_id": c.id[:40], "conclusion": conclusion}
                if c.url:
                    data["url"] = c.url[:512]
                if c.completed_at:
                    data["completed_at"] = _rfc3339(c.completed_at)
                if initial:
                    data["initial"] = True
                ev = self._emit("check.completed", f"github:check:{p.repo}:{c.id}:{conclusion}",
                                f"change/{cid}/check/{c.name[:200]}", data, product=p.name, at=c.completed_at)
                done[c.id] = c.conclusion or "unknown"
                if ev:
                    out.append(ev)
            new["checks"][pr.head] = done                # only open heads are kept
        return out

    # ------------------------------------------------------------------ branches
    def _branches(self, p, state: dict, new: dict, branches: dict[str, str], initial: bool) -> list[Event]:
        out: list[Event] = []
        seen = state.get("branches") or {}
        new["branches"] = dict(branches)
        for name, sha in sorted(branches.items()):
            if seen.get(name) == sha:
                continue
            data = {"product": p.name, "repo": p.repo, "branch": name[:255], "head": sha}
            if seen.get(name):
                data["previous"] = seen[name]
            if initial:
                data["initial"] = True
            ev = self._emit("branch.pushed", f"github:push:{p.repo}:{name}:{sha}", f"product/{p.name}/branch/{name}",
                            data, product=p.name)
            if ev:
                out.append(ev)
        for name in sorted(set(seen) - set(branches)):
            ev = self._emit("branch.deleted", f"github:branch-deleted:{p.repo}:{name}:{seen[name]}",
                            f"product/{p.name}/branch/{name}",
                            {"product": p.name, "repo": p.repo, "branch": name[:255], "previous": seen[name]},
                            product=p.name)
            if ev:
                out.append(ev)
        return out

    # ------------------------------------------------------------------ issues
    def _issue_data(self, p, i: GitIssue, **extra) -> dict:
        d = {"product": p.name, "repo": p.repo, "work_item": work_item_id(p.codehost, p.repo, i.number),
             "number": i.number, "title": i.title[:512], "state": i.state, "labels": [lb[:100] for lb in i.labels[:50]]}
        for k in ("author", "url", "created_at", "updated_at", "closed_at"):
            v = getattr(i, k)
            if v:
                d[k] = _rfc3339(v) if k.endswith("_at") else v[:512]
        return {**d, **{k: v for k, v in extra.items() if v not in (None, "", False)}}

    def _issues(self, p, state: dict, new: dict, issues: list[GitIssue], initial: bool, now: str) -> list[Event]:
        out: list[Event] = []
        seen, closed = state.get("issues") or {}, dict(state.get("closed_issues") or {})
        for i in issues:
            key, wid = str(i.number), work_item_id(p.codehost, p.repo, i.number)
            snap = {"title": i.title[:512], "labels": list(i.labels)}
            new["issues"][key] = snap
            prev = seen.get(key)
            if prev is None:
                reopened = key in closed
                at = (i.updated_at or now) if reopened else (i.created_at or now)
                eid = f"github:issue-opened:{p.repo}#{i.number}" + (f":reopened:{at}" if reopened else "")
                ev = self._emit("workitem.opened", eid, f"workitem/{wid}",
                                self._issue_data(p, i, initial=initial, reopened=reopened), product=p.name, at=at)
                closed.pop(key, None)
            elif prev != snap:
                at = i.updated_at or now
                ev = self._emit("workitem.updated", f"github:issue-updated:{p.repo}#{i.number}:{at}", f"workitem/{wid}",
                                self._issue_data(p, i), product=p.name, at=at)
            else:
                continue
            if ev:
                out.append(ev)
        for key in sorted(set(seen) - {str(i.number) for i in issues}, key=int):
            i = self.git.issue(p.repo, int(key))
            if i is not None and i.state == "open":
                new["issues"][key] = seen[key]
                continue
            if i is None:                                # deleted or turned into something else
                i = GitIssue(number=int(key), title=seen[key].get("title", ""), state="closed", closed_at=now)
            at = i.closed_at or now
            wid = work_item_id(p.codehost, p.repo, i.number)
            ev = self._emit("workitem.closed", f"github:issue-closed:{p.repo}#{i.number}:{at}", f"workitem/{wid}",
                            self._issue_data(p, i), product=p.name, at=at)
            closed[key] = at
            if ev:
                out.append(ev)
        new["closed_issues"] = dict(sorted(closed.items(), key=lambda kv: kv[1])[-KEEP_CLOSED:])
        return out

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        log.info("git poller %s: %d products, every %ds", self.engine, len(self.products()), self.interval_s)
        while True:
            started = time.monotonic()
            try:
                sent = self.step()
                if sent:
                    log.info("published %d events", len(sent))
            except Exception:                            # a bus or store hiccup must not stop polling
                log.exception("poll cycle failed; retrying")
            time.sleep(max(1.0, self.interval_s - (time.monotonic() - started)))


def _epoch(t: str | None) -> float:
    if not t:
        return 0.0
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _rfc3339(t: str) -> str:
    """GitHub's `2026-11-21T10:00:00Z` as the envelope's RFC 3339 with microseconds."""
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return events.now()


def gh_token(gh: str = "gh") -> str:
    """The token of the `gh` login on this machine (root's, on the main node), read from
    the CLI's output; '' when there is none."""
    try:
        p = subprocess.run([gh, "auth", "token", "--hostname", "github.com"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    from mission_control import registry
    from mission_control.adapters.git.github import GitHubActivity
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m mission_control.poller")
    ap.add_argument("--project", default=os.environ.get("GCP_PROJECT", ""))
    ap.add_argument("--engine", default=os.environ.get("MC_ENGINE") or socket.gethostname().split(".")[0].lower())
    ap.add_argument("--topic", default="factory-events")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]), help="factory checkout with products/")
    ap.add_argument("--interval", type=int, default=60, help="seconds between polls (with a token)")
    ap.add_argument("--anonymous-interval", type=int, default=600, help="seconds between polls without a token")
    ap.add_argument("--no-token", action="store_true", help="do not use the gh login (public repositories only)")
    ap.add_argument("--once", action="store_true", help="poll once, print what was published and exit")
    o = ap.parse_args(argv)
    if not o.project:
        print("--project or GCP_PROJECT is required", file=sys.stderr)
        return 2
    source = (lambda: "") if o.no_token else gh_token
    git = GitHubActivity(token_source=source)
    interval = o.interval if git.authenticated else max(o.interval, o.anonymous_interval)
    if not git.authenticated:
        log.warning("no GitHub token (no gh login): public repositories only, polling every %ds", interval)
    poller = Poller(git, registry.get("bus", "pubsub")(project=o.project),
                    registry.get("store", "firestore")(project=o.project),
                    products_dir=Path(o.repo) / "products", engine=o.engine, topic=o.topic, interval_s=interval)
    if o.once:
        sent = poller.step()
        for ev in sent:
            print(f"{ev['type'].removeprefix('dev.factory.'):<22} {ev['subject']}")
        u = git.usage()
        print(f"published {len(sent)}, dropped {poller.dropped}; requests {u['requests']} "
              f"(304: {u['not_modified']}), rate {u.get('rate_remaining', '?')}/{u.get('rate_limit', '?')}, "
              f"token {'yes' if git.authenticated else 'no'}")
        return 0
    poller.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
