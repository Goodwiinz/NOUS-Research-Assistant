"""
SQLAlchemy models for Evidence Agreement Meter
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.sql import func

from .base import GUID
from .base import BaseModel as SQLBaseModel


class StanceEnum(str, Enum):
    """Stance classification options"""

    SUPPORTING = "supporting"
    OPPOSING = "opposing"
    NEUTRAL = "neutral"
    NOT_ADDRESSED = "not_addressed"


class StanceClassificationModel(SQLBaseModel):
    """
    SQLAlchemy model for stance classifications

    Stores individual source classifications on claims with confidence scores
    and justification excerpts. Stores the classifier pipeline namespace and the
    exact inference model used for reproducibility.
    """

    __tablename__ = "stance_classifications"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    claim_hash = Column(
        String(64),
        nullable=False,
        index=True,
        doc="SHA256 hash of normalized claim text",
    )
    claim_text = Column(Text, nullable=True, doc="Original normalized-input claim text")
    source_id = Column(
        GUID(), nullable=False, index=True, doc="UUID of the source document"
    )
    source_content_hash = Column(
        String(64), nullable=True, doc="Content revision classified for this row"
    )
    organization_id = Column(
        GUID(),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc="Owning org — tenant boundary for stance classifications",
    )
    stance = Column(
        SQLEnum(StanceEnum),
        nullable=False,
        doc="Classification of source's stance on claim",
    )
    confidence = Column(
        Float, nullable=False, doc="Confidence score between 0.0 and 1.0"
    )
    justification_excerpt = Column(
        Text, nullable=True, doc="Text excerpt that justifies the stance classification"
    )
    model_version = Column(
        String(50),
        nullable=False,
        doc="Classifier pipeline version used for classification and uniqueness",
    )
    inference_model_version = Column(
        String(100),
        nullable=True,
        doc="Exact inference model that produced the selected classification",
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Add check constraints for data validation
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_stance_classifications_confidence",
        ),
        # One classification per (claim, source, model, org). organization_id is part of
        # the key so two orgs analyzing the same claim are independent rows — never collide
        # or overwrite each other (the tenant boundary). A plain organization_id index is
        # created implicitly by index=True on the column above.
        UniqueConstraint(
            "claim_hash",
            "source_id",
            "model_version",
            "organization_id",
            name="uq_stance_classifications_org_claim_src_model",
        ),
        # The same key as a unique index, created by add_org_id_stance_class
        # (add_org_id_to_stance_classifications) for databases that predate the
        # constraint. Redundant with the constraint, but every migrated
        # database has both.
        Index(
            "ux_stance_classifications_org_claim_src_model",
            "claim_hash",
            "source_id",
            "model_version",
            "organization_id",
            unique=True,
        ),
        {"extend_existing": True},
    )

    def __repr__(self):
        return (
            f"<StanceClassification(id={self.id}, "
            f"claim_hash={self.claim_hash[:8]}..., "
            f"source_id={self.source_id}, "
            f"organization_id={self.organization_id}, "
            f"stance={self.stance.value}, "
            f"confidence={self.confidence:.2f})>"
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        return {
            "id": str(self.id),
            "claim_hash": self.claim_hash,
            "claim_text": self.claim_text,
            "source_id": str(self.source_id),
            "source_content_hash": self.source_content_hash,
            "organization_id": (
                str(self.organization_id) if self.organization_id else None
            ),
            "stance": getattr(self.stance, "value", self.stance),
            "confidence": self.confidence,
            "justification_excerpt": self.justification_excerpt,
            "model_version": self.model_version,
            "inference_model_version": self.inference_model_version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_classification(
        cls, classification_data: dict, claim_hash: str, classifier_version: str
    ):
        """Create instance from stance classification result"""
        return cls(
            claim_hash=claim_hash,
            source_id=classification_data["source_id"],
            stance=StanceEnum(classification_data["stance"]),
            confidence=classification_data["confidence"],
            justification_excerpt=classification_data.get("justification_excerpt"),
            model_version=classifier_version,
            inference_model_version=classification_data["model_version"],
        )
