"""Independent decision roles for canonical research projects."""

from enum import Enum as PyEnum

from sqlalchemy import Column, Enum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class ResearchProjectRole(str, PyEnum):
    REVIEWER = "reviewer"
    ADJUDICATOR = "adjudicator"
    SUPERVISOR = "supervisor"


class ResearchProjectRoleAssignment(BaseModel):
    """An explicit, non-hierarchical decision role on a Collection."""

    __tablename__ = "research_project_role_assignments"
    __table_args__ = (
        UniqueConstraint(
            "collection_id",
            "user_id",
            "role",
            name="uq_research_project_role_assignment",
        ),
    )

    collection_id = Column(
        GUID(),
        ForeignKey("collections.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(
        Enum(
            ResearchProjectRole,
            values_callable=lambda values: [value.value for value in values],
            native_enum=True,
            name="researchprojectrole",
        ),
        nullable=False,
    )
    assigned_by_id = Column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    collection = relationship("Collection")
    user = relationship("User", foreign_keys=[user_id])
    assigned_by = relationship("User", foreign_keys=[assigned_by_id])
