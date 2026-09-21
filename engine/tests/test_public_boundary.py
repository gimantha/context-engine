"""Public serialization guard tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from context_engine.knowledge_backend.boundary import PrivateValueError, to_public_value
from context_engine.knowledge_backend.types import (
    AccessPartitionRef,
    BackendReference,
    EvidenceItem,
    QueryResult,
)


def test_engine_evidence_serializes_without_private_values():
    value = QueryResult(
        evidence=(
            EvidenceItem(
                evidence_id="evi-1",
                record_id="record-1",
                source_id="source-1",
                source_version="1",
                passage="safe passage",
                score=0.9,
            ),
        ),
        insufficient_evidence=False,
    )
    serialized = to_public_value(value)
    assert serialized["evidence"][0]["record_id"] == "record-1"


@pytest.mark.parametrize(
    "private_value",
    [AccessPartitionRef("partition-a"), BackendReference("native:opaque")],
)
def test_private_values_cannot_serialize(private_value):
    @dataclass(frozen=True)
    class Envelope:
        value: object

    with pytest.raises(PrivateValueError):
        to_public_value(Envelope(private_value))
