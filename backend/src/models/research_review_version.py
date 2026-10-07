"""Superseding review versions and their release links (GOO-320).

Two insert-only tables (GOO-309's trigger refuses UPDATE and DELETE):

- ``research_review_versions``: one linear chain per Collection. The root
  (``version_number`` 1, no parent, no delta) freezes the project's corpus,
  decisions and PRISMA hash; each successor names its parent and exactly one
  accepted GOO-319 execution delta. ``UNIQUE(parent_review_version_id)``
  keeps the chain linear and ``UNIQUE(accepted_execution_id)`` lets a delta
  be accepted once. Decisions are carried by reference (resolution and
  event ids), never copied. Work status, accounting and staleness are
  derived on read; nothing here is ever stamped.
- ``research_review_release_links``: one GOO-315 release per version, naming
  the release linked to the parent version it supersedes. The parent's
  release row is never touched.

Never ``ResearchRun`` (one engine execution) or ``GeneratedDraft.version``
(a draft counter). The migration ``d4a6c8e0f2b3`` freezes the same DDL.
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
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

ROOT_SHAPE_CHECK = (
    "(parent_review_version_id IS NULL) = (accepted_execution_id IS NULL)"
    " AND (accepted_execution_id IS NULL) = (delta_hash IS NULL)"
    " AND (parent_review_version_id IS NULL) = (version_number = 1)"
)
ROOT_WHERE = "parent_review_version_id IS NULL"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


def _created_at() -> Column:
    return Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


class ResearchReviewVersion(Base):
    __tablename__ = "research_review_versions"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    version_number = Column(Integer, nullable=False)
    parent_review_version_id: Column = _fk("research_review_versions", nullable=True)
    accepted_execution_id = _fk("research_search_executions", nullable=True)
    delta_hash = Column(String(64), nullable=True)
    protocol_version_id = _fk("research_protocol_versions")
    strategy_version = Column(String(80), nullable=True)
    input_versions = Column(JSONB, nullable=False)
    report_ids = Column(JSONB, nullable=False)
    carried = Column(JSONB, nullable=False)
    required_work = Column(JSONB, nullable=False)
    needs_attention = Column(JSONB, nullable=False)
    missing_history = Column(JSONB, nullable=False)
    prisma_body_hash = Column(String(64), nullable=False)
    content_hash = Column(String(64), nullable=False)
    rationale = Column(Text, nullable=False)
    created_by_id = _fk("users")
    actor_role = Column(String(16), nullable=False)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "parent_review_version_id", name="uq_research_review_versions_parent"
        ),
        UniqueConstraint(
            "accepted_execution_id", name="uq_research_review_versions_execution"
        ),
        Index(
            "uq_review_version_root",
            "collection_id",
            unique=True,
            postgresql_where=sql_text(ROOT_WHERE),
            sqlite_where=sql_text(ROOT_WHERE),
        ),
        CheckConstraint(ROOT_SHAPE_CHECK, name="ck_research_review_versions_root"),
        CheckConstraint(
            "actor_role = 'supervisor'", name="ck_research_review_versions_role"
        ),
        Index("idx_research_review_versions_collection", "collection_id"),
    )


class ResearchReviewReleaseLink(Base):
    __tablename__ = "research_review_release_links"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    review_version_id = _fk("research_review_versions")
    release_id = _fk("manuscript_releases")
    supersedes_release_id = _fk("manuscript_releases", nullable=True)
    linked_by_id = _fk("users")
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "review_version_id", name="uq_research_review_release_links_version"
        ),
        UniqueConstraint(
            "supersedes_release_id",
            name="uq_research_review_release_links_supersedes",
        ),
        Index("idx_research_review_release_links_collection", "collection_id"),
    )
