"""HTML templates for the dashboard.

Plain f-strings, no Jinja — keeps deps small and the rendered output is
trivial to inspect. All user content goes through `html.escape` (re-exported
as `e`) so embedded angle brackets / quotes can't break out.

Layout convention: every page calls `_layout(title, body)` which wraps the
body in <html>/<head>/<body> with the shared CSS. Tokens are appended to
in-page links via `link(path, token)`.
"""

from __future__ import annotations

from html import escape as e


_CSS = """
* { box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: #0d1117; color: #e6edf3; margin: 0; padding: 0;
}
header { background: #161b22; padding: 16px 24px; border-bottom: 1px solid #30363d; }
header h1 { margin: 0; font-size: 20px; }
header nav { margin-top: 8px; font-size: 14px; }
header nav a { color: #58a6ff; text-decoration: none; margin-right: 16px; }
header nav a:hover { text-decoration: underline; }
main { max-width: 1100px; margin: 0 auto; padding: 24px; }
h2 { font-size: 18px; border-bottom: 1px solid #30363d; padding-bottom: 8px; margin-top: 32px; }
h2:first-child { margin-top: 0; }
table { border-collapse: collapse; width: 100%; margin: 12px 0 24px; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #21262d; }
th { background: #161b22; font-weight: 500; color: #8b949e; }
tr:hover { background: #161b22; }
.session-link { color: #58a6ff; text-decoration: none; }
.session-link:hover { text-decoration: underline; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 3px;
       font-size: 12px; background: #21262d; color: #8b949e; margin-right: 4px; }
.tag-active { background: #1f4d2c; color: #58e08e; }
.tag-warn { background: #4d3f1f; color: #e0c558; }
form { margin: 16px 0; }
input, select, textarea {
    background: #0d1117; color: #e6edf3; border: 1px solid #30363d;
    padding: 8px; border-radius: 4px; font-family: inherit; font-size: 14px;
    width: 100%; max-width: 400px;
}
textarea { font-family: monospace; min-height: 100px; }
button {
    background: #238636; color: white; border: none; padding: 8px 16px;
    border-radius: 4px; cursor: pointer; font-size: 14px; margin-right: 8px;
}
button:hover { background: #2ea043; }
button.danger { background: #da3633; }
button.danger:hover { background: #f85149; }
button.secondary { background: #21262d; color: #e6edf3; }
button.secondary:hover { background: #30363d; }
.transcript {
    background: #161b22; border-radius: 6px; padding: 16px;
    font-family: monospace; font-size: 13px; white-space: pre-wrap;
    max-height: 600px; overflow-y: auto; line-height: 1.5;
}
.role-user { color: #58a6ff; font-weight: bold; }
.role-assistant { color: #58e08e; font-weight: bold; }
.muted { color: #8b949e; font-size: 13px; }
pre.tool-input {
    background: #0d1117; padding: 8px; border-radius: 4px; overflow-x: auto;
    font-size: 12px;
}
.flash { background: #1f4d2c; color: #58e08e; padding: 8px 12px;
         border-radius: 4px; margin-bottom: 16px; }
.flash.error { background: #4d1f23; color: #ff7b72; }
.field-grid { display: grid; grid-template-columns: 200px 1fr; gap: 8px 16px;
              max-width: 600px; margin-bottom: 16px; }
.field-grid div:nth-child(odd) { color: #8b949e; }
.empty { color: #8b949e; font-style: italic; padding: 16px 0; }
"""


def link(path: str, token: str) -> str:
    """Build an in-page URL with the auth token preserved."""
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}token={e(token)}"


def _layout(title: str, body: str, *, token: str, flash: str | None = None) -> str:
    flash_html = (
        f'<div class="flash">{e(flash)}</div>' if flash else ""
    )
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} — Claude bridge</title>
<style>{_CSS}</style>
</head>
<body>
<header>
<h1>Claude bridge dashboard</h1>
<nav>
  <a href="{link("/", token)}">sessions</a>
  <a href="{link("/approvals", token)}">approvals</a>
  <a href="{link("/settings", token)}">settings</a>
  <a href="{link("/stats", token)}">stats</a>
