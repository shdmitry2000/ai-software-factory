"""N1, engine side: the console's base branch reaches the engine's next lap."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mission_control import events, projections, registry
from mission_control.adapters.engine.archon import ArchonEngine
from mission_control.console import views
from mission_control.handler import BASE_FILE, CommandHandler, issue

REPO = Path(__file__).resolve().parents[2]
PRODUCT = "file-to-markdown-convertor"


class Bus:
    def publish(self, topic, e, *, ordering_key=""):
        return e["id"]


@pytest.fixture
def env(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    subprocess.run(["git", "-C", str(app), "remote", "add", "origin",
                    "https://github.com/shdmitry2000/file-to-markdown-convertor.git"], check=True)
    store = registry.get("store", "memory")()
    h = CommandHandler(runtime=registry.get("runtime", "fake")(), bus=Bus(), engine="factory-1", store=store,
                       products_dir=REPO / "products", app_dir=app)
    return h, store, app


def run(h, store, action, target, **args):
    cmd = issue(Bus(), "factory-commands", action, target, who="t", reason="n1 test", args=args)
    out = h.handle(cmd)
    for e in out:
        assert events.validate(e) == []
        projections.project(e, store)
    return store.get("commands", cmd["data"]["command_id"])


def test_the_engine_checkout_is_matched_to_its_product(env):
    h, store, app = env
    assert h._app_product() == PRODUCT
    subprocess.run(["git", "-C", str(app), "remote", "set-url", "origin", "git@github.com:someone/else.git"], check=True)
    assert h._app_product() == ""


def test_start_writes_the_effective_base(env):
    h, store, app = env
    assert h.sync_base() == "main" and (app / BASE_FILE).read_text() == "main\n"
    store.upsert("products", PRODUCT, {"product": PRODUCT, "effective": {"base_branch": "develop"}})
    assert h.sync_base() == "develop" and (app / BASE_FILE).read_text() == "develop\n"
    assert h.write_base("develop") is False                # unchanged: not rewritten


def test_a_console_change_reaches_the_engine_at_once(env):
    h, store, app = env
    h.sync_products()
    h.sync_base()
    c = run(h, store, "product.update", PRODUCT, field="base_branch", old="main", new="release/2")
    assert c["status"] == "succeeded" and "next lap" in c["detail"]
    assert (app / BASE_FILE).read_text() == "release/2\n"
    with pytest.raises(ValueError):
        h.write_base("../etc")


def test_bridge_reports_the_base_and_the_console_flags_a_mismatch(env, tmp_path):
    h, store, app = env
    h.write_base("develop")
    eng = ArchonEngine(db=str(tmp_path / "none.db"), engine="factory-1", product=PRODUCT, app_dir=str(app))
    hb = eng.heartbeat()
    assert hb["data"]["base_branch"] == "develop" and events.validate(hb) == []
    projections.project(hb, store)
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store.upsert("products", PRODUCT, {"product": PRODUCT, "effective": {"base_branch": "main"}, "seed_time": old})
    at = datetime.fromisoformat(hb["time"].replace("Z", "+00:00")) + timedelta(seconds=30)
    probs = {p.kind: p for p in views.problems(store, at)}
    assert probs["engine.base_mismatch"].product == PRODUCT
    p = views.products(store, at, REPO / "products")[0]
    assert p["engine_bases"] == [{"engine": "factory-1", "base": "develop", "ok": False}]
    h.write_base("main")
    projections.project({**eng.heartbeat(), "id": "bridge:factory-1:later"}, store)
    assert "engine.base_mismatch" not in {p.kind for p in views.problems(store, at)}
