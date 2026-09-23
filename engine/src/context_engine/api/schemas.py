"""REST request and response models using only engine-owned concepts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, model_validator

from context_engine.domain import (
    Action,
    ContextSpace,
    Grant,
    IngestionCommand,
    Job,
    RecordStatus,
    Source,
    SourceCheckpoint,
    StagedUpload,
)
from context_engine.security.identity import AuthenticatedPrincipal

_GRANT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"


class _ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class CreateContextSpaceRequest(_ApiModel):
    """Validate a public request to create a context space."""

    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str | None, Field(max_length=1000)] = None


class ContextSpaceResponse(_ApiModel):
    """Represent a context space without internal storage details."""

    id: str
    name: str
    description: str | None = None
    state: str
    created_at: datetime = Field(alias="createdAt")

    @classmethod
    def from_domain(cls, value: ContextSpace) -> ContextSpaceResponse:
        """Translate a domain context space into its REST representation."""

        return cls(
            id=value.id,
            name=value.name,
            description=value.description,
            state=value.state.value,
            createdAt=value.created_at,
        )


class IngestionRequest(_ApiModel):
    """Validate the canonical public ingestion envelope."""

    schema_version: Literal["1"] = Field(alias="schemaVersion")
    space_id: Annotated[str, Field(min_length=1, max_length=200)] = Field(alias="spaceId")
    source_id: Annotated[str, Field(min_length=1, max_length=200)] = Field(alias="sourceId")
    source_record_id: Annotated[str, Field(min_length=1, max_length=500)] = Field(
        alias="sourceRecordId"
    )
    source_version: Annotated[str, Field(min_length=1, max_length=200)] = Field(
        alias="sourceVersion"
    )
    operation: Literal["upsert", "delete", "acl_changed"]
    content_type: Annotated[str | None, Field(max_length=200)] = Field(
        default=None, alias="contentType"
    )
    content_ref: Annotated[str | None, Field(min_length=1, max_length=1000)] = Field(
        default=None, alias="contentRef"
    )
    source_url: Annotated[AnyUrl | None, Field(max_length=2000)] = Field(
        default=None, alias="sourceUrl"
    )
    content_hash: Annotated[str | None, Field(pattern=r"^sha256:[0-9a-f]{64}$")] = Field(
        default=None, alias="contentHash"
    )
    source_observed_at: datetime = Field(alias="sourceObservedAt")
    audience: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=300)]], Field(min_length=1)
    ]
    source_acl_version: Annotated[str, Field(min_length=1, max_length=200)] = Field(
        alias="sourceAclVersion"
    )
    idempotency_key: Annotated[str, Field(min_length=8, max_length=500)] = Field(
        alias="idempotencyKey"
    )

    @model_validator(mode="after")
    def validate_operation_content(self) -> IngestionRequest:
        """Enforce operation-specific content and audience requirements."""

        if len(set(self.audience)) != len(self.audience):
            raise ValueError("audience values must be unique")
        if self.operation == "upsert" and not all(
            (self.content_type, self.content_ref, self.content_hash)
        ):
            raise ValueError("upsert requires contentType, contentRef, and contentHash")
        return self

    def to_command(self) -> IngestionCommand:
        """Translate the validated request into an application command."""

        return IngestionCommand(
            schema_version=self.schema_version,
            space_id=self.space_id,
            source_id=self.source_id,
            source_record_id=self.source_record_id,
            source_version=self.source_version,
            operation=self.operation,
            source_observed_at=self.source_observed_at,
            audience=tuple(self.audience),
            source_acl_version=self.source_acl_version,
            idempotency_key=self.idempotency_key,
            content_type=self.content_type,
            content_ref=self.content_ref,
            source_url=str(self.source_url) if self.source_url else None,
            content_hash=self.content_hash,
        )


class JobAcceptedResponse(_ApiModel):
    """Return the stable handle for accepted asynchronous work."""

    job_id: str = Field(alias="jobId")
    status_url: str = Field(alias="statusUrl")


class ErrorResponse(_ApiModel):
    """Return a stable caller-safe REST error."""

    code: str
    message: str
    trace_id: str = Field(alias="traceId")


class JobErrorResponse(_ApiModel):
    """Describe the caller-safe failure associated with a job."""

    code: str
    message: str
    trace_id: str = Field(alias="traceId")


class JobResponse(_ApiModel):
    """Expose public job state without its payload or lease details."""

    id: str
    state: str
    operation: str
    trace_id: str = Field(alias="traceId")
    attempt_count: int = Field(alias="attemptCount")
    created_at: datetime = Field(alias="createdAt")
    error: JobErrorResponse | None = None

    @classmethod
    def from_domain(cls, value: Job) -> JobResponse:
        """Translate an internal job into its public status representation."""

        error = None
        if value.error_code and value.error_message:
            error = JobErrorResponse(
                code=value.error_code,
                message=value.error_message,
                traceId=value.trace_id,
            )
        return cls(
            id=value.id,
            state=value.public_state,
            operation=value.operation.value,
            traceId=value.trace_id,
            attemptCount=value.attempt_count,
            createdAt=value.created_at,
            error=error,
        )


class HealthResponse(_ApiModel):
    """Report process liveness or readiness."""

    status: Literal["ok", "unavailable"]


class PrincipalResponse(_ApiModel):
    """Describe the calling principal as the engine resolved it from the credential."""

    id: str
    kind: str
    email: str | None = None
    groups: list[str]

    @classmethod
    def from_principal(cls, value: AuthenticatedPrincipal) -> PrincipalResponse:
        """Translate the authenticated principal into its REST representation."""

        return cls(
            id=value.principal_id,
            kind=value.kind.value,
            email=value.email,
            groups=sorted(value.groups),
        )


class EffectivePermissionsResponse(_ApiModel):
    """Return the caller's effective actions on one visible resource."""

    resource_id: str = Field(alias="resourceId")
    actions: list[str]


