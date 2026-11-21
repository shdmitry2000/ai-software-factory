"""PromptRegistry contract: every adapter must pass. Versions are immutable, labels move."""
from __future__ import annotations

import pytest

from mission_control import registry
from mission_control.ports import is_adapter_for


def _files(tmp_path):
    return registry.get("prompts", "files")(root=str(tmp_path))


CANDIDATES = {"files": _files}


@pytest.fixture(params=sorted(CANDIDATES))
def prompts(request, tmp_path):
    return CANDIDATES[request.param](tmp_path)


def test_is_a_prompt_registry(prompts):
    assert is_adapter_for("prompts", prompts)


def test_missing_prompt_and_label(prompts):
    assert prompts.get("nope/missing") is None
    prompts.put("a/b", "v1", author="t")
    assert prompts.get("a/b", label="production") is None          # no label yet


def test_versions_are_immutable_and_ordered(prompts):
    one = prompts.put("a/b", "first", author="t")
    two = prompts.put("a/b", "second", author="t")
    assert one.version != two.version and prompts.versions("a/b") == [two.version, one.version]
    assert prompts.get("a/b", version=one.version).text == "first"   # unchanged by the second put
    assert one.hash.startswith("sha256:") and one.hash != two.hash


def test_label_moves_are_rollout_and_rollback(prompts):
    one = prompts.put("a/b", "first", author="t")
    two = prompts.put("a/b", "second", author="t")
    prompts.set_label("a/b", "production", one.version)
    assert prompts.get("a/b").text == "first"
    prompts.set_label("a/b", "production", two.version)
    got = prompts.get("a/b")
    assert (got.text, got.version, got.label) == ("second", two.version, "production")
    prompts.set_label("a/b", "production", one.version)              # rollback
    assert prompts.get("a/b").version == one.version
    assert prompts.labels("a/b") == {"production": one.version}


def test_label_must_point_at_a_version(prompts):
    prompts.put("a/b", "x", author="t")
    with pytest.raises(KeyError):
        prompts.set_label("a/b", "production", "999")


def test_hash_is_content_only(prompts):
    assert prompts.put("a/b", "same", author="x").hash == prompts.put("c/d", "same", author="y").hash
