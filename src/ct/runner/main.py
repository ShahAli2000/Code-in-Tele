"""Runner entry point.

Run from the project root:
    uv run python -m ct.runner.main

Reads CT_RUNNER_HOST / CT_RUNNER_PORT / BRIDGE_HMAC_SECRET from .env. The
secret is REQUIRED on tailnet binds; on 127.0.0.1 it's optional (and Phase 2
localhost tests skip it).
"""

from __future__ import annotations

import asyncio
import logging
import sys

import structlog

from ct.config import load_settings
from ct.runner.server import run


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


async def _run() -> int:
    settings = load_settings()
    _configure_logging(settings.log_level, settings.log_format)
    log = structlog.get_logger("ct.runner.main")

    host = settings.runner_host
    port = settings.runner_port
    is_localhost = host in ("127.0.0.1", "localhost", "::1")
    secret: bytes | None = None
    if not is_localhost:
        if not settings.bridge_hmac_secret:
            log.error("runner.missing_secret_on_tailnet_bind", host=host)
            return 1
        secret = settings.bridge_hmac_secret.encode("utf-8")
    elif settings.bridge_hmac_secret:
        # If the operator set the secret, use it even on localhost — defence in
        # depth and consistency with the bridge's expectation.
        secret = settings.bridge_hmac_secret.encode("utf-8")

    return await run(host=host, port=port, secret=secret)


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
