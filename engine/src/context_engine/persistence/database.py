"""SQLite connection and forward-only migration support."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ControlDatabase:
    """Own SQLite connections and apply ordered SQL migrations."""

    def __init__(self, path: Path, migrations_path: Path) -> None:
        self.path = path
        self.migrations_path = migrations_path

    def connect(self) -> sqlite3.Connection:
        """Open and configure a SQLite connection for control-plane use."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection and always close it afterward."""

        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction with commit, rollback, and cleanup."""

        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> tuple[str, ...]:
        """Apply unapplied forward-only SQL migrations in filename order."""

        if not self.migrations_path.is_dir():
            raise RuntimeError(f"Migration directory does not exist: {self.migrations_path}")
        applied: list[str] = []
        with self.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            known = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for path in sorted(self.migrations_path.glob("*.sql")):
                version = path.stem
                if version in known:
                    continue
                quoted_version = version.replace("'", "''")
                # Keep the schema change and its version marker in the same SQLite transaction.
                script = (
                    "BEGIN IMMEDIATE;\n"
                    + path.read_text()
                    + f"\nINSERT INTO schema_migrations(version) VALUES ('{quoted_version}');\n"
                    + "COMMIT;"
                )
                connection.executescript(script)
                applied.append(version)
        return tuple(applied)

    def ping(self) -> bool:
        """Return whether the control database accepts a simple query."""

        try:
            with self.connection() as connection:
                return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False
