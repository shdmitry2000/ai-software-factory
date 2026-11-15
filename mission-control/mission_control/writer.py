"""The writer: Pub/Sub push → validate → store.

Pub/Sub push POSTs {"message": {"data": <base64 event>, "messageId": ...}, "subscription": ...}.
A valid event is stored under `events/{event id}` (the id is deterministic, so a
redelivery rewrites the same document) and answered 204. An invalid one is answered 400:
Pub/Sub retries it and, after the subscription's max attempts, moves it to the dead-letter
topic, where it shows up as a problem instead of disappearing.

Run locally:
    MISSION_CONTROL_CONFIG=mission-control.yaml python -m mission_control.writer   # serves $PORT (8080)
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from mission_control.events import validate
from mission_control.ports import StateStore

log = logging.getLogger("mission_control.writer")
EVENTS = "events"


def handle(body: Any, store: StateStore) -> tuple[int, str]:
    """Process one push request body; returns (HTTP status, reason)."""
    try:
        message = body["message"]
        event = json.loads(base64.b64decode(message["data"], validate=True))
    except (KeyError, TypeError, ValueError, binascii.Error) as e:
        return 400, f"not a Pub/Sub push with a JSON event: {e}"
    errors = validate(event)
    if errors:
        return 400, "invalid event: " + "; ".join(errors[:5])
    doc = dict(event)
    doc["received_at"] = datetime.now(timezone.utc).isoformat()
    doc["bus_message_id"] = str(message.get("messageId") or message.get("message_id") or "")
    store.upsert(EVENTS, event["id"], doc)
    return 204, "stored"


def make_app(store: StateStore):
    """A WSGI app: POST / takes a push; GET /health answers ok (Cloud Run reserves paths ending in z, so not /healthz)."""

    def app(environ, start_response):
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/")
        if method == "GET" and path == "/health":
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]
        if method != "POST" or path != "/":
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found"]
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
            body = json.loads(environ["wsgi.input"].read(size) or b"null")
        except ValueError:
            body = None
        status, reason = handle(body, store)
        if status >= 400:
            log.warning("rejected push: %s", reason)
        line = {204: "204 No Content", 400: "400 Bad Request"}.get(status, f"{status} Error")
        start_response(line, [("Content-Type", "text/plain")])
        return [] if status == 204 else [reason.encode()]

    return app


def create():
    """WSGI factory for gunicorn: `gunicorn 'mission_control.writer:create()'`."""
    from mission_control import config

    logging.basicConfig(level=logging.INFO)
    return make_app(config.load().adapter("store"))


def main() -> None:
    from wsgiref.simple_server import make_server

    from mission_control import config

    logging.basicConfig(level=logging.INFO)
    store = config.load().adapter("store")
    port = int(os.environ.get("PORT", "8080"))
    log.info("writer listening on :%d", port)
    make_server("", port, make_app(store)).serve_forever()


if __name__ == "__main__":
    main()
