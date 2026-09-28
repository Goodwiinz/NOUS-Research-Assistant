"""
ResearchRun model for tracking execution of a research blueprint.
"""

from enum import Enum as PyEnum

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .base import GUID, BaseModel


class RunStatus(PyEnum):
    """Status of a research run."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class ResearchRun(BaseModel):
    """A single execution of a research blueprint."""

    __tablename__ = "research_runs"

    blueprint_id = Column(GUID(), ForeignKey("research_blueprints.id"), nullable=False)
    blueprint_version = Column(Integer, nullable=False)
    protocol_version_id = Column(
        GUID(),
        ForeignKey("research_protocol_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    effective_plan_hash = Column(String(64), nullable=True)
    conformance_status = Column(
        String(32),
        nullable=False,
        default="legacy_unbound",
        server_default="legacy_unbound",
    )
    status = Column(String(50), nullable=False, default="pending")
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    reproducibility_manifest = Column(JSONB, nullable=True)
    total_tokens = Column(Integer, nullable=False, default=0)

    def __init__(self, **kwargs):
        if "status" not in kwargs:
            kwargs["status"] = "pending"
        if "total_tokens" not in kwargs:
            kwargs["total_tokens"] = 0
        if "conformance_status" not in kwargs:
            kwargs["conformance_status"] = "legacy_unbound"
        super().__init__(**kwargs)

    # Relationships
    blueprint = relationship("ResearchBlueprint", back_populates="runs")
    steps = relationship(
        "ResearchStep",
        back_populates="run",
        cascade="all, delete-orphan",
    )
    sources = relationship(
        "ResearchSource",
        back_populates="run",
        cascade="all, delete-orphan",
    )