</nav>
</header>
<main>
{flash_html}
{body}
</main>
</body>
</html>"""


def render_sessions(sessions: list[dict], *, token: str) -> str:
    if not sessions:
        body = '<p class="empty">No active sessions. Start one in Telegram with /new.</p>'
    else:
        rows = []
        for s in sessions:
            name = e(s["project_name"])
            tid = s["thread_id"]
            runner = e(s.get("runner_name", "?"))
            mode = e(s.get("permission_mode", "?"))
            model = e(s.get("model") or "(default)")
            cwd = e(s.get("cwd", "?"))
            rows.append(
                f'<tr>'
                f'<td><a class="session-link" href="{link(f"/sessions/{tid}", token)}">{name}</a></td>'
                f'<td><span class="tag tag-active">{runner}</span></td>'
                f'<td><span class="tag">{mode}</span></td>'
                f'<td><span class="tag">{model}</span></td>'
                f'<td class="muted">{cwd}</td>'
                f'</tr>'
            )
        body = (
            "<h2>Active sessions</h2>"
            "<table>"
            "<thead><tr><th>project</th><th>runner</th><th>mode</th><th>model</th><th>cwd</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
        )
    return _layout("sessions", body, token=token)


def render_session(
    session: dict, entries: list[tuple[str, str]], *, token: str,
) -> str:
    name = e(session["project_name"])
    runner = e(session.get("runner_name", "?"))
    cwd = e(session.get("cwd", "?"))
    sdk_id = e(session.get("sdk_session_id") or "(not assigned yet)")

    if not entries:
        transcript_html = '<p class="empty">Transcript empty.</p>'
    else:
        bits = []
        for role, text in entries:
            cls = "role-user" if role == "user" else "role-assistant"
            label = "👤 user" if role == "user" else "🤖 assistant"
            bits.append(
                f'<div><span class="{cls}">{label}</span></div>'
                f"<div>{e(text)}</div>"
            )
        transcript_html = f'<div class="transcript">{"<br>".join(bits)}</div>'

    body = f"""
<h2>{name}</h2>
<div class="field-grid">
  <div>runner</div><div>{runner}</div>
  <div>cwd</div><div>{cwd}</div>
  <div>sdk_session_id</div><div class="muted">{sdk_id}</div>
</div>
<h2>Recent transcript</h2>
{transcript_html}
<p class="muted">refresh to see new messages — live updates are deferred.</p>
"""
    return _layout(name, body, token=token)


def render_approvals(rows: list[dict], *, token: str, flash: str | None = None) -> str:
    if not rows:
        body = '<h2>Pending approvals</h2><p class="empty">No pending approvals. 🎉</p>'
    else:
        cards = []
        for r in rows:
            tid = r["thread_id"]
            tool_name = e(r["tool_name"])
            tool_use_id = e(r["tool_use_id"])
            input_json = e(r.get("input_json", "{}"))
            ts = e(r.get("created_at", ""))
            cards.append(f"""
<h2>🔧 {tool_name} <span class="muted">— thread {tid} — {ts}</span></h2>
<pre class="tool-input">{input_json}</pre>
<form method="post" action="{link(f"/approvals/{tool_use_id}", token)}">
  <input type="hidden" name="token" value="{e(token)}">
  <button name="action" value="allow">✓ Approve</button>
  <button name="action" value="allow_remember" class="secondary">✓ Always allow {tool_name}</button>
  <button name="action" value="deny" class="danger">✗ Deny</button>
</form>
""")
        body = "".join(cards)
    return _layout("approvals", body, token=token, flash=flash)


def render_settings(
    *,
    defaults: dict,
    macs: list[dict],
    token: str,
    flash: str | None = None,
) -> str:
    quiet_start = e(defaults.get("quiet_hours_start") or "")
    quiet_end = e(defaults.get("quiet_hours_end") or "")
    auto_allow = e(defaults.get("default_auto_allow_tools") or "")
    default_runner = e(defaults.get("default_runner_name") or "")
    default_model = e(defaults.get("default_model") or "")
    default_effort = e(defaults.get("default_effort") or "")
    default_mode = e(defaults.get("default_permission_mode") or "")

    macs_rows = []
    for m in macs:
        name = e(m["name"])
        host = e(m["host"])
        port = m["port"]
        main_dir = e(m.get("main_dir") or "(default $HOME)")
        shortcuts = m.get("shortcuts") or []
        sc_html = (
            "<br>".join(f"• {e(s)}" for s in shortcuts) if shortcuts
            else '<span class="muted">none</span>'
        )
        macs_rows.append(
            f"<tr><td>{name}</td><td>{host}:{port}</td><td>{main_dir}</td><td>{sc_html}</td></tr>"
        )
    macs_table = (
        "<table><thead><tr><th>name</th><th>address</th><th>main_dir</th><th>shortcuts</th></tr></thead>"
        f"<tbody>{''.join(macs_rows)}</tbody></table>"
        if macs_rows else '<p class="empty">No remote macs registered.</p>'
    )

    body = f"""
