"""Load configuration from .env.

The bridge reads everything it needs from environment variables (loaded from
`.env` if present). Settings are materialised as a frozen dataclass so the rest
of the code can ask for typed fields instead of stringly-typed os.environ.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"missing required env var {name!r}; copy .env.example to .env and fill it in"
        )
    return value


def _int_required(name: str) -> int:
    raw = _required(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"env var {name!r} must be an integer (got {raw!r})") from exc


def _int_list(name: str) -> list[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    out: list[int] = []
    for part in raw.split(","):
        s = part.strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except ValueError as exc:
            raise RuntimeError(
                f"env var {name!r} contains non-integer entry {s!r}"
            ) from exc
    return out


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: int
    telegram_allowed_user_ids: tuple[int, ...]
    anthropic_api_key: str  # may be empty when using Pro/Max OAuth
    bridge_hmac_secret: str
    db_path: Path
    runner_host: str
    runner_port: int
    log_level: str
    log_format: str
    dashboard_host: str   # bind address for the local web UI
    dashboard_port: int   # port for the local web UI
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])

    def is_user_allowed(self, user_id: int) -> bool:
        return user_id in self.telegram_allowed_user_ids


_settings: Settings | None = None


def load_settings(*, dotenv_path: Path | None = None, force: bool = False) -> Settings:
    """Load env (.env if present), validate required vars, return Settings.

    Idempotent — repeat calls return the cached value unless force=True.
    """
    global _settings
    if _settings is not None and not force:
        return _settings

    env_file = dotenv_path or Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)

    allowed = tuple(_int_list("TELEGRAM_ALLOWED_USER_IDS"))
    if not allowed:
        raise RuntimeError(
            "TELEGRAM_ALLOWED_USER_IDS is empty; the bot would reject every user."
        )
    # Multi-user is intentionally unsupported. The bot's session ownership,
    # dashboard ACL, and profiles/defaults are all global — adding a second
    # user_id silently grants them access to every other user's sessions and
    # transcripts. If you really want multi-user, opt in explicitly via env;
    # but read CONTRIBUTING.md first because there are real changes you'll
    # need to make to session ownership before it's safe.
    if len(allowed) > 1 and os.environ.get("CT_ALLOW_MULTIPLE_USERS", "").strip() != "1":
        raise RuntimeError(
            f"TELEGRAM_ALLOWED_USER_IDS has {len(allowed)} users but the bot's "
            "session model is single-user — every allowed user sees every other "
            "allowed user's sessions, transcripts, and profile data. To bypass "
            "this guard at your own risk, set CT_ALLOW_MULTIPLE_USERS=1."
        )

    _settings = Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_int_required("TELEGRAM_CHAT_ID"),
        telegram_allowed_user_ids=allowed,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        bridge_hmac_secret=_required("BRIDGE_HMAC_SECRET"),
        db_path=Path(os.environ.get("CT_DB_PATH", "./state/ct.db")).expanduser(),
        runner_host=os.environ.get("CT_RUNNER_HOST", "127.0.0.1"),
        runner_port=int(os.environ.get("CT_RUNNER_PORT", "8765")),
        log_level=os.environ.get("CT_LOG_LEVEL", "INFO").upper(),
        log_format=os.environ.get("CT_LOG_FORMAT", "console"),
        # When CT_DASHBOARD_HOST is unset, the bridge probes `tailscale ip -4`
        # at boot and binds to the tailnet IP — reachable from your phone or
        # laptop on the tailnet, but not exposed on the LAN. Set explicitly
        # to 127.0.0.1 for loopback-only, or 0.0.0.0 to bind all interfaces.
        dashboard_host=os.environ.get("CT_DASHBOARD_HOST", "").strip(),
        dashboard_port=int(os.environ.get("CT_DASHBOARD_PORT", "8766")),
    )
    return _settings
