"""Actual migration proof for legacy run retention and empty-history round trips."""

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterator, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url

from src.models import Base

VERSIONS = Path(__file__).parents[2] / "alembic" / "versions"
LEDGER_REVISION = "a3c5e7f901b2_create_research_decision_ledger.py"
PROTOCOL_REVISION = "b4d6f8021a3c_add_research_protocols.py"
PROTOCOL_TABLES = {
    "research_questions",
    "research_question_versions",
    "research_protocols",
    "research_protocol_versions",
    "protocol_registration_operations",
    "protocol_deviations",
}


def _migration(filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(filename, VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_revision(connection: Connection, module: ModuleType, action: str) -> None:
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    cast(Callable[[], None], getattr(module, action))()


@pytest.fixture
def legacy_schema(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[Engine, dict[str, UUID]]]:
    configured = os.getenv("RESEARCH_DECISION_DATABASE_URL")
    url = make_url(
        configured or str(request.getfixturevalue("postgres_container")["url"])
    ).set(drivername="postgresql+psycopg2")
    schema = f"test_protocol_migration_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    ids = {
        name: uuid4()
        for name in (
            "org",
            "user",
            "workspace",
            "collection",
            "project",
            "blueprint",
            "run",
        )
    }
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        for table in (
            "protocol_deviations",
            "protocol_registration_operations",
            "research_protocol_versions",
            "research_protocols",
            "research_question_versions",
            "research_questions",
            "research_decision_events",
            "research_decision_streams",
        ):
            connection.exec_driver_sql(f"DROP TABLE {table} CASCADE")
        for column in (
            "protocol_version_id",
            "effective_plan_hash",
            "conformance_status",
        ):
            connection.exec_driver_sql(
                f"ALTER TABLE research_runs DROP COLUMN {column} CASCADE"
            )
        connection.execute(
            text("""INSERT INTO organizations
                (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,
                 is_active,created_at,updated_at,is_deleted)
                VALUES (:org,'legacy','FREE',0,1,true,now(),now(),false);
                INSERT INTO users
                (id,email,password_hash,first_name,last_name,role,is_active,
                 login_count,organization_id,created_at,updated_at,is_deleted)
                VALUES (:user,:email,'x','x','x','USER',true,0,:org,
                        now(),now(),false);
                INSERT INTO workspaces
                (id,name,is_archived,is_public,owner_id,organization_id,
                 created_at,updated_at,is_deleted)
                VALUES (:workspace,'legacy',false,false,:user,:org,
                        now(),now(),false);
                INSERT INTO collections
                (id,workspace_id,name,project_type,research_status,tags,is_private,
                 created_at,updated_at,is_deleted)
                VALUES (:collection,:workspace,'legacy','research','active','[]',
                        true,now(),now(),false);
                INSERT INTO research_projects
                (id,name,owner_id,status,collection_id,created_at,updated_at,is_deleted)
                VALUES (:project,'legacy',:user,'active',:collection,
                        now(),now(),false);
                INSERT INTO research_blueprints
                (id,project_id,name,version,is_immutable,
                 created_at,updated_at,is_deleted)
                VALUES (:blueprint,:project,'legacy',1,false,now(),now(),false);
                INSERT INTO research_runs
                (id,blueprint_id,blueprint_version,status,total_tokens,
                 created_at,updated_at,is_deleted)
                VALUES (:run,:blueprint,1,'completed',7,now(),now(),false)"""),
            {**ids, "email": f"legacy-{uuid4()}@test.invalid"},
        )
    try:
        yield engine, ids
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def _assert_upgraded_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    assert (
        PROTOCOL_TABLES
        | {
            "research_decision_streams",
            "research_decision_events",
        }
        <= tables
    )
    columns = {
        column["name"]: column for column in inspector.get_columns("research_runs")
    }
    assert columns["protocol_version_id"]["nullable"] is True
    assert columns["effective_plan_hash"]["nullable"] is True
    assert columns["conformance_status"]["nullable"] is False
    assert "legacy_unbound" in str(columns["conformance_status"]["default"])


def test_protocol_migrations_preserve_legacy_run_and_round_trip_empty_history(
    legacy_schema: tuple[Engine, dict[str, UUID]],
) -> None:
    engine, ids = legacy_schema
    ledger = _migration(LEDGER_REVISION)
    protocol = _migration(PROTOCOL_REVISION)
    with engine.begin() as connection:
        _run_revision(connection, ledger, "upgrade")
        _run_revision(connection, protocol, "upgrade")
        _assert_upgraded_schema(connection)
        retained = connection.execute(
            text("""SELECT id,blueprint_id,protocol_version_id,
                    effective_plan_hash,conformance_status
                FROM research_runs WHERE id=:run"""),
            ids,
        ).one()
        assert tuple(retained) == (
            ids["run"],
            ids["blueprint"],
            None,
            None,
            "legacy_unbound",
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM research_decision_events"))
            == 0
        )

        _run_revision(connection, protocol, "downgrade")
        _run_revision(connection, ledger, "downgrade")
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM research_runs WHERE id=:run AND blueprint_id=:blueprint"
                ),
                ids,
            )
            == 1
        )
        assert not (
            PROTOCOL_TABLES | {"research_decision_streams", "research_decision_events"}
        ) & set(inspect(connection).get_table_names())

        _run_revision(connection, ledger, "upgrade")
        _run_revision(connection, protocol, "upgrade")
        _assert_upgraded_schema(connection)
        retained_again = connection.execute(
            text("""SELECT id,blueprint_id,protocol_version_id,
                    effective_plan_hash,conformance_status
                FROM research_runs WHERE id=:run"""),
            ids,
        ).one()
        assert tuple(retained_again) == tuple(retained)
