"""Shared source-record fixtures for engine tests."""

from __future__ import annotations

import hashlib

import pytest

from context_engine.knowledge_backend import SourceRecord


def make_record(
    record_id: str,
    version: str,
    content: str,
    *,
    source_id: str = "source-incidents",
    title: str | None = None,
    entities: tuple[str, ...] = (),
) -> SourceRecord:
    return SourceRecord(
        record_id=record_id,
        source_id=source_id,
        version=version,
        content=content,
        content_hash="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        title=title,
        source_url=f"https://sources.invalid/{record_id}",
        entities=entities,
    )


@pytest.fixture
def record_factory():
    return make_record
