"""Console web app: shell, identity, read-only status screens (UI-0, UI-1).

Everything shown comes from the StateStore (Firestore in production), read with the
main node's identity, plus the products files of the console's checkout. No event
payloads are rendered, only type, subject and source, so nothing sensitive (and never
holdout text) reaches the browser.

Live updates: `/stream` (SSE) sends `changed` when a new event is stored; each screen's
main section re-fetches its own fragment on that signal (throttled in the page).

Actions (UI-2): every button sends a `command.issued` on factory-commands, as the
operator the socket identified, with the reason they typed; the command handler on the
main node executes it. The console never writes to the store. Requests that change
anything must come from htmx (HX-Request header) on a localhost origin, and every
request must name a localhost Host, so another web page cannot act through the tunnel.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mission_control import deployspec
from mission_control import handler as handler_mod
from mission_control.console import views
from mission_control.console.peercred import GROUP, Operator, operator_for
from mission_control.deployspec import ROOT
from mission_control.ports import EventBus, StateStore

HERE = Path(__file__).resolve().parent

# Screens of docs/console-ui-plan.md §4, in nav order: (path, label, stage that builds it)
SCREENS = [
    ("", "Overview", "UI-0"), ("projects", "Projects", "UI-1"), ("agents", "Agents", "UI-1"),
    ("hosts", "Hosts", "UI-3"), ("deploy", "Deploy", "UI-3"), ("chains", "Chains", "UI-3"),
    ("runs", "Runs", "UI-1"), ("workflows", "Workflows", "UI-5"), ("compliance", "Compliance", "UI-1"),
    ("problems", "Problems", "UI-1"), ("memories", "Memories", "UI-6"), ("prompts", "Prompts", "UI-6"),
    ("audit", "Audit", "UI-2"),
]
BUILT = {"UI-0", "UI-1", "UI-2", "UI-3"}
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "console")
OPEN_PATHS = ("/api/health", "/static/")


def create_app(store: StateStore, *, resolve: Callable[[tuple | None], Operator | None] | None = None,
               dev_operator: str = "", archon_url: str = "http://localhost:3090/",
               engine: str = "factory-1", poll_s: float = 1.5, demo: bool = False,
               products_dir: str | Path = ROOT / "products", bus: EventBus | None = None,
               commands_topic: str = "factory-commands", prompts=None,
               allowed_hosts: tuple[str, ...] = LOCAL_HOSTS, deploy_dir: str | Path = ROOT / "deploy",
               logs_socket: str = handler_mod.LOGS_SOCKET, logs_source=None,
               counts_ttl_s: float = 60.0) -> FastAPI:
    """`resolve` maps the socket peer (pid, uid, gid) to an Operator; `dev_operator`
    lets a local TCP run (no peer credentials) in under that name, for development."""
    app = FastAPI(title="Factory console", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    resolve = resolve or (lambda creds: operator_for(creds[1]) if creds else None)
    pdir = Path(products_dir)
    ddir = Path(deploy_dir)
    logs_source = logs_source or (lambda dep, tail, follow: socket_logs(logs_socket, dep, tail, follow))
    counts_cache: dict[tuple, tuple[float, int]] = {}

    def load_spec() -> tuple[deployspec.Spec | None, str]:
        try:
            return deployspec.load(ddir, products_dir=pdir), ""
        except deployspec.SpecError as e:
            return None, "; ".join(e.problems)[:600]
        except Exception as e:                      # a broken checkout must not take the page down
            return None, f"{type(e).__name__}: {e}"[:600]

    def cached_count(where: list) -> int:
        key = tuple(map(tuple, where))
        hit = counts_cache.get(key)
        if hit and time.monotonic() - hit[0] < counts_ttl_s:
            return hit[1]
        n = store.count("events", where=where)
        counts_cache[key] = (time.monotonic(), n)
        return n

    def build_source() -> str:
        p = subprocess.run(["git", "-C", str(ddir.parent), "log", "-1", "--format=%h %s"],
                           capture_output=True, text=True)
        return p.stdout.strip()[:80] if p.returncode == 0 else ""

    @app.middleware("http")
    async def who(request: Request, call_next):
        host = (urlparse(f"//{request.headers.get('host', '')}").hostname or "").strip("[]")
        if host not in allowed_hosts:              # DNS rebinding: only our own names
            return JSONResponse({"error": "unexpected Host header"}, status_code=421)
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

    # ------------------------------------------------------------------ data per screen
    def recent(limit: int = 15) -> list[dict]:
        at = views.now()
        rows = store.query("events", order_by="received_at", descending=True, limit=limit)
        return [{"type": str(r.get("type", "")).removeprefix("dev.factory."), "subject": r.get("subject", ""),
                 "source": r.get("factoryengine", ""), "ago": views.ago(r.get("received_at") or r.get("time"), at)}
                for r in rows]

    def data_overview(q: dict) -> dict:
        at = views.now()
        probs = views.problems(store, at)
        return {"engines": views.engines(store, at), "agents": views.agents(store, at),
                "deployments": views.deployments(store, at), "commands": views.commands(store, at),
                "needs": [p for p in probs if p.severity != "info" and not p.acked_by], "events": recent()}

    def data_agents(q: dict) -> dict:
        at = views.now()
        return {"engines": views.engines(store, at), "agents": views.agents(store, at),
                "deployments": views.deployments(store, at)}

    def data_projects(q: dict) -> dict:
        at = views.now()
        return {"products": views.products(store, at, pdir, prompts, problem_list=views.problems(store, at))}

    def data_compliance(q: dict) -> dict:
        at = views.now()
        rows = views.reports(store, at, product=q.get("product", ""), status=q.get("status", ""))
        everything = views.reports(store, at)
        return {"reports": rows, "q": q,
                "options": {"product": sorted({r.get("product") for r in everything if r.get("product")}),
                            "status": sorted({r.get("status") for r in everything if r.get("status")})}}

    def data_runs(q: dict) -> dict:
        at = views.now()
        return {"runs": views.runs(store, at, status=q.get("status", ""), product=q.get("product", ""),
                                   workflow=q.get("workflow", "")),
                "q": q, "options": views.run_filters(store)}

    def data_problems(q: dict) -> dict:
        at = views.now()
        return {"problems": views.problems(store, at), "rules": views.rule_log(store, at)}

    def data_audit(q: dict) -> dict:
        at = views.now()
        return {"commands": views.audit(store, at, who=q.get("who", ""), action=q.get("action", ""),
                                        status=q.get("status", "")),
                "q": q, "options": views.audit_filters(store)}

    def data_hosts(q: dict) -> dict:
        spec, err = load_spec()
        from mission_control.adapters.runtime.gce import MACHINE_TYPES
        hs = views.hosts(store, views.now(), spec)
        return {"hosts": hs, "spec_error": err, "machine_types": MACHINE_TYPES,
                "n_vms": sum(1 for h in hs if h["kind"] == "vm" and h["state"] != "removed")}

    def data_deploy(q: dict) -> dict:
        spec, err = load_spec()
        return {**views.deploy(store, views.now(), spec), "spec_error": err, "source": build_source()}

    def data_chains(q: dict) -> dict:
        spec, err = load_spec()
        return {**views.chains(store, views.now(), spec, count=cached_count), "spec_error": err}

    SCREEN_DATA = {"": data_overview, "agents": data_agents, "projects": data_projects,
                   "compliance": data_compliance, "runs": data_runs, "problems": data_problems,
                   "audit": data_audit, "hosts": data_hosts, "deploy": data_deploy, "chains": data_chains}
    FILTERS = {"compliance": ("product", "status"), "runs": ("status", "product", "workflow"),
               "audit": ("who", "action", "status")}

    def ctx(request: Request, active: str, **kw) -> dict:
        return {"screens": SCREENS, "built": BUILT, "active": active, "op": request.state.operator,
                "archon_url": archon_url, "engine": engine, "demo": demo, "can_act": bus is not None, **kw}

    def query_of(request: Request, screen: str) -> dict:
        return {k: request.query_params.get(k, "") for k in FILTERS.get(screen, ())}

    def screen_page(screen: str, label: str):
        async def page(request: Request):
            q = query_of(request, screen)
            data = await asyncio.to_thread(SCREEN_DATA[screen], q)
            qs = urlencode({k: v for k, v in q.items() if v})
            frag = f"/fragments/{screen or 'overview'}" + (f"?{qs}" if qs else "")
            return templates.TemplateResponse(request, "screen.html",
                                              ctx(request, screen, label=label, frag=frag,
                                                  body=f"_{screen or 'overview'}.html", **data))
        return page

    def screen_fragment(screen: str):
        async def fragment(request: Request):
            data = await asyncio.to_thread(SCREEN_DATA[screen], query_of(request, screen))
            return templates.TemplateResponse(request, f"_{screen or 'overview'}.html", ctx(request, screen, **data))
        return fragment

    for path, label, stage in SCREENS:
        if path in SCREEN_DATA:
            app.add_api_route(f"/{path}", screen_page(path, label), methods=["GET"], response_class=HTMLResponse)
            app.add_api_route(f"/fragments/{path or 'overview'}", screen_fragment(path), methods=["GET"],
                              response_class=HTMLResponse)
        else:
            def make(path=path, label=label, stage=stage):
                async def page(request: Request):
                    return templates.TemplateResponse(request, "placeholder.html",
                                                      ctx(request, path, label=label, stage=stage))
                return page
            app.add_api_route(f"/{path}", make(), methods=["GET"], response_class=HTMLResponse)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: str):
        r = await asyncio.to_thread(views.run_detail, store, run_id, views.now())
        if r is None:
            return templates.TemplateResponse(request, "placeholder.html",
                                              ctx(request, "runs", label="Run not found", stage="-"), status_code=404)
        return templates.TemplateResponse(request, "run.html", ctx(request, "runs", run=r))

    # ------------------------------------------------------------------ actions
    def refuse_unsafe(request: Request) -> str:
        if request.headers.get("hx-request") != "true":
            return "actions must come from the console page"
        origin = request.headers.get("origin")
        if origin and (urlparse(origin).hostname or "").strip("[]") not in allowed_hosts:
            return "cross-origin request refused"
        if bus is None:
            return "this console is read-only (no command bus configured)"
        return ""

    def send(request: Request, action: str, target: str, reason: str, args: dict) -> dict:
        ev = handler_mod.issue(bus, commands_topic, action, target, who=request.state.operator.name,
                               reason=reason, args=args, engine=engine, via="console")
        return {"command_id": ev["data"]["command_id"], "action": action, "target": target}

    def sent_response(request: Request, sent: list[dict], error: str = "", status_code: int = 200):
        return templates.TemplateResponse(request, "_sent.html", {"sent": sent, "error": error},
                                          status_code=status_code)

    @app.post("/act", response_class=HTMLResponse)
    async def act(request: Request):
        why = refuse_unsafe(request)
        if why:
            return sent_response(request, [], why, 403)
        form = await request.form()
        action, target = str(form.get("action", "")), str(form.get("target", ""))
        reason = str(form.get("reason", "")).strip()
        if len(reason) < 3:
            return sent_response(request, [], "say why (at least 3 characters); it goes in the audit log", 400)
        args = {k[4:]: str(v) for k, v in form.items() if k.startswith("arg_") and str(v) != ""}
        if "replicas" in args:
            try:
                args["replicas"] = int(args["replicas"])
            except ValueError:
                return sent_response(request, [], "replicas must be a whole number", 400)
        try:
            sent = [await asyncio.to_thread(send, request, action, target, reason, args)]
        except ValueError as e:
            return sent_response(request, [], str(e)[:300], 400)
        return sent_response(request, sent)

    @app.post("/settings/{product}", response_class=HTMLResponse)
    async def settings(request: Request, product: str):
        """One product.update per changed field, each carrying the value the operator saw."""
        why = refuse_unsafe(request)
        if why:
            return sent_response(request, [], why, 403)
        form = await request.form()
        reason = str(form.get("reason", "")).strip()
        if len(reason) < 3:
            return sent_response(request, [], "say why (at least 3 characters); it goes in the audit log", 400)
        from mission_control.products import EDITABLE
        changes = []
        for f in EDITABLE:
            new, old = form.get(f"new:{f}"), form.get(f"old:{f}")
            if new is None or old is None:
                continue
            new, old = str(new).strip(), str(old)
            same = new == old
            if f == "compliance.daily_cap_usd":
                try:
                    same = float(new) == float(old)
                except ValueError:
                    same = False
            if not same:
                changes.append((f, old, new))
        if not changes:
            return sent_response(request, [], "nothing changed")
        sent = []
        for f, old, new in changes:
            sent.append(await asyncio.to_thread(send, request, "product.update", product, reason,
                                                {"field": f, "old": old, "new": new}))
        return sent_response(request, sent)

    @app.get("/panels/deploy", response_class=HTMLResponse)
    async def deploy_form(request: Request, component: str = "", host: str = ""):
        """The deploy dialog, shown in the panel above the screen (live refresh leaves it alone)."""
        def data():
            spec, err = load_spec()
            hs = [h for h in views.hosts(store, views.now(), spec) if h["state"] in ("healthy", "unknown") or h["host"] == "local"]
            return {"spec": spec, "spec_error": err, "hosts": hs, "component": component, "host": host,
                    "products": sorted(p.stem for p in pdir.glob("*.yaml")),
                    "deploy": views.deploy(store, views.now(), spec), "source": build_source()}
        d = await asyncio.to_thread(data)
        return templates.TemplateResponse(request, "_deploy_form.html", ctx(request, "deploy", **d))

    @app.post("/deploy", response_class=HTMLResponse)
    async def deploy_post(request: Request):
        """deployment.apply for component@host with the dialog's choices."""
        why = refuse_unsafe(request)
        if why:
            return sent_response(request, [], why, 403)
        form = await request.form()
        comp, host = str(form.get("component", "")), str(form.get("host", ""))
        reason = str(form.get("reason", "")).strip()
        if len(reason) < 3:
            return sent_response(request, [], "say why (at least 3 characters); it goes in the audit log", 400)
        target = f"{comp}@{host}"
        if not handler_mod.DEPLOYMENT_ID.match(target):
            return sent_response(request, [], f"choose a component and a host (got {target!r})", 400)
        args: dict = {}
        r = str(form.get("replicas", "")).strip()
        if r:
            if not r.isdigit():
                return sent_response(request, [], "replicas must be a whole number", 400)
            args["replicas"] = int(r)
        image = str(form.get("image", "build"))
        if image == "build":
            args["build"] = "1"
        elif image != "pinned":
            args["digest"] = image
        if form.get("product"):
            args["products"] = str(form.get("product"))
        try:
            sent = [await asyncio.to_thread(send, request, "deployment.apply", target, reason, args)]
        except ValueError as e:
            return sent_response(request, [], str(e)[:300], 400)
        return sent_response(request, sent)

    @app.get("/panels/logs/{dep}", response_class=HTMLResponse)
    async def logs_panel(request: Request, dep: str, tail: int = 200):
        if not handler_mod.DEPLOYMENT_ID.match(dep):
            return HTMLResponse("not a deployment id", status_code=400)
        return templates.TemplateResponse(request, "_logs.html", {"dep": dep, "tail": max(1, min(tail, 2000))})

    @app.get("/panels/logs/{dep}/stream")
    async def logs_stream(request: Request, dep: str, tail: int = 200, follow: int = 1):
        """SSE: `line` events (escaped HTML rows), then `end` when the log closes."""
        if not handler_mod.DEPLOYMENT_ID.match(dep):
            return JSONResponse({"error": "not a deployment id"}, status_code=400)

        async def gen():
            q: asyncio.Queue = asyncio.Queue(maxsize=2000)

            async def pump():
                try:
                    async for line in logs_source(dep, max(1, min(tail, 2000)), bool(follow)):
                        await q.put(line)
                finally:
                    try:
                        q.put_nowait(None)
                    except asyncio.QueueFull:
                        pass

            task = asyncio.create_task(pump())
            try:
                while True:
                    try:
                        line = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": keepalive\n\n"
                        continue
                    if line is None:
                        break
                    yield f"event: line\ndata: <div>{html.escape(line)}</div>\n\n"
                yield "event: end\ndata: <div class=\"muted\">— end of log —</div>\n\n"
            finally:
                task.cancel()

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/fragments/command/{cid}", response_class=HTMLResponse)
    async def command_fragment(request: Request, cid: str):
        c = await asyncio.to_thread(store.get, "commands", cid)
        steps = [v for _, v in sorted(((c or {}).get("steps") or {}).items())]
        return templates.TemplateResponse(request, "_command.html", {"c": c, "cid": cid, "steps": steps})

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
        d = await asyncio.to_thread(data_overview, {})
        d["needs"] = [p.__dict__ for p in d["needs"]]
        return d

    @app.get("/api/problems")
    async def api_problems():
        return [p.__dict__ for p in await asyncio.to_thread(views.problems, store, views.now())]

    # ------------------------------------------------------------------ live (SSE)
    @app.get("/stream")
    async def stream(request: Request):
        """Sends `changed` when a new event is stored. Polls the newest event only while
        a page is open; a comment every 15 s keeps proxies from closing the stream."""
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
                    first = last is None
                    last, idle = key, 0.0
                    if not first:
                        yield f"event: changed\ndata: {key}\n\n"
                    else:
                        yield ": connected\n\n"
                else:
                    idle += poll_s
                    if idle >= 15:
                        idle = 0.0
                        yield ": keepalive\n\n"
                await asyncio.sleep(poll_s)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app


async def socket_logs(path: str, dep: str, tail: int, follow: bool) -> AsyncIterator[str]:
    """Log lines from the command handler's logs socket (root only, on the main node)."""
    try:
        reader, writer = await asyncio.open_unix_connection(path, limit=1 << 16)
    except OSError as e:
        yield f"[console] cannot reach the command handler's logs ({type(e).__name__}); is mission-control-handler running?"
        return
    try:
        writer.write((json.dumps({"deployment": dep, "tail": tail, "follow": follow}) + "\n").encode())
        await writer.drain()
        head = (await reader.readline()).decode("utf-8", "replace").strip()
        if head != "ok":
            yield f"[handler] {head or 'no answer'}"
            return
        while True:
            raw = await reader.readline()
            if not raw:
                return
            yield ANSI.sub("", raw.decode("utf-8", "replace").rstrip("\n"))
    finally:
        writer.close()


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
