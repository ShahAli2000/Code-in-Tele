"""Audio transcription via mlx-whisper.

Runs locally on Apple Silicon — no API keys, no per-minute cost. The model
downloads once on first use (~150 MB for the base model). All inference
runs off the asyncio event loop via asyncio.to_thread so the bridge stays
responsive while a transcription is in flight.

Falls back to a no-op (None return) if mlx_whisper isn't importable, so
the rest of the bot keeps working on platforms where the dep wasn't
installed.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import structlog

log = structlog.get_logger(__name__)

# Apple's MLX-accelerated Whisper. Import lazily so non-arm64 hosts stay
# happy without it.
try:
    import mlx_whisper  # type: ignore[import-not-found]
    _MLX_AVAILABLE = True
except ImportError:
    mlx_whisper = None  # type: ignore[assignment]
    _MLX_AVAILABLE = False

# Default model. Trade-offs:
#   tiny       ~75 MB  — fastest, OK for one-line voice notes
#   base       ~150 MB — sweet spot (default)
#   small      ~500 MB — better for noisy / accented audio
#   large-v3-turbo  ~1.5 GB — best quality, still fast
DEFAULT_MODEL = "mlx-community/whisper-base-mlx"


def is_available() -> bool:
    """True iff transcription will actually do something. False on platforms
    where mlx-whisper isn't installed."""
    return _MLX_AVAILABLE


async def transcribe_bytes(
    content: bytes,
    *,
    suffix: str = ".ogg",
    model: str = DEFAULT_MODEL,
) -> str | None:
    """Transcribe an audio clip. Returns the text, or None if transcription
    isn't available on this host. Raises on hard errors."""
    if not _MLX_AVAILABLE:
        log.info("transcribe.unavailable")
        return None

    # mlx_whisper.transcribe wants a path on disk. Write the bytes to a temp
    # file, run the model in a worker thread (it's CPU/GPU-bound and sync),
    # then clean up.
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        result = await asyncio.to_thread(
            mlx_whisper.transcribe,
            tmp_path,
            path_or_hf_repo=model,
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    text = (result.get("text") or "").strip() if isinstance(result, dict) else ""
    return text or None
