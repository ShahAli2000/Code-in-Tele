-- Schema is idempotent (CREATE IF NOT EXISTS) so it's safe to run on every boot.

-- Phase 1+: one row per Telegram forum topic that the bridge is tracking.
-- runner_name added in v3 — without it, restore always tried the default
-- runner regardless of where the session was originally created.
CREATE TABLE IF NOT EXISTS sessions (
    thread_id        INTEGER PRIMARY KEY,           -- Telegram message_thread_id
    project_name     TEXT    NOT NULL,
    cwd              TEXT    NOT NULL,
    sdk_session_id   TEXT,                          -- nullable; assigned on first turn
    permission_mode  TEXT    NOT NULL,
    state            TEXT    NOT NULL DEFAULT 'active',  -- 'active' | 'closed' | 'orphaned'
    runner_name      TEXT    NOT NULL DEFAULT 'studio',
    model            TEXT,                          -- nullable; SDK default if NULL
    effort           TEXT,                          -- 'low' | 'medium' | 'high' | 'max'; null = SDK default
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    last_activity    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Phase 5: pending tool-use approvals — survive bridge restart so a button
-- tap after the restart still resolves through to the runner's SDK callback.
CREATE TABLE IF NOT EXISTS pending_permissions (
    tool_use_id      TEXT    PRIMARY KEY,
    thread_id        INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,             -- the Telegram message hosting the card
    tool_name        TEXT    NOT NULL,
    input_json       TEXT    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    decided_at       TEXT,
    decision         TEXT,                          -- 'allow' | 'deny' | NULL while waiting
    FOREIGN KEY (thread_id) REFERENCES sessions(thread_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_permissions_thread ON pending_permissions(thread_id);
CREATE INDEX IF NOT EXISTS idx_pending_permissions_unresolved
    ON pending_permissions(decided_at) WHERE decided_at IS NULL;

-- Phase 6: saved profiles. Hybrid model — only set the fields you want
-- locked; null fields fall through to bot defaults at /new time.
CREATE TABLE IF NOT EXISTS profiles (
    name             TEXT    PRIMARY KEY,
    dir              TEXT,
    runner_name      TEXT,
    model            TEXT,
    effort           TEXT,
    permission_mode  TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);

-- Phase 3: registered remote runners. The implicit local "studio" runner is
-- not stored here; only macs the user adds via /macs add land in this table.
CREATE TABLE IF NOT EXISTS macs (
    name             TEXT    PRIMARY KEY,
    host             TEXT    NOT NULL,
    port             INTEGER NOT NULL,
    added_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    last_connected   TEXT
);

-- schema_version: a tiny key-value table so future migrations have a foothold.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '5');

-- Default seeds for bot-wide defaults (used when a profile + /new override
-- don't specify the field). NULL means SDK default.
INSERT OR IGNORE INTO meta(key, value) VALUES ('default_runner_name', 'studio');
INSERT OR IGNORE INTO meta(key, value) VALUES ('default_permission_mode', 'acceptEdits');
