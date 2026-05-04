"""HMAC + timestamp replay protection on the wire protocol."""

from __future__ import annotations

import time

import pytest

from ct.protocol.auth import frame, unframe, _MAX_TS_SKEW_S
from ct.protocol.envelopes import Envelope, ProtocolError


SECRET = b"test-shared-secret-32-bytes-long"


def _make_env() -> Envelope:
    return Envelope(type="ping", id="42", payload={})


def test_round_trip_with_secret_and_fresh_ts() -> None:
    env = _make_env()
    line = frame(env, SECRET)
    out = unframe(line, SECRET)
    assert out.type == "ping"
    assert out.id == "42"


def test_signature_mismatch_rejected() -> None:
    env = _make_env()
    line = frame(env, SECRET)
    # Flip a single byte in the signature.
    sig, _, body = line.partition(":")
    bad = sig[:-1] + ("a" if sig[-1] != "a" else "b") + ":" + body
    with pytest.raises(ProtocolError, match="signature"):
        unframe(bad, SECRET)


def test_stale_envelope_rejected() -> None:
    env = _make_env()
    env.ts = time.time() - (_MAX_TS_SKEW_S + 5.0)  # well past window
    line = frame(env, SECRET)
    with pytest.raises(ProtocolError, match="outside"):
        unframe(line, SECRET)


def test_future_envelope_rejected() -> None:
    env = _make_env()
    env.ts = time.time() + (_MAX_TS_SKEW_S + 5.0)  # too far in the future
    line = frame(env, SECRET)
    with pytest.raises(ProtocolError, match="outside"):
        unframe(line, SECRET)


def test_no_secret_skips_replay_check() -> None:
    """Phase-2 / localhost mode: no secret = no signing = no replay check."""
    env = _make_env()
    env.ts = time.time() - 3600.0  # an hour stale
    line = frame(env, None)  # no signing
    out = unframe(line, None)
    assert out.type == "ping"


def test_tampering_with_ts_breaks_signature() -> None:
    """The ts is inside the signed body, so changing it on the wire invalidates
    the signature — replay protection isn't bypassable by editing only ts."""
    env = _make_env()
    line = frame(env, SECRET)
    sig, _, body = line.partition(":")
    # Re-serialize body with a stale ts (without re-signing)
    import json
    obj = json.loads(body)
    obj["ts"] = time.time() - 10000
    tampered_body = json.dumps(obj, separators=(",", ":"))
    bad = sig + ":" + tampered_body
    with pytest.raises(ProtocolError, match="signature"):
        unframe(bad, SECRET)
