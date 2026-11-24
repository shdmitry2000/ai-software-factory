"""F7: scheduled problem rules open and clear problems and alert on new ones."""
from __future__ import annotations

import base64
import io
import json
from datetime import datetime, timedelta, timezone

from mission_control import alerts, events, projections, registry
from mission_control.adapters.notifier.log import LogNotifier
from mission_control.console import views
from mission_control.writer import make_app

T0 = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)


class Bus:
    def __init__(self):
        self.sent = []

    def publish(self, topic, e, *, ordering_key=""):
        assert events.validate(e) == []
        self.sent.append(e)
        return e["id"]


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def beat(store, at, engine="factory-1"):
    store.upsert("engines", engine, {"engine": engine, "last_heartbeat": iso(at)})


def env():
    return registry.get("store", "memory")(), Bus(), registry.get("notifier", "memory")()


def test_a_silent_engine_opens_alerts_once_and_clears():
    store, bus, note = env()
    beat(store, T0)
    assert alerts.check(store, bus, note, T0 + timedelta(seconds=60))["opened"] == []
    s = alerts.check(store, bus, note, T0 + timedelta(minutes=4))
    assert s["opened"] == ["engine.silent:factory-1"] and s["alerts"] == 1
    assert note.sent[0]["severity"] == "critical" and "silent" in note.sent[0]["title"]
    doc = store.get("problems", "engine.silent:factory-1")
    assert doc["state"] == "open" and doc["alerted"] and doc["occurrences"] == 1
    s = alerts.check(store, bus, note, T0 + timedelta(minutes=6))
    assert s["opened"] == [] and s["alerts"] == 0 and len(note.sent) == 1        # no repeat
    beat(store, T0 + timedelta(minutes=7))
    s = alerts.check(store, bus, note, T0 + timedelta(minutes=8))
    assert s["cleared"] == ["engine.silent:factory-1"] and note.sent[-1]["title"].startswith("Cleared:")
    doc = store.get("problems", "engine.silent:factory-1")
    assert doc["state"] == "resolved" and doc["resolved_at"]
    assert [e["type"].rsplit(".", 1)[1] for e in bus.sent] == ["opened", "resolved"]
    assert bus.sent[1]["data"]["by_rule"] is True
    assert store.get("problem_states", "engine.silent:factory-1") is None          # not an operator resolution
    assert store.get(*views.RULES_DOC)["at"] == iso(T0 + timedelta(minutes=8))


def test_events_are_deterministic_and_replay_is_harmless():
    store, bus, note = env()
    beat(store, T0)
    at = T0 + timedelta(minutes=5)
    alerts.check(store, bus, note, at)
    again = registry.get("store", "memory")()
    beat(again, T0)
    b2 = Bus()
    alerts.check(again, b2, registry.get("notifier", "memory")(), at)
    assert [e["id"] for e in bus.sent] == [e["id"] for e in b2.sent]
    before = store.get("problems", "engine.silent:factory-1")
    projections.project(bus.sent[0], store)                       # the writer's push of the same event
    assert store.get("problems", "engine.silent:factory-1") == before


def test_info_is_recorded_but_not_alerted_and_acked_problems_stay_quiet():
    store, bus, note = env()
    store.upsert("pullrequests", "p__3", {"product": "p", "pr": 3, "state": "open", "head": "a" * 40,
                                          "checks": {"a" * 40: {"ci": {"status": "completed", "conclusion": "failure"}}}})
    alerts.check(store, bus, note, T0)
    assert store.get("problems", "pullrequest.checks_failed:p__3")["severity"] == "info" and note.sent == []
    beat(store, T0 - timedelta(hours=1))
    pid = "engine.silent:factory-1"
    store.upsert("problem_states", pid, {"problem_id": pid, "ack": {"since": iso(T0 - timedelta(hours=1))}})
    alerts.check(store, bus, note, T0)
    assert store.get("problems", pid)["state"] == "open" and note.sent == []


def test_a_worse_severity_alerts_again():
    store, bus, note = env()
    store.upsert("hosts", "local", {"host": "local", "kind": "local", "last_heartbeat": iso(T0),
                                    "health": {"disk_used_pct": 90.0, "disk_total_gb": 50}})
    alerts.check(store, bus, note, T0 + timedelta(seconds=30))
    assert [n["severity"] for n in note.sent] == ["warning"]                     # disk
    alerts.check(store, bus, note, T0 + timedelta(minutes=5))
    assert [n["severity"] for n in note.sent] == ["warning", "critical"]         # + host silent


def test_the_console_flags_a_stopped_check_and_shows_the_log():
    store, bus, note = env()
    beat(store, T0)
    alerts.check(store, bus, note, T0 + timedelta(minutes=4))
    later = T0 + timedelta(minutes=30)
    kinds = {p.kind for p in views.problems(store, later)}
    assert "rules.silent" in kinds
    log = views.rule_log(store, later)
    assert log["late"] and log["recent"][0]["problem_id"] == "engine.silent:factory-1"
    assert "engine.silent:factory-1" in log["alerted"]
    beat(store, later)
    s = alerts.check(store, bus, note, later)                     # the check never opens rules.silent itself
    assert "rules.silent:scheduler" not in s["opened"]
    assert "rules.silent" not in {p.kind for p in views.problems(store, later)}


def test_writer_runs_the_rules_on_post():
    store, bus, note = env()
    beat(store, views.now() - timedelta(hours=1))
    app = make_app(store, rules=lambda: alerts.check(store, bus, note))
    status = {}

    def call(method, path, body=b""):
        env_ = {"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": io.BytesIO(body)}
        out = b"".join(app(env_, lambda s, h: status.update(s=s)))
        return status["s"], out

    s, out = call("POST", "/rules")
    assert s.startswith("200") and json.loads(out)["opened"] == ["engine.silent:factory-1"]
    push = {"message": {"data": base64.b64encode(json.dumps(bus.sent[0]).encode()).decode(), "messageId": "1"}}
    s, _ = call("POST", "/", json.dumps(push).encode())
    assert s.startswith("204")
    assert make_app(store)({"REQUEST_METHOD": "POST", "PATH_INFO": "/rules"}, lambda s, h: status.update(s=s)) \
        and status["s"].startswith("404")


def test_log_notifier_writes_one_structured_line():
    buf = io.StringIO()
    LogNotifier(buf).send("Engine factory-1 is silent", "last heartbeat 4m ago", severity="critical", link="/agents")
    line = json.loads(buf.getvalue())
    assert line["severity"] == "ERROR" and line["logging.googleapis.com/labels"] == {"factory_alert": "1"}
    assert line["alert"]["link"] == "/agents"
