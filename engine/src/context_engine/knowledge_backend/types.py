"""Engine-owned values crossing the knowledge backend boundary."""

from __future__ import annotations

from dataclasses import dataclass, field


def _required(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} is required")


@dataclass(frozen=True, slots=True)
class PrincipalContext:
    """Authenticated engine principal and request trace context."""

    principal_id: str
    trace_id: str

    def __post_init__(self) -> None:
        _required(self.principal_id, "principal_id")
        _required(self.trace_id, "trace_id")


@dataclass(frozen=True, slots=True)
class AccessPartitionRef:
    """Internal isolation reference. Public serializers must reject this type."""

    value: str

    def __post_init__(self) -> None:
        _required(self.value, "access partition")


@dataclass(frozen=True, slots=True)
class BackendReference:
    """Opaque provider handle. Public serializers must reject this type."""

    value: str

    def __post_init__(self) -> None:
        _required(self.value, "backend reference")


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """Engine-owned content and lineage for one source-record version."""

    record_id: str
    source_id: str
    version: str
    content: str
    content_hash: str
    title: str | None = None
    source_url: str | None = None
    entities: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("record_id", "source_id", "version", "content", "content_hash"):
            _required(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class QueryRequest:
    """Bounded evidence retrieval request."""

    text: str
    limit: int = 10

    def __post_init__(self) -> None:
        _required(self.text, "query text")
        if not 1 <= self.limit <= 100:
            raise ValueError("limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class EnrichmentRequest:
    """Explicit versioned enrichment operation."""

    operation_id: str
    pipeline_version: str

    def __post_init__(self) -> None:
        _required(self.operation_id, "operation_id")
        _required(self.pipeline_version, "pipeline_version")


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """Authorized source-linked evidence returned by a backend."""

    evidence_id: str
    record_id: str
    source_id: str
    source_version: str
    passage: str
    score: float
    location: str | None = None
    graph_path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Result of storing or replaying one source record."""

    record_id: str
    source_version: str
    backend_reference: BackendReference
    created: bool


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Evidence result with an explicit insufficient-evidence state."""

    evidence: tuple[EvidenceItem, ...]
    insufficient_evidence: bool


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """Summary of an explicit enrichment operation."""

    operation_id: str
    affected_records: int
    created_artifacts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeletionResult:
    """Outcome and residual failures for one record deletion."""

    record_id: str
    deleted: bool
    residual_failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Safety and lifecycle operations supported by a backend."""

    supports_update: bool
    supports_enrichment: bool
    supports_record_deletion: bool
    isolation_enforced: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BackendHealth:
    """Readiness and capabilities reported by a backend."""

    ready: bool
    detail: str = ""
    capabilities: BackendCapabilities = field(
        default_factory=lambda: BackendCapabilities(False, False, False, False)
    )
