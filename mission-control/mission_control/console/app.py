"""Console web app (UI-0): shell, navigation, health, operator identity, live feed.

Everything shown comes from the StateStore (Firestore in production), read with the
main node's identity. No event payloads are rendered, only type, subject and source,
so nothing sensitive (and never holdout text) reaches the browser.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mission_control.console.peercred import GROUP, Operator, operator_for
from mission_control.ports import StateStore

HERE = Path(__file__).resolve().parent

# Screens of docs/console-ui-plan.md §4, in nav order: (path, label, stage that builds it)
SCREENS = [
    ("", "Overview", "UI-0"), ("projects", "Projects", "UI-1"), ("agents", "Agents", "UI-1"),
    ("hosts", "Hosts", "UI-3"), ("deploy", "Deploy", "UI-3"), ("chains", "Chains", "UI-3"),
    ("runs", "Runs", "UI-1"), ("workflows", "Workflows", "UI-5"), ("compliance", "Compliance", "UI-1"),
    ("problems", "Problems", "UI-1"), ("memories", "Memories", "UI-6"), ("prompts", "Prompts", "UI-6"),
    ("audit", "Audit", "UI-2"),
]
OPEN_PATHS = ("/api/health", "/static/")
ENGINE_STALE_S = 180


def _age_s(t: str, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(t.replace("Z", "+00:00"))).total_seconds()
    except (AttributeError, ValueError):
        return None


def _ago(t: str, now: datetime) -> str:
    s = _age_s(t, now)
    if s is None:
        return "never"
    s = max(0, int(s))
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago" if s < 7200 else f"{s // 3600}h ago"


def create_app(store: StateStore, *, resolve: Callable[[tuple | None], Operator | None] | None = None,
               dev_operator: str = "", archon_url: str = "http://localhost:3090/",
               engine: str = "factory-1", poll_s: float = 1.5, demo: bool = False) -> FastAPI:
    """`resolve` maps the socket peer (pid, uid, gid) to an Operator; `dev_operator`
    lets a local TCP run (no peer credentials) in under that name, for development."""
    app = FastAPI(title="Factory console", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    resolve = resolve or (lambda creds: operator_for(creds[1]) if creds else None)

    @app.middleware("http")
    async def who(request: Request, call_next):
        op = resolve(request.scope.get("peercred"))
        if op is None and dev_operator:
            op = Operator(dev_operator, None, True, "dev")
        request.state.operator = op
        if request.url.path.startswith(OPEN_PATHS):
            return await call_next(request)
        if op is None or not op.allowed:
            name = op.name if op else "unknown (no peer credentials)"
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "forbidden", "operator": name, "need_group": GROUP}, status_code=403)
            return templates.TemplateResponse(request, "denied.html", {"name": name, "group": GROUP}, status_code=403)
        return await call_next(request)

    def ctx(request: Request, active: str, **kw) -> dict:
        return {"screens": SCREENS, "active": active, "op": request.state.operator,
                "archon_url": archon_url, "engine": engine, "demo": demo, **kw}

    # ------------------------------------------------------------------ data
    def overview() -> dict:
        now = datetime.now(timezone.utc)
        engines = store.query("engines")
        for e in engines:
            age = _age_s(e.get("last_heartbeat", ""), now)
            e["alive"] = age is not None and age < ENGINE_STALE_S
            e["ago"] = _ago(e.get("last_heartbeat", ""), now)
        agents = store.query("agents")
        for a in agents:
            age = _age_s(a.get("last_heartbeat", ""), now)
            a["fresh"] = age is not None and age < ENGINE_STALE_S
            a["ago"] = _ago(a.get("last_heartbeat", ""), now)
        deployments = store.query("deployments")
        commands = store.query("commands", order_by="issued_at", descending=True, limit=5)
        for c in commands:
            c["ago"] = _ago(c.get("issued_at", ""), now)
        return {"engines": engines, "agents": agents, "deployments": deployments, "commands": commands,
                "needs": _needs(engines, agents, deployments)}

    def _needs(engines, agents, deployments) -> list[str]:
        out = [f"engine {e.get('engine')} silent ({e['ago']})" for e in engines if not e["alive"]]
        out += [f"agent {a.get('agent_id')} silent ({a['ago']})" for a in agents
                if not a["fresh"] and a.get("state") != "stopped"]
        out += [f"deployment {d.get('deployment')} is {d.get('state')}" for d in deployments
                if d.get("state") not in ("running", "removed")]
        out += [f"deployment {d.get('deployment')}: last {d.get('last_action')} failed" for d in deployments
                if d.get("last_ok") is False]
        return out

    def recent(limit: int = 15) -> list[dict]:
        now = datetime.now(timezone.utc)
        rows = store.query("events", order_by="received_at", descending=True, limit=limit)
        return [{"type": str(r.get("type", "")).removeprefix("dev.factory."), "subject": r.get("subject", ""),
                 "source": r.get("factoryengine", ""), "ago": _ago(r.get("received_at") or r.get("time", ""), now),
                 "key": r.get("received_at", "")} for r in rows]

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        data, events = await asyncio.gather(asyncio.to_thread(overview), asyncio.to_thread(recent))
        return templates.TemplateResponse(request, "overview.html", ctx(request, "", events=events, **data))

    for path, label, stage in SCREENS[1:]:
        def make(path=path, label=label, stage=stage):
            async def page(request: Request):
                return templates.TemplateResponse(request, "placeholder.html",
                                                  ctx(request, path, label=label, stage=stage))
            return page
        app.add_api_route(f"/{path}", make(), methods=["GET"], response_class=HTMLResponse)

    @app.get("/fragments/recent", response_class=HTMLResponse)
    async def recent_fragment(request: Request):
        events = await asyncio.to_thread(recent)
        return templates.TemplateResponse(request, "_recent.html", {"events": events})

    @app.get("/fragments/tiles", response_class=HTMLResponse)
    async def tiles_fragment(request: Request):
        data = await asyncio.to_thread(overview)
        return templates.TemplateResponse(request, "_tiles.html", data)

    # ------------------------------------------------------------------ api
    @app.get("/api/health")
    async def health(request: Request):
        try:
            await asyncio.to_thread(store.query, "engines", limit=1)
            ok, detail = True, "store reachable"
        except Exception as e:                      # report, don't raise
            ok, detail = False, f"store: {type(e).__name__}"
        return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 503)

    @app.get("/api/whoami")
    async def whoami(request: Request):
        op: Operator = request.state.operator
        return {"name": op.name, "uid": op.uid, "how": op.how, "groups": list(op.groups)}

    @app.get("/api/overview")
    async def api_overview():
        return await asyncio.to_thread(overview)

    # ------------------------------------------------------------------ live (SSE)
    @app.get("/stream")
    async def stream(request: Request):
        """Pushes fresh `recent` and `tiles` fragments when a new event is stored.
        Polls the newest event only while a browser is connected."""
        recent_t = templates.get_template("_recent.html")
        tiles_t = templates.get_template("_tiles.html")

        def frame(name: str, html: str) -> str:
            return f"event: {name}\n" + "".join(f"data: {line}\n" for line in html.splitlines()) + "\n"

        async def gen():
            last, idle = None, 0.0
            while not await request.is_disconnected():
                try:
                    top = await asyncio.to_thread(store.query, "events", order_by="received_at",
                                                  descending=True, limit=1)
                    key = top[0].get("received_at") if top else ""
                except Exception:
                    key = last
                if key != last:
                    last, idle = key, 0.0
                    events, data = await asyncio.gather(asyncio.to_thread(recent), asyncio.to_thread(overview))
                    yield frame("recent", recent_t.render(events=events))
                    yield frame("tiles", tiles_t.render(**data))
                else:
                    idle += poll_s
                    if idle >= 15:
                        idle = 0.0
                        yield ": keepalive\n\n"
                await asyncio.sleep(poll_s)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app
