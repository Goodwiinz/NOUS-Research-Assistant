"""Versioned structured handoff left by an external harness in one chat."""

from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import GUID, BaseModel


class IntegrationHandoff(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "integration_handoffs"
    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "project_id",
            "version",
            name="uq_integration_handoffs_thread_project_version",
        ),
        # Per thread, never global: a global key would let tenants probe ids.
        UniqueConstraint(
            "thread_id",
            "project_id",
            "handoff_id",
            name="uq_integration_handoffs_thread_project_handoff",
        ),
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=False
    )
    thread_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    handoff_id: Mapped[UUID] = mapped_column(GUID(), nullable=False)
    goal: Mapped[str] = mapped_column(String(2000), nullable=False)
    decisions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    remaining: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # [{"artifact_version_id": str, "summary": str}]
    results: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False)
    harness_name: Mapped[str] = mapped_column(String(64), nullable=False)
    harness_session_id: Mapped[str | None] = mapped_column(String(128))
    grant_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("integration_grants.id"), nullable=False
    )
    consent_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("integration_grant_requests.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
