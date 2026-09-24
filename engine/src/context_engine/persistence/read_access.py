"""Which principals currently hold backend read access on which partitions."""

from __future__ import annotations

from datetime import UTC, datetime

from .database import ControlDatabase


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


class ReadAccessRepository:
    """Track read access applied in the backend so changes are applied as a diff."""

    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def applied_readers(self, partition_id: str) -> frozenset[str]:
        """Return the principals holding backend read access on a partition."""

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT principal_id FROM backend_read_access WHERE partition_id = ?",
                (partition_id,),
            ).fetchall()
        return frozenset(row["principal_id"] for row in rows)

    def record_grant(self, partition_id: str, principal_id: str) -> None:
        """Record that the backend granted read access."""

        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO backend_read_access(partition_id, principal_id, granted_at)
                VALUES (?, ?, ?)
                """,
                (partition_id, principal_id, _timestamp()),
            )

    def remove_grant(self, partition_id: str, principal_id: str) -> None:
        """Record that the backend revoked read access."""

        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM backend_read_access WHERE partition_id = ? AND principal_id = ?",
                (partition_id, principal_id),
            )

    def last_synced(self) -> tuple[int, int] | None:
        """Return the policy version and principal count read access last reflected."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT policy_version, principal_count FROM read_access_sync WHERE id = 1"
            ).fetchone()
        return (row["policy_version"], row["principal_count"]) if row else None

    def set_synced(self, policy_version: int, principal_count: int) -> None:
        """Record the policy version and principal count read access now reflects."""

        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO read_access_sync(id, policy_version, principal_count, synced_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    policy_version = excluded.policy_version,
                    principal_count = excluded.principal_count,
                    synced_at = excluded.synced_at
                """,
                (policy_version, principal_count, _timestamp()),
            )

    def invalidate(self) -> None:
        """Force the next check to run a full synchronization."""

        with self.database.transaction() as connection:
            connection.execute("DELETE FROM read_access_sync WHERE id = 1")
