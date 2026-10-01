"""Verified draft releases (GOO-307).

A row is a person's promotion of one exact draft version: its content hash
and the claim versions and assessments that authorized it. Insert-only except
one guarded ``stale_at``/``stale_event_id`` stamp. Status is derived: no row
is ``candidate``, a live row (``stale_at IS NULL``) ``verified``, only stale
rows ``stale``. The partial unique index allows one live release per draft;
the migration ``d7f9b1c3e5a8`` freezes the same DDL.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

ACTOR_ROLE_CHECK = "actor_role IN ('adjudicator','supervisor')"
STALE_PAIR_CHECK = "(stale_at IS NULL) = (stale_event_id IS NULL)"
LIVE = "stale_at IS NULL"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


class DraftRelease(Base):
    __tablename__ = "draft_releases"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    draft_id = _fk("generated_drafts")
    draft_version = Column(Integer, nullable=False)
    content_hash = Column(String(64), nullable=False)
    claim_version_ids = Column(JSONB, nullable=False)
    assessment_ids = Column(JSONB, nullable=False)
    interpretation_claim_version_ids = Column(JSONB, nullable=False)
    protocol_version_id = _fk("research_protocol_versions", nullable=True)
    policy_version = Column(SmallInteger, nullable=False)
    promoted_by_id = _fk("users")
    actor_role = Column(String(16), nullable=False)
    rationale = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )
    stale_at = Column(DateTime(timezone=True), nullable=True)
    stale_event_id: Column = Column(GUID(), nullable=True)

    __table_args__ = (
        Index(
            "uq_draft_releases_live",
            "draft_id",
            unique=True,
            postgresql_where=sql_text(LIVE),
            sqlite_where=sql_text(LIVE),
        ),
        CheckConstraint(ACTOR_ROLE_CHECK, name="ck_draft_releases_actor_role"),
        CheckConstraint(STALE_PAIR_CHECK, name="ck_draft_releases_stale_pair"),
        Index("idx_draft_releases_collection", "collection_id"),
    )
