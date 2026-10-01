"""Insert-only fresh reruns of a manifested run and their attempts (GOO-313).

``experiment_reruns``: one row per admitted request, carrying the comparison
rule declared (and hashed) before anything executed. ``experiment_rerun_attempts``:
one terminal row per attempt; ``UNIQUE(rerun_id, attempt)`` makes the first
terminal insert the only publication, so a cancel, an interrupt sweep and a
late worker can never both publish. An attempt with no row is running (or
interrupted once its ledger lease expires). ``executed`` and ``reproduced``
are separate results: ``reproduction`` is set iff ``status = 'executed'``.
The trigger in migration ``c0f2a4b6d8e9`` refuses UPDATE and DELETE on both,
and that migration freezes this DDL.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

ATTEMPT_STATUSES = (
    "restoration_failed",
    "environment_unavailable",
    "execution_failed",
    "cancelled",
    "interrupted",
    "executed",
)
STATUS_CHECK = "status IN ({})".format(",".join(f"'{s}'" for s in ATTEMPT_STATUSES))
REPRODUCTION_CHECK = (
    "reproduction IS NULL OR reproduction IN ('reproduced','not_reproduced')"
)
EXECUTED_IFF_REPRODUCTION = "(status = 'executed') = (reproduction IS NOT NULL)"


def _fk(target: str) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=False
    )


def _now() -> Column:
    return Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


class ExperimentRerun(Base):
    __tablename__ = "experiment_reruns"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    run_id = _fk("research_runs")
    manifest_id = _fk("research_run_manifests")
    manifest_hash = Column(String(64), nullable=False)
    rule = Column(JSONB, nullable=False)
    rule_hash = Column(String(64), nullable=False)
    requested_by_id = _fk("users")
    actor_role = Column(String(16), nullable=False)
    idempotency_key = Column(String(255), nullable=False)
    created_at = _now()

    __table_args__ = (
        UniqueConstraint("run_id", "idempotency_key", name="uq_experiment_reruns_key"),
        CheckConstraint("actor_role = 'reviewer'", name="ck_experiment_reruns_actor"),
    )


class ExperimentRerunAttempt(Base):
    __tablename__ = "experiment_rerun_attempts"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    rerun_id = _fk("experiment_reruns")
    attempt = Column(SmallInteger, nullable=False)
    status = Column(String(24), nullable=False)
    reproduction = Column(String(16), nullable=True)
    environment_validation = Column(JSONB, nullable=False)
    input_validation = Column(JSONB, nullable=False)
    reasons = Column(JSONB, nullable=False)
    comparison = Column(JSONB, nullable=True)
    comparison_hash = Column(String(64), nullable=True)
    outputs = Column(JSONB, nullable=False)
    template_id = Column(String(255), nullable=True)
    sandbox_id = Column(String(255), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = _now()

    __table_args__ = (
        UniqueConstraint("rerun_id", "attempt", name="uq_experiment_rerun_attempts"),
        CheckConstraint("attempt >= 1", name="ck_experiment_rerun_attempts_attempt"),
        CheckConstraint(STATUS_CHECK, name="ck_experiment_rerun_attempts_status"),
        CheckConstraint(
            REPRODUCTION_CHECK, name="ck_experiment_rerun_attempts_reproduction"
        ),
        CheckConstraint(
            EXECUTED_IFF_REPRODUCTION, name="ck_experiment_rerun_attempts_executed"
        ),
    )