class PutGrantRequest(_ApiModel):
    """Validate a grant body; the actor is never part of it, only the target."""

    actions: Annotated[list[str], Field(min_length=1, max_length=len(Action))]
    principal_id: Annotated[str | None, Field(min_length=1, max_length=200)] = Field(
        default=None, alias="principalId"
    )
    group: Annotated[str | None, Field(min_length=1, max_length=300)] = None

    @model_validator(mode="after")
    def validate_subject_and_actions(self) -> PutGrantRequest:
        """Require exactly one subject and only known engine actions."""

        if (self.principal_id is None) == (self.group is None):
            raise ValueError("exactly one of principalId or group is required")
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("actions must be unique")
        known = {item.value for item in Action}
        if not set(self.actions) <= known:
            raise ValueError("actions must be engine actions")
        return self

    def to_actions(self) -> frozenset[Action]:
        """Return the validated actions as domain values."""

        return frozenset(Action(item) for item in self.actions)


class GrantResponse(_ApiModel):
    """Represent one grant without exposing who created it."""

    id: str
    resource_id: str = Field(alias="resourceId")
    actions: list[str]
    principal_id: str | None = Field(default=None, alias="principalId")
    group: str | None = None

    @classmethod
    def from_domain(cls, value: Grant) -> GrantResponse:
        """Translate a domain grant into its REST representation."""

        return cls(
            id=value.id,
            resourceId=value.resource_id,
            actions=sorted(item.value for item in value.actions),
            principalId=value.principal_id,
            group=value.group,
        )


_MappingKey = Annotated[str, Field(min_length=1, max_length=300)]
_MappingValue = Annotated[str, Field(min_length=1, max_length=300)]


class RegisterSourceRequest(_ApiModel):
    """Validate a source registration; the audience mapping is write-only configuration."""

    name: Annotated[str, Field(min_length=1, max_length=120)]
    type: Annotated[str, Field(min_length=1, max_length=60)]
    audience_mapping: Annotated[dict[_MappingKey, _MappingValue], Field(max_length=500)] = Field(
        alias="audienceMapping"
    )
    version_ordering: Literal["numeric", "lexicographic"] = Field(
        default="numeric", alias="versionOrdering"
    )


