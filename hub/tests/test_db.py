"""Tests for DT-10: the SQLite database, migrations and stored time format."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from daytrace_hub import db as db_module
from daytrace_hub.db import (
    Database,
    Migration,
    current_version,
    load_migrations,
    migrate,
    split_statements,
    transaction,
    utc_offset_minutes,
    utc_text,
)

EXPECTED_TABLES = {"schema_migrations", "devices", "events", "sessions", "category_overrides", "nudge_log", "settings"}
NOW = "2026-09-25T18:00:00.000000Z"
LATEST = len(load_migrations())


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


def with_extra(*extra: Migration) -> list[Migration]:
    """The shipped migrations plus test-only ones numbered after them."""
    return [*load_migrations(), *extra]


def extra(offset: int, name: str, sql: str) -> Migration:
    return Migration(LATEST + offset, name, sql)


# --- the database file --------------------------------------------------------------------------------------


def test_initialize_creates_a_wal_database_with_every_table(tmp_path: Path) -> None:
    database = Database(tmp_path / "new" / "folder" / "hub.db")
    assert database.initialize() == list(range(1, LATEST + 1))
    with database.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert EXPECTED_TABLES <= tables
    assert database.schema_version() == LATEST


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


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO devices (device_id, name, device_type, paired_at) VALUES ('x', 'x', 'toaster', '2026')",
        "INSERT INTO devices (device_id, name, device_type, paired_at) VALUES ('x', 'x', 'linux', '2026')",
        "INSERT INTO category_overrides VALUES ('tiktok', 'fun', 'user', '2026')",
        "INSERT INTO category_overrides VALUES ('tiktok', 'social', 'robot', '2026')",
    ],
)
def test_values_outside_the_contract_are_refused(db: Database, sql: str) -> None:
    with db.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_a_connection_can_be_used_from_another_thread(db: Database) -> None:
    # FastAPI runs sync endpoints on a threadpool, often not the thread that opened the connection.
    import threading

    result: list[int] = []
    with db.connect() as conn:
        thread = threading.Thread(target=lambda: result.append(conn.execute("SELECT 1").fetchone()[0]))
        thread.start()
        thread.join()
    assert result == [1]


# --- transactions -------------------------------------------------------------------------------------------


def test_transaction_commits_or_rolls_back(db: Database) -> None:
    with db.connect() as conn:
        with transaction(conn):
            add_device(conn)
        with pytest.raises(KeyError), transaction(conn):
            add_device(conn, "iphone-1")
            raise KeyError("boom")
        assert [r[0] for r in conn.execute("SELECT device_id FROM devices")] == ["android-1"]


def test_transaction_keeps_the_original_error_when_sqlite_already_rolled_back(db: Database) -> None:
    with db.connect() as conn, pytest.raises(KeyError), transaction(conn):
        conn.execute("ROLLBACK")  # what SQLite does by itself after a disk-full or I/O error
        raise KeyError("the real problem")


def test_a_failed_commit_leaves_the_connection_usable(tmp_path: Path) -> None:
    with Database(tmp_path / "hub.db").connect() as conn:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)"
        )
        with pytest.raises(sqlite3.IntegrityError), transaction(conn):
            conn.execute("INSERT INTO child VALUES (99)")  # only checked at COMMIT
        assert not conn.in_transaction
        with transaction(conn):
            conn.execute("INSERT INTO parent VALUES (1)")
        assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0


# --- migrations ---------------------------------------------------------------------------------------------


def test_a_database_newer_than_the_hub_is_refused(db: Database) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO schema_migrations VALUES (?, 'from_the_future', ?)", (LATEST + 50, NOW))
        with pytest.raises(RuntimeError, match="newer than this hub"):
            migrate(conn)


def test_a_failing_migration_changes_nothing(db: Database) -> None:
    broken = extra(1, "broken", "CREATE TABLE half_done (id INTEGER); SELECT nope FROM nowhere;")
    with db.connect() as conn:
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn, with_extra(broken))
        assert current_version(conn) == LATEST
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'half_done'").fetchone()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1  # switched back on after the failure


def test_a_migration_another_process_just_applied_is_skipped(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two processes (hub + tracker) start together: both read the old version before either takes the lock.
    new_table = extra(1, "add_table", "CREATE TABLE added (id INTEGER);")
    with db.connect() as conn:
        migrate(conn, with_extra(new_table))  # the other process wins the race
    real_version = db_module.current_version
    calls = {"n": 0}

    def stale_first_read(conn: sqlite3.Connection) -> int:
        calls["n"] += 1
        return LATEST if calls["n"] == 1 else real_version(conn)

    monkeypatch.setattr(db_module, "current_version", stale_first_read)
    with db.connect() as conn:
        assert migrate(conn, with_extra(new_table)) == []  # would fail with "table added already exists"


def test_two_processes_can_create_the_same_database_at_once(tmp_path: Path) -> None:
    # Real concurrency: both open a brand-new file together (the WAL switch and migration 1 race).
    import threading

    for attempt in range(5):
        path = tmp_path / f"hub-{attempt}.db"
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def start(
            name: str, path: Path = path, barrier: threading.Barrier = barrier, results: dict[str, object] = results
        ) -> None:
            database = Database(path)
            barrier.wait()
            try:
                results[name] = database.initialize()
            except Exception as exc:  # noqa: BLE001
                results[name] = exc

        threads = [threading.Thread(target=start, args=(name,)) for name in ("hub", "tracker")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(isinstance(r, list) for r in results.values()), results
        assert sorted(v for r in results.values() for v in r) == list(range(1, LATEST + 1))  # type: ignore[union-attr]


def test_migrations_rebuild_a_table_that_others_reference(db: Database) -> None:
    # The standard SQLite way to change a CHECK list: create, copy, drop, rename. Needs foreign keys off.
    rebuild = extra(1, "rebuild_devices", """
        CREATE TABLE devices_new (
            device_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            device_type TEXT NOT NULL CHECK (device_type IN ('windows', 'macos', 'android', 'ios', 'browser',
                                                              'viewer', 'watch')),
            token_hash TEXT UNIQUE, paired_at TEXT NOT NULL, last_seen TEXT, revoked_at TEXT);
        INSERT INTO devices_new SELECT * FROM devices;
        DROP TABLE devices;
        ALTER TABLE devices_new RENAME TO devices;
    """)
    with db.connect() as conn:
        add_device(conn)
        add_event(conn, "seq:1")
        assert migrate(conn, with_extra(rebuild)) == [LATEST + 1]
        conn.execute("INSERT INTO devices (device_id, name, device_type, paired_at) VALUES ('w', 'w', 'watch', ?)", (NOW,))
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_a_migration_that_breaks_foreign_keys_is_rolled_back(db: Database) -> None:
    orphaning = extra(1, "orphan", "DELETE FROM devices;")
    with db.connect() as conn:
        add_device(conn)
        add_event(conn, "seq:1")
        with pytest.raises(RuntimeError, match="broken foreign key"):
            migrate(conn, with_extra(orphaning))
        assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
        assert current_version(conn) == LATEST


def test_an_unfinished_migration_is_refused_before_anything_runs(db: Database) -> None:
    unfinished = extra(1, "unfinished", "CREATE TABLE t (id INTEGER);\n/* TODO: add an index later")
    with db.connect() as conn:
        with pytest.raises(RuntimeError, match="unfinished"):
            migrate(conn, with_extra(unfinished))
        assert current_version(conn) == LATEST
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 't'").fetchone()


def test_migrations_with_comments_strings_and_triggers_work(tmp_path: Path) -> None:
    tricky = Migration(1, "tricky", """
        -- a comment; with a semicolon
        CREATE TABLE t (v TEXT DEFAULT 'a;b', n INTEGER DEFAULT 0);
        /* block comment; also with a semicolon */
        CREATE TABLE audit (v TEXT);
        CREATE TRIGGER t_audit AFTER INSERT ON t BEGIN
            INSERT INTO audit VALUES (new.v);
            UPDATE t SET n = 1 WHERE rowid = new.rowid;
        END;
        CREATE INDEX t_by_v ON t (v)
    """)
    with Database(tmp_path / "hub.db").connect() as conn:
        assert migrate(conn, [tricky]) == [1]
        conn.execute("INSERT INTO t (v) VALUES ('x;y')")
        assert conn.execute("SELECT v, n FROM t").fetchone()[:] == ("x;y", 1)
        assert conn.execute("SELECT v FROM audit").fetchone()[0] == "x;y"
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 't_by_v'").fetchone()


@pytest.mark.parametrize("statement", ["BEGIN;", "COMMIT;", "END TRANSACTION;", "ROLLBACK;", "SAVEPOINT x;", "release x;"])
def test_migrations_cannot_control_transactions(statement: str) -> None:
    with pytest.raises(RuntimeError, match="must not contain"):
        split_statements(f"CREATE TABLE a (id INTEGER);\n-- note\n{statement}\nCREATE TABLE b (id INTEGER);")


def test_empty_statements_and_comment_only_tails_are_skipped() -> None:
    assert split_statements(";;\nCREATE TABLE a (id INTEGER);\n-- the end\n") == ["CREATE TABLE a (id INTEGER);"]


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


def test_duplicate_versions_passed_to_migrate_are_refused(db: Database) -> None:
    with db.connect() as conn, pytest.raises(RuntimeError, match="without gaps"):
        migrate(conn, [*load_migrations(), Migration(LATEST, "again", "SELECT 1;")])


def test_shipped_migrations_are_numbered_from_one() -> None:
    assert [m.version for m in load_migrations()] == list(range(1, LATEST + 1))


# --- stored times -------------------------------------------------------------------------------------------


def test_utc_text_is_fixed_width_and_sorts_like_time() -> None:
    earlier = datetime(2026, 9, 25, 14, 3, 10, tzinfo=timezone(timedelta(hours=-4)))  # 18:03:10 UTC
    later = datetime(2026, 9, 25, 18, 3, 10, 5, tzinfo=UTC)
    assert utc_text(earlier) == "2026-09-25T18:03:10.000000Z"
    assert utc_text(later) == "2026-09-25T18:03:10.000005Z"
    assert utc_text(earlier) < utc_text(later)
    assert len(utc_text(earlier)) == len(utc_text(later))
    assert utc_text(datetime(1000, 1, 1, tzinfo=UTC)) == "1000-01-01T00:00:00.000000Z"


def test_offsets_are_kept_in_minutes() -> None:
    assert utc_offset_minutes(datetime(2026, 9, 25, tzinfo=timezone(timedelta(hours=5, minutes=30)))) == 330
    assert utc_offset_minutes(datetime(2026, 9, 25, tzinfo=timezone(timedelta(hours=-4)))) == -240


def test_naive_times_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_text(datetime(2026, 9, 25, 12, 0))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_offset_minutes(datetime(2026, 9, 25, 12, 0))  # noqa: DTZ001


@pytest.mark.parametrize(
    "moment",
    [
        datetime(9999, 12, 31, 23, 30, tzinfo=timezone(timedelta(hours=-5))),  # past year 9999 in UTC
        datetime(1000, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))),  # before year 1000 in UTC
    ],
)
def test_times_without_a_storable_utc_value_raise_value_error(moment: datetime) -> None:
    with pytest.raises(ValueError, match="outside the range"):
        utc_text(moment)
