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
import importlib.util
import os
import tempfile

import structlog

log = structlog.get_logger(__name__)

_FFMPEG_PATH_PREPARED = False  # only do the PATH setup once

# Default model. Trade-offs:
#   tiny       ~75 MB  — fastest, OK for one-line voice notes
#   base       ~150 MB — sweet spot (default)
#   small      ~500 MB — better for noisy / accented audio
#   large-v3-turbo  ~1.5 GB — best quality, still fast
DEFAULT_MODEL = "mlx-community/whisper-base-mlx"


def is_available() -> bool:
    """True iff transcription will actually do something. Cheap to call —
    just probes whether the dep can be imported, doesn't actually load it."""
    return importlib.util.find_spec("mlx_whisper") is not None


def _prepare_ffmpeg_path() -> None:
    """Ensure ffmpeg is on PATH for the openai-whisper subprocess shell-out.
    Idempotent — only runs the imageio_ffmpeg bookkeeping the first time."""
    global _FFMPEG_PATH_PREPARED
    if _FFMPEG_PATH_PREPARED:
        return
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]
    except ImportError:
        _FFMPEG_PATH_PREPARED = True
        return
    bin_path = imageio_ffmpeg.get_ffmpeg_exe()
    bin_dir = os.path.dirname(bin_path)
    if bin_dir and bin_dir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    # imageio's binary has a versioned name; whisper expects literal `ffmpeg`.
    alias = os.path.join(bin_dir, "ffmpeg")
    if not os.path.exists(alias):
        try:
            os.symlink(bin_path, alias)
        except OSError:
            pass
    _FFMPEG_PATH_PREPARED = True


async def transcribe_bytes(
    content: bytes,
    *,
    suffix: str = ".ogg",
    model: str = DEFAULT_MODEL,
) -> str | None:
    """Transcribe an audio clip. Returns the text, or None if transcription
    isn't available on this host. Raises on hard errors.

    Imports mlx_whisper + sets up ffmpeg PATH lazily on first call so the
    bridge's startup time isn't dominated by torch/mlx import (~25 s on
    Apple Silicon). After the first transcription, subsequent ones are fast."""
    if not is_available():
        log.info("transcribe.unavailable")
        return None
    _prepare_ffmpeg_path()
    # Heavy import deferred until first use.
    import mlx_whisper  # type: ignore[import-not-found]

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
