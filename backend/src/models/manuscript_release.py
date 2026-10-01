"""Immutable candidate and verified manuscript releases (GOO-315).

One insert-only row per packaged snapshot of one exact saved draft version.
A ``candidate`` is an editor's build and exposes every unresolved check; a
``verified`` row is an adjudicator's or supervisor's promotion of exactly one
candidate (``UNIQUE(candidate_release_id)``) and names the live GOO-307
``draft_release`` it relied on. Status is derived on read, never stamped. The
migration ``e4c6a8b0d2f3`` freezes the same DDL plus the insert-only trigger.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

STAGE_CHECK = "stage IN ('candidate','verified')"
ACTOR_ROLE_CHECK = "actor_role IN ('editor','adjudicator','supervisor')"
VERIFIED_SHAPE = (
    "(stage = 'verified')"
    " = (candidate_release_id IS NOT NULL AND draft_release_id IS NOT NULL)"
)
STAGE_ROLE = (
    "(stage = 'candidate' AND actor_role = 'editor')"
    " OR (stage = 'verified' AND actor_role IN ('adjudicator','supervisor'))"
)


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


class ManuscriptRelease(Base):
    __tablename__ = "manuscript_releases"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    draft_id = _fk("generated_drafts")
    draft_version = Column(Integer, nullable=False)
    content_hash = Column(String(64), nullable=False)
    stage = Column(String(16), nullable=False)
    candidate_release_id: Column = _fk("manuscript_releases", nullable=True)
    draft_release_id = _fk("draft_releases", nullable=True)
    snapshot = Column(JSONB, nullable=False)
    snapshot_hash = Column(String(64), nullable=False)
    checks = Column(JSONB, nullable=False)
    checks_hash = Column(String(64), nullable=False)
    package_files = Column(JSONB, nullable=False)
    package_sha256 = Column(String(64), nullable=False)
    package_storage_key = Column(String(512), nullable=False)
    created_by_id = _fk("users")
    actor_role = Column(String(16), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "candidate_release_id", name="uq_manuscript_releases_candidate"
        ),
        CheckConstraint(STAGE_CHECK, name="ck_manuscript_releases_stage"),
        CheckConstraint(ACTOR_ROLE_CHECK, name="ck_manuscript_releases_actor_role"),
        CheckConstraint(VERIFIED_SHAPE, name="ck_manuscript_releases_verified"),
        CheckConstraint(STAGE_ROLE, name="ck_manuscript_releases_stage_role"),
        Index("idx_manuscript_releases_collection", "collection_id"),
        Index("idx_manuscript_releases_draft", "draft_id"),
    )
