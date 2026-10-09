"""Project memories a user chose to share with one connected device.

One row per consent request (the browser-approved grant request). Grants
renew every few minutes under the same consent, so the selection follows the
consent, not a token. Nothing is shared until the user checks memories.
"""

from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import GUID, BaseModel


class IntegrationContextSelection(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "integration_context_selections"
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=False
    )
    consent_id: Mapped[UUID] = mapped_column(
        GUID(),
        ForeignKey("integration_grant_requests.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Ordered memory ids as strings; revalidated against the project on every read.
    memory_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
