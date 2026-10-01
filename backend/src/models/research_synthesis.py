"""Insert-only quantitative synthesis results (GOO-311).

One row per execution of the protocol-selected SMD (Hedges' g) / DerSimonian-
Laird calculation over a frozen GOO-310 evidence table version. A changed
input is a successor row (``supersedes_result_id``); a failed validation is
retained too, with NULL numbers. Staleness is derived on read. The trigger in
migration ``a6c8e0b2d4f5`` refuses UPDATE and DELETE, and that migration
freezes the same DDL.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

INITIAL_RESULT = "supersedes_result_id IS NULL"
STATUS_CHECK = "status IN ('computed','validation_failed')"
NUMBERS_CHECK = "(status = 'computed') = (estimate IS NOT NULL)"
I2_CHECK = "i2 IS NULL OR (i2 >= 0 AND i2 <= 1)"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


class SynthesisResult(Base):
    __tablename__ = "synthesis_results"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    protocol_version_id = _fk("research_protocol_versions")
    table_version_id = _fk("evidence_table_versions")
    outcome_key = Column(String(100), nullable=False)
    timepoint = Column(String(100), nullable=False)
    measure = Column(String(32), nullable=False)
    model = Column(String(32), nullable=False)
    config = Column(JSONB, nullable=False)
    config_hash = Column(String(64), nullable=False)
    estimator_version = Column(String(64), nullable=False)
    software = Column(JSONB, nullable=False)
    status = Column(String(20), nullable=False)
    included = Column(JSONB, nullable=False)
    excluded = Column(JSONB, nullable=False)
    estimate = Column(Float, nullable=True)
    se = Column(Float, nullable=True)
    ci_low = Column(Float, nullable=True)
    ci_high = Column(Float, nullable=True)
    q = Column(Float, nullable=True)
    df = Column(Integer, nullable=True)
    tau2 = Column(Float, nullable=True)
    i2 = Column(Float, nullable=True)
    input_hash = Column(String(64), nullable=False)
    result_hash = Column(String(64), nullable=False)
    executed_by_id = _fk("users")
    actor_role = Column(String(16), nullable=False)
    supersedes_result_id: Column = _fk("synthesis_results", nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )

    __table_args__ = (
        UniqueConstraint("supersedes_result_id", name="uq_synthesis_supersedes"),
        CheckConstraint(STATUS_CHECK, name="ck_synthesis_status"),
        CheckConstraint(NUMBERS_CHECK, name="ck_synthesis_numbers"),
        CheckConstraint(I2_CHECK, name="ck_synthesis_i2"),
        CheckConstraint("actor_role = 'reviewer'", name="ck_synthesis_actor_role"),
        Index(
            "uq_synthesis_initial",
            "collection_id",
            "outcome_key",
            "timepoint",
            unique=True,
            postgresql_where=sql_text(INITIAL_RESULT),
            sqlite_where=sql_text(INITIAL_RESULT),
        ),
    )
