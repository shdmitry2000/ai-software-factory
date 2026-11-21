"""Build a component's image on the main node from its `build` section (deploy/).

`build()` is a generator of Steps whose return value is the image id, so a caller
writes `digest = yield from builder.build(component)`.
"""
from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path

from mission_control.deployspec import Component
from mission_control.ports import Step


class DockerBuilder:
    def __init__(self, repo_root: str | Path, *, docker: str = "docker", keep: int = 5):
        self.root, self.docker, self.keep = Path(repo_root), docker, keep

    def _tag(self) -> str:
        p = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--short=12", "HEAD"],
                           capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else "dev"

    def build(self, c: Component) -> Generator[Step, None, str]:
        if not c.build:
            yield Step("build", False, f"{c.name} has no build section")
            return ""
        ref = f"{c.image}:{self._tag()}"
        ctx = self.root / c.build.get("context", ".")
        p = subprocess.run([self.docker, "build", "-q", "-f", str(self.root / c.build["dockerfile"]),
                            "-t", ref, str(ctx)], capture_output=True, text=True)
        if p.returncode:
            yield Step("build", False, (p.stderr or p.stdout).strip()[-400:])
            return ""
        image_id = subprocess.run([self.docker, "image", "inspect", "--format", "{{.Id}}", ref],
                                  capture_output=True, text=True).stdout.strip()
        yield Step("build", True, f"{ref} {image_id[:19]}")
        self._prune(c.image)
        return image_id

    def has(self, image_id: str) -> bool:
        """Whether this node still has the image (it may not after a rebuild)."""
        return subprocess.run([self.docker, "image", "inspect", image_id], capture_output=True).returncode == 0

    def _prune(self, repo: str) -> None:
        """Keep the newest `keep` tags of this image."""
        tags = subprocess.run([self.docker, "image", "ls", repo, "--format", "{{.Tag}}"],
                              capture_output=True, text=True).stdout.split()
        for tag in tags[self.keep:]:
            subprocess.run([self.docker, "image", "rm", "-f", f"{repo}:{tag}"], capture_output=True)


class FakeBuilder:
    def __init__(self, digest: str = "sha256:" + "b" * 64, fail: bool = False):
        self.digest, self.fail = digest, fail
        self.built: list[str] = []
        self.images: set[str] = set()

    def has(self, image_id: str) -> bool:
        return image_id in self.images

    def build(self, c: Component) -> Generator[Step, None, str]:
        if self.fail:
            yield Step("build", False, "build failed")
            return ""
        self.built.append(c.name)
        self.images.add(self.digest)
        yield Step("build", True, f"{c.image} {self.digest[:19]}")
        return self.digest
