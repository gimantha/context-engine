"""SQLite repository for principals, grants, policy version, and access decisions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from sqlite3 import Connection, Row
from uuid import uuid4

from context_engine.domain import ROOT_RESOURCE_ID, Action, Grant, Principal, PrincipalKind

from .database import ControlDatabase


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _principal(row: Row) -> Principal:
    return Principal(
        id=row["id"],
        issuer=row["issuer"],
        subject=row["subject"],
        kind=PrincipalKind(row["kind"]),
        email=row["email"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _grant(row: Row) -> Grant:
    return Grant(
        id=row["id"],
        resource_id=row["resource_id"],
        actions=frozenset(Action(item) for item in json.loads(row["actions_json"])),
        principal_id=row["principal_id"],
        group=row["group_name"],
        created_by=row["created_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _actions_json(actions: frozenset[Action]) -> str:
    return json.dumps(sorted(item.value for item in actions), separators=(",", ":"))


class AuthorizationRepository:
    """Persist identity and grant state and answer authorization lookups."""

    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    # Principals

    def get_principal(self, principal_id: str) -> Principal | None:
        """Return a principal by engine identifier when it exists."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM principals WHERE id = ?", (principal_id,)
            ).fetchone()
        return _principal(row) if row else None

    def resolve_principal(
        self, issuer: str, subject: str, kind: PrincipalKind, email: str | None
    ) -> Principal:
        """Return the principal for a verified identity, creating it on first sight.

        The (issuer, subject) pair is the identity key. Email is an attribute that may change
        without changing the principal.
        """

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM principals WHERE issuer = ? AND subject = ?", (issuer, subject)
            ).fetchone()
        if row and row["email"] == email and row["kind"] == kind.value:
            return _principal(row)
        now = _timestamp()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM principals WHERE issuer = ? AND subject = ?", (issuer, subject)
            ).fetchone()
            if existing is None:
                principal_id = f"prn_{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO principals(id, issuer, subject, kind, email, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (principal_id, issuer, subject, kind.value, email, now, now),
                )
            else:
                principal_id = existing["id"]
                connection.execute(
                    "UPDATE principals SET email = ?, kind = ?, updated_at = ? WHERE id = ?",
                    (email, kind.value, now, principal_id),
                )
            row = connection.execute(
                "SELECT * FROM principals WHERE id = ?", (principal_id,)
            ).fetchone()
        return _principal(row)

    # Policy version

    def policy_version(self) -> int:
        """Return the monotonic version that changes whenever a grant changes."""

        with self.database.connection() as connection:
            return connection.execute("SELECT version FROM policy_state WHERE id = 1").fetchone()[0]

    @staticmethod
    def _bump_policy_version(connection: Connection) -> None:
        # Bumping inside the grant transaction lets version-keyed caches invalidate atomically.
        connection.execute("UPDATE policy_state SET version = version + 1 WHERE id = 1")

    # Resources

    def resource_chain(self, resource_id: str) -> tuple[str, ...] | None:
        """Return a resource with its ancestors, or None when the resource is unknown."""

        if resource_id == ROOT_RESOURCE_ID:
            return (ROOT_RESOURCE_ID,)
        with self.database.connection() as connection:
            space = connection.execute(
                "SELECT id FROM context_spaces WHERE id = ?", (resource_id,)
            ).fetchone()
            if space is not None:
                return (resource_id, ROOT_RESOURCE_ID)
            source = connection.execute(
                "SELECT space_id FROM sources WHERE id = ?", (resource_id,)
            ).fetchone()
        if source is None:
            return None
        # A source inherits grants from its space, and the space from the root.
        return (resource_id, source["space_id"], ROOT_RESOURCE_ID)

    # Grants

    def grants_for(
        self, resource_ids: tuple[str, ...], principal_id: str, groups: frozenset[str]
    ) -> tuple[Grant, ...]:
        """Return grants on the resources that name the principal or any of its groups."""

        if not resource_ids:
            return ()
        resource_marks = ",".join("?" for _ in resource_ids)
        parameters: list[str] = [*resource_ids, principal_id]
        clause = "principal_id = ?"
        if groups:
            group_marks = ",".join("?" for _ in groups)
            clause += f" OR group_name IN ({group_marks})"
            parameters.extend(sorted(groups))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM grants WHERE resource_id IN ({resource_marks}) AND ({clause})",
                parameters,
            ).fetchall()
        return tuple(_grant(row) for row in rows)

    def list_grants(self, resource_id: str) -> tuple[Grant, ...]:
        """Return every grant on one resource in stable order."""

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM grants WHERE resource_id = ? ORDER BY created_at, id",
                (resource_id,),
            ).fetchall()
        return tuple(_grant(row) for row in rows)

    def get_grant(self, resource_id: str, grant_id: str) -> Grant | None:
        """Return one grant when it exists."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM grants WHERE resource_id = ? AND id = ?", (resource_id, grant_id)
            ).fetchone()
        return _grant(row) if row else None

    def put_grant(
        self,
        resource_id: str,
        grant_id: str,
        actions: frozenset[Action],
        principal_id: str | None,
        group: str | None,
        created_by: str,
    ) -> Grant:
        """Create or replace a grant and bump the policy version in the same transaction."""

        now = _timestamp()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT created_at FROM grants WHERE resource_id = ? AND id = ?",
                (resource_id, grant_id),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            connection.execute(
                """
                INSERT INTO grants(
                    resource_id, id, principal_id, group_name, actions_json,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_id, id) DO UPDATE SET
                    principal_id = excluded.principal_id,
                    group_name = excluded.group_name,
                    actions_json = excluded.actions_json,
                    updated_at = excluded.updated_at
                """,
                (
                    resource_id,
                    grant_id,
                    principal_id,
                    group,
                    _actions_json(actions),
                    created_by,
                    created_at,
                    now,
                ),
            )
            self._bump_policy_version(connection)
            row = connection.execute(
                "SELECT * FROM grants WHERE resource_id = ? AND id = ?", (resource_id, grant_id)
            ).fetchone()
        return _grant(row)

    def delete_grant(self, resource_id: str, grant_id: str) -> bool:
        """Delete a grant and bump the policy version; return whether a grant existed."""

        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM grants WHERE resource_id = ? AND id = ?", (resource_id, grant_id)
            )
            if cursor.rowcount:
                self._bump_policy_version(connection)
        return cursor.rowcount == 1

    def ensure_bootstrap_grant(self, principal_id: str, actions: frozenset[Action]) -> bool:
        """Create the root bootstrap grant for a principal once; never overwrite later edits."""

        if not actions:
            return False
        now = _timestamp()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO grants(
                    resource_id, id, principal_id, group_name, actions_json,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, 'bootstrap', ?, ?)
                """,
                (
                    ROOT_RESOURCE_ID,
                    f"bootstrap-{principal_id}",
                    principal_id,
                    _actions_json(actions),
                    now,
                    now,
                ),
            )
            if cursor.rowcount:
                self._bump_policy_version(connection)
        return cursor.rowcount == 1

    # Decisions

    def record_decision(
        self,
        principal_id: str,
        action: Action,
        resource_id: str,
        allowed: bool,
        reason_code: str,
        policy_version: int,
        trace_id: str,
    ) -> None:
        """Append one access decision to the audit table."""

        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO access_decisions(
                    id, principal_id, action, resource_id, allowed, reason_code,
                    policy_version, trace_id, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"dec_{uuid4().hex}",
                    principal_id,
                    action.value,
                    resource_id,
                    1 if allowed else 0,
                    reason_code,
                    policy_version,
                    trace_id,
                    _timestamp(),
                ),
            )

    def count_decisions(self, reason_code: str | None = None) -> int:
        """Return the number of recorded decisions, optionally for one reason code."""

        with self.database.connection() as connection:
            if reason_code is None:
                return connection.execute("SELECT COUNT(*) FROM access_decisions").fetchone()[0]
            return connection.execute(
                "SELECT COUNT(*) FROM access_decisions WHERE reason_code = ?", (reason_code,)
            ).fetchone()[0]
