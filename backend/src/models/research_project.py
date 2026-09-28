"""
ResearchProject model for organizing research activities.
"""

from sqlalchemy import Column, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class ResearchProject(BaseModel):
    """A research project owned by a user."""

    __tablename__ = "research_projects"

    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(GUID(), ForeignKey("users.id"), nullable=False)
    collection_id = Column(
        GUID(),
        ForeignKey("collections.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    status = Column(String(50), nullable=False, default="active")
    settings = Column(JSONB, nullable=True)

    def __init__(self, **kwargs):
        if "status" not in kwargs:
            kwargs["status"] = "active"
        super().__init__(**kwargs)

    # Relationships
    owner = relationship("User", backref="research_projects")
    collection = relationship("Collection", backref="research_engine_project")
    blueprints = relationship(
        "ResearchBlueprint",
        back_populates="project",
        cascade="all, delete-orphan",
    )
