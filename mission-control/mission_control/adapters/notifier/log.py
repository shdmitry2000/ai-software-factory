"""Notifier into the service's log, as one structured JSON line per alert.

On Cloud Run a JSON line on stdout becomes a Cloud Logging entry with that `severity`
(critical → ERROR, warning → WARNING, info → INFO) and `labels.factory_alert = "1"`,
so a log-based alert policy in Cloud Monitoring can forward it to email, chat or a
pager without the factory holding any of those credentials.
"""
from __future__ import annotations

import json
import sys

from mission_control.registry import register

LEVEL = {"critical": "ERROR", "warning": "WARNING", "info": "INFO"}


class LogNotifier:
    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdout

    def send(self, title: str, body: str, *, severity: str = "info", link: str = "") -> None:
        line = {"severity": LEVEL.get(severity, "INFO"), "message": f"{title}: {body}" if body else title,
                "alert": {"title": title, "body": body, "severity": severity, "link": link},
                "logging.googleapis.com/labels": {"factory_alert": "1"}}
        self.stream.write(json.dumps(line) + "\n")
        self.stream.flush()


class MemoryNotifier:
    """Keeps what was sent (tests, one-machine runs)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, title: str, body: str, *, severity: str = "info", link: str = "") -> None:
        self.sent.append({"title": title, "body": body, "severity": severity, "link": link})


@register("notifier", "log")
def make_log(**kw) -> LogNotifier:
    return LogNotifier()


@register("notifier", "memory")
def make_memory(**kw) -> MemoryNotifier:
    return MemoryNotifier()
