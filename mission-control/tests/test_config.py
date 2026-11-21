"""The config file, not the code, decides which adapter serves a port."""
from __future__ import annotations

import pytest

from mission_control import config, registry
from mission_control.adapters.store.memory import MemoryStore


class OtherStore(MemoryStore):
    """A second store adapter, standing in for 'switch Firestore to Postgres'."""

    def __init__(self, label: str = "") -> None:
        super().__init__()
        self.label = label


registry.register("store", "memory2")(lambda **s: OtherStore(**s))


def test_config_picks_the_adapter():
    cfg = config.loads("ports:\n  store:\n    adapter: memory\n")
    assert type(cfg.adapter("store")) is MemoryStore


def test_switching_adapter_is_a_config_change_only():
    cfg = config.loads("ports:\n  store:\n    adapter: memory2\n    label: switched\n")
    store = cfg.adapter("store")
    assert isinstance(store, OtherStore) and store.label == "switched"
    store.upsert("runs", "r1", {"status": "running"})       # same port, same calls
    assert store.get("runs", "r1") == {"status": "running"}


def test_env_values_are_expanded(monkeypatch):
    monkeypatch.setenv("MC_LABEL", "from-env")
    cfg = config.loads("ports:\n  store:\n    adapter: memory2\n    label: ${MC_LABEL}\n")
    assert cfg.adapter("store").label == "from-env"


def test_env_default(monkeypatch):
    monkeypatch.delenv("MC_MISSING", raising=False)
    cfg = config.loads("ports:\n  store:\n    adapter: memory2\n    label: ${MC_MISSING:-fallback}\n")
    assert cfg.adapter("store").label == "fallback"


def test_unknown_port_is_rejected():
    with pytest.raises(KeyError, match="unknown port"):
        config.loads("ports:\n  teleport:\n    adapter: memory\n")


def test_unknown_adapter_names_the_known_ones():
    cfg = config.loads("ports:\n  store:\n    adapter: nosuch\n")
    with pytest.raises(KeyError, match="known: .*memory"):
        cfg.adapter("store")


def test_adapter_must_implement_the_port():
    registry.register("store", "broken")(lambda **s: object())
    cfg = config.loads("ports:\n  store:\n    adapter: broken\n")
    with pytest.raises(TypeError, match="does not implement"):
        cfg.adapter("store")
