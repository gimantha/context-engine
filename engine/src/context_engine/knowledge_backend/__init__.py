"""Provider-neutral knowledge backend boundary."""

from .dummy import DummyKnowledgeBackend
from .errors import BackendError, BackendErrorCode
from .port import KnowledgeBackend
from .state import BackendStateStore, InMemoryBackendState
from .types import (
    AccessPartitionRef,
    BackendCapabilities,
    BackendHealth,
    BackendReference,
    DeletionResult,
    EnrichmentRequest,
    EnrichmentResult,
    EvidenceItem,
    ExpectedRecord,
    IndexingProgress,
    IndexingProgressRequest,
    IngestionResult,
    PrincipalContext,
    QueryRequest,
    QueryResult,
    SourceRecord,
)

__all__ = [
    "AccessPartitionRef",
    "BackendCapabilities",
    "BackendError",
    "BackendErrorCode",
    "BackendHealth",
    "BackendReference",
    "BackendStateStore",
    "DeletionResult",
    "EnrichmentRequest",
    "EnrichmentResult",
    "EvidenceItem",
    "ExpectedRecord",
    "DummyKnowledgeBackend",
    "IndexingProgress",
    "IndexingProgressRequest",
    "InMemoryBackendState",
    "IngestionResult",
    "KnowledgeBackend",
    "PrincipalContext",
    "QueryRequest",
    "QueryResult",
    "SourceRecord",
]
