"""Offline checks of the jd01_document_embeddings migration (GOO-435 S5a).

``alembic upgrade FROM:TO --sql`` renders the revision without a database. Same
subprocess pattern as ``test_search_vector_repair_migration``: never import
alembic in this process
(``tests/unit/ci/test_migration_tests_collect_together.py``).

NOT RUN here: executing the revision. That is
``tests/integration/test_semantic_search_postgres.py`` (needs PostgreSQL with
pgvector; it skips on a server without it).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT = "aw03_artifact_completion"
REVISION = "jd01_document_embeddings"


def _alembic(*alembic_args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "SUPABASE_DB_URL"}
    # Only the dialect is read from the URL; nothing connects.
    env["DATABASE_URL"] = "postgresql://offline.invalid/nous"
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *alembic_args, "--sql"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    code = "\n".join(
        line for line in done.stdout.splitlines() if not line.lstrip().startswith("--")
    )
    return re.sub(r"\s+", " ", code).strip()


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _alembic("upgrade", f"{PARENT}:{REVISION}")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _alembic("downgrade", f"{REVISION}:{PARENT}")


def test_revision_id_fits_the_version_table() -> None:
    assert len(REVISION) <= 32


def test_it_stacks_on_the_previous_head() -> None:
    source = (BACKEND_ROOT / "alembic" / "versions" / f"{REVISION}.py").read_text()
    assert f'down_revision = "{PARENT}"' in source


def test_the_extension_is_created_before_the_table(upgrade_sql: str) -> None:
    assert "CREATE EXTENSION IF NOT EXISTS vector" in upgrade_sql
    assert upgrade_sql.index("CREATE EXTENSION") < upgrade_sql.index(
        "CREATE TABLE IF NOT EXISTS document_embeddings"
    )


def test_a_server_without_pgvector_skips_the_revision_with_a_warning(
    upgrade_sql: str,
) -> None:
    """CI's blocking integration lane runs ``alembic upgrade head`` on a stock
    ``postgres:15``: the chain must pass there, loudly, and create nothing."""
    guard = upgrade_sql.index("pg_available_extensions WHERE name = 'vector'")
    assert guard < upgrade_sql.index("CREATE EXTENSION")
    skipped = upgrade_sql[guard : upgrade_sql.index("CREATE EXTENSION")]
    assert "RAISE WARNING" in skipped and "RETURN" in skipped
    assert "NOT created" in skipped


def test_the_table_has_the_agreed_columns_and_keys(upgrade_sql: str) -> None:
    table = upgrade_sql[upgrade_sql.index("CREATE TABLE IF NOT EXISTS") :]
    table = table[: table.index(");")]
    assert "document_id uuid NOT NULL PRIMARY KEY" in table
    assert "REFERENCES documents (id) ON DELETE CASCADE" in table
    assert "organization_id uuid NOT NULL" in table
    assert "REFERENCES organizations (id) ON DELETE CASCADE" in table
    assert "embedding vector(1024) NOT NULL" in table
    assert "model varchar(255) NOT NULL" in table
    assert "content_hash varchar(64) NOT NULL" in table
    assert "created_at timestamp with time zone NOT NULL DEFAULT now()" in table
    assert "updated_at timestamp with time zone NOT NULL DEFAULT now()" in table


def test_it_indexes_the_organization_and_the_cosine_hnsw(upgrade_sql: str) -> None:
    assert (
        "CREATE INDEX IF NOT EXISTS ix_document_embeddings_organization_id "
        "ON document_embeddings (organization_id)"
    ) in upgrade_sql
    assert (
        "CREATE INDEX IF NOT EXISTS ix_document_embeddings_embedding_hnsw "
        "ON document_embeddings USING hnsw (embedding vector_cosine_ops)"
    ) in upgrade_sql


def test_the_data_api_is_locked_out(upgrade_sql: str) -> None:
    assert "ALTER TABLE document_embeddings ENABLE ROW LEVEL SECURITY" in upgrade_sql
    for role in ("anon", "authenticated"):
        assert f"REVOKE ALL ON TABLE document_embeddings FROM {role}" in upgrade_sql


def test_it_is_idempotent(upgrade_sql: str) -> None:
    assert "CREATE TABLE IF NOT EXISTS" in upgrade_sql
    assert upgrade_sql.count("CREATE INDEX IF NOT EXISTS") == 2


def test_downgrade_drops_the_table_and_keeps_the_extension(downgrade_sql: str) -> None:
    assert "DROP TABLE IF EXISTS document_embeddings" in downgrade_sql
    assert "DROP EXTENSION" not in downgrade_sql


def test_the_model_width_matches_the_migration() -> None:
    from src.core.config import settings
    from src.models.document_embedding import DOCUMENT_EMBEDDING_DIMENSIONS

    source = (BACKEND_ROOT / "alembic" / "versions" / f"{REVISION}.py").read_text()
    assert f"vector({DOCUMENT_EMBEDDING_DIMENSIONS})" in source
    assert settings.COHERE_EMBED_DIMENSIONS == DOCUMENT_EMBEDDING_DIMENSIONS


def test_the_model_stays_off_base_metadata() -> None:
    """``Base.metadata.create_all`` (r6h3 baseline, test fixtures, dev bootstrap)
    must not try to create a ``vector`` column on a server without pgvector."""
    import src.models
    from src.models.base import Base
    from src.models.document_embedding import MIGRATION_OWNED_TABLES, DocumentEmbedding

    assert "document_embeddings" in MIGRATION_OWNED_TABLES
    assert "document_embeddings" not in Base.metadata.tables
    assert DocumentEmbedding.__table__.metadata is not Base.metadata
    assert "DocumentEmbedding" in src.models.__all__


def _ddl_block(upgrade_sql: str) -> str:
    """The nested block from ``CREATE EXTENSION`` to the end of its handler."""
    start = upgrade_sql.index("CREATE EXTENSION")
    return upgrade_sql[start : upgrade_sql.index("END; END $jd01$", start)]


def test_refused_pgvector_ddl_warns_instead_of_failing_the_chain(
    upgrade_sql: str,
) -> None:
    """A role without the right to create extensions, or a pgvector without
    ``hnsw`` (< 0.5), must not fail the migration Job and block the rollout."""
    block = _ddl_block(upgrade_sql)
    handler = block[block.index("EXCEPTION") :]
    for condition in (
        "insufficient_privilege",
        "feature_not_supported",
        "undefined_object",
        "undefined_file",
    ):
        assert condition in handler
    assert "RAISE WARNING" in handler and "NOT created" in handler
    assert "SQLSTATE" in handler and "SQLERRM" in handler
    # Never re-raised: the handler only warns.
    assert not re.search(r"\bRAISE( EXCEPTION)?;", handler)


def test_every_pgvector_statement_runs_inside_the_guarded_block(
    upgrade_sql: str,
) -> None:
    body = _ddl_block(upgrade_sql)
    guarded = body[: body.index("EXCEPTION")]
    outside = upgrade_sql.replace(body, "")
    for statement in (
        "CREATE EXTENSION IF NOT EXISTS vector",
        "CREATE TABLE IF NOT EXISTS document_embeddings",
        "USING hnsw (embedding vector_cosine_ops)",
        "ENABLE ROW LEVEL SECURITY",
        "REVOKE ALL ON TABLE document_embeddings FROM anon",
    ):
        assert statement in guarded
        assert statement not in outside
    # The nested block opens right before the extension, inside the DO body.
    assert (
        upgrade_sql[: upgrade_sql.index("CREATE EXTENSION")].rstrip().endswith("BEGIN")
    )


def test_recovery_names_the_parent_never_downgrade_minus_one(upgrade_sql: str) -> None:
    """``alembic downgrade -1`` from a later head undoes the wrong revision."""
    source = (BACKEND_ROOT / "alembic" / "versions" / f"{REVISION}.py").read_text()
    gotchas = (BACKEND_ROOT.parent / "docs" / "engineering" / "gotchas.md").read_text()
    for text in (source, gotchas):
        assert "downgrade -1" not in text
        assert f"alembic downgrade {PARENT} && alembic upgrade {REVISION}" in text
        assert f"alembic upgrade {PARENT}:{REVISION} --sql" in text
    # Both warnings carry the explicit recovery.
    recovery = f"alembic downgrade {PARENT} && alembic upgrade {REVISION}"
    assert upgrade_sql.count(recovery) == 2
