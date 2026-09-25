"""Tests for DT-10: the SQLite database, migrations and stored time format."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from daytrace_hub.db import (
    Database,
    Migration,
    current_version,
    load_migrations,
    migrate,
    utc_offset_minutes,
    utc_text,
)

EXPECTED_TABLES = {"schema_migrations", "devices", "events", "sessions", "category_overrides", "nudge_log", "settings"}
NOW = "2026-09-25T18:00:00.000000Z"


def add_device(conn: sqlite3.Connection, device_id: str = "android-1") -> None:
    conn.execute(
        "INSERT INTO devices (device_id, name, device_type, token_hash, paired_at) VALUES (?, ?, 'android', ?, ?)",
        (device_id, "Galaxy phone", f"hash-{device_id}", NOW),
    )


def add_event(conn: sqlite3.Connection, dedup_key: str, device_id: str = "android-1", **overrides: object) -> None:
    row = {
        "device_id": device_id, "dedup_key": dedup_key, "seq": 1, "kind": "app_session", "source": "usagestats",
        "start_utc": "2026-09-25T18:03:10.000000Z", "end_utc": "2026-09-25T18:21:44.000000Z",
        "utc_offset_min": -240, "received_at": NOW, **overrides,
    }
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO events ({columns}) VALUES ({placeholders})", tuple(row.values()))


def test_initialize_creates_a_wal_database_with_every_table(tmp_path: Path) -> None:
    database = Database(tmp_path / "new" / "folder" / "hub.db")
    assert database.initialize() == [1]
    with database.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert EXPECTED_TABLES <= tables
    assert database.schema_version() == len(load_migrations())


def test_initialize_twice_applies_nothing_the_second_time(db: Database) -> None:
    assert db.initialize() == []


def test_foreign_keys_are_enforced(db: Database) -> None:
    with db.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        add_event(conn, "seq:1", device_id="unknown-device")


def test_the_same_dedup_key_cannot_be_stored_twice_for_a_device(db: Database) -> None:
    with db.connect() as conn:
        add_device(conn)
        add_device(conn, "iphone-1")
        add_event(conn, "seq:1")
        add_event(conn, "seq:1", device_id="iphone-1")  # same key on another device is fine
        with pytest.raises(sqlite3.IntegrityError):
            add_event(conn, "seq:1")


def test_an_event_cannot_end_before_it_starts(db: Database) -> None:
    with db.connect() as conn:
        add_device(conn)
        with pytest.raises(sqlite3.IntegrityError):
            add_event(conn, "seq:9", end_utc="2026-09-25T17:00:00.000000Z")


def test_unknown_device_types_and_categories_are_refused(db: Database) -> None:
    with db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO devices (device_id, name, device_type, paired_at) VALUES ('x', 'x', 'toaster', ?)", (NOW,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO category_overrides VALUES ('tiktok', 'fun', 'user', ?)", (NOW,)
            )


def test_utc_text_is_fixed_width_and_sorts_like_time() -> None:
    earlier = datetime(2026, 9, 25, 14, 3, 10, tzinfo=timezone(timedelta(hours=-4)))  # 18:03:10 UTC
    later = datetime(2026, 9, 25, 18, 3, 10, 5, tzinfo=UTC)
    assert utc_text(earlier) == "2026-09-25T18:03:10.000000Z"
    assert utc_text(later) == "2026-09-25T18:03:10.000005Z"
    assert utc_text(earlier) < utc_text(later)
    assert len(utc_text(earlier)) == len(utc_text(later))


def test_offsets_are_kept_in_minutes() -> None:
    assert utc_offset_minutes(datetime(2026, 9, 25, tzinfo=timezone(timedelta(hours=5, minutes=30)))) == 330
    assert utc_offset_minutes(datetime(2026, 9, 25, tzinfo=timezone(timedelta(hours=-4)))) == -240


def test_naive_times_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_text(datetime(2026, 9, 25, 12, 0))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_offset_minutes(datetime(2026, 9, 25, 12, 0))  # noqa: DTZ001


def test_a_database_newer_than_the_hub_is_refused(db: Database) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO schema_migrations VALUES (99, 'from_the_future', ?)", (NOW,))
        with pytest.raises(RuntimeError, match="newer than this hub"):
            migrate(conn)


def test_a_failing_migration_changes_nothing(tmp_path: Path) -> None:
    database = Database(tmp_path / "hub.db")
    database.initialize()
    broken = [*load_migrations(), Migration(2, "broken", "CREATE TABLE half_done (id INTEGER); SELECT nope FROM nowhere;")]
    with database.connect() as conn:
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn, broken)
        assert current_version(conn) == 1
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'half_done'").fetchone()


def test_migrations_with_comments_and_semicolons_in_strings_work(tmp_path: Path) -> None:
    tricky = Migration(1, "tricky", "-- a comment; with a semicolon\nCREATE TABLE t (v TEXT DEFAULT 'a;b');")
    with Database(tmp_path / "hub.db").connect() as conn:
        assert migrate(conn, [tricky]) == [1]
        conn.execute("INSERT INTO t DEFAULT VALUES")
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "a;b"


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (["0001_init.sql", "0003_skip.sql"], "without gaps"),
        (["0001_init.sql", "0001_again.sql"], "without gaps"),
        (["1_init.sql"], "must be named like"),
        (["0001_Init-Table.sql"], "must be named like"),
    ],
)
def test_badly_numbered_or_named_migrations_are_refused(tmp_path: Path, files: list[str], message: str) -> None:
    for name in files:
        (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(RuntimeError, match=message):
        load_migrations(tmp_path)


def test_shipped_migrations_are_numbered_from_one() -> None:
    assert [m.version for m in load_migrations()] == list(range(1, len(load_migrations()) + 1))
