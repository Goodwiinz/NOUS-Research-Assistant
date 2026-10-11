"""Explicit authenticated audiences for immutable artifact versions."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import GUID, BaseModel


class ArtifactShare(BaseModel):
    __tablename__ = "artifact_shares"

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    creator_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_id: Mapped[UUID] = mapped_column(
        GUID(),
        ForeignKey("artifact_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArtifactShareAudience(BaseModel):
    __tablename__ = "artifact_share_audiences"
    __table_args__ = (
        UniqueConstraint("share_id", "user_id", name="uq_artifact_share_audience_user"),
    )

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]
    share_id: Mapped[UUID] = mapped_column(
        GUID(),
        ForeignKey("artifact_shares.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
