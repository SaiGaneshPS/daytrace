"""DT-10: SQLite connection (WAL) and migrations.

Each profile has its own database file. Connections are short-lived (one per unit of work); SQLite in WAL
mode lets the dashboard read while collectors write.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
BUSY_TIMEOUT_MS = 5000


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations(directory: Path | None = None) -> list[Migration]:
    """All migrations (by default the ones shipped in daytrace_hub/migrations), in version order.

    Badly named files, gaps and duplicate numbers are errors.
    """
    folder = directory if directory is not None else resources.files("daytrace_hub").joinpath("migrations")
    migrations: list[Migration] = []
    for entry in folder.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = MIGRATION_NAME.match(entry.name)
        if not match:
            raise RuntimeError(f"migration file {entry.name!r} must be named like 0001_short_name.sql")
        migrations.append(Migration(int(match["version"]), match["name"], entry.read_text(encoding="utf-8")))
    migrations.sort(key=lambda m: m.version)
    expected = list(range(1, len(migrations) + 1))
    if [m.version for m in migrations] != expected:
        raise RuntimeError(f"migrations must be numbered 1..N without gaps, found {[m.version for m in migrations]}")
    return migrations


class Database:
    """One profile's SQLite database file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection in autocommit mode; use `with transaction(conn):` for multi-statement writes."""
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> list[int]:
        """Create the folder and file if needed, switch to WAL, and apply pending migrations.

        Returns the versions applied by this call (empty when the database was already up to date).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError(f"could not switch {self.path} to WAL mode (got {mode})")
            return migrate(conn)

    def schema_version(self) -> int:
        with self.connect() as conn:
            return current_version(conn)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not exists:
        return 0
    return int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> list[int]:
    """Apply every migration newer than the database, each in its own transaction."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied: list[int] = []
    migrations = load_migrations() if migrations is None else migrations
    version = current_version(conn)
    if version > len(migrations):
        raise RuntimeError(
            f"database is at schema version {version}, newer than this hub ({len(migrations)}); update the hub"
        )
    for migration in migrations:
        if migration.version <= version:
            continue
        # executescript lets SQLite parse the file itself (comments, strings, triggers); the explicit
        # BEGIN/COMMIT makes the migration and its version row one atomic step, so migration files must not
        # contain BEGIN or COMMIT themselves. executescript takes no parameters, hence the escaping.
        name = migration.name.replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n;\n"
            "INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({int(migration.version)}, '{name}', '{utc_text(datetime.now(UTC))}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(migration.version)
    return applied


def utc_text(moment: datetime) -> str:
    """Fixed-width UTC text (2026-09-25T18:03:10.000000Z), so stored times compare and sort as plain text."""
    if moment.tzinfo is None:
        raise ValueError("times stored in the database must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utc_offset_minutes(moment: datetime) -> int:
    """The offset the time was sent with, in minutes (for showing local times later)."""
    offset = moment.utcoffset()
    if offset is None:
        raise ValueError("times stored in the database must be timezone-aware")
    return int(offset.total_seconds() // 60)
