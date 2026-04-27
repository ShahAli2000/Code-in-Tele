-- Phase 1 schema: one row per Telegram forum topic that the bridge is tracking.
-- Schema is idempotent (CREATE IF NOT EXISTS) so it's safe to run on every boot.

CREATE TABLE IF NOT EXISTS sessions (
    thread_id        INTEGER PRIMARY KEY,           -- Telegram message_thread_id
    project_name     TEXT    NOT NULL,
    cwd              TEXT    NOT NULL,
    sdk_session_id   TEXT,                          -- nullable; assigned on first turn
    permission_mode  TEXT    NOT NULL,
    state            TEXT    NOT NULL DEFAULT 'active',  -- 'active' | 'closed' | 'orphaned'
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    last_activity    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);

-- schema_version: a tiny key-value table so future migrations have a foothold.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
