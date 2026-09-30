"""PostgreSQL upgrade/downgrade proof for the academic-wave migrations."""

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterator, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from src.models import Base
from src.models.research_project import ResearchProject

VERSIONS = Path(__file__).parents[2] / "alembic" / "versions"


def _load_migration(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        filename.removesuffix(".py"), VERSIONS / filename
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(connection: Connection, module: ModuleType, direction: str) -> None:
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    migration = cast(Callable[[], None], getattr(module, direction))
    migration()


def _history_ids(connection: Connection, project_id: UUID) -> dict[str, set[UUID]]:
    queries = {
        "blueprints": "SELECT id FROM research_blueprints WHERE project_id=:project",
        "runs": """
            SELECT rr.id FROM research_runs rr
            JOIN research_blueprints rb ON rb.id = rr.blueprint_id
            WHERE rb.project_id=:project
        """,
        "steps": """
            SELECT rs.id FROM research_steps rs
            JOIN research_runs rr ON rr.id = rs.run_id
            JOIN research_blueprints rb ON rb.id = rr.blueprint_id
            WHERE rb.project_id=:project
        """,
        "sources": """
            SELECT rs.id FROM research_sources rs
            JOIN research_runs rr ON rr.id = rs.run_id
            JOIN research_blueprints rb ON rb.id = rr.blueprint_id
            WHERE rb.project_id=:project
        """,
    }
    return {
        name: {
            cast(UUID, row[0])
            for row in connection.execute(text(query), {"project": project_id}).all()
        }
        for name, query in queries.items()
    }


@pytest.fixture
def pre_wave_connection(request: pytest.FixtureRequest) -> Iterator[Connection]:
    configured_url = os.getenv("ACADEMIC_MIGRATION_DATABASE_URL")
    url = make_url(
        configured_url or str(request.getfixturevalue("postgres_container")["url"])
    ).set(drivername="postgresql+psycopg2")
    schema = f"test_academic_wave_{uuid4().hex}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    scoped_engine = create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    Base.metadata.create_all(scoped_engine)
    with scoped_engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE research_project_role_assignments")
        connection.exec_driver_sql("DROP TYPE researchprojectrole")
        connection.exec_driver_sql("DROP TABLE draft_reviews")
        connection.exec_driver_sql(
            "ALTER TABLE research_projects DROP COLUMN collection_id CASCADE"
        )
    try:
        with scoped_engine.connect() as connection:
            yield connection
    finally:
        scoped_engine.dispose()
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


def _seed_pre_wave_rows(connection: Connection) -> dict[str, UUID]:
    ids = {
        name: uuid4()
        for name in (
            "owner",
            "other_owner",
            "workspace",
            "deleted_workspace",
            "matched",
            "foreign_owner",
            "deleted_project",
            "deleted_collection",
            "deleted_workspace_project",
            "independent_project",
            "independent_collection",
        )
    }
    suffix = uuid4().hex
    connection.execute(
        text("""
            INSERT INTO users
                (id,email,password_hash,first_name,last_name,role,is_active,
                 login_count,created_at,updated_at,is_deleted)
            VALUES
                (:owner,:owner_email,'x','x','x','USER',true,0,now(),now(),false),
                (:other,:other_email,'x','x','x','USER',true,0,now(),now(),false)
            """),
        {
            "owner": ids["owner"],
            "other": ids["other_owner"],
            "owner_email": f"owner-{suffix}@test.invalid",
            "other_email": f"other-{suffix}@test.invalid",
        },
    )
    connection.execute(
        text("""
            INSERT INTO workspaces
                (id,name,is_archived,is_public,owner_id,created_at,updated_at,is_deleted)
            VALUES
                (:workspace,'live',false,false,:owner,now(),now(),false),
                (:deleted_workspace,'deleted',false,false,:owner,now(),now(),true)
            """),
        ids,
    )
    collection_rows = [
        ("matched", "workspace", False),
        ("foreign_owner", "workspace", False),
        ("deleted_project", "workspace", False),
        ("deleted_collection", "workspace", True),
        ("deleted_workspace_project", "deleted_workspace", False),
        ("independent_collection", "workspace", False),
    ]
    for collection_name, workspace_name, deleted in collection_rows:
        connection.execute(
            text("""
                INSERT INTO collections
                    (id,workspace_id,name,project_type,research_status,tags,is_private,
                     created_at,updated_at,is_deleted)
                VALUES
                    (:id,:workspace,:name,'research','active','[]',true,
                     now(),now(),:deleted)
                """),
            {
                "id": ids[collection_name],
                "workspace": ids[workspace_name],
                "name": collection_name,
                "deleted": deleted,
            },
        )
    project_rows = [
        ("matched", "owner", False),
        ("foreign_owner", "other_owner", False),
        ("deleted_project", "owner", True),
        ("deleted_collection", "owner", False),
        ("deleted_workspace_project", "owner", False),
        ("independent_project", "owner", False),
    ]
    for project_name, owner_name, deleted in project_rows:
        connection.execute(
            text("""
                INSERT INTO research_projects
                    (id,name,owner_id,status,created_at,updated_at,is_deleted)
                VALUES (:id,:name,:owner,'active',now(),now(),:deleted)
                """),
            {
                "id": ids[project_name],
                "name": project_name,
                "owner": ids[owner_name],
                "deleted": deleted,
            },
        )
    connection.commit()
    return ids


def test_agent_operation_migration_accepts_model_baseline_table(
    pre_wave_connection: Connection,
) -> None:
    """The later raw-SQL migration must tolerate the model baseline table."""
    connection = pre_wave_connection
    migration = _load_migration("20260925_agent_tool_operation_results.py")

    _run_migration(connection, migration, "upgrade")

    assert inspect(connection).has_table("agent_tool_operations")
    assert "ix_agent_tool_operations_thread_turn" in {
        index["name"]
        for index in inspect(connection).get_indexes("agent_tool_operations")
    }
    assert connection.execute(
        text(
            "SELECT relrowsecurity FROM pg_class "
            "WHERE oid = to_regclass('agent_tool_operations')"
        )
    ).scalar_one()


def _assert_backfill(connection: Connection, ids: dict[str, UUID]) -> None:
    rows = dict(
        connection.execute(
            text("SELECT id, collection_id FROM research_projects")
        ).all()
    )
    assert set(rows) == {
        ids["matched"],
        ids["foreign_owner"],
        ids["deleted_project"],
        ids["deleted_collection"],
        ids["deleted_workspace_project"],
        ids["independent_project"],
    }
    assert rows[ids["matched"]] == ids["matched"]
    assert all(
        rows[ids[name]] is None
        for name in (
            "foreign_owner",
            "deleted_project",
            "deleted_collection",
            "deleted_workspace_project",
            "independent_project",
        )
    )


def test_academic_wave_migrations_upgrade_downgrade_round_trip(
    pre_wave_connection: Connection,
) -> None:
    connection = pre_wave_connection
    ids = _seed_pre_wave_rows(connection)
    link = _load_migration("v4w5x6y7z8a9_link_research_projects.py")
    reviews = _load_migration("w5x6y7z8a9b0_create_draft_reviews.py")
    roles = _load_migration("x6y7z8a9b0c1_create_research_project_role_assignments.py")

    _run_migration(connection, link, "upgrade")
    _assert_backfill(connection, ids)
    connection.commit()

    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text("""
                UPDATE research_projects SET collection_id=:collection
                WHERE id=:project
                """),
            {"collection": ids["matched"], "project": ids["independent_project"]},
        )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text("""
                UPDATE research_projects SET collection_id=:collection
                WHERE id=:project
                """),
            {"collection": uuid4(), "project": ids["independent_project"]},
        )

    _run_migration(connection, reviews, "upgrade")
    _run_migration(connection, roles, "upgrade")
    role_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO research_project_role_assignments
                (id,collection_id,user_id,role,assigned_by_id,
                 created_at,updated_at,is_deleted)
            VALUES
                (:id,:collection,:user,'reviewer',:assigner,
                 now(),now(),false),
                (:second,:collection,:user,'adjudicator',:assigner,
                 now(),now(),false)
            """),
        {
            "id": role_id,
            "second": uuid4(),
            "collection": ids["independent_collection"],
            "user": ids["other_owner"],
            "assigner": ids["owner"],
        },
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text("""
                INSERT INTO research_project_role_assignments
                    (id,collection_id,user_id,role,assigned_by_id,
                     created_at,updated_at,is_deleted)
                VALUES
                    (:id,:collection,:user,'reviewer',:assigner,
                     now(),now(),false)
                """),
            {
                "id": uuid4(),
                "collection": ids["independent_collection"],
                "user": ids["other_owner"],
                "assigner": ids["owner"],
            },
        )
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(
            text("""
                INSERT INTO research_project_role_assignments
                    (id,collection_id,user_id,role,assigned_by_id,
                     created_at,updated_at,is_deleted)
                VALUES
                    (:id,:collection,:user,'owner',:assigner,
                     now(),now(),false)
                """),
            {
                "id": uuid4(),
                "collection": ids["independent_collection"],
                "user": ids["other_owner"],
                "assigner": ids["owner"],
            },
        )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text("DELETE FROM collections WHERE id=:collection"),
            {"collection": ids["independent_collection"]},
        )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text("DELETE FROM users WHERE id=:user"), {"user": ids["owner"]}
        )
    role_inspector = inspect(connection)
    assert {
        index["name"]
        for index in role_inspector.get_indexes("research_project_role_assignments")
    } >= {
        "ix_research_project_role_assignments_collection_id",
        "ix_research_project_role_assignments_user_id",
    }
    assert any(
        constraint["name"] == "uq_research_project_role_assignment"
        for constraint in role_inspector.get_unique_constraints(
            "research_project_role_assignments"
        )
    )
    blueprint_id, run_id, step_id, source_id = uuid4(), uuid4(), uuid4(), uuid4()
    connection.execute(
        text("""
            INSERT INTO research_blueprints
                (id,project_id,name,version,is_immutable,
                 created_at,updated_at,is_deleted)
            VALUES (:blueprint,:project,'retained',1,false,now(),now(),false);
            INSERT INTO research_runs
                (id,blueprint_id,blueprint_version,status,total_tokens,
                 created_at,updated_at,is_deleted)
            VALUES (:run,:blueprint,1,'pending',0,now(),now(),false);
            INSERT INTO research_steps
                (id,run_id,step_index,step_type,mode,temperature,token_count,
                 created_at,updated_at,is_deleted)
            VALUES (:step,:run,0,'search','deterministic',0,0,
                    now(),now(),false)
            ;
            INSERT INTO research_sources
                (id,run_id,connector_type,title,created_at,updated_at,is_deleted)
            VALUES (:source,:run,'test','retained source',now(),now(),false)
            """),
        {
            "blueprint": blueprint_id,
            "project": ids["matched"],
            "run": run_id,
            "step": step_id,
            "source": source_id,
        },
    )
    history_before = _history_ids(connection, ids["matched"])
    assert history_before == {
        "blueprints": {blueprint_id},
        "runs": {run_id},
        "steps": {step_id},
        "sources": {source_id},
    }
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text("DELETE FROM research_projects WHERE id=:project"),
            {"project": ids["matched"]},
        )
    assert _history_ids(connection, ids["matched"]) == history_before
    orm = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        linked_project = orm.get(ResearchProject, ids["matched"])
        assert linked_project is not None
        orm.delete(linked_project)
        with pytest.raises(IntegrityError):
            orm.flush()
        orm.rollback()
    finally:
        orm.close()
    assert _history_ids(connection, ids["matched"]) == history_before
    connection.execute(
        text("UPDATE research_projects SET is_deleted=true WHERE id=:project"),
        {"project": ids["matched"]},
    )
    assert connection.execute(
        text("SELECT is_deleted FROM research_projects WHERE id=:project"),
        {"project": ids["matched"]},
    ).scalar_one()
    connection.execute(
        text("UPDATE research_projects SET is_deleted=false WHERE id=:project"),
        {"project": ids["matched"]},
    )
    review_id = uuid4()
    connection.execute(
        text("""
            INSERT INTO draft_reviews
                (id,project_id,candidate_content_hash,candidate_content,
                 source_document_ids,review,outcome,created_at,updated_at,is_deleted)
            VALUES
                (:id,:project,:hash,'candidate','[]','{}','blocked',
                 now(),now(),false)
            """),
        {"id": review_id, "project": ids["matched"], "hash": "a" * 64},
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text("""
                INSERT INTO draft_reviews
                    (id,project_id,candidate_content_hash,candidate_content,
                     source_document_ids,review,outcome,created_at,updated_at,is_deleted)
                VALUES
                    (:id,:project,:hash,'candidate','[]','{}','unknown',
                     now(),now(),false)
                """),
            {"id": uuid4(), "project": ids["matched"], "hash": "b" * 64},
        )
    connection.commit()

    _run_migration(connection, roles, "downgrade")
    _run_migration(connection, reviews, "downgrade")
    _run_migration(connection, link, "downgrade")
    connection.commit()
    inspector = inspect(connection)
    assert "draft_reviews" not in inspector.get_table_names()
    assert "research_project_role_assignments" not in inspector.get_table_names()
    assert "collection_id" not in {
        column["name"] for column in inspector.get_columns("research_projects")
    }
    assert (
        connection.execute(text("SELECT count(*) FROM research_projects")).scalar_one()
        == 6
    )

    _run_migration(connection, link, "upgrade")
    _run_migration(connection, reviews, "upgrade")
    _run_migration(connection, roles, "upgrade")
    connection.commit()
    _assert_backfill(connection, ids)
    assert "draft_reviews" in inspect(connection).get_table_names()
    assert "research_project_role_assignments" in inspect(connection).get_table_names()


_RESOLUTION_TABLES = ("screening_resolutions",)  # GOO-302: drop first
_SCREENING_TABLES = (
    "screening_suggestions",
    "screening_observations",
    "screening_assignments",
    "screening_queues",
)
_IMPORT_TABLES = ("research_import_records", "research_import_receipts")
_IDENTITY_TABLES = (
    "research_report_observations",
    "research_report_identifiers",
    "research_reports",
    "research_studies",
)


def test_report_identity_migration_upgrade_downgrade_round_trip(
    pre_wave_connection: Connection,
) -> None:
    """GOO-299 tables are created by the revision itself, with RLS enabled."""
    connection = pre_wave_connection
    # GOO-301/302 screening tables reference research_reports: drop them first.
    for table in (
        *_RESOLUTION_TABLES,
        *_SCREENING_TABLES,
        *_IMPORT_TABLES,
        *_IDENTITY_TABLES,
    ):
        connection.exec_driver_sql(f'DROP TABLE "{table}"')
    migration = _load_migration("c9d2e4f6a8b1_create_report_identities.py")

    _run_migration(connection, migration, "upgrade")
    inspector = inspect(connection)
    for table in _IDENTITY_TABLES:
        assert inspector.has_table(table)
        assert connection.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:t)"),
            {"t": table},
        ).scalar_one()
    assert "uq_research_report_identifier_value" in {
        c["name"]
        for c in inspector.get_unique_constraints("research_report_identifiers")
    }
    assert "uq_research_report_observation_source" in {
        c["name"]
        for c in inspector.get_unique_constraints("research_report_observations")
    }

    _run_migration(connection, migration, "downgrade")
    inspector = inspect(connection)
    assert not any(inspector.has_table(table) for table in _IDENTITY_TABLES)


def test_search_import_migration_upgrade_downgrade_round_trip(
    pre_wave_connection: Connection,
) -> None:
    """GOO-300 tables are created by d4e6f8a0b2c3 itself, with RLS enabled."""
    connection = pre_wave_connection
    for table in _IMPORT_TABLES:
        connection.exec_driver_sql(f'DROP TABLE "{table}"')
    migration = _load_migration("d4e6f8a0b2c3_create_search_imports.py")
    assert migration.down_revision == "c9d2e4f6a8b1"

    _run_migration(connection, migration, "upgrade")
    inspector = inspect(connection)
    for table in _IMPORT_TABLES:
        assert inspector.has_table(table)
        assert connection.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:t)"),
            {"t": table},
        ).scalar_one()
    assert {
        "uq_research_import_receipt_dedup",
        "uq_research_import_receipt_version",
    } <= {
        c["name"] for c in inspector.get_unique_constraints("research_import_receipts")
    }
    assert {
        "ck_research_import_receipt_counts",
        "ck_research_import_receipt_kind",
        "ck_research_import_receipt_version",
    } <= {
        c["name"] for c in inspector.get_check_constraints("research_import_receipts")
    }
    assert {
        "ck_research_import_record_rejection",
        "ck_research_import_record_report",
    } <= {c["name"] for c in inspector.get_check_constraints("research_import_records")}

    _run_migration(connection, migration, "downgrade")
    inspector = inspect(connection)
    assert not any(inspector.has_table(table) for table in _IMPORT_TABLES)


def test_screening_queue_migration_upgrade_downgrade_round_trip(
    pre_wave_connection: Connection,
) -> None:
    """GOO-301 tables are created by e1f3a5c7d9b2 itself, with RLS enabled."""
    connection = pre_wave_connection
    for table in (*_RESOLUTION_TABLES, *_SCREENING_TABLES):
        connection.exec_driver_sql(f'DROP TABLE "{table}"')
    connection.exec_driver_sql("DROP INDEX idx_research_decision_event_idempotency")
    migration = _load_migration("e1f3a5c7d9b2_create_screening_queues.py")
    assert migration.down_revision == "d4e6f8a0b2c3"

    _run_migration(connection, migration, "upgrade")
    inspector = inspect(connection)
    for table in _SCREENING_TABLES:
        assert inspector.has_table(table)
        assert connection.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:t)"),
            {"t": table},
        ).scalar_one()
    partial = {
        index["name"]: index["dialect_options"].get("postgresql_where")
        for table in ("screening_assignments", "screening_observations")
        for index in inspector.get_indexes(table)
        if index["unique"] and index["dialect_options"].get("postgresql_where")
    }
    assert partial == {
        "uq_screening_assignment_active": "(revoked_at IS NULL)",
        "uq_screening_observation_initial": "(supersedes_observation_id IS NULL)",
    }
    assert "idx_research_decision_event_idempotency" in {
        index["name"] for index in inspector.get_indexes("research_decision_events")
    }

    _run_migration(connection, migration, "downgrade")
    inspector = inspect(connection)
    assert not any(inspector.has_table(table) for table in _SCREENING_TABLES)
    assert "idx_research_decision_event_idempotency" not in {
        index["name"] for index in inspector.get_indexes("research_decision_events")
    }


def test_screening_resolution_migration_upgrade_downgrade_round_trip(
    pre_wave_connection: Connection,
) -> None:
    """GOO-302's table is created by f3b5d7e9a1c4 itself, with RLS enabled."""
    connection = pre_wave_connection
    for table in _RESOLUTION_TABLES:
        connection.exec_driver_sql(f'DROP TABLE "{table}"')
    migration = _load_migration("f3b5d7e9a1c4_create_screening_resolutions.py")
    assert migration.down_revision == "e1f3a5c7d9b2"

    _run_migration(connection, migration, "upgrade")
    inspector = inspect(connection)
    assert inspector.has_table("screening_resolutions")
    assert connection.execute(
        text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:t)"),
        {"t": "screening_resolutions"},
    ).scalar_one()
    assert {
        index["name"]: index["dialect_options"].get("postgresql_where")
        for index in inspector.get_indexes("screening_resolutions")
        if index["unique"] and index["dialect_options"].get("postgresql_where")
    } == {"uq_screening_resolution_initial": "(supersedes_resolution_id IS NULL)"}

    _run_migration(connection, migration, "downgrade")
    assert not inspect(connection).has_table("screening_resolutions")
