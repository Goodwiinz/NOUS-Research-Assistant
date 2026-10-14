"""Durable native publication intents, committed with completed drafts."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import GUID, BaseModel


class NativeArtifactIntent(BaseModel):
    __tablename__ = "native_artifact_intents"
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]
    draft_id: Mapped[UUID] = mapped_column(
        GUID(),
        ForeignKey("generated_drafts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="CASCADE")
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.job_id", ondelete="CASCADE")
    )
    draft_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_token: Mapped[UUID | None] = mapped_column(GUID())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("artifact_versions.id")
    )
    last_error: Mapped[str | None] = mapped_column(String(64))
