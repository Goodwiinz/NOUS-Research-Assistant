"""Statement sets, author approvals, ORCID receipts and venue checks (GOO-316).

All four tables are insert-only (GOO-309's trigger in ``f6a8c0d2e4b5``).

- ``manuscript_statement_sets``: one versioned document per Collection
  (``UNIQUE(supersedes_set_id)`` plus a partial unique initial set).
- ``manuscript_statement_approvals``: one per author per set, bound to the
  set hash; ``in_app_self`` or a ``recorded_attestation`` with a note.
- ``orcid_authentications``: a user's non-secret OAuth receipt. Never an
  access, refresh or ID token (only the ID token's sha256).
- ``venue_checks``: one profile run bound to an exact package hash.

Authorship grants no permission: nothing in ``project_access`` reads these.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

INITIAL_SET = "supersedes_set_id IS NULL"
APPROVAL_METHOD_CHECK = "method IN ('in_app_self','recorded_attestation')"
ATTESTATION_NOTE_CHECK = (
    "method <> 'recorded_attestation' OR attestation_note IS NOT NULL"
)
ORCID_CHECK = r"orcid ~ '^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$'"
ENVIRONMENT_CHECK = "environment IN ('sandbox','production')"
VENUE_STATUS_CHECK = "status IN ('pass','fail')"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


def _created_at() -> Column:
    return Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


class ManuscriptStatementSet(Base):
    __tablename__ = "manuscript_statement_sets"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    body = Column(JSONB, nullable=False)
    set_hash = Column(String(64), nullable=False)
    schema = Column(String(32), nullable=False, default="nous.statements/1")
    supersedes_set_id: Column = _fk("manuscript_statement_sets", nullable=True)
    created_by_id = _fk("users")
    actor_role = Column(String(16), nullable=False, default="editor")
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "supersedes_set_id", name="uq_manuscript_statement_sets_supersedes"
        ),
        UniqueConstraint("id", "set_hash", name="uq_manuscript_statement_sets_hash"),
        Index(
            "uq_manuscript_statement_sets_initial",
            "collection_id",
            unique=True,
            postgresql_where=sql_text(INITIAL_SET),
            sqlite_where=sql_text(INITIAL_SET),
        ),
        CheckConstraint(
            "actor_role = 'editor'", name="ck_manuscript_statement_sets_role"
        ),
        Index("idx_manuscript_statement_sets_collection", "collection_id"),
    )


class ManuscriptStatementApproval(Base):
    __tablename__ = "manuscript_statement_approvals"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    statement_set_id: Column = Column(GUID(), nullable=False)
    author_key = Column(String(64), nullable=False)
    set_hash = Column(String(64), nullable=False)
    method = Column(String(24), nullable=False)
    approved_by_id = _fk("users")
    attestation_note = Column(Text, nullable=True)
    created_at = _created_at()

    __table_args__ = (
        # An approval binds the exact hash of the set it names.
        ForeignKeyConstraint(
            ["statement_set_id", "set_hash"],
            ["manuscript_statement_sets.id", "manuscript_statement_sets.set_hash"],
            ondelete="RESTRICT",
            name="fk_manuscript_statement_approvals_set",
        ),
        UniqueConstraint(
            "statement_set_id",
            "author_key",
            name="uq_manuscript_statement_approvals_author",
        ),
        CheckConstraint(
            APPROVAL_METHOD_CHECK, name="ck_manuscript_statement_approvals_method"
        ),
        CheckConstraint(
            ATTESTATION_NOTE_CHECK, name="ck_manuscript_statement_approvals_note"
        ),
    )


class OrcidAuthentication(Base):
    __tablename__ = "orcid_authentications"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id = _fk("users")
    orcid = Column(String(19), nullable=False)
    environment = Column(String(16), nullable=False)
    scope = Column(String(64), nullable=False)
    name_claim = Column(String(255), nullable=True)
    client_id = Column(String(64), nullable=False)
    token_received_at = Column(DateTime(timezone=True), nullable=False)
    id_token_sha256 = Column(String(64), nullable=True)
    flow_state_sha256 = Column(String(64), nullable=False)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "orcid",
            "token_received_at",
            name="uq_orcid_authentications_receipt",
        ),
        # A PostgreSQL regex; the SQLite unit schema skips it.
        CheckConstraint(ORCID_CHECK, name="ck_orcid_authentications_orcid").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint(ENVIRONMENT_CHECK, name="ck_orcid_authentications_env"),
        Index("idx_orcid_authentications_user", "user_id"),
    )


class VenueCheck(Base):
    __tablename__ = "venue_checks"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    release_id = _fk("manuscript_releases")
    profile_id = Column(String(64), nullable=False)
    profile_version = Column(SmallInteger, nullable=False)
    package_sha256 = Column(String(64), nullable=False)
    anonymized_sha256 = Column(String(64), nullable=True)
    result = Column(JSONB, nullable=False)
    status = Column(String(8), nullable=False)
    checked_by_id = _fk("users")
    created_at = _created_at()

    __table_args__ = (
        CheckConstraint(VENUE_STATUS_CHECK, name="ck_venue_checks_status"),
        Index("idx_venue_checks_release", "release_id"),
    )
