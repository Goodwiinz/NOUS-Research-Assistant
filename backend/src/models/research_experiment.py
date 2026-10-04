"""Insert-only run manifests, retained run artifacts and figure records (GOO-312).

``research_run_manifests``: one ``nous.run-manifest/2`` document per run,
written once at terminal run status in the same transaction as the status.
``research_run_artifacts``: every retained input, code, environment and
output file of a run, addressed by sha256; ``storage_key`` is private and
never leaves the service. ``research_figures``: a versioned figure or table
record pointing at one exact output; a changed output is a successor row
(``supersedes_figure_id``). The trigger in migration ``b8e0c2d4f6a7``
refuses UPDATE and DELETE on all three, and that migration freezes this DDL.
"""

import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from .base import GUID, Base

ARTIFACT_ROLE_CHECK = "role IN ('input','code','environment','output')"
COMPLETENESS_CHECK = "completeness IN ('complete','incomplete')"
FIGURE_KIND_CHECK = "kind IN ('figure','table')"
INITIAL_FIGURE = "supersedes_figure_id IS NULL"


def _fk(target: str, *, nullable: bool = False) -> Column:
    return Column(
        GUID(), ForeignKey(f"{target}.id", ondelete="RESTRICT"), nullable=nullable
    )


def _created_at() -> Column:
    return Column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


class ResearchRunManifest(Base):
    __tablename__ = "research_run_manifests"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id = _fk("research_runs")
    collection_id = _fk("collections")
    schema_version = Column(SmallInteger, nullable=False)
    manifest = Column(JSONB, nullable=False)
    manifest_hash = Column(String(64), nullable=False)
    completeness = Column(String(16), nullable=False)
    missing = Column(JSONB, nullable=False)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_research_run_manifests_run"),
        CheckConstraint("schema_version = 2", name="ck_research_run_manifests_schema"),
        CheckConstraint(COMPLETENESS_CHECK, name="ck_research_run_manifests_state"),
    )


class ResearchRunArtifact(Base):
    __tablename__ = "research_run_artifacts"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id = _fk("research_runs")
    collection_id = _fk("collections")
    organization_id = _fk("organizations")
    role = Column(String(16), nullable=False)
    name = Column(String(255), nullable=False)
    media_type = Column(String(255), nullable=False)
    sha256 = Column(String(64), nullable=False)
    byte_size = Column(BigInteger, nullable=False)
    storage_key = Column(String(512), nullable=False)
    source_ref = Column(JSONB, nullable=True)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint("run_id", "role", "name", name="uq_research_run_artifacts"),
        CheckConstraint(ARTIFACT_ROLE_CHECK, name="ck_research_run_artifacts_role"),
        CheckConstraint("byte_size >= 0", name="ck_research_run_artifacts_size"),
    )


class ResearchFigure(Base):
    __tablename__ = "research_figures"

    id: Column = Column(GUID(), primary_key=True, default=uuid.uuid4)
    collection_id = _fk("collections")
    figure_key = Column(String(64), nullable=False)
    kind = Column(String(16), nullable=False)
    caption = Column(Text, nullable=False)
    output_artifact_id = _fk("research_run_artifacts")
    run_id = _fk("research_runs")
    manifest_id = _fk("research_run_manifests")
    supersedes_figure_id: Column = _fk("research_figures", nullable=True)
    created_by_id = _fk("users")
    actor_role = Column(String(16), nullable=False)
    created_at = _created_at()

    __table_args__ = (
        UniqueConstraint("supersedes_figure_id", name="uq_research_figures_supersedes"),
        CheckConstraint(FIGURE_KIND_CHECK, name="ck_research_figures_kind"),
        CheckConstraint("actor_role = 'editor'", name="ck_research_figures_actor"),
        Index(
            "uq_research_figures_initial",
            "collection_id",
            "figure_key",
            unique=True,
            postgresql_where=sql_text(INITIAL_FIGURE),
            sqlite_where=sql_text(INITIAL_FIGURE),
        ),
    )