<h2>Quiet hours</h2>
<form method="post" action="{link("/settings/quiet", token)}">
  <div class="field-grid">
    <div>start (HH:MM)</div><div><input name="start" value="{quiet_start}" placeholder="22:00"></div>
    <div>end (HH:MM)</div><div><input name="end" value="{quiet_end}" placeholder="08:00"></div>
  </div>
  <button>Save</button>
  <button name="clear" value="1" class="secondary">Clear</button>
</form>

<h2>Auto-allow tools (bot-wide)</h2>
<form method="post" action="{link("/settings/allow", token)}">
  <div class="field-grid">
    <div>CSV</div><div><input name="csv" value="{auto_allow}" placeholder="Read,Glob,Grep"></div>
  </div>
  <button>Save</button>
  <button name="clear" value="1" class="secondary">Clear</button>
</form>

<h2>Bot defaults (read-only here; edit in Telegram with /defaults)</h2>
<div class="field-grid">
  <div>runner</div><div>{default_runner or '<span class="muted">(none)</span>'}</div>
  <div>model</div><div>{default_model or '<span class="muted">(SDK default)</span>'}</div>
  <div>effort</div><div>{default_effort or '<span class="muted">(SDK default)</span>'}</div>
  <div>permission mode</div><div>{default_mode or '<span class="muted">acceptEdits</span>'}</div>
</div>

<h2>Registered macs</h2>
{macs_table}
<p class="muted">Manage with /macs in Telegram. Browse-shortcut adds via /macs config NAME add_shortcut=&lt;path&gt;.</p>
"""
    return _layout("settings", body, token=token, flash=flash)


def render_stats(stats: dict, *, token: str) -> str:
    if stats["total_events"] == 0:
        body = '<h2>Activity</h2><p class="empty">No events logged yet.</p>'
        return _layout("stats", body, token=token)

    by_kind = stats["by_kind"]
    by_kind_rows = "".join(
        f"<tr><td>{e(k)}</td><td>{v:,}</td></tr>"
        for k, v in by_kind.items()
    )
    top_tools_rows = "".join(
        f"<tr><td>{e(name)}</td><td>{count:,}</td></tr>"
        for name, count in stats["top_tools"]
    )
    sessions_rows = "".join(
        f"<tr><td>thread {tid}</td><td>{count:,}</td><td class=\"muted\">{e(str(ts))}</td></tr>"
        for tid, count, ts in stats["by_session"]
    )

    body = f"""
<h2>Overview</h2>
<div class="field-grid">
  <div>total events</div><div>{stats['total_events']:,}</div>
  <div>last 24h</div><div>{stats['recent_24h']:,}</div>
</div>

<h2>By kind</h2>
<table><thead><tr><th>kind</th><th>count</th></tr></thead>
<tbody>{by_kind_rows}</tbody></table>

<h2>Top tools</h2>
<table><thead><tr><th>tool</th><th>uses</th></tr></thead>
<tbody>{top_tools_rows or '<tr><td class="muted" colspan="2">none yet</td></tr>'}</tbody></table>

<h2>Most active sessions</h2>
<table><thead><tr><th>session</th><th>events</th><th>last activity</th></tr></thead>
<tbody>{sessions_rows}</tbody></table>
"""
    return _layout("stats", body, token=token)


def render_unauthorized() -> str:
    return """<!DOCTYPE html><html><head><title>401</title></head>
<body style="font-family:sans-serif;background:#0d1117;color:#e6edf3;padding:32px">
<h1>401 — token required</h1>
<p>Append <code>?token=&lt;your-token&gt;</code>. The bot DMs the URL with token embedded
when you start the tunnel via <code>/tunnel on</code>.</p></body></html>"""
