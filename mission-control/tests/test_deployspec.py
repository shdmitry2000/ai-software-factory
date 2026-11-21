"""Deploy files: the repo's own files load; every kind of bad file is refused."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mission_control import deployspec
from mission_control.deployspec import SpecError

REPO = Path(__file__).resolve().parents[2]


def test_repo_deploy_files_load_and_resolve():
    spec = deployspec.load(REPO / "deploy", products_dir=REPO / "products")
    assert "compliance" in spec.components and "local" in spec.hosts
    d = deployspec.resolve(spec, "compliance@local", vars={"PROJECT": "proj-1"}, digest="sha256:" + "a" * 64)
    assert d.id == "compliance@local" and d.llm == "project" and d.run_as == "10001"
    assert d.env["GCP_PROJECT"] == "proj-1" and d.env["FACTORY_PRODUCT"] == "file-to-markdown-convertor"
    assert d.digest == "sha256:" + "a" * 64 and "/home/agent" in d.tmpfs
    assert ("dev.factory.pullrequest.opened", "starts", "compliance") in spec.chains()


@pytest.fixture
def ddir(tmp_path):
    shutil.copytree(REPO / "deploy", tmp_path / "deploy")
    shutil.copytree(REPO / "products", tmp_path / "products")
    return tmp_path


def load(ddir):
    return deployspec.load(ddir / "deploy", products_dir=ddir / "products")


def edit(path: Path, old: str, new: str):
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


def problems(ddir) -> str:
    with pytest.raises(SpecError) as e:
        load(ddir)
    return "\n".join(e.value.problems)


COMP = ("deploy", "components", "compliance.yaml")


@pytest.mark.parametrize("file,old,new,expect", [
    (COMP, "llm: project", "llm: vertex", "llm"),
    (COMP, "MC_TOPIC: factory-events", "GITHUB_TOKEN: abc", "looks like a secret"),
    (COMP, "${PROJECT}", "${NOPE}", "unknown ${NOPE}"),
    (COMP, "dev.factory.pullrequest.opened", "dev.factory.made.up", "not in schema/types.md"),
    (COMP, "name: compliance", "name: other", "must match the file name"),
    (COMP, 'run_as: "10001"', "run_as: root", "run_as"),
    (COMP, "image: factory/agent", "imagez: factory/agent", "image"),
    (("deploy", "hosts.yaml"), "kind: local", "kind: vm", "exactly one host of kind local"),
    (("deploy", "hosts.yaml"), "kind: local", "kind: cloud", "kind"),
    (("deploy", "deployments.yaml"), "host: local", "host: nowhere", "no host 'nowhere'"),
    (("deploy", "deployments.yaml"), "component: compliance", "component: ghost", "no component 'ghost'"),
    (("deploy", "deployments.yaml"), "[file-to-markdown-convertor]", "[no-such-product]", "no product 'no-such-product'"),
    (("deploy", "deployments.yaml"), "[file-to-markdown-convertor]", "[]", "exactly one"),
    (("deploy", "deployments.yaml"), "replicas: 1", "replicas: 99", "replicas"),
    (("deploy", "deployments.yaml"), "replicas: 1", 'digest: "latest"', "digest"),
])
def test_bad_files_are_refused(ddir, file, old, new, expect):
    edit(ddir.joinpath(*file), old, new)
    assert expect in problems(ddir)


def test_duplicate_deployment_and_all_problems_reported_together(ddir):
    p = ddir / "deploy" / "deployments.yaml"
    p.write_text(p.read_text() + "  - component: compliance\n    host: local\n    products: [file-to-markdown-convertor]\n")
    edit(ddir.joinpath(*COMP), "MC_TOPIC: factory-events", "API_KEY: x")
    out = problems(ddir)
    assert "listed twice" in out and "looks like a secret" in out


def test_resolve_needs_every_placeholder_and_a_known_id():
    spec = deployspec.load(REPO / "deploy", products_dir=REPO / "products")
    with pytest.raises(SpecError, match="PROJECT"):
        deployspec.resolve(spec, "compliance@local")
    with pytest.raises(KeyError):
        deployspec.resolve(spec, "ghost@local", vars={"PROJECT": "p"})
