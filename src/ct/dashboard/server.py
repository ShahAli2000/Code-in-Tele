"""aiohttp web server for the dashboard.

Runs in the same asyncio loop as the bridge — same DB connection, same
in-memory session map, same RunnerPool. No IPC, no separate process.

Routes:
    GET  /                      sessions list
    GET  /sessions/{thread_id}  transcript view
    GET  /approvals             pending permission cards
    POST /approvals/{tool_use_id} (action=allow|allow_remember|deny)
    GET  /settings              read + simple forms
    POST /settings/quiet        update quiet_hours_start/_end
    POST /settings/allow        update default_auto_allow_tools
    GET  /stats                 mirrors /stats command output

Token auth via middleware (`auth.py`). All routes require ?token= or the
ct_token cookie set on first valid hit.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import structlog
from aiohttp import web

from ct.dashboard.auth import token_middleware
from ct.dashboard.templates import (
    render_approvals,
    render_session,
    render_sessions,
    render_settings,
    render_stats,
)

if TYPE_CHECKING:
    from ct.bridge.bot import BridgeBot

log = structlog.get_logger(__name__)


def build_app(*, bridge: "BridgeBot", token: str) -> web.Application:
    """Construct the dashboard's aiohttp app. The app stores `bridge` and
    `dashboard_token` in its dict so handlers can pull what they need."""
    app = web.Application(middlewares=[token_middleware])
    app["bridge"] = bridge
    app["dashboard_token"] = token

    app.router.add_get("/", handle_index)
    app.router.add_get("/sessions/{thread_id}", handle_session)
    app.router.add_get("/approvals", handle_approvals)
    app.router.add_post("/approvals/{tool_use_id}", handle_approval_action)
    app.router.add_get("/settings", handle_settings)
    app.router.add_post("/settings/quiet", handle_settings_quiet)
    app.router.add_post("/settings/allow", handle_settings_allow)
    app.router.add_get("/stats", handle_stats)
    return app


async def _read_form(request: web.Request) -> Any:
    """Return the form data the auth middleware may have already read."""
    if "form_data" in request:
        return request["form_data"]
    return await request.post()


async def handle_index(request: web.Request) -> web.Response:
    bridge: BridgeBot = request.app["bridge"]
    token = request.app["dashboard_token"]
    sessions: list[dict] = []
    for s in bridge.sessions.all():
        sessions.append({
            "thread_id": s.thread_id,
            "project_name": s.project_name,
            "cwd": s.cwd,
            "runner_name": s.runner_name,
            "permission_mode": s.runner.permission_mode,
            "model": s.model,
        })
    sessions.sort(key=lambda s: s["project_name"].lower())
    return web.Response(
        text=render_sessions(sessions, token=token),
        content_type="text/html",
    )


async def handle_session(request: web.Request) -> web.Response:
    bridge: BridgeBot = request.app["bridge"]
    token = request.app["dashboard_token"]
    try:
        thread_id = int(request.match_info["thread_id"])
    except ValueError:
        return web.Response(text="bad thread_id", status=400)
    session = bridge.sessions.get(thread_id)
    if session is None:
        return web.Response(text="no such session", status=404)

    sdk_id = session.runner.session_id
    entries: list[tuple[str, str]] = []
    if sdk_id:
        try:
            entries = await bridge.runners.get(session.runner_name).get_logs(
                sdk_session_id=sdk_id, cwd=session.cwd, limit=50,
            )
        except Exception:
            log.exception("dashboard.session_logs_failed", thread_id=thread_id)

    return web.Response(
        text=render_session(
            {
                "project_name": session.project_name,
                "runner_name": session.runner_name,
                "cwd": session.cwd,
                "sdk_session_id": sdk_id,
            },
            entries,
            token=token,
        ),
        content_type="text/html",
    )


async def handle_approvals(request: web.Request) -> web.Response:
    bridge: BridgeBot = request.app["bridge"]
    token = request.app["dashboard_token"]
    rows = await bridge.db.list_undecided_permissions()
    items = [
        {
            "tool_use_id": r.tool_use_id,
            "thread_id": r.thread_id,
            "tool_name": r.tool_name,
            "input_json": _pretty_json(r.input_json),
            "created_at": r.created_at,
        }
        for r in rows
    ]
    flash = request.query.get("flash")
    return web.Response(
        text=render_approvals(items, token=token, flash=flash),
        content_type="text/html",
    )


async def handle_approval_action(request: web.Request) -> web.Response:
    bridge: BridgeBot = request.app["bridge"]
    token = request.app["dashboard_token"]
    tool_use_id = request.match_info["tool_use_id"]
    form = await _read_form(request)
    action = form.get("action") or "deny"
    allow = action in ("allow", "allow_remember")
    remember = action == "allow_remember"

    # Find the runner holding this future. The bridge's PermissionsUI keeps
    # the in-memory mapping; we look it up by tool_use_id.
    entry = bridge.permissions_ui._pending.pop(tool_use_id, None)
    flash_text: str
    if entry is None:
        # Either resolved, expired, or post-restart. Mark in DB so the row
        # doesn't keep showing up in /approvals.
        await bridge.db.mark_permission_decided(
            tool_use_id, "expired" if not allow else "stale_allow",
        )
        flash_text = "card expired or already resolved"
    else:
        runner, _, _, _ = entry
        try:
            await runner.resolve_permission(
                tool_use_id, allow=allow,
                deny_message="denied via dashboard",
                remember=remember,
            )
            await bridge.db.delete_pending_permission(tool_use_id)
            flash_text = (
                "approved + remembered" if remember
                else "approved" if allow else "denied"
            )
        except Exception as exc:
            log.exception("dashboard.approve_failed", tool_use_id=tool_use_id)
            flash_text = f"failed: {exc!s}"

    raise web.HTTPSeeOther(
        location=f"/approvals?token={token}&flash={_url_encode(flash_text)}"
    )


async def handle_settings(request: web.Request) -> web.Response:
    bridge: BridgeBot = request.app["bridge"]
    token = request.app["dashboard_token"]
    defaults = await bridge.db.all_defaults()
    macs = await bridge.db.list_macs()
    macs_dicts = [
        {
            "name": m.name,
            "host": m.host,
            "port": m.port,
            "main_dir": m.main_dir,
            "shortcuts": list(m.shortcuts),
        }
        for m in macs
    ]
    flash = request.query.get("flash")
    return web.Response(
        text=render_settings(
            defaults=defaults, macs=macs_dicts, token=token, flash=flash,
        ),
        content_type="text/html",
    )


async def handle_settings_quiet(request: web.Request) -> web.Response:
    bridge: BridgeBot = request.app["bridge"]
    token = request.app["dashboard_token"]
    form = await _read_form(request)
    if form.get("clear"):
        await bridge.db.set_default("quiet_hours_start", None)
        await bridge.db.set_default("quiet_hours_end", None)
        bridge._defaults_cache["quiet_hours_start"] = None
        bridge._defaults_cache["quiet_hours_end"] = None
        flash = "quiet hours cleared"
    else:
        start = (form.get("start") or "").strip()
        end = (form.get("end") or "").strip()
        if not _is_hhmm(start) or not _is_hhmm(end):
            flash = "format must be HH:MM (24h)"
        else:
            await bridge.db.set_default("quiet_hours_start", start)
            await bridge.db.set_default("quiet_hours_end", end)
            bridge._defaults_cache["quiet_hours_start"] = start
            bridge._defaults_cache["quiet_hours_end"] = end
            flash = f"quiet hours: {start} → {end}"
    raise web.HTTPSeeOther(
        location=f"/settings?token={token}&flash={_url_encode(flash)}"
    )


async def handle_settings_allow(request: web.Request) -> web.Response:
    bridge: BridgeBot = request.app["bridge"]
    token = request.app["dashboard_token"]
    form = await _read_form(request)
    if form.get("clear"):
        await bridge.db.set_default("default_auto_allow_tools", None)
        bridge._defaults_cache["default_auto_allow_tools"] = None
        flash = "auto-allow cleared"
    else:
        csv = (form.get("csv") or "").strip()
        clean = ",".join(
            sorted({t.strip() for t in csv.split(",") if t.strip()})
        )
        await bridge.db.set_default("default_auto_allow_tools", clean or None)
        bridge._defaults_cache["default_auto_allow_tools"] = clean or None
        flash = f"auto-allow: {clean or '(none)'}"
    raise web.HTTPSeeOther(
        location=f"/settings?token={token}&flash={_url_encode(flash)}"
    )


async def handle_stats(request: web.Request) -> web.Response:
    bridge: BridgeBot = request.app["bridge"]
    token = request.app["dashboard_token"]
    stats = await bridge.db.stats_overview()
    return web.Response(
        text=render_stats(stats, token=token),
        content_type="text/html",
    )


# ---- helpers ----------------------------------------------------------------

def _pretty_json(s: str) -> str:
    """Re-indent a JSON string for inline display. Fall back to raw on parse
    failure — the source string already came from input_json column which we
    truncate at 8K, so worst case the user sees less-pretty text."""
    import json as _json
    try:
        return _json.dumps(_json.loads(s), indent=2)
    except (ValueError, TypeError):
        return s


def _is_hhmm(s: str) -> bool:
    if not s or ":" not in s:
        return False
    h, _, m = s.partition(":")
    if not (h.isdigit() and m.isdigit()):
        return False
    return 0 <= int(h) <= 23 and 0 <= int(m) <= 59


def _url_encode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


async def serve(*, bridge: "BridgeBot", host: str, port: int, token: str) -> web.AppRunner:
    """Start the dashboard server. Returns the runner so callers can shut it
    down cleanly. Bridge keeps a reference and cleans up in shutdown()."""
    app = build_app(bridge=bridge, token=token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("dashboard.serving", host=host, port=port)
    return runner
