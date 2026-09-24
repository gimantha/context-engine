"""Private access-partition references derived from mapped record audiences."""

from __future__ import annotations

from collections.abc import Iterable

from context_engine.domain import partition_key
from context_engine.knowledge_backend.types import AccessPartitionRef


def partition_for(space_id: str, audiences: Iterable[str]) -> AccessPartitionRef:
    """Return the partition reference for records in a space that share these audiences."""

    return AccessPartitionRef(partition_key(space_id, audiences))
