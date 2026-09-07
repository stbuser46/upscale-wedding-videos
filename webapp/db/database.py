from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import time
from typing import Callable, Iterator, TypeVar


MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# Errors that are worth retrying rather than crashing on: the database file not
# yet mounted/created after a reboot, a locked database, or a transient I/O
# failure. sqlite3.OperationalError covers "unable to open database file" and
# "database is locked"; OSError covers the filesystem not being ready yet.
TRANSIENT_DB_ERRORS = (sqlite3.OperationalError, OSError)

T = TypeVar("T")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def retry_db(
    operation: Callable[[], T],
    *,
    attempts: int = 8,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    on_error: Callable[[Exception, int, float], None] | None = None,
) -> T:
    """Run a DB operation, retrying transient failures with exponential backoff.

    ``attempts <= 0`` retries indefinitely (used for startup, where the database
    may simply not be available yet after a reboot). Non-transient errors and
    the final failed attempt propagate to the caller.
    """
    delay = base_delay
    attempt = 0
    while True:
        attempt += 1
        try:
            return operation()
        except TRANSIENT_DB_ERRORS as exc:
            last_attempt = attempts > 0 and attempt >= attempts
            if last_attempt:
                raise
            if on_error is not None:
                on_error(exc, attempt, delay)
            time.sleep(delay)
            delay = min(max_delay, delay * 2)


class _AutoCloseConnection(sqlite3.Connection):
    """sqlite3's context manager commits/rolls back on ``__exit__`` but does NOT
    close the connection — so ``with connect() as db:`` leaks the underlying file
    descriptors (catalog.sqlite + -wal + -shm). Under the worker's high
    connection churn (heartbeats, per-second state checks) those pile up until
    the process hits its fd limit and every open fails with "unable to open
    database file". Closing here releases the fds on every ``with`` exit."""

    def __exit__(self, exc_type, exc, tb):
        try:
            super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(database_path: Path | str) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None, factory=_AutoCloseConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def migrate(database_path: Path | str) -> None:
    with connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(migration.name.split("_", 1)[0])
            if version in applied:
                continue
            script = migration.read_text(encoding="utf-8")
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + script
                + f"\nINSERT INTO schema_migrations(version, applied_at) "
                f"VALUES ({version}, '{utc_now()}');\nCOMMIT;"
            )


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
