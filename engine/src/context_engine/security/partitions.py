"""Deterministic internal access partitions derived from mapped record audiences."""

from __future__ import annotations

import hashlib
import json

from context_engine.knowledge_backend.types import AccessPartitionRef


def partition_for(space_id: str, audiences: tuple[str, ...]) -> AccessPartitionRef:
    """Return the partition for records in a space that share exactly these audiences.

    Records with the same mapped audiences always land in the same partition, whatever the
    order of the tags they arrived with. The reference is private and never serialized.
    """

    if not space_id or not audiences:
        raise ValueError("a partition needs a space and at least one audience")
    canonical = json.dumps([space_id, sorted(set(audiences))], separators=(",", ":"))
    return AccessPartitionRef("prt_" + hashlib.sha256(canonical.encode()).hexdigest()[:32])
