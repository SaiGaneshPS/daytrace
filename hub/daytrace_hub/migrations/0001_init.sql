-- DT-10: initial schema. Times are stored with db.utc_text() (fixed-width UTC, for example
-- 2026-09-25T18:03:10.000000Z) so they compare and sort as text; the original offset is kept to show
-- local times. device_type matches the list in docs/api.md (POST /pair/claim).
-- Migrations must not contain BEGIN or COMMIT: db.migrate() runs each file in its own transaction.

CREATE TABLE devices (
    device_id   TEXT PRIMARY KEY CHECK (length(device_id) BETWEEN 1 AND 64),
    name        TEXT NOT NULL,
    device_type TEXT NOT NULL CHECK (device_type IN ('windows', 'macos', 'android', 'ios', 'browser', 'viewer')),
    token_hash  TEXT UNIQUE,            -- NULL for the hub's own desktop tracker
    paired_at   TEXT NOT NULL,
    last_seen   TEXT,
    revoked_at  TEXT                    -- set on revoke, the data stays
);

CREATE TABLE events (
    id             INTEGER PRIMARY KEY,
    device_id      TEXT NOT NULL REFERENCES devices (device_id),
    dedup_key      TEXT NOT NULL,       -- Event.dedup_key(): ext:..., seq:... or content:...
    seq            INTEGER,
    external_id    TEXT,
    kind           TEXT NOT NULL,
    source         TEXT NOT NULL,
    start_utc      TEXT NOT NULL,
    end_utc        TEXT,
    utc_offset_min INTEGER NOT NULL,    -- offset the collector sent, in minutes
    app            TEXT,
    app_id         TEXT,
    title          TEXT,
    category       TEXT,
    data           TEXT NOT NULL DEFAULT '{}',
    received_at    TEXT NOT NULL,
    updated_at     TEXT,                -- set when an external_id event replaced this row
    UNIQUE (device_id, dedup_key),
    CHECK (end_utc IS NULL OR end_utc >= start_utc)
);

CREATE INDEX events_by_start ON events (start_utc);
CREATE INDEX events_by_device_start ON events (device_id, start_utc);
CREATE INDEX events_by_kind_start ON events (kind, start_utc);
CREATE INDEX events_by_device_seq ON events (device_id, seq);

CREATE TABLE sessions (
    id               INTEGER PRIMARY KEY,
    device_id        TEXT NOT NULL REFERENCES devices (device_id),
    local_date       TEXT NOT NULL,     -- YYYY-MM-DD in the device's local time
    start_utc        TEXT NOT NULL,
    end_utc          TEXT NOT NULL,
    seconds          REAL NOT NULL CHECK (seconds >= 0),
    app              TEXT,
    app_id           TEXT,
    title            TEXT,
    category         TEXT,
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    CHECK (end_utc >= start_utc)
);

CREATE INDEX sessions_by_device_date ON sessions (device_id, local_date);
CREATE INDEX sessions_by_date ON sessions (local_date);

CREATE TABLE category_overrides (
    app_key    TEXT PRIMARY KEY,        -- lowercase app name, app_id or domain
    category   TEXT NOT NULL CHECK (category IN ('social', 'video', 'work', 'study', 'comms', 'games', 'health', 'other')),
    source     TEXT NOT NULL CHECK (source IN ('user', 'ai')),
    updated_at TEXT NOT NULL
);

CREATE TABLE nudge_log (
    id         INTEGER PRIMARY KEY,
    rule       TEXT NOT NULL,
    device_id  TEXT REFERENCES devices (device_id),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX nudge_log_by_rule_time ON nudge_log (rule, created_at);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                 -- JSON
);
