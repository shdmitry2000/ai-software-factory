"""Chain triggers (R6): a component's `consumes` → one filtered subscription per deployment.

At deployment.apply the command handler makes sure the deployment has its own subscription
on factory-events that lets through only the event types the component consumes (and,
for a per-product component, only that product's events), grants the host's service
account the right to pull from it, and hands its name to the containers as
`MC_TRIGGER_SUBSCRIPTION`. The agent pulls from it and acts within seconds; its timer
stays as the safety net. deployment.remove (and host.remove) delete the subscription.

Filtering happens in Pub/Sub on message attributes, which the pubsub bus sets on every
publish: `type` (the CloudEvents type) and `product` (factoryproduct, when present).
The handler only ever touches subscriptions named `factory-trig-*`.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from mission_control.ports import Event, Step

PREFIX = "factory-trig-"
FILTER_MAX = 256                       # Pub/Sub's limit on a filter expression
_TYPE = re.compile(r"^dev\.factory\.[a-z_]+\.([a-z_]+|\*)$")


@dataclass(frozen=True)
class Trigger:
    subscription: str
    types: tuple[str, ...]
    product: str = ""

    def accepts(self, ev: Event) -> bool:
        """The same rule as the Pub/Sub filter, for the in-memory bus and for agents."""
        t = str(ev.get("type", ""))
        if not any(t == x or (x.endswith(".*") and t.startswith(x[:-1])) for x in self.types):
            return False
        return not self.product or ev.get("factoryproduct") == self.product

    def pubsub_filter(self) -> str:
        parts = [f'hasPrefix(attributes.type, "{x[:-1]}")' if x.endswith(".*") else f'attributes.type = "{x}"'
                 for x in self.types]
        expr = parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"
        if self.product:
            expr += f' AND attributes.product = "{self.product}"'
        return expr


def subscription_name(deployment_id: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", deployment_id.lower().replace("@", "--")).strip("-")
    return (PREFIX + slug)[:255]


def trigger_for(deployment_id: str, consumes: tuple[str, ...], product: str = "") -> Trigger:
    bad = [t for t in consumes if not _TYPE.match(t)]
    if bad:
        raise ValueError(f"cannot trigger on {', '.join(bad)}")
    if product and not re.match(r"^[A-Za-z0-9_.-]+$", product):
        raise ValueError(f"bad product name {product!r}")
    t = Trigger(subscription_name(deployment_id), tuple(consumes), product)
    if len(t.pubsub_filter().encode()) > FILTER_MAX:
        raise ValueError(f"{deployment_id}: the trigger filter is over {FILTER_MAX} bytes; use a wildcard type (x.*)")
    return t


def _check_name(name: str) -> None:
    if not name.startswith(PREFIX):
        raise ValueError(f"only {PREFIX}* subscriptions are managed here (got {name!r})")


class PubSubTriggers:
    """Subscriptions in Pub/Sub. `member_for(host)` names the identity that pulls on that
    host (factory-1: factory-vm; a VM host: factory-agent)."""

    def __init__(self, project: str, *, topic: str = "factory-events", member_for: Callable[[str], str] | None = None,
                 subscriber=None) -> None:
        from google.cloud import pubsub_v1
        self.project, self.topic = project, topic
        self._sub = subscriber or pubsub_v1.SubscriberClient()
        self.member_for = member_for or (lambda host: f"serviceAccount:factory-{'vm' if host == 'local' else 'agent'}"
                                                      f"@{project}.iam.gserviceaccount.com")

    def _path(self, name: str) -> str:
        return self._sub.subscription_path(self.project, name)

    def _get(self, name: str):
        from google.api_core import exceptions as gexc
        try:
            return self._sub.get_subscription(request={"subscription": self._path(name)})
        except gexc.NotFound:
            return None

    def ensure(self, t: Trigger, host: str) -> Iterator[Step]:
        from google.api_core import exceptions as gexc
        from google.protobuf import duration_pb2
        _check_name(t.subscription)
        want = t.pubsub_filter()
        cur = self._get(t.subscription)
        if cur is not None and cur.filter != want:      # a filter cannot be changed in place
            self._sub.delete_subscription(request={"subscription": self._path(t.subscription)})
            cur = None
        if cur is None:
            try:
                self._sub.create_subscription(request={
                    "name": self._path(t.subscription), "topic": f"projects/{self.project}/topics/{self.topic}",
                    "filter": want, "ack_deadline_seconds": 60,
                    "message_retention_duration": duration_pb2.Duration(seconds=86400),
                    "expiration_policy": {},             # never expires while the deployment exists
                    "labels": {"factory": "trigger"}})
            except gexc.AlreadyExists:
                pass
            detail = f"{t.subscription} created: {want}"
        else:
            detail = f"{t.subscription} ({want})"
        member = self.member_for(host)
        policy = self._sub.get_iam_policy(request={"resource": self._path(t.subscription)})
        if not any(b.role == "roles/pubsub.subscriber" and member in b.members for b in policy.bindings):
            policy.bindings.add(role="roles/pubsub.subscriber", members=[member])
            self._sub.set_iam_policy(request={"resource": self._path(t.subscription), "policy": policy})
        yield Step("trigger", True, detail)

    def remove(self, name: str) -> Iterator[Step]:
        from google.api_core import exceptions as gexc
        _check_name(name)
        try:
            self._sub.delete_subscription(request={"subscription": self._path(name)})
        except gexc.NotFound:
            return
        yield Step("trigger", True, f"{name} deleted")


class MemoryTriggers:
    """The same on the in-memory bus (tests, one-machine runs)."""

    def __init__(self, bus, *, topic: str = "factory-events") -> None:
        self.bus, self.topic = bus, topic
        self.active: dict[str, Trigger] = {}
        self.members: dict[str, str] = {}

    def ensure(self, t: Trigger, host: str) -> Iterator[Step]:
        _check_name(t.subscription)
        created = t.subscription not in self.active or self.active[t.subscription] != t
        if t.subscription in self.active and created:
            self.bus.delete_subscription(t.subscription)
        self.active[t.subscription], self.members[t.subscription] = t, host
        self.bus.create_subscription(t.subscription, self.topic, accept=t.accepts)
        yield Step("trigger", True, f"{t.subscription} {'created' if created else 'exists'}: {t.pubsub_filter()}")

    def remove(self, name: str) -> Iterator[Step]:
        _check_name(name)
        if self.active.pop(name, None) is None:
            return
        self.members.pop(name, None)
        self.bus.delete_subscription(name)
        yield Step("trigger", True, f"{name} deleted")
