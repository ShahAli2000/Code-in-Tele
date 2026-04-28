"""Token auth for the dashboard.

Single static token persisted to the meta defaults table so it survives bridge
restarts (URLs the user has bookmarked still work). Generated lazily on first
boot — `ensure_token` returns the existing one or creates a new one.

Token check happens once per request via a middleware. Token is read from:
- query string `?token=...`
- form field `token` (POSTs)
- cookie `ct_token` (set on first valid query-string hit)

Failures return a plain 401 page.
"""

from __future__ import annotations

import secrets

from aiohttp import web

from ct.dashboard.templates import render_unauthorized


_TOKEN_KEY = "dashboard_token"
_COOKIE = "ct_token"


async def ensure_token(db) -> str:
    """Return the dashboard token, generating + persisting one if absent."""
    existing = await db.get_default(_TOKEN_KEY)
    if existing:
        return existing
    token = secrets.token_urlsafe(24)
    await db.set_default(_TOKEN_KEY, token)
    return token


async def rotate_token(db) -> str:
    """Force-generate a new token. Old URLs stop working immediately."""
    token = secrets.token_urlsafe(24)
    await db.set_default(_TOKEN_KEY, token)
    return token


@web.middleware
async def token_middleware(request: web.Request, handler):
    """Reject any request without a matching token, except for static asset
    routes that we don't have any of yet."""
    expected = request.app["dashboard_token"]
    supplied = (
        request.query.get("token")
        or request.cookies.get(_COOKIE)
    )
    if not supplied and request.method == "POST":
        # POST forms include token as a hidden field
        try:
            data = await request.post()
            supplied = data.get("token")
            request["form_data"] = data  # cache so handlers don't re-read
        except Exception:
            supplied = None
    if not supplied or not secrets.compare_digest(supplied, expected):
        return web.Response(
            text=render_unauthorized(), content_type="text/html", status=401,
        )
    response = await handler(request)
    # Set a cookie on success so subsequent links don't always need ?token=,
    # but the cookie isn't strictly necessary — links carry the token too.
    if isinstance(response, web.Response) and _COOKIE not in response.cookies:
        response.set_cookie(
            _COOKIE, expected,
            httponly=True, samesite="Strict", max_age=86400 * 30,
        )
    return response
