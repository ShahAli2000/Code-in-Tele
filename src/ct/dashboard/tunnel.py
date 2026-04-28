"""Cloudflared "Quick Tunnel" subprocess wrapper.

`/tunnel on` spawns `cloudflared tunnel --url http://<host>:<port>` (host
defaults to the dashboard's bind address) and parses its stderr for the
public `https://*.trycloudflare.com` URL. `/tunnel off` SIGTERMs the
subprocess.

Quick Tunnels are anonymous, ephemeral, and rate-limited - suitable for
single-user "open my dashboard from my phone" use, not for production
exposure. The token in the URL is the only auth.
"""

from __future__ import annotations

import asyncio
import re
import shutil

import structlog

log = structlog.get_logger(__name__)

# Cloudflared logs the URL as something like:
#   "+----------------------------------------+"
#   "|  https://foo-bar-baz.trycloudflare.com  |"
#   "+----------------------------------------+"
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class TunnelManager:
    """Owns the cloudflared subprocess. One instance per BridgeBot."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._public_url: str | None = None
        self._reader_task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def public_url(self) -> str | None:
        return self._public_url

    async def start(
        self, *, local_port: int, local_host: str = "127.0.0.1",
        timeout: float = 30.0,
    ) -> str:
        """Spawn cloudflared pointed at http://{local_host}:{local_port} and
        wait for the public URL to appear in its output. The host must match
        whatever address the dashboard is actually bound to — by default the
        bridge passes its tailnet IP. Raises if the binary is missing, the
        subprocess exits early, or the URL doesn't appear within `timeout`."""
        if self.is_running:
            raise RuntimeError(f"tunnel already running: {self._public_url}")
        binary = shutil.which("cloudflared")
        if binary is None:
            raise RuntimeError(
                "cloudflared not installed. brew install cloudflared, then retry."
            )
        # All args come from typed parameters and are passed as a list — no
        # shell interpretation, no injection surface.
        self._proc = await asyncio.create_subprocess_exec(
            binary, "tunnel", "--url", f"http://{local_host}:{local_port}",
            "--no-autoupdate",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        url_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def _reader() -> None:
            assert self._proc is not None and self._proc.stdout is not None
            try:
                async for raw in self._proc.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    log.debug("tunnel.line", line=line.rstrip())
                    if not url_future.done():
                        m = _URL_RE.search(line)
                        if m:
                            url_future.set_result(m.group(0))
            except Exception:
                log.exception("tunnel.reader_failed")
            finally:
                if self._proc is not None:
                    rc = await self._proc.wait()
                    log.info("tunnel.exited", returncode=rc)
                if not url_future.done():
                    url_future.set_exception(RuntimeError("cloudflared exited before URL appeared"))

        self._reader_task = asyncio.create_task(_reader(), name="ct-tunnel-reader")
        try:
            url = await asyncio.wait_for(url_future, timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            await self.stop()
            raise
        self._public_url = url
        log.info("tunnel.started", url=url, local_host=local_host, local_port=local_port)
        return url

    async def stop(self) -> None:
        """Terminate the cloudflared subprocess (idempotent)."""
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
        self._proc = None
        self._reader_task = None
        self._public_url = None
        log.info("tunnel.stopped")

    def status_text(self) -> str:
        """Human-readable status for /tunnel status."""
        if self.is_running and self._public_url:
            return f"🌐 tunnel: ON\n{self._public_url}"
        if self.is_running:
            return "⏳ tunnel: starting (no URL yet)"
        return "💤 tunnel: off"


def public_url_with_token(public: str | None, token: str) -> str | None:
    """Helper: build the visit-able URL by appending the auth token."""
    if not public:
        return None
    sep = "&" if "?" in public else "?"
    return f"{public}{sep}token={token}"
