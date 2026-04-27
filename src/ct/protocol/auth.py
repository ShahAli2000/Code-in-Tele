"""HMAC-SHA256 envelope signing for Tailscale-authenticated bridge ↔ runner.

Tailscale already provides authenticated, encrypted point-to-point transport
for everything on the tailnet. HMAC on top of it is *defence in depth*: it
catches misconfigured peers (e.g. a runner accidentally bound to 0.0.0.0
instead of the tailnet IP), confused-deputy attacks via DNS, and any future
extension where we expose the runner over a non-Tailscale path.

Protocol:
- Frame on the wire is `<hex_sig>:<envelope_json>` (single line, hex sig first).
- Signature is HMAC-SHA256 of the envelope JSON bytes, hex-encoded.
- Both sides hold the shared `BRIDGE_HMAC_SECRET` from .env. Verification is
  constant-time. Wrong sig → reject; the protocol layer treats it as a
  ProtocolError so the connection drops.
"""

from __future__ import annotations

import hashlib
import hmac

from ct.protocol.envelopes import Envelope, ProtocolError

_HEX_SIG_LEN = 64  # SHA-256 → 32 bytes → 64 hex chars
_FRAME_SEP = ":"


def sign(secret: bytes, body: str) -> str:
    """Return the hex HMAC-SHA256 of `body` under `secret`."""
    return hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()


def frame(envelope: Envelope, secret: bytes | None) -> str:
    """Serialise + (optionally) sign an envelope into a single wire line.

    If `secret` is None, the frame is `:<json>` (empty signature) — used for
    Phase-2 localhost where signing is overkill. The unframe side enforces
    the same policy: no secret means signatures aren't checked.
    """
    body = envelope.to_json()
    sig = sign(secret, body) if secret else ""
    return f"{sig}{_FRAME_SEP}{body}"


def unframe(line: str, secret: bytes | None) -> Envelope:
    """Parse + verify a wire line into an Envelope. Raises ProtocolError on
    bad framing, missing/wrong signature, or malformed JSON."""
    if _FRAME_SEP not in line:
        raise ProtocolError("missing signature separator")
    sig, _, body = line.partition(_FRAME_SEP)

    if secret is not None:
        if len(sig) != _HEX_SIG_LEN:
            raise ProtocolError(
                f"signature must be {_HEX_SIG_LEN} hex chars (got {len(sig)})"
            )
        expected = sign(secret, body)
        if not hmac.compare_digest(sig, expected):
            raise ProtocolError("signature mismatch")
    elif sig and len(sig) == _HEX_SIG_LEN:
        # Peer sent a signature but we have no secret to check it. Accept the
        # body but log nothing here (callers can warn if they care).
        pass

    return Envelope.from_json(body)
