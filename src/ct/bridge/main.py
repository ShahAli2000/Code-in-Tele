"""Entry point for the bridge.

Run from the project root:
    uv run python -m ct.bridge.main

Long-polls Telegram (no public IP / webhook needed). Wires aiogram dispatcher,
allowlist middleware, command + topic-message handlers, permission inline-button
callback. Stops every active SessionRunner cleanly on SIGINT / SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from ct.bridge.bot import BridgeBot
from ct.bridge.runner_client import RunnerPool
from ct.config import load_settings
from ct.dashboard.auth import ensure_token
from ct.dashboard.server import serve as serve_dashboard
from ct.store.db import Db


# Shown in Telegram's `/` autocomplete dropdown. Order matters — Telegram
# preserves it. Descriptions are capped at 256 chars; keep them short.
BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="new",         description="Start a new session (or pick a profile)"),
    BotCommand(command="menu",        description="Action card for this topic"),
    BotCommand(command="m",           description="Action card for this topic (short)"),
    BotCommand(command="list",        description="Show active sessions"),
    BotCommand(command="permissions", description="Show or change permission mode"),
    BotCommand(command="model",       description="Show or live-swap the model"),
    BotCommand(command="effort",      description="Show or set effort level"),
    BotCommand(command="think",       description="Toggle adaptive thinking on|off for this session"),
    BotCommand(command="close",       description="Close this topic's session"),
    BotCommand(command="fork",        description="Branch this session into a new topic"),
    BotCommand(command="restart",     description="Reattach SDK to this transcript (unstick)"),
    BotCommand(command="undo",        description="Reverse last destructive action within 30 min"),
    BotCommand(command="move",        description="Migrate this session to another mac (mac=NAME)"),
    BotCommand(command="resume",      description="Re-attach orphaned sessions when the runner is back online"),
    BotCommand(command="logs",        description="Show last N transcript entries"),
    BotCommand(command="export",      description="Export full transcript as markdown"),
    BotCommand(command="get",         description="Download a file from the runner (/get <path>)"),
    BotCommand(command="quiet",       description="Set quiet hours (silent notifications window)"),
    BotCommand(command="search",      description="Search past transcripts (/search <pattern>)"),
    BotCommand(command="stats",       description="Activity stats: turns, top tools, per-session"),
    BotCommand(command="allow",       description="Bot-wide pre-approved tools (auto-allow list)"),
    BotCommand(command="tunnel",      description="Start/stop cloudflared tunnel for the web dashboard"),
    BotCommand(command="save",        description="Save a project profile"),
    BotCommand(command="profiles",    description="List saved profiles"),
    BotCommand(command="defaults",    description="Show or set bot-wide defaults"),
    BotCommand(command="macs",        description="List or manage registered runners"),
    BotCommand(command="status",      description="Bot uptime + runners + sessions"),
    BotCommand(command="context",     description="Show context-window usage for this session"),
    BotCommand(command="rewind",      description="Rewind file changes since the last user message"),
    BotCommand(command="help",        description="Show usage"),
]


def _configure_logging(level: str, fmt: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stderr,
    )
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def _resolve_dashboard_host(configured: str, log) -> str:
    """Pick the dashboard bind address.

    If CT_DASHBOARD_HOST is set explicitly, honor it (lets users override —
    e.g. =0.0.0.0 to bind everywhere, or =127.0.0.1 for loopback). Otherwise
    probe `tailscale ip -4` and bind to the first IPv4 the tailnet daemon
    hands back, so the dashboard is reachable from your phone/laptop over
    Tailscale but not exposed to the LAN. Falls back to 127.0.0.1 with a
    warning if tailscale isn't installed or returns nothing.
    """
    if configured:
        return configured

    import subprocess
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if result.returncode == 0:
            for raw in result.stdout.splitlines():
                line = raw.strip()
                if line:
                    log.info("dashboard.bind_tailnet", host=line)
                    return line
        log.warning(
            "dashboard.tailscale_returned_nothing",
            returncode=result.returncode,
        )
    except FileNotFoundError:
        log.warning("dashboard.tailscale_not_on_path")
    except subprocess.TimeoutExpired:
        log.warning("dashboard.tailscale_probe_timeout")
    except Exception:
        log.exception("dashboard.tailscale_probe_failed")

    log.warning(
        "dashboard.fallback_loopback",
        reason="binding to 127.0.0.1 — dashboard will only be reachable locally",
    )
    return "127.0.0.1"


async def _run() -> int:
    settings = load_settings()
    _configure_logging(settings.log_level, settings.log_format)
    log = structlog.get_logger("ct.bridge.main")

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    # Install the retry-after middleware before any messages are sent so even
    # restoration / startup notifications survive a Telegram 429.
    from ct.bridge._telegram_retry import install as _install_retry_middleware
    _install_retry_middleware(bot)
    db = Db(settings.db_path)
    await db.open()

    # Connect to the local runner. On Mac Studio it's the in-machine LaunchAgent
    # bound to 127.0.0.1; on other deployments it's whatever CT_RUNNER_HOST/PORT
    # point at. Phase 3 adds /macs commands to register additional remote runners.
    secret = (
        settings.bridge_hmac_secret.encode("utf-8")
        if settings.bridge_hmac_secret
        else None
    )
    runners = RunnerPool(secret=secret)
    try:
        await runners.add_runner(
            name="studio",
            host=settings.runner_host,
            port=settings.runner_port,
        )
    except Exception as exc:
        await db.close()
        await bot.session.close()
        raise RuntimeError(
            f"couldn't reach runner at {settings.runner_host}:{settings.runner_port} — "
            f"is the runner LaunchAgent loaded? ({exc!r})"
        ) from exc
    # Make sure studio has a row in the macs table so its main_dir lives in
    # the same place as remote macs (and /macs commands list it uniformly).
    await db.insert_mac("studio", settings.runner_host, settings.runner_port)

    # Reconnect every previously-registered remote mac. Failures here are
    # warnings, not fatal — a mac being asleep shouldn't block the bridge.
    # Skip 'studio' since it's added above as the local loopback runner.
    log_main = structlog.get_logger("ct.bridge.main")
    for mac in await db.list_macs():
        if mac.name == "studio":
            continue
        try:
            await runners.add_runner(
                name=mac.name, host=mac.host, port=mac.port,
                max_attempts=3, retry_interval=1.5,
            )
            await db.update_mac_connected(mac.name)
            log_main.info("bridge.mac_reconnected", name=mac.name, host=mac.host, port=mac.port)
        except Exception as exc:
            log_main.warning(
                "bridge.mac_unreachable",
                name=mac.name, host=mac.host, port=mac.port, error=str(exc),
            )

    bridge = BridgeBot(
        bot=bot, settings=settings, db=db, runner_pool=runners,
        default_runner="studio",
    )

    me = await bot.get_me()
    # Register the slash-command dropdown. Idempotent — safe to run on every
    # boot. Default scope = applies to every chat the bot's in.
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception:
        log.exception("bridge.set_commands_failed")  # non-fatal
    log.info(
        "bridge.starting",
        bot_username=me.username,
        bot_id=me.id,
        chat_id=settings.telegram_chat_id,
        allowed_users=len(settings.telegram_allowed_user_ids),
        db_path=str(settings.db_path),
        runner=f"{settings.runner_host}:{settings.runner_port}",
    )

    # Rehydrate active sessions from the DB before polling, so messages that
    # land on an existing topic immediately route to a live runner.
    restored = await bridge.restore_sessions()
    expired = await bridge.expire_pending_approvals()
    log.info("bridge.ready", restored_sessions=restored, expired_approvals=expired)

    # Start the local web dashboard. Token is generated on first boot and
    # persisted, so URLs the user has bookmarked keep working across restarts.
    dashboard_token = await ensure_token(db)
    bridge._defaults_cache["dashboard_token"] = dashboard_token
    dashboard_host = _resolve_dashboard_host(settings.dashboard_host, log)
    # cloudflared (in /tunnel on) needs to know which address to proxy to —
    # whatever we just bound the dashboard to. Stash it where cmd_tunnel can
    # read it (alongside the dashboard token).
    bridge._defaults_cache["dashboard_host"] = dashboard_host
    dashboard_runner = await serve_dashboard(
        bridge=bridge,
        host=dashboard_host,
        port=settings.dashboard_port,
        token=dashboard_token,
    )
    log.info(
        "dashboard.url",
        local=f"http://{dashboard_host}:{settings.dashboard_port}/?token={dashboard_token}",
    )

    stop_event = asyncio.Event()

    def _on_signal(signum: int) -> None:
        log.info("bridge.signal", signum=signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal, sig)

    polling_task = asyncio.create_task(
        bridge.dp.start_polling(
            bot,
            allowed_updates=bridge.dp.resolve_used_update_types(),
            handle_signals=False,
        ),
        name="bridge-polling",
    )
    # Start the idle housekeeping loop. It expires stale permission cards
    # every IDLE_CHECK_INTERVAL_S — separate task so a slow expiry edit
    # doesn't block polling.
    idle_task = bridge.start_idle_check()
    # Round-trips a T_PING to every connected runner once per
    # HEALTH_CHECK_INTERVAL_S so /macs can render 🟢/🟡/🔴 instead of relying
    # on `last_connected` (which goes stale the moment a mac sleeps).
    health_task = bridge.start_health_check()
    # Daily prune of state='closed' sessions older than CT_PRUNE_RETENTION_DAYS
    # (default 90). Without this the DB grows forever — sessions are never
    # auto-deleted, only marked closed.
    prune_task = bridge.start_prune_daemon()

    await stop_event.wait()
    log.info("bridge.shutting_down")

    # Aiogram 3: cancelling the task is the supported stop path. The dispatcher
    # also exposes an awaitable stop_polling() coroutine, but cancellation is
    # safer because it works regardless of the dispatcher's internal state.
    polling_task.cancel()
    idle_task.cancel()
    health_task.cancel()
    prune_task.cancel()
    try:
        await polling_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await idle_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await prune_task
    except (asyncio.CancelledError, Exception):
        pass
    try:
        await health_task
    except (asyncio.CancelledError, Exception):
        pass

    # Tunnel must die before the dashboard server — otherwise cloudflared
    # keeps proxying to a port nothing's listening on.
    await bridge._tunnel.stop()
    await dashboard_runner.cleanup()
    await bridge.shutdown()
    await bot.session.close()
    await db.close()
    log.info("bridge.stopped")
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
