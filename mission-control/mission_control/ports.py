"""The ports: every outside system mission control talks to, as an interface.

Core code imports only this module. Each port has adapters under
`mission_control/adapters/<port>/<name>` and one contract test suite under
`contract_tests/<port>/` that every adapter must pass. Which adapter serves a port is
chosen in `mission-control.yaml`, never in code.

Events are CloudEvents 1.0 in structured JSON form (see `schema/envelope.v1.json`); they
travel as plain dicts so no adapter forces a CloudEvents library on the others.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

Event = dict[str, Any]
"""A CloudEvents 1.0 event, structured JSON form."""

Doc = dict[str, Any]
"""A stored document (a projection row)."""


# --------------------------------------------------------------------------- EventBus


@dataclass(frozen=True)
class Delivery:
    """One received event. `ack()` once it is handled; an unacked delivery comes back."""

    event: Event
    ack: Callable[[], None]
    nack: Callable[[], None]
    attempt: int = 1


@runtime_checkable
class EventBus(Protocol):
    def publish(self, topic: str, event: Event, *, ordering_key: str = "") -> str:
        """Send one event; returns the bus's message id. Same ordering key → same order."""

    def pull(self, subscription: str, *, max_messages: int = 10, timeout_s: float = 5.0) -> list[Delivery]:
        """Receive up to `max_messages`; empty list when nothing arrives within the timeout."""


# --------------------------------------------------------------------------- StateStore


@runtime_checkable
class StateStore(Protocol):
    def get(self, collection: str, doc_id: str) -> Doc | None: ...

    def upsert(self, collection: str, doc_id: str, doc: Doc) -> None:
        """Create or replace. Writing the same doc twice leaves one document."""

    def merge(self, collection: str, doc_id: str, fields: Doc) -> None:
        """Update only the given fields, creating the document if needed."""

    def update(self, collection: str, doc_id: str, fn: Callable[[Doc | None], Doc | None]) -> Doc | None:
        """Read-modify-write atomically (transaction); `fn` returning None deletes."""

    def query(self, collection: str, *, where: Iterable[tuple[str, str, Any]] = (),
              order_by: str = "", descending: bool = False, limit: int = 0) -> list[Doc]:
        """Filter with (field, op, value), op in ==, !=, <, <=, >, >=, in."""

    def delete(self, collection: str, doc_id: str) -> None: ...


# --------------------------------------------------------------------------- WorkTracker


@dataclass
class WorkItem:
    id: str                               # neutral: tracker:<adapter>/<key>
    title: str
    state: str                            # open | closed
    labels: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)   # neutral ids
    claimed_by: str = ""                  # engine name, empty when unclaimed
    url: str = ""                         # human link, for display only
    raw: Doc = field(default_factory=dict)


@runtime_checkable
class WorkTracker(Protocol):
    def list(self, *, state: str = "open", labels: Iterable[str] = ()) -> list[WorkItem]: ...

    def get(self, item_id: str) -> WorkItem | None: ...

    def comment(self, item_id: str, body: str) -> str:
        """Returns the comment's neutral id."""

    def set_labels(self, item_id: str, add: Iterable[str] = (), remove: Iterable[str] = ()) -> None: ...

    def claim(self, item_id: str, engine: str, run: str) -> bool:
        """True if this engine now holds the claim (the oldest claim wins)."""

    def release(self, item_id: str, engine: str) -> None: ...


# --------------------------------------------------------------------------- CodeHost


@dataclass
class Change:
    id: str                               # neutral: codehost:<adapter>/<repo>#<n>
    title: str
    state: str                            # open | merged | closed
    head: str                             # head commit sha
    base: str = ""                        # branch the change targets
    work_item: str = ""                   # neutral work item id it delivers
    checks: dict[str, str] = field(default_factory=dict)  # name → success|failure|pending
    review: str = ""                      # ready | changes_requested | ""
    url: str = ""


@runtime_checkable
class CodeHost(Protocol):
    def changes(self, *, state: str = "open") -> list[Change]: ...

    def get_change(self, change_id: str) -> Change | None: ...

    def protected_paths(self) -> list[str]: ...

    def diff(self, change_id: str, *, max_bytes: int = 200_000) -> tuple[str, bool]:
        """Unified diff of the change against its base, and whether it was truncated."""


# --------------------------------------------------------------------------- MemoryStore


@dataclass
class MemoryDoc:
    product: str
    name: str                             # e.g. MISSION.md, DECISIONS.md, holdout/HOLDOUT.md
    body: str
    version: str                          # adapter-specific (git sha, revision number)
    scope: str = "all"                    # all | holdout (only holdout agents may read)


@runtime_checkable
class MemoryStore(Protocol):
    def get(self, product: str, name: str, *, reader_scope: str = "all") -> MemoryDoc | None:
        """A holdout-scoped doc is returned only to a reader with scope 'holdout'."""

    def put(self, product: str, name: str, body: str, *, scope: str = "all", author: str = "") -> MemoryDoc: ...

    def list(self, product: str, *, reader_scope: str = "all") -> list[str]: ...

    def history(self, product: str, name: str) -> list[str]:
        """Versions, newest first."""

    def search(self, product: str, text: str, *, reader_scope: str = "all", limit: int = 10) -> list[MemoryDoc]: ...


# --------------------------------------------------------------------------- PromptRegistry


@dataclass(frozen=True)
class Prompt:
    name: str                             # e.g. compliance/rules
    text: str
    version: str                          # immutable, adapter-specific ("3", a git sha, an MLflow version)
    hash: str                             # sha256:<hex> of text, the same in every adapter
    label: str = ""                       # the label it was fetched by, if any


