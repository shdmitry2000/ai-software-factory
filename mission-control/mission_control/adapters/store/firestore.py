"""Firestore StateStore.

Settings (mission-control.yaml):
    adapter: firestore
    project: ${GCP_PROJECT}
    database: (default)          # optional
    prefix: ""                   # optional collection-name prefix, e.g. "staging_"

Document ids may hold "/" (neutral ids like tracker:github/owner/repo#14); Firestore
forbids it in ids, so ids are percent-encoded on the way in and decoded on the way out.
A query that filters on one field and orders by another needs a composite index; until
one exists the adapter orders in memory and logs once, so callers never see the gap.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote, unquote

from google.api_core import exceptions as gexc
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from mission_control.ports import Doc
from mission_control.registry import register

log = logging.getLogger(__name__)
_OPS = {"==": "==", "!=": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">=", "in": "in"}


def _enc(doc_id: str) -> str:
    return quote(doc_id, safe="")


def _dec(doc_id: str) -> str:
    return unquote(doc_id)


class FirestoreStore:
    def __init__(self, project: str, database: str = "(default)", prefix: str = "") -> None:
        self._db = firestore.Client(project=project, database=database)
        self._prefix = prefix
        self._warned: set[tuple] = set()

    def _col(self, collection: str):
        return self._db.collection(self._prefix + collection)

    def get(self, collection: str, doc_id: str) -> Doc | None:
        snap = self._col(collection).document(_enc(doc_id)).get()
        return snap.to_dict() if snap.exists else None

    def upsert(self, collection: str, doc_id: str, doc: Doc) -> None:
        self._col(collection).document(_enc(doc_id)).set(doc)

    def merge(self, collection: str, doc_id: str, fields: Doc) -> None:
        self._col(collection).document(_enc(doc_id)).set(fields, merge=True)

    def update(self, collection: str, doc_id: str, fn: Callable[[Doc | None], Doc | None]) -> Doc | None:
        ref = self._col(collection).document(_enc(doc_id))

        @firestore.transactional
        def run(tx) -> Doc | None:
            snap = ref.get(transaction=tx)
            new = fn(snap.to_dict() if snap.exists else None)
            if new is None:
                tx.delete(ref)
            else:
                tx.set(ref, new)
            return new

        return run(self._db.transaction())

    def query(self, collection: str, *, where: Iterable[tuple[str, str, Any]] = (),
              order_by: str = "", descending: bool = False, limit: int = 0) -> list[Doc]:
        where = list(where)
        q = self._col(collection)
        for field, op, value in where:
            q = q.where(filter=FieldFilter(field, _OPS[op], value))
        other_fields = {f for f, _, _ in where if f != order_by}
        if order_by:
            ordered = q.order_by(order_by, direction=firestore.Query.DESCENDING if descending else firestore.Query.ASCENDING)
            try:
                docs = [s.to_dict() for s in (ordered.limit(limit) if limit else ordered).stream()]
                return docs
            except gexc.FailedPrecondition:
                key = (collection, tuple(sorted(other_fields)), order_by)
                if key not in self._warned:
                    self._warned.add(key)
                    log.warning("no composite index for %s where %s order by %s; ordering in memory",
                                collection, sorted(other_fields), order_by)
        docs = [s.to_dict() for s in q.stream()]
        if order_by:
            docs = sorted((d for d in docs if order_by in d), key=lambda d: d[order_by], reverse=descending)
        return docs[:limit] if limit else docs

    def count(self, collection: str, *, where: Iterable[tuple[str, str, Any]] = ()) -> int:
        """Server-side count (an aggregation: about one read per 1000 matches). Equality
        plus a range on another field needs a composite index (infra/setup.sh)."""
        q = self._col(collection)
        for field, op, value in where:
            q = q.where(filter=FieldFilter(field, _OPS[op], value))
        try:
            return int(q.count().get()[0][0].value)
        except gexc.FailedPrecondition:
            log.warning("no composite index to count %s where %s; counting documents", collection,
                        [f for f, _, _ in where])
            return sum(1 for _ in q.stream())

    def delete(self, collection: str, doc_id: str) -> None:
        self._col(collection).document(_enc(doc_id)).delete()

    # ------------------------------------------------------------------ admin (tests)
    def drop_collection(self, collection: str) -> int:
        n = 0
        for snap in self._col(collection).stream():
            snap.reference.delete()
            n += 1
        return n


@register("store", "firestore")
def make(project: str, database: str = "(default)", prefix: str = "", **_ignored) -> FirestoreStore:
    return FirestoreStore(project=project, database=database, prefix=prefix)
