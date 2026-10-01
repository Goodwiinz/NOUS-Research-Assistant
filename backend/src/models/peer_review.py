"""External peer-review rounds, comments, responses and decisions (GOO-314).

Five insert-only tables, independent of ``draft_reviews`` (machine citation
review). An external reviewer is a labelled identity inside one round; there
is no ``user_id`` and nothing maps a reviewer to a project user or role. A
round reviews one exact saved draft version (bound by content hash).
Comments and responses are versioned through ``supersedes_*`` (the tip is the
row nobody supersedes). Decisions hold two chains per comment: assignment
(``assigned``) and resolution (``resolved``/``reopened``). Status and anchor
state are derived, never stored. Migration ``d2a4c6e8f0b1`` freezes this DDL.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

ANCHOR_SHAPE_CHECK = (
    "(start_char IS NULL) = (quote IS NULL)"
    " AND (start_char IS NULL) = (end_char IS NULL)"
    " AND (start_char IS NULL) = (quote_sha256 IS NULL)"
    " AND (start_char IS NULL OR (0 <= start_char AND start_char < end_char))"
)
INITIAL_COMMENT = "supersedes_comment_id IS NULL"
RESPONSE_KIND_CHECK = "kind IN ('change','no_change')"
CHANGE_SHAPE_CHECK = (
    "(kind = 'change') = (revised_draft_id IS NOT NULL"
    " AND diff_sha256 IS NOT NULL AND rationale IS NULL)"
)
NO_CHANGE_RATIONALE_CHECK = (
    "kind = 'change' OR (rationale IS NOT NULL AND length(trim(rationale)) > 0)"
)
INITIAL_RESPONSE = "supersedes_response_id IS NULL"
DECISION_KIND_CHECK = "kind IN ('assigned','resolved','reopened')"
DECISION_ROLE_CHECK = (
    "(kind = 'assigned' AND actor_role = 'editor')"
    " OR (kind IN ('resolved','reopened') AND actor_role = 'adjudicator')"
)
DECISION_SHAPE_CHECK = (
    "(kind <> 'assigned' OR assignee_id IS NOT NULL)"
    " AND (kind <> 'resolved' OR response_id IS NOT NULL)"
)
INITIAL_DECISION = "supersedes_decision_id IS NULL"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


def _created_at() -> Column:
    return Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


class PeerReviewRound(Base):
    """One external review round of one exact saved draft version."""

    __tablename__ = "peer_review_rounds"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    draft_id = _fk("generated_drafts")
    draft_content_hash = Column(String(64), nullable=False)
    label = Column(String(255), nullable=False)
    received_at = Column(Date, nullable=True)
    created_by_id = _fk("users")
    created_at = _created_at()

    __table_args__ = (Index("idx_peer_review_rounds_collection", "collection_id"),)


class PeerReviewReviewer(Base):
    """An external reviewer: a label ("Reviewer 2"), never a project user."""

    __tablename__ = "peer_review_reviewers"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    round_id: Column = _fk("peer_review_rounds")
    label = Column(String(64), nullable=False)
    display_name = Column(String(255), nullable=True)
    created_by_id = _fk("users")
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint("round_id", "label", name="uq_peer_review_reviewers_label"),
        UniqueConstraint("id", "round_id", name="uq_peer_review_reviewers_scope"),
    )


class PeerReviewComment(Base):
    """One version of a reviewer comment, optionally anchored to a passage."""

    __tablename__ = "peer_review_comments"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    round_id: Column = Column(GUID(), nullable=False)
    reviewer_id: Column = Column(GUID(), nullable=False)
    number = Column(SmallInteger, nullable=False)
    body = Column(Text, nullable=False)
    draft_id = _fk("generated_drafts")
    draft_content_hash = Column(String(64), nullable=False)
    start_char = Column(Integer, nullable=True)
    end_char = Column(Integer, nullable=True)
    quote = Column(Text, nullable=True)
    quote_sha256 = Column(String(64), nullable=True)
    supersedes_comment_id: Column = _fk("peer_review_comments", nullable=True)
    author_id = _fk("users")
    created_at = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["reviewer_id", "round_id"],
            ["peer_review_reviewers.id", "peer_review_reviewers.round_id"],
            ondelete="RESTRICT",
            name="fk_peer_review_comments_reviewer",
        ),
        UniqueConstraint(
            "supersedes_comment_id", name="uq_peer_review_comments_supersedes"
        ),
        Index(
            "uq_peer_review_comments_initial",
            "round_id",
            "number",
            unique=True,
            postgresql_where=sql_text(INITIAL_COMMENT),
            sqlite_where=sql_text(INITIAL_COMMENT),
        ),
        Index("idx_peer_review_comments_round", "round_id"),
        CheckConstraint("number >= 1", name="ck_peer_review_comments_number"),
        CheckConstraint(ANCHOR_SHAPE_CHECK, name="ck_peer_review_comments_anchor"),
    )


class PeerReviewResponse(Base):
    """One version of the authors' response to a comment (by root id)."""

    __tablename__ = "peer_review_responses"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    comment_root_id: Column = _fk("peer_review_comments")
    kind = Column(String(16), nullable=False)
    body = Column(Text, nullable=False)
    revised_draft_id = _fk("generated_drafts", nullable=True)
    revised_content_hash = Column(String(64), nullable=True)
    base_draft_id = _fk("generated_drafts", nullable=True)
    diff_sha256 = Column(String(64), nullable=True)
    rationale = Column(Text, nullable=True)
    evidence_claim_version_ids = Column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    supersedes_response_id: Column = _fk("peer_review_responses", nullable=True)
    author_id = _fk("users")
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "supersedes_response_id", name="uq_peer_review_responses_supersedes"
        ),
        Index(
            "uq_peer_review_responses_initial",
            "comment_root_id",
            unique=True,
            postgresql_where=sql_text(INITIAL_RESPONSE),
            sqlite_where=sql_text(INITIAL_RESPONSE),
        ),
        CheckConstraint(RESPONSE_KIND_CHECK, name="ck_peer_review_responses_kind"),
        CheckConstraint(CHANGE_SHAPE_CHECK, name="ck_peer_review_responses_change"),
        CheckConstraint(
            NO_CHANGE_RATIONALE_CHECK, name="ck_peer_review_responses_rationale"
        ),
    )


class PeerReviewDecision(Base):
    """An assignment (EDIT) or a resolution/reopening (ADJUDICATE)."""

    __tablename__ = "peer_review_decisions"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    comment_root_id: Column = _fk("peer_review_comments")
    kind = Column(String(16), nullable=False)
    assignee_id = _fk("users", nullable=True)
    response_id = _fk("peer_review_responses", nullable=True)
    rationale = Column(Text, nullable=True)
    actor_id = _fk("users")
    actor_role = Column(String(16), nullable=False)
    supersedes_decision_id: Column = _fk("peer_review_decisions", nullable=True)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "supersedes_decision_id", name="uq_peer_review_decisions_supersedes"
        ),
        # One initial row per chain: assignment vs resolution.
        Index(
            "uq_peer_review_decisions_initial",
            "comment_root_id",
            sql_text("(kind = 'assigned')"),
            unique=True,
            postgresql_where=sql_text(INITIAL_DECISION),
            sqlite_where=sql_text(INITIAL_DECISION),
        ),
        CheckConstraint(DECISION_KIND_CHECK, name="ck_peer_review_decisions_kind"),
        CheckConstraint(DECISION_ROLE_CHECK, name="ck_peer_review_decisions_role"),
        CheckConstraint(DECISION_SHAPE_CHECK, name="ck_peer_review_decisions_shape"),
    )