@runtime_checkable
class PromptRegistry(Protocol):
    def get(self, name: str, *, label: str = "production", version: str = "") -> Prompt | None:
        """By exact version when given, else by label. None when missing."""

    def put(self, name: str, text: str, *, author: str, note: str = "") -> Prompt:
        """Store a new immutable version (never overwrites one)."""

    def set_label(self, name: str, label: str, version: str) -> None:
        """Point a label at an existing version (rollout and rollback)."""

    def versions(self, name: str) -> list[str]:
        """Versions, newest first."""

    def labels(self, name: str) -> dict[str, str]: ...


# --------------------------------------------------------------------------- EngineAdapter


@runtime_checkable
class EngineAdapter(Protocol):
    def events_since(self, cursor: str, *, limit: int = 500) -> tuple[list[Event], str]:
        """Engine events as CloudEvents after `cursor`, and the new cursor. Read-only."""

    def heartbeat(self) -> Event:
        """A dev.factory.engine.heartbeat event describing the engine right now."""


# --------------------------------------------------------------------------- Secrets


@runtime_checkable
class Secrets(Protocol):
    def get(self, name: str) -> str | None: ...

    def put(self, name: str, value: str) -> None: ...

    def names(self) -> list[str]:
        """Names only, never values."""


# --------------------------------------------------------------------------- Notifier


@runtime_checkable
class Notifier(Protocol):
    def send(self, title: str, body: str, *, severity: str = "info", link: str = "") -> None: ...


# --------------------------------------------------------------------------- AgentRuntime
# Hosts run containers; a deployment is one component on one host. Only the main node
# drives this port. Design: docs/deploy-and-hosts-plan.md.

LLM_METHODS = ("none", "project", "api_key")
"""How a deployment gets Claude. `project` = the project's own login (subscription token
or API key, whichever the main node holds); `api_key` = an Anthropic API key only."""


@dataclass(frozen=True)
class Host:
    id: str                         # "local" = the main node itself; "vm-agents-1" …
    kind: str                       # local | vm  (later: ssh, gke)
    address: str = ""
    state: str = "unknown"          # unknown | installing | healthy | unreachable
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Deployment:
    """Desired state of one component on one host. Holds no secret: the LLM credential
    is resolved by the adapter on the host and handed over as a file."""
    component: str
    host: str
    image: str                      # repository[:tag]
    digest: str = ""                # sha256:… ; pinned when set
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)        # non-secret settings only
    llm: str = "none"               # one of LLM_METHODS
    replicas: int = 1
    run_as: str = ""                # container uid; the credential file is readable by it alone
    tmpfs: tuple[str, ...] = ("/tmp",)                        # writable paths (root fs is read-only)
    cpus: str = ""
    memory: str = ""

    @property
    def id(self) -> str:
        return f"{self.component}@{self.host}"

    def ref(self) -> str:
        """What the container runtime runs: the digest when pinned, else the image."""
        if self.digest and self.digest.startswith("sha256:") and "@" not in self.image:
            base = self.image.rsplit(":", 1)[0] if ":" in self.image.rsplit("/", 1)[-1] else self.image
            return f"{base}@{self.digest}"
        return self.image


@dataclass(frozen=True)
class Instance:
    deployment: str                 # Deployment.id
    name: str                       # container name
    host: str
    state: str                      # running | exited | created | restarting | paused | dead
    image: str = ""
    digest: str = ""
    started_at: str = ""


@dataclass(frozen=True)
class Step:
    """One step of a long action; the command handler turns each into an event."""
    step: str
    ok: bool
    detail: str = ""                # short, never contains a secret


@runtime_checkable
class AgentRuntime(Protocol):
    def hosts(self) -> list[Host]: ...

    def create_host(self, host: Host) -> Iterable[Step]:
        """Create a new host (a sandbox VM). Not every adapter can; those raise ValueError."""

    def install(self, host_id: str) -> Iterable[Step]:
        """Make a host ready to run containers. Idempotent."""

    def remove_host(self, host_id: str) -> Iterable[Step]: ...

    def apply(self, deployment: Deployment) -> Iterable[Step]:
        """Make the host match `deployment`. Idempotent: an unchanged spec restarts nothing."""

    def remove(self, deployment_id: str) -> Iterable[Step]: ...

    def start(self, deployment_id: str) -> Iterable[Step]: ...

    def stop(self, deployment_id: str) -> Iterable[Step]: ...

    def restart(self, deployment_id: str) -> Iterable[Step]: ...

    def scale(self, deployment_id: str, replicas: int) -> Iterable[Step]: ...

    def status(self) -> list[Instance]: ...

    def logs(self, deployment_id: str, *, tail: int = 200) -> list[str]:
        """Last `tail` lines of every replica. Unknown deployment: KeyError."""


PORTS: dict[str, type] = {
    "bus": EventBus,
    "store": StateStore,
    "tracker": WorkTracker,
    "codehost": CodeHost,
    "memory": MemoryStore,
    "engine": EngineAdapter,
    "prompts": PromptRegistry,
    "secrets": Secrets,
    "notifier": Notifier,
    "runtime": AgentRuntime,
}
"""Port name (as used in mission-control.yaml) → interface."""


def is_adapter_for(port: str, obj: object) -> bool:
    """True when `obj` has every method of the named port (structural check)."""
    return isinstance(obj, PORTS[port])


__all__ = [
    "AgentRuntime", "Deployment", "Host", "Instance", "LLM_METHODS", "Step",
    "Change", "CodeHost", "Delivery", "Doc", "EngineAdapter", "Event", "EventBus",
    "MemoryDoc", "MemoryStore", "Notifier", "PORTS", "Prompt", "PromptRegistry", "Secrets", "StateStore", "WorkItem",
    "WorkTracker", "is_adapter_for",
]
