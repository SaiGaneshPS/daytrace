"""DT-10: SQLite connection (WAL) and migrations.

Each profile has its own database file. Connections are short-lived (one per unit of work); SQLite in WAL
mode lets the dashboard read while collectors write.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
BUSY_TIMEOUT_MS = 5000
# migrate() wraps each migration in its own transaction, so a migration must not start or end one itself.
TRANSACTION_KEYWORDS = frozenset({"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"})
_LEADING_COMMENTS = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.DOTALL)
_FIRST_WORD = re.compile(r"[A-Za-z]+")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations(directory: Path | None = None) -> list[Migration]:
    """All migrations (by default the ones shipped in daytrace_hub/migrations), in version order.

    Badly named files, gaps and duplicate numbers are errors.
    """
    folder: Traversable | Path = (
        directory if directory is not None else resources.files("daytrace_hub").joinpath("migrations")
    )
    migrations: list[Migration] = []
    for entry in folder.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = MIGRATION_NAME.match(entry.name)
        if not match:
            raise RuntimeError(f"migration file {entry.name!r} must be named like 0001_short_name.sql")
        migrations.append(Migration(int(match["version"]), match["name"], entry.read_text(encoding="utf-8")))
    migrations.sort(key=lambda m: m.version)
    check_sequence(migrations)
    return migrations


def check_sequence(migrations: list[Migration]) -> None:
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise RuntimeError(f"migrations must be numbered 1..N without gaps, found {versions}")


def split_statements(sql: str) -> list[str]:
    """Split a migration into single statements, using SQLite's own parser to find where each one ends.

    Comments, strings and trigger bodies (CREATE TRIGGER ... BEGIN ...; END;) are handled by
    sqlite3.complete_statement. A script that ends inside a comment or string is an error, and so is any
    statement that would start or end a transaction.
    """
    statements: list[str] = []
    buffer = ""
    for char in sql:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer.strip():
        if not sqlite3.complete_statement(buffer + "\n;"):
            raise RuntimeError("migration ends inside an unfinished statement, string or comment")
        statements.append(buffer)
    runnable: list[str] = []
    for statement in statements:
        word = _FIRST_WORD.match(statement, _LEADING_COMMENTS.match(statement).end())
        if word is None:
            continue  # only comments or an empty statement
        if word.group().upper() in TRANSACTION_KEYWORDS:
            raise RuntimeError(f"migrations must not contain {word.group().upper()}; migrate() adds the transaction")
        runnable.append(statement.strip())
    return runnable


class Database:
    """One profile's SQLite database file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _open(self) -> sqlite3.Connection:
        # check_same_thread=False: FastAPI may open a connection in a dependency and use it in a sync endpoint
        # on another threadpool thread. Each connection still has exactly one user at a time.
        conn = sqlite3.connect(
            self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False
        )
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
            mode = _switch_to_wal(conn)
            if str(mode).lower() != "wal":
                raise RuntimeError(f"could not switch {self.path} to WAL mode (got {mode})")
            return migrate(conn)

    def schema_version(self) -> int:
        with self.connect() as conn:
            return current_version(conn)


def _switch_to_wal(conn: sqlite3.Connection) -> str:
    """PRAGMA journal_mode = WAL, retried while another process holds the lock.

    SQLite does not call the busy handler for this switch, so two processes opening a new database at the
    same moment would otherwise fail at once with "database is locked". WAL is stored in the file, so after
    the first process switches, the retry in the other one is a no-op.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            return str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        except sqlite3.OperationalError as exc:
            busy = exc.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            if not busy or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any error (including a failed COMMIT)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        # SQLite may already have rolled back (disk full, I/O error); never hide the original error.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not exists:
        return 0
    return int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])


def _refuse_newer(version: int, latest: int) -> None:
    if version > latest:
        raise RuntimeError(f"database is at schema version {version}, newer than this hub ({latest}); update the hub")


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> list[int]:
    """Apply every migration newer than the database, each one atomic together with its version row.

    Safe when several processes (hub, tracker, seed) start on the same database at once: the version is
    checked again after the write lock is taken, so a migration another process just applied is skipped.
    Foreign keys are off while migrating (SQLite ignores that pragma inside a transaction, so it is set
    before BEGIN), which allows the usual table rebuild; PRAGMA foreign_key_check runs before each commit.
    """
    migrations = load_migrations() if migrations is None else migrations
    check_sequence(migrations)
    latest = len(migrations)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    version = current_version(conn)
    _refuse_newer(version, latest)
    pending = [m for m in migrations if m.version > version]
    if not pending:
        return []
    statements = {m.version: split_statements(m.sql) for m in pending}  # parse errors surface before any change
    applied: list[int] = []
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for migration in pending:
            with transaction(conn):
                version = current_version(conn)
                _refuse_newer(version, latest)
                if version >= migration.version:
                    continue  # another process applied it while we waited for the lock
                for statement in statements[migration.version]:
                    conn.execute(statement)
                broken = conn.execute("PRAGMA foreign_key_check").fetchall()
                if broken:
                    raise RuntimeError(f"migration {migration.version} left {len(broken)} broken foreign key(s)")
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (migration.version, migration.name, utc_text(datetime.now(UTC))),
                )
            applied.append(migration.version)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return applied


def utc_text(moment: datetime) -> str:
    """Fixed-width UTC text (2026-09-25T18:03:10.000000Z), so stored times compare and sort as plain text."""
    if moment.tzinfo is None:
        raise ValueError("times stored in the database must be timezone-aware")
    try:
        utc = moment.astimezone(UTC)
    except OverflowError:
        raise ValueError("time is outside the range the hub can store") from None
    if utc.year < 1000:
        raise ValueError("time is outside the range the hub can store")
    # Formatted by hand: strftime("%Y") does not zero-pad on every platform.
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}.{utc.microsecond:06d}Z"
    )


def utc_offset_minutes(moment: datetime) -> int:
    """The offset the time was sent with, in minutes (for showing local times later)."""
    offset = moment.utcoffset()
    if offset is None:
        raise ValueError("times stored in the database must be timezone-aware")
    return int(offset.total_seconds() // 60)
