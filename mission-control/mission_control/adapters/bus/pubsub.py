"""GCP Pub/Sub EventBus.

Events travel in CloudEvents *structured* mode: the whole event is the message body,
with `content-type: application/cloudevents+json`, so any CloudEvents-aware consumer
(Eventarc, Knative, another bus bridge) reads them unchanged.

Settings (mission-control.yaml):
    adapter: pubsub
    project: ${GCP_PROJECT}
    endpoint: me-west1-pubsub.googleapis.com:443   # optional; a regional endpoint keeps ordering in one region
"""
from __future__ import annotations

import json

from google.api_core import exceptions as gexc
from google.cloud import pubsub_v1

from mission_control.ports import Delivery, Event
from mission_control.registry import register

CONTENT_TYPE = "application/cloudevents+json"


class PubSubBus:
    def __init__(self, project: str, endpoint: str = "") -> None:
        self.project = project
        opts = {"api_endpoint": endpoint} if endpoint else None
        self._pub = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True),
            client_options=opts,
        )
        self._sub = pubsub_v1.SubscriberClient(client_options=opts)

    # ------------------------------------------------------------------ paths
    def _topic(self, topic: str) -> str:
        return topic if topic.startswith("projects/") else self._pub.topic_path(self.project, topic)

    def _subscription(self, sub: str) -> str:
        return sub if sub.startswith("projects/") else self._sub.subscription_path(self.project, sub)

    # ------------------------------------------------------------------ port
    def publish(self, topic: str, event: Event, *, ordering_key: str = "") -> str:
        path = self._topic(topic)
        body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode()
        attrs = {"content-type": CONTENT_TYPE, "type": str(event.get("type", ""))}
        if event.get("factoryproduct"):        # trigger subscriptions filter on these (mission_control.triggers)
            attrs["product"] = str(event["factoryproduct"])
        future = self._pub.publish(path, body, ordering_key=ordering_key, **attrs)
        try:
            return future.result(timeout=30)
        except Exception:
            if ordering_key:   # a failed ordered publish pauses its key until resumed
                self._pub.resume_publish(path, ordering_key)
            raise

    def pull(self, subscription: str, *, max_messages: int = 10, timeout_s: float = 5.0) -> list[Delivery]:
        path = self._subscription(subscription)
        try:
            resp = self._sub.pull(request={"subscription": path, "max_messages": max_messages}, timeout=timeout_s)
        except (gexc.DeadlineExceeded, gexc.RetryError):
            return []
        out = []
        for rm in resp.received_messages:
            out.append(Delivery(
                event=json.loads(rm.message.data),
                ack=self._acker(path, rm.ack_id),
                nack=self._nacker(path, rm.ack_id),
                attempt=rm.delivery_attempt or 1,
            ))
        return out

    def _acker(self, path: str, ack_id: str):
        return lambda: self._sub.acknowledge(request={"subscription": path, "ack_ids": [ack_id]})

    def _nacker(self, path: str, ack_id: str):
        return lambda: self._sub.modify_ack_deadline(
            request={"subscription": path, "ack_ids": [ack_id], "ack_deadline_seconds": 0})

    # ------------------------------------------------------------------ admin (setup and tests)
    def create_topic(self, topic: str) -> None:
        try:
            self._pub.create_topic(request={"name": self._topic(topic)})
        except gexc.AlreadyExists:
            pass

    def create_subscription(self, subscription: str, topic: str, *, dead_letter_topic: str = "",
                            max_delivery_attempts: int = 10, ack_deadline_s: int = 20) -> None:
        req = {
            "name": self._subscription(subscription),
            "topic": self._topic(topic),
            "enable_message_ordering": True,
            "ack_deadline_seconds": ack_deadline_s,
        }
        if dead_letter_topic:
            req["dead_letter_policy"] = {"dead_letter_topic": self._topic(dead_letter_topic),
                                         "max_delivery_attempts": max_delivery_attempts}
        try:
            self._sub.create_subscription(request=req)
        except gexc.AlreadyExists:
            pass

    def delete_subscription(self, subscription: str) -> None:
        try:
            self._sub.delete_subscription(request={"subscription": self._subscription(subscription)})
        except gexc.NotFound:
            pass

    def delete_topic(self, topic: str) -> None:
        try:
            self._pub.delete_topic(request={"topic": self._topic(topic)})
        except gexc.NotFound:
            pass


@register("bus", "pubsub")
def make(project: str, endpoint: str = "", **_ignored) -> PubSubBus:
    return PubSubBus(project=project, endpoint=endpoint)
