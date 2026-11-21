"""Writer: valid pushes are stored once, invalid ones are refused (so they reach the DLQ)."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from mission_control.adapters.store.memory import MemoryStore
from mission_control.writer import EVENTS, handle, make_app

EXAMPLES = Path(__file__).resolve().parent.parent / "schema" / "examples"


def push(event) -> dict:
    data = event if isinstance(event, bytes) else json.dumps(event).encode()
    return {"message": {"data": base64.b64encode(data).decode(), "messageId": "m-1"}, "subscription": "s"}


def example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def test_valid_event_is_stored_under_its_id():
    store, e = MemoryStore(), example("node.started.json")
    assert handle(push(e), store) == (204, "stored")
    doc = store.get(EVENTS, e["id"])
    assert doc["type"] == e["type"] and doc["bus_message_id"] == "m-1" and "received_at" in doc


def test_redelivery_leaves_one_document():
    store, e = MemoryStore(), example("run.failed.json")
    handle(push(e), store)
    handle(push(e), store)
    assert len(store.query(EVENTS)) == 1


def test_invalid_event_is_refused_and_not_stored():
    store, e = MemoryStore(), example("run.failed.json")
    del e["data"]["failure_class"]
    status, reason = handle(push(e), store)
    assert status == 400 and "failure_class" in reason
    assert store.query(EVENTS) == []


def test_garbage_is_refused():
    store = MemoryStore()
    assert handle(push(b"not json"), store)[0] == 400
    assert handle({"nope": 1}, store)[0] == 400
    assert handle(None, store)[0] == 400


def test_wsgi_app_round_trip():
    store, e = MemoryStore(), example("engine.heartbeat.json")
    body = json.dumps(push(e)).encode()
    seen = {}
    out = make_app(store)({"REQUEST_METHOD": "POST", "PATH_INFO": "/", "CONTENT_LENGTH": str(len(body)),
                           "wsgi.input": io.BytesIO(body)}, lambda s, h: seen.setdefault("status", s))
    assert seen["status"].startswith("204") and list(out) == []
    assert store.get(EVENTS, e["id"]) is not None
