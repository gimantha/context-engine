"""Durable state a knowledge backend keeps in the engine's control database.

The values are opaque to the engine. A backend encodes whatever it needs to find its own
isolation units, items, and identities again after a restart; the engine stores them next to
its authoritative ledger and never serializes them publicly.
"""

from __future__ import annotations

from typing import Protocol


class BackendStateStore(Protocol):
    """Opaque key-value state for partition bindings, record references, and identities."""

    def get_binding(self, partition: str) -> str | None:
        """Return the stored binding for a partition."""

        ...

    def put_binding(self, partition: str, binding: str) -> None:
        """Store or replace the binding for a partition."""

        ...

    def get_record_reference(self, partition: str, source_id: str, record_id: str) -> str | None:
        """Return the backend reference of a record held in a partition."""

        ...

    def put_record_reference(
        self, partition: str, source_id: str, record_id: str, reference: str
    ) -> None:
        """Store or replace the backend reference of a record held in a partition."""

        ...

    def find_record(self, reference: str) -> tuple[str, str, str] | None:
        """Return the partition, source, and record that a reference belongs to."""

        ...

    def delete_record_reference(self, reference: str) -> None:
        """Drop a backend reference once its item has been deleted."""

        ...

    def get_identity(self, principal_id: str) -> str | None:
        """Return the backend identity mapped to an engine principal."""

        ...

    def claim_identity(self, principal_id: str, identity: str) -> str:
        """Map a principal to an identity once; return the identity that is actually stored."""

        ...


class InMemoryBackendState:
    """Process-local state for tests and the architecture spike."""

    def __init__(self) -> None:
        self._bindings: dict[str, str] = {}
        self._references: dict[tuple[str, str, str], str] = {}
        self._locations: dict[str, tuple[str, str, str]] = {}
        self._identities: dict[str, str] = {}

    def get_binding(self, partition: str) -> str | None:
        """Return the stored binding for a partition."""

        return self._bindings.get(partition)

    def put_binding(self, partition: str, binding: str) -> None:
        """Store or replace the binding for a partition."""

        self._bindings[partition] = binding

    def get_record_reference(self, partition: str, source_id: str, record_id: str) -> str | None:
        """Return the backend reference of a record held in a partition."""

        return self._references.get((partition, source_id, record_id))

    def put_record_reference(
        self, partition: str, source_id: str, record_id: str, reference: str
    ) -> None:
        """Store or replace the backend reference of a record held in a partition."""

        key = (partition, source_id, record_id)
        previous = self._references.get(key)
        if previous is not None:
            self._locations.pop(previous, None)
        self._references[key] = reference
        self._locations[reference] = key

    def find_record(self, reference: str) -> tuple[str, str, str] | None:
        """Return the partition, source, and record that a reference belongs to."""

        return self._locations.get(reference)

    def delete_record_reference(self, reference: str) -> None:
        """Drop a backend reference once its item has been deleted."""

        key = self._locations.pop(reference, None)
        if key is not None:
            self._references.pop(key, None)

    def get_identity(self, principal_id: str) -> str | None:
        """Return the backend identity mapped to an engine principal."""

        return self._identities.get(principal_id)

    def claim_identity(self, principal_id: str, identity: str) -> str:
        """Map a principal to an identity once; return the identity that is actually stored."""

        return self._identities.setdefault(principal_id, identity)
