"""Custom MCP tools registered with every SessionRunner.

Right now this module hosts a single tool — `svg_to_png` — exposed through an
SDK MCP server so Claude can invoke it like any built-in tool. Keeping these
in a dedicated module means the wire-up in `adapter.py` stays small and any
future tools have an obvious home.

`svg_to_png` shells out to `rsvg-convert` (from librsvg). It's a one-time
`brew install librsvg` on macOS; the tool detects absence and returns a
helpful error rather than silently failing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any

import structlog
from claude_agent_sdk import create_sdk_mcp_server, tool

log = structlog.get_logger(__name__)


@tool(
    "svg_to_png",
    "Convert an SVG file (or inline SVG markup) to a PNG file using librsvg. "
    "Provide either svg_path (file on disk) or svg_content (inline string). "
    "Always provide png_path - the destination file is created/overwritten. "
    "Optional width/height in pixels; if omitted, the SVG's intrinsic size "
    "is used.",
    {
        "svg_path": str,
        "svg_content": str,
        "png_path": str,
        "width": int,
        "height": int,
    },
)
async def svg_to_png(args: dict[str, Any]) -> dict[str, Any]:
    rsvg = shutil.which("rsvg-convert")
    if rsvg is None:
        return {
            "content": [{
                "type": "text",
                "text": (
                    "rsvg-convert not found on this runner.\n"
                    "Install it once: `brew install librsvg`.\n"
                    "After install, the tool works without restart."
                ),
            }],
            "isError": True,
        }

    svg_path = args.get("svg_path")
    svg_content = args.get("svg_content")
    png_path = args.get("png_path")
    width = args.get("width")
    height = args.get("height")

    if not png_path or not isinstance(png_path, str):
        return _err("png_path is required")
    if not svg_path and not svg_content:
        return _err("provide either svg_path or svg_content")

    cmd: list[str] = [rsvg]
    if isinstance(width, int) and width > 0:
        cmd += ["-w", str(width)]
    if isinstance(height, int) and height > 0:
        cmd += ["-h", str(height)]
    cmd += ["-o", png_path]

    stdin_bytes: bytes | None = None
    if svg_path and isinstance(svg_path, str):
        if not os.path.isfile(os.path.expanduser(svg_path)):
            return _err(f"svg_path does not exist or is not a file: {svg_path}")
        cmd.append(os.path.expanduser(svg_path))
    else:
        stdin_bytes = (svg_content or "").encode("utf-8")

    # Pass args as a list (no shell). All values are derived from typed
    # parameters; rsvg-convert's CLI doesn't interpret shell metacharacters
    # in args because we never go through a shell.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=stdin_bytes)
    if proc.returncode != 0:
        return _err(
            f"rsvg-convert exited with {proc.returncode}: "
            f"{stderr.decode('utf-8', errors='replace').strip()}"
        )
    try:
        size = os.path.getsize(os.path.expanduser(png_path))
    except OSError:
        size = -1
    return {
        "content": [{
            "type": "text",
            "text": f"✓ wrote {png_path} ({size} bytes)",
        }],
    }


def _err(msg: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": msg}],
        "isError": True,
    }


def build_mcp_server() -> Any:
    """Construct the SDK MCP server every SessionRunner registers. Cheap to
    rebuild per-session - no I/O, just dataclass construction - so we don't
    bother memoising it."""
    return create_sdk_mcp_server(
        name="ct",
        version="0.1.0",
        tools=[svg_to_png],
    )
