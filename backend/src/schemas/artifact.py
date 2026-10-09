"""Artifact publication contracts and stable service errors."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ArtifactError(Exception):
    """Base for stable artifact failures; routers map subclasses to HTTP."""


class ArtifactNotFound(ArtifactError):
    """Artifact, version, or upload is missing or outside the caller's scope."""


class ArtifactAccessDenied(ArtifactError):
    """Caller identity does not authorize the project."""


class ArtifactConflict(ArtifactError):
    """Replay with a different payload, stale parent, or expired reservation."""

    current_version_id: UUID | None = None


class ArtifactDigestMismatch(ArtifactError):
    """Uploaded bytes do not match the reserved size or SHA-256."""


class ArtifactTooLarge(ArtifactError):
    """Reservation exceeds the per-file limit."""


class ArtifactQuotaExceeded(ArtifactError):
    """Reservation exceeds the project quota including open reservations."""


class ArtifactStorageUnavailable(ArtifactError):
    """Object storage failed or the committed blob is missing."""


class ArtifactProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    producer: Literal["harness", "nous", "user"]
    native_item_id: str | None = Field(default=None, max_length=255)
    source_ids: list[UUID] = Field(default_factory=list, max_length=50)
    command: str | None = Field(default=None, max_length=2000)
    code_revision: str | None = Field(default=None, max_length=128)


class ReserveArtifactUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publication_id: UUID
    byte_size: int = Field(ge=0)
    mime_type: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublishVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publication_id: UUID
    upload_id: UUID
    title: str = Field(min_length=1, max_length=255)
    provenance: ArtifactProvenance
    artifact_id: UUID | None = None
    expected_parent_version_id: UUID | None = None


class ArtifactUploadDTO(BaseModel):
    upload_id: UUID
    expires_at: datetime


class ArtifactVersionDTO(BaseModel):
    artifact_id: UUID
    version_id: UUID
    parent_version_id: UUID | None
    title: str
    mime_type: str
    byte_size: int
    sha256: str
    created_at: datetime
    provenance: ArtifactProvenance


class ArtifactReferenceDTO(BaseModel):
    artifact_id: UUID
    version_id: UUID
    run_id: UUID | None
    thread_id: UUID | None
    message_id: UUID | None


class ThreadArtifactDTO(BaseModel):
    version: ArtifactVersionDTO
    reference: ArtifactReferenceDTO


class ProjectArtifactDTO(BaseModel):
    artifact_id: UUID
    title: str
    kind: str
    current_version: ArtifactVersionDTO
    thread_id: UUID | None  # chat the current version was produced in, if any
    updated_at: datetime


class EditArtifactVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_parent_version_id: UUID
    publication_id: UUID
    # The service applies the 2 MiB limit to encoded bytes as well.
    text: str = Field(max_length=2 * 1024 * 1024)


class ArtifactCapabilitiesDTO(BaseModel):
    editing_enabled: bool
    preview_enabled: bool


class ArtifactEditConflictDetail(BaseModel):
    current_version_id: UUID | None


class ArtifactEditConflictError(BaseModel):
    message: Literal["Artifact publication conflict"] = "Artifact publication conflict"
    status_code: Literal[409] = 409
    type: Literal["http_error"] = "http_error"
    details: ArtifactEditConflictDetail


class ArtifactEditConflictResponse(BaseModel):
    error: ArtifactEditConflictError
