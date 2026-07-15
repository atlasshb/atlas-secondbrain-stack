"""Cerebras Phase 0 — Pulse cockpit.

Read-only tile grid over your existing host feeds + exactly one approval-gated
self-heal, routed through mind/gate.py. Auth is Caddy+Authentik forward_auth;
the app additionally refuses action POSTs without an authenticated identity.
"""
from __future__ import annotations

import os
import sqlite3
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .action_log import ActionLog
from .engine import Engine
from .executor import ACTIONS, ActionError, DevRunner, Executor, ProdRunner
from .gate_client import make_gate
from .notify import make_notifier
from .probes import _human_age, _parse_ts

VERSION = "0.1.0-phase0"
BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CEREBRAS_DATA_DIR", "/opt/app/cerebras/data"))
CONFIG = os.environ.get("CEREBRAS_CONNECTORS", "/opt/app/cerebras/connectors.yaml")
DEV = os.environ.get("CEREBRAS_DEV") == "1"

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="cerebras", version=VERSION, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")  # autoescape on (injection-safety)

engine = Engine(CONFIG, DATA_DIR)
gate = make_gate(DATA_DIR)
action_log = ActionLog(DATA_DIR / "actions.log.jsonl")
notify = make_notifier()
runner = DevRunner() if DEV else ProdRunner(
    {"host": os.environ.get("STALWART_TLS_HOST", "127.0.0.1"),
     "port": int(os.environ.get("STALWART_TLS_PORT", "8443"))})
executor = Executor(gate, action_log, notify, runner, odoo_cfg={
    k: v for k, v in {
        "url": os.environ.get("ODOO_URL"),
        "db": os.environ.get("ODOO_DB", "odoo"),
        "uid": int(os.environ.get("ODOO_UID", "0") or 0),
        "password": os.environ.get("ODOO_PASSWORD"),
        "project_id": os.environ.get("ODOO_PROJECT_ID", "1"),
    }.items() if v})


@app.on_event("startup")
def _startup():
    engine.refresh()
    engine.start()


def _user(request: Request) -> str | None:
    if DEV:
        return "dev"
    return request.headers.get("x-authentik-username")


def _same_origin(request: Request) -> bool:
    """CSRF defense for state-changing requests that doesn't depend on the
    Authentik cookie's SameSite config: the browser must prove same-origin."""
    sfs = request.headers.get("sec-fetch-site")
    if sfs is not None:
        return sfs in ("same-origin", "none")
    for hdr in ("origin", "referer"):
        val = request.headers.get(hdr)
        if val:
            return urllib.parse.urlsplit(val).hostname == request.url.hostname
    return False  # no browser provenance at all on a POST -> reject


@app.middleware("http")
async def require_identity(request: Request, call_next):
    # /healthz stays reachable without SSO (independent liveness, NFR-07);
    # everything else must arrive through the Authentik forward_auth proxy.
    if request.url.path != "/healthz":
        if _user(request) is None:
            return JSONResponse({"error": "no authenticated identity"}, status_code=403)
        if request.method not in ("GET", "HEAD") and not DEV and not _same_origin(request):
            return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
    return await call_next(request)


# ------------------------------------------------------------------ views

def _board_ctx(request: Request) -> dict:
    tiles = []
    for t in engine.board():
        d = t.as_dict()
        d["last_check_h"] = _rel(t.last_check)
        d["last_good_h"] = _rel(t.last_good)
        d["history"] = engine.history(t.id)
        tiles.append(d)
    pending = []
    for p in gate.pending():
        payload = p.get("payload") or {}
        aid_action = payload.get("action_id", "stalwart_restart")
        action = ACTIONS.get(aid_action)
        if not action:
            continue
        pending.append({
            "id": p["id"], "action_id": action["id"], "title": action["title"],
            "command_label": action["command_label"],
            "age": _rel(p["ts"]) or "?",
        })
    return {
        "request": request,
        "tiles": tiles,
        "pending": pending,
        "proposed_tile_ids": {ACTIONS[p["action_id"]]["tile_id"] for p in pending},
    }


def _rel(ts: str | None) -> str:
    if not ts:
        return "never"
    # ledger/kernel timestamps are UTC; sqlite's datetime('now') emits them
    # tz-naive — pin naive strings to UTC before parsing
    if isinstance(ts, str) and not (ts.endswith("Z") or "+" in ts[10:]):
        ts = ts.rstrip() + "+00:00"
    parsed = _parse_ts(ts)
    if parsed is None:
        return ts
    return _human_age(max(0, time.time() - parsed)) + " ago"


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    ctx = _board_ctx(request)
    ctx["now"] = datetime.now().strftime("%H:%M")
    ctx["version"] = VERSION
    return templates.TemplateResponse("index.html", ctx)


@app.get("/partials/board", response_class=HTMLResponse)
def partial_board(request: Request):
    return templates.TemplateResponse("board.html", _board_ctx(request))


@app.get("/partials/clock", response_class=HTMLResponse)
def partial_clock():
    return HTMLResponse(datetime.now().strftime("%H:%M"))


@app.post("/refresh", response_class=HTMLResponse)
def manual_refresh(request: Request):
    engine.refresh()
    return templates.TemplateResponse("board.html", _board_ctx(request))


@app.get("/log", response_class=HTMLResponse)
def log_page(request: Request):
    intact, detail = action_log.verify()
    return templates.TemplateResponse("log.html", {
        "request": request, "records": list(reversed(action_log.tail(200))),
        "intact": intact, "chain_detail": detail, "version": VERSION,
    })


# ------------------------------------------------------------------ actions

def _action_response(request: Request, error: str | None = None):
    ctx = _board_ctx(request)
    if error:
        ctx["flash"] = error
    return templates.TemplateResponse("board.html", ctx)


# NOTE: all three action routes are sync `def` on purpose — FastAPI runs them
# in its threadpool, so the docker restart + TLS re-probe (up to ~2min) never
# blocks the event loop (board polls and /healthz stay responsive).

@app.post("/actions/{action_id}/propose", response_class=HTMLResponse)
def propose(action_id: str, request: Request):
    try:
        executor.propose(action_id, _user(request) or "unknown")
    except (ActionError, sqlite3.Error) as e:
        return _action_response(request, str(e))
    return _action_response(request)


@app.post("/actions/{action_id}/approve", response_class=HTMLResponse)
def approve(action_id: str, request: Request, proposal_id: int = Form(...)):
    try:
        executor.approve_and_run(action_id, proposal_id, _user(request) or "unknown")
        engine.refresh(ACTIONS[action_id]["tile_id"])  # auto tile-flip
    except (ActionError, sqlite3.Error, KeyError) as e:
        return _action_response(request, str(e))
    return _action_response(request)


@app.post("/actions/{action_id}/reject", response_class=HTMLResponse)
def reject(action_id: str, request: Request, proposal_id: int = Form(...)):
    try:
        executor.reject(action_id, proposal_id, _user(request) or "unknown")
    except (ActionError, sqlite3.Error, KeyError) as e:
        return _action_response(request, str(e))
    return _action_response(request)


# ------------------------------------------------------------------ api

@app.get("/api/tiles")
def api_tiles():
    return [t.as_dict() for t in engine.board()]


@app.get("/healthz")
def healthz():
    age = engine.loop_age_s()
    intact, _ = action_log.verify()
    ok = age is not None and age < 300 and intact
    return JSONResponse(
        {"ok": ok, "version": VERSION, "probe_loop_age_s": age,
         "action_log_intact": intact},
        status_code=200 if ok else 503)
