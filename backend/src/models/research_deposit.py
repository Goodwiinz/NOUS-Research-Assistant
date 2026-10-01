"""Archive deposits of verified manuscript releases (GOO-318).

Two insert-only evidence tables and one mutable work queue:

- ``archive_deposit_approvals``: one row per approval or revocation. An
  approval binds the exact release, its package hash, the repository, the
  account label and the action ``publish``.
- ``archive_deposit_attempts``: one row per phase attempt, chained by
  ``previous_id`` (``UNIQUE``, so a chain is linear and two workers cannot
  both append the next phase). The ``prepared`` row's id is the operation id.
  Remote deposition, record and DOI ids are kept on the attempt that saw
  them. Status is derived from the chain (``deposit_rules``), never stamped.
- ``archive_deposit_outbox``: the only mutable row; the work queue, never
  the record. Deleting it loses no evidence (``deposit_service.requeue``
  rebuilds it from the chain).

The migration ``b0e2a4c6d8f9`` freezes the same DDL plus GOO-309's
insert-only trigger on the two evidence tables.
"""

import uuid

from sqlalchemy import (
    Boolean,
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

APPROVAL_KIND_CHECK = "kind IN ('approved','revoked')"
ACTION_CHECK = "action = 'publish'"
REPOSITORY_CHECK = "repository = 'zenodo_sandbox'"
APPROVER_ROLE_CHECK = "actor_role IN ('adjudicator','supervisor')"
PHASE_CHECK = (
    "phase IN ('prepared','draft_created','files_uploaded','published','verified')"
)
OUTCOME_CHECK = "outcome IN ('succeeded','failed','unknown')"
CHAIN_ROOT_CHECK = "(phase = 'prepared') = (previous_id IS NULL)"
OPERATION_ROOT_CHECK = "phase <> 'prepared' OR operation_id = id"
OUTBOX_STATUS_CHECK = "status IN ('pending','processing','done','skipped')"
PREPARED = "phase = 'prepared'"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


def _created_at() -> Column:
    return Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


class ArchiveDepositApproval(Base):
    __tablename__ = "archive_deposit_approvals"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    release_id = _fk("manuscript_releases")
    package_sha256 = Column(String(64), nullable=False)
    repository = Column(String(32), nullable=False)
    account_ref = Column(String(128), nullable=False)
    action = Column(String(16), nullable=False)
    kind = Column(String(16), nullable=False)
    approved_by_id = _fk("users")
    actor_role = Column(String(16), nullable=False)
    rationale = Column(Text, nullable=True)
    created_at = _created_at()

    __table_args__ = (
        CheckConstraint(APPROVAL_KIND_CHECK, name="ck_archive_deposit_approvals_kind"),
        CheckConstraint(ACTION_CHECK, name="ck_archive_deposit_approvals_action"),
        CheckConstraint(
            REPOSITORY_CHECK, name="ck_archive_deposit_approvals_repository"
        ),
        CheckConstraint(APPROVER_ROLE_CHECK, name="ck_archive_deposit_approvals_role"),
        Index("idx_archive_deposit_approvals_release", "release_id"),
    )


class ArchiveDepositAttempt(Base):
    __tablename__ = "archive_deposit_attempts"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    operation_id: Column = Column(GUID(), nullable=False)
    release_id = _fk("manuscript_releases")
    repository = Column(String(32), nullable=False)
    account_ref = Column(String(128), nullable=False)
    phase = Column(String(16), nullable=False)
    outcome = Column(String(16), nullable=False)
    retryable = Column(Boolean, nullable=False, default=False)
    idempotency_key = Column(String(255), nullable=True)
    request_fingerprint = Column(String(64), nullable=True)
    requested_by_id = _fk("users")
    approval_id = _fk("archive_deposit_approvals", nullable=True)
    files = Column(JSONB, nullable=False)
    remote_deposition_id = Column(String(64), nullable=True)
    remote_record_id = Column(String(64), nullable=True)
    doi = Column(String(255), nullable=True)
    request = Column(JSONB, nullable=False)
    response = Column(JSONB, nullable=False)
    reason = Column(String(200), nullable=True)
    previous_id: Column = _fk("archive_deposit_attempts", nullable=True)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint("previous_id", name="uq_archive_deposit_attempts_previous"),
        CheckConstraint(PHASE_CHECK, name="ck_archive_deposit_attempts_phase"),
        CheckConstraint(OUTCOME_CHECK, name="ck_archive_deposit_attempts_outcome"),
        CheckConstraint(CHAIN_ROOT_CHECK, name="ck_archive_deposit_attempts_root"),
        CheckConstraint(
            OPERATION_ROOT_CHECK, name="ck_archive_deposit_attempts_operation"
        ),
        CheckConstraint(REPOSITORY_CHECK, name="ck_archive_deposit_attempts_repo"),
        Index(
            "uq_deposit_idempotency",
            "collection_id",
            "idempotency_key",
            unique=True,
            postgresql_where=sql_text(PREPARED),
            sqlite_where=sql_text(PREPARED),
        ),
        Index(
            "uq_deposit_live",
            "release_id",
            "repository",
            unique=True,
            postgresql_where=sql_text(PREPARED),
            sqlite_where=sql_text(PREPARED),
        ),
        Index("idx_archive_deposit_attempts_operation", "operation_id"),
        Index("idx_archive_deposit_attempts_collection", "collection_id"),
    )


class ArchiveDepositOutbox(Base):
    __tablename__ = "archive_deposit_outbox"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    operation_id: Column = Column(
        GUID(),
        ForeignKey("archive_deposit_attempts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status = Column(String(16), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    created_at = _created_at()
    updated_at = _created_at()
    last_error = Column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_archive_deposit_outbox_operation"),
        CheckConstraint(OUTBOX_STATUS_CHECK, name="ck_archive_deposit_outbox_status"),
        Index("idx_archive_deposit_outbox_status", "status"),
    )
