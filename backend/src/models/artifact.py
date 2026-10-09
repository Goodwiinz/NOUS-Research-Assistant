"""Durable generated-output artifacts: private bytes, immutable versions, references.

Artifacts are separate from knowledge documents and never indexed. Access is
project-scoped through the owning workspace; public workspace visibility alone
grants nothing.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import GUID, BaseModel


class Artifact(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "artifacts"
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False, index=True
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=False, index=True
    )
    owner_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # Not a FK: versions point at artifacts, so the pointer would be circular.
    current_version_id: Mapped[UUID | None] = mapped_column(GUID())


class ArtifactVersion(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]
    __tablename__ = "artifact_versions"
    artifact_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("artifacts.id"), nullable=False, index=True
    )
    parent_version_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("artifact_versions.id")
    )
    upload_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("artifact_uploads.id"), nullable=False, unique=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Private object key; never exposed through a DTO.
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    producer: Mapped[str] = mapped_column(String(16), nullable=False)
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.job_id")
    )
    thread_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="SET NULL")
    )
    grant_id: Mapped[UUID | None] = mapped_column(GUID())


class ArtifactUpload(BaseModel):
    """Reservation for one publication; bytes land before metadata commits."""

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]
    __tablename__ = "artifact_uploads"
    __table_args__ = (
        UniqueConstraint(
            "grant_id", "publication_id", name="uq_artifact_upload_publication"
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=False, index=True
    )
    grant_id: Mapped[UUID] = mapped_column(GUID(), nullable=False)
    publication_id: Mapped[UUID] = mapped_column(GUID(), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Canonical hash of the reserve request, then of the publish request.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    publish_hash: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(String(512))
    stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    version_id: Mapped[UUID | None] = mapped_column(GUID())


class ArtifactReference(BaseModel):
    """Where a version was produced; loaded with transcripts, not from events."""

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]
    __tablename__ = "artifact_references"
    artifact_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("artifacts.id"), nullable=False
    )
    version_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("artifact_versions.id"), nullable=False, unique=True
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.job_id")
    )
    thread_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="SET NULL"), index=True
    )
    message_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("chat_messages.id", ondelete="SET NULL")
    )


class ArtifactLifecycleOutbox(BaseModel):
    """One announcement intent per committed version; delivered by the drain."""

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]
    __tablename__ = "artifact_lifecycle_outbox"
    __table_args__ = (
        UniqueConstraint(
            "version_id", "kind", name="uq_artifact_lifecycle_version_kind"
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    artifact_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("artifacts.id"), nullable=False
    )
    version_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("artifact_versions.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.job_id")
    )
    thread_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="SET NULL")
    )
    # pending -> delivered (event appended) | skipped (no run, or run already closed)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(200))
