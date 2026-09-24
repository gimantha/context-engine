"""Control-database storage for the knowledge backend's opaque state."""

from __future__ import annotations

from datetime import UTC, datetime

from .database import ControlDatabase


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


class SqliteBackendState:
    """Persist bindings, record references, and identities so a restart loses nothing.

    The engine never interprets these values; only the private adapter can decode them.
    """

    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def get_binding(self, partition: str) -> str | None:
        """Return the stored binding for a partition."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT binding FROM backend_bindings WHERE partition_id = ?", (partition,)
            ).fetchone()
        return row["binding"] if row else None

    def put_binding(self, partition: str, binding: str) -> None:
        """Store or replace the binding for a partition."""

        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO backend_bindings(partition_id, binding, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(partition_id) DO UPDATE SET
                    binding = excluded.binding, updated_at = excluded.updated_at
                """,
                (partition, binding, _timestamp()),
            )

    def get_record_reference(self, partition: str, source_id: str, record_id: str) -> str | None:
        """Return the backend reference of a record held in a partition."""

        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT reference FROM backend_record_refs
                WHERE partition_id = ? AND source_id = ? AND record_id = ?
                """,
                (partition, source_id, record_id),
            ).fetchone()
        return row["reference"] if row else None

    def put_record_reference(
        self, partition: str, source_id: str, record_id: str, reference: str
    ) -> None:
        """Store or replace the backend reference of a record held in a partition."""

        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO backend_record_refs(
                    partition_id, source_id, record_id, reference, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(partition_id, source_id, record_id) DO UPDATE SET
                    reference = excluded.reference, updated_at = excluded.updated_at
                """,
                (partition, source_id, record_id, reference, _timestamp()),
            )

    def find_record(self, reference: str) -> tuple[str, str, str] | None:
        """Return the partition, source, and record that a reference belongs to."""

        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT partition_id, source_id, record_id FROM backend_record_refs
                WHERE reference = ?
                """,
                (reference,),
            ).fetchone()
        return (row["partition_id"], row["source_id"], row["record_id"]) if row else None

    def delete_record_reference(self, reference: str) -> None:
        """Drop a backend reference once its item has been deleted."""

        with self.database.transaction() as connection:
            connection.execute("DELETE FROM backend_record_refs WHERE reference = ?", (reference,))

    def get_identity(self, principal_id: str) -> str | None:
        """Return the backend identity mapped to an engine principal."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT identity FROM backend_identities WHERE principal_id = ?", (principal_id,)
            ).fetchone()
        return row["identity"] if row else None

    def claim_identity(self, principal_id: str, identity: str) -> str:
        """Map a principal to an identity once; concurrent claimers all get the first one."""

        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO backend_identities(principal_id, identity, created_at)
                VALUES (?, ?, ?)
                """,
                (principal_id, identity, _timestamp()),
            )
            row = connection.execute(
                "SELECT identity FROM backend_identities WHERE principal_id = ?", (principal_id,)
            ).fetchone()
        return row["identity"]
