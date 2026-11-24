"""PromptRegistry on plain files: the first adapter, reviewable through pull requests.

    <root>/<name>/0001.md          immutable versions, numbered (never edited once merged)
    <root>/<name>/labels.yaml      production: 1   (a label move is a one-line PR)

Numbered versions plus movable labels is the same model as MLflow's prompt registry
(versions + aliases), so the service adapter later is a straight swap.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from mission_control.ports import Prompt
from mission_control.registry import register

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)*$")
_VERSION = re.compile(r"^(\d{4,})\.md$")


def text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class FilePrompts:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _dir(self, name: str) -> Path:
        if not _NAME.match(name):
            raise ValueError(f"bad prompt name {name!r}")
        return self.root / name

    def versions(self, name: str) -> list[str]:
        d = self._dir(name)
        nums = [int(m.group(1)) for f in d.glob("*.md") if (m := _VERSION.match(f.name))] if d.is_dir() else []
        return [str(n) for n in sorted(nums, reverse=True)]

    def labels(self, name: str) -> dict[str, str]:
        f = self._dir(name) / "labels.yaml"
        raw = yaml.safe_load(f.read_text()) if f.is_file() else None
        return {str(k): str(v) for k, v in (raw or {}).items()}

    def get(self, name: str, *, label: str = "production", version: str = "") -> Prompt | None:
        v = version or self.labels(name).get(label, "")
        if not v or not v.isdigit():
            return None
        f = self._dir(name) / f"{int(v):04d}.md"
        if not f.is_file():
            return None
        text = f.read_text()
        return Prompt(name=name, text=text, version=str(int(v)), hash=text_hash(text),
                      label="" if version else label)

    def put(self, name: str, text: str, *, author: str, note: str = "") -> Prompt:
        d = self._dir(name)
        d.mkdir(parents=True, exist_ok=True)
        n = int((self.versions(name) or ["0"])[0]) + 1
        f = d / f"{n:04d}.md"
        with f.open("x") as fh:          # "x": an existing version is never overwritten
            fh.write(text)
        return Prompt(name=name, text=text, version=str(n), hash=text_hash(text))

    def set_label(self, name: str, label: str, version: str) -> None:
        if version not in self.versions(name):
            raise KeyError(f"{name} has no version {version!r}")
        labels = self.labels(name) | {label: version}
        (self._dir(name) / "labels.yaml").write_text(
            yaml.safe_dump({k: int(v) for k, v in sorted(labels.items())}, sort_keys=False))


@register("prompts", "files")
def make(root: str) -> FilePrompts:
    return FilePrompts(root)