class UpdateSourceRequest(_ApiModel):
    """Validate a partial source update; at least one field must be present."""

    name: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    state: Literal["ready", "paused"] | None = None
    audience_mapping: Annotated[dict[_MappingKey, _MappingValue] | None, Field(max_length=500)] = (
        Field(default=None, alias="audienceMapping")
    )

    @model_validator(mode="after")
    def require_a_change(self) -> UpdateSourceRequest:
        """Reject an empty update."""

        if self.name is None and self.state is None and self.audience_mapping is None:
            raise ValueError("at least one field is required")
        return self


class SourceResponse(_ApiModel):
    """Represent a source without its audience mapping or internal bindings."""

    id: str
    space_id: str = Field(alias="spaceId")
    name: str
    type: str
    state: str
    version_ordering: str = Field(alias="versionOrdering")
    created_at: datetime = Field(alias="createdAt")

    @classmethod
    def from_domain(cls, value: Source) -> SourceResponse:
        """Translate a domain source into its REST representation."""

        return cls(
            id=value.id,
            spaceId=value.space_id,
            name=value.name,
            type=value.type,
            state=value.state.value,
            versionOrdering=value.version_ordering.value,
            createdAt=value.created_at,
        )


class PutCheckpointRequest(_ApiModel):
    """Validate a connector cursor."""

    cursor: Annotated[str, Field(min_length=1, max_length=4000)]


class CheckpointResponse(_ApiModel):
    """Return the stored connector cursor."""

    source_id: str = Field(alias="sourceId")
    cursor: str
    updated_at: datetime = Field(alias="updatedAt")

    @classmethod
    def from_domain(cls, value: SourceCheckpoint) -> CheckpointResponse:
        """Translate a checkpoint into its REST representation."""

        return cls(sourceId=value.source_id, cursor=value.cursor, updatedAt=value.updated_at)


class UploadResponse(_ApiModel):
    """Return the staged-object handle an ingestion event references as contentRef."""

    upload_id: str = Field(alias="uploadId")
    source_id: str = Field(alias="sourceId")
    content_type: str = Field(alias="contentType")
    size_bytes: int = Field(alias="sizeBytes")
    content_hash: str = Field(alias="contentHash")
    expires_at: datetime = Field(alias="expiresAt")

    @classmethod
    def from_domain(cls, value: StagedUpload) -> UploadResponse:
        """Translate a staged upload into its REST representation."""

        return cls(
            uploadId=value.id,
            sourceId=value.source_id,
            contentType=value.content_type,
            sizeBytes=value.size_bytes,
            contentHash=value.content_hash,
            expiresAt=value.expires_at,
        )


class RecordStatusResponse(_ApiModel):
    """Expose a record's lifecycle without content, audiences, or backend references."""

    space_id: str = Field(alias="spaceId")
    source_id: str = Field(alias="sourceId")
    record_id: str = Field(alias="recordId")
    state: str
    current_version: str = Field(alias="currentVersion")
    source_acl_version: str = Field(alias="sourceAclVersion")
    content_hash: str | None = Field(default=None, alias="contentHash")
    quarantine_reason: str | None = Field(default=None, alias="quarantineReason")
    updated_at: datetime = Field(alias="updatedAt")

    @classmethod
    def from_domain(cls, value: RecordStatus) -> RecordStatusResponse:
        """Translate a ledger record into its REST representation."""

        return cls(
            spaceId=value.space_id,
            sourceId=value.source_id,
            recordId=value.source_record_id,
            state=value.state.value,
            currentVersion=value.current_version,
            sourceAclVersion=value.source_acl_version,
            contentHash=value.content_hash,
            quarantineReason=value.quarantine_reason,
            updatedAt=value.updated_at,
        )
