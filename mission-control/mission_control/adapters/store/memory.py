"""In-process StateStore: for tests and single-machine runs. Not durable."""
from __future__ import annotations

import copy
import operator
import threading
from collections.abc import Callable, Iterable
from typing import Any

from mission_control.ports import Doc
from mission_control.registry import register

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
    ">": operator.gt, ">=": operator.ge, "in": lambda a, b: a in b,
}


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Doc]] = {}
        self._lock = threading.RLock()

    def get(self, collection: str, doc_id: str) -> Doc | None:
        with self._lock:
            doc = self._data.get(collection, {}).get(doc_id)
            return copy.deepcopy(doc) if doc is not None else None

    def upsert(self, collection: str, doc_id: str, doc: Doc) -> None:
        with self._lock:
            self._data.setdefault(collection, {})[doc_id] = copy.deepcopy(doc)

    def merge(self, collection: str, doc_id: str, fields: Doc) -> None:
        with self._lock:
            self._data.setdefault(collection, {}).setdefault(doc_id, {}).update(copy.deepcopy(fields))

    def update(self, collection: str, doc_id: str, fn: Callable[[Doc | None], Doc | None]) -> Doc | None:
        with self._lock:
            new = fn(self.get(collection, doc_id))
            if new is None:
                self.delete(collection, doc_id)
            else:
                self.upsert(collection, doc_id, new)
            return copy.deepcopy(new)

    def query(self, collection: str, *, where: Iterable[tuple[str, str, Any]] = (),
              order_by: str = "", descending: bool = False, limit: int = 0) -> list[Doc]:
        with self._lock:
            docs = [copy.deepcopy(d) for d in self._data.get(collection, {}).values()]
        for field, op, value in where:
            docs = [d for d in docs if field in d and _OPS[op](d[field], value)]
        if order_by:
            docs = [d for d in docs if order_by in d]
            docs.sort(key=lambda d: d[order_by], reverse=descending)
        return docs[:limit] if limit else docs

    def count(self, collection: str, *, where: Iterable[tuple[str, str, Any]] = ()) -> int:
        return len(self.query(collection, where=where))

    def delete(self, collection: str, doc_id: str) -> None:
        with self._lock:
            self._data.get(collection, {}).pop(doc_id, None)


@register("store", "memory")
def make(**_settings) -> MemoryStore:
    return MemoryStore()
