"""In-process EventBus: for tests and for running everything on one machine.

Semantics follow Pub/Sub closely enough that the same contract tests pass: each
subscription gets every message published to its topic; a delivery stays leased until
acked or nacked; a nack puts it back at the front with its attempt count raised; and while
a message with an ordering key is leased, later messages with the same key wait.
"""
from __future__ import annotations

import copy
import itertools
import threading
from collections import deque
from dataclasses import dataclass, field

from mission_control.ports import Delivery, Event
from mission_control.registry import register


@dataclass
class _Msg:
    mid: str
    event: Event
    key: str
    attempt: int = 1


@dataclass
class _Sub:
    queue: deque = field(default_factory=deque)
    leased: dict = field(default_factory=dict)   # mid → _Msg


class MemoryBus:
    def __init__(self) -> None:
        self._subs: dict[str, _Sub] = {}
        self._topics: dict[str, list[str]] = {}
        self._ids = itertools.count(1)
        self._cv = threading.Condition()

    def create_subscription(self, subscription: str, topic: str) -> None:
        with self._cv:
            self._subs.setdefault(subscription, _Sub())
            if subscription not in self._topics.setdefault(topic, []):
                self._topics[topic].append(subscription)

    def publish(self, topic: str, event: Event, *, ordering_key: str = "") -> str:
        with self._cv:
            mid = str(next(self._ids))
            for name in self._topics.get(topic, []):
                self._subs[name].queue.append(_Msg(mid, copy.deepcopy(event), ordering_key))
            self._cv.notify_all()
            return mid

    def pull(self, subscription: str, *, max_messages: int = 10, timeout_s: float = 5.0) -> list[Delivery]:
        with self._cv:
            sub = self._subs[subscription]
            self._cv.wait_for(lambda: self._ready(sub), timeout=timeout_s)
            out: list[Delivery] = []
            blocked = {m.key for m in sub.leased.values() if m.key}
            for msg in list(sub.queue):
                if len(out) >= max_messages:
                    break
                if msg.key and msg.key in blocked:
                    continue
                sub.queue.remove(msg)
                sub.leased[msg.mid] = msg
                out.append(Delivery(copy.deepcopy(msg.event), *self._handles(sub, msg), attempt=msg.attempt))
            # keys taken in this batch stay in order inside the batch; block their followers
            return out

    def _ready(self, sub: _Sub) -> bool:
        blocked = {m.key for m in sub.leased.values() if m.key}
        return any(not m.key or m.key not in blocked for m in sub.queue)

    def _handles(self, sub: _Sub, msg: _Msg):
        def ack() -> None:
            with self._cv:
                sub.leased.pop(msg.mid, None)
                self._cv.notify_all()

        def nack() -> None:
            with self._cv:
                if sub.leased.pop(msg.mid, None) is not None:
                    msg.attempt += 1
                    sub.queue.appendleft(msg)
                    self._cv.notify_all()

        return ack, nack


@register("bus", "memory")
def make(**_settings) -> MemoryBus:
    return MemoryBus()
