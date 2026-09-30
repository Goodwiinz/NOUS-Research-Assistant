"""PostgreSQL coverage for deterministic legacy research-project backfill."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from src.models import Base


def _load_backfill() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "scripts"
        / "maintenance"
        / "backfill_research_project_collections.py"
    )
    spec = importlib.util.spec_from_file_location("research_project_backfill", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BACKFILL = _load_backfill()


@pytest.fixture
def backfill_engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    configured_url = os.getenv("RESEARCH_PROJECT_BACKFILL_DATABASE_URL")
    url = make_url(
        configured_url or str(request.getfixturevalue("postgres_container")["url"])
    ).set(drivername="postgresql+psycopg2")
    schema = f"test_research_project_backfill_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    scoped = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(scoped)
    try:
        yield scoped
    finally:
        scoped.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def _seed(connection: Connection) -> dict[str, UUID]:
    ids = {
        name: uuid4()
        for name in (
            "org_a",
            "org_b",
            "owner",
            "foreign_owner",
            "org_mismatch_owner",
            "live_workspace",
            "foreign_workspace",
            "org_mismatch_workspace",
            "personal_foreign_workspace",
            "archived_workspace",
            "deleted_workspace",
            "mapped",
            "target_collision",
            "owner_conflict",
            "org_conflict",
            "archived",
            "deleted_workspace_project",
            "deleted_collection",
            "deleted_project",
            "unresolved",
            "already_linked",
            "already_linked_collection",
            "personal_foreign_collection",
            "personal_cross_org_link",
            "claimant",
        )
    }
    db = connection
    db.execute(
        text("""
            INSERT INTO organizations
                (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,
                 is_active,created_at,updated_at,is_deleted)
            VALUES
                (:org_a,'A','FREE',0,1,true,now(),now(),false),
                (:org_b,'B','FREE',0,1,true,now(),now(),false)
            """),
        ids,
    )
    for name, org in (
        ("owner", "org_a"),
        ("foreign_owner", "org_a"),
        ("org_mismatch_owner", "org_b"),
    ):
        db.execute(
            text("""
                INSERT INTO users
                    (id,email,password_hash,first_name,last_name,role,is_active,
                     login_count,organization_id,created_at,updated_at,is_deleted)
                VALUES
                    (:id,:email,'x','x','x','USER',true,0,:org,now(),now(),false)
                """),
            {
                "id": ids[name],
                "email": f"{name}-{uuid4()}@test.invalid",
                "org": ids[org],
            },
        )
    workspace_rows: tuple[tuple[str, str, str | None, bool, bool], ...] = (
        ("live_workspace", "owner", "org_a", False, False),
        ("foreign_workspace", "foreign_owner", "org_a", False, False),
        ("org_mismatch_workspace", "org_mismatch_owner", "org_a", False, False),
        ("personal_foreign_workspace", "org_mismatch_owner", None, False, False),
        ("archived_workspace", "owner", "org_a", True, False),
        ("deleted_workspace", "owner", "org_a", False, True),
    )
    for name, owner, workspace_org, archived, deleted in workspace_rows:
        db.execute(
            text("""
                INSERT INTO workspaces
                    (id,name,is_archived,is_public,owner_id,organization_id,
                     created_at,updated_at,is_deleted)
                VALUES
                    (:id,:name,:archived,false,:owner,:org,now(),now(),:deleted)
                """),
            {
                "id": ids[name],
                "name": name,
                "archived": archived,
                "owner": ids[owner],
                "org": ids[workspace_org] if workspace_org else None,
                "deleted": deleted,
            },
        )
    for name, workspace, deleted in (
        ("mapped", "live_workspace", False),
        ("target_collision", "live_workspace", False),
        ("owner_conflict", "foreign_workspace", False),
        ("org_conflict", "org_mismatch_workspace", False),
        ("archived", "archived_workspace", False),
        ("deleted_workspace_project", "deleted_workspace", False),
        ("deleted_collection", "live_workspace", True),
        ("deleted_project", "live_workspace", False),
        ("already_linked_collection", "live_workspace", False),
        ("personal_foreign_collection", "personal_foreign_workspace", False),
    ):
        db.execute(
            text("""
                INSERT INTO collections
                    (id,workspace_id,name,project_type,research_status,tags,
                     is_private,created_at,updated_at,is_deleted)
                VALUES
                    (:id,:workspace,:name,'research','active','[]',true,
                     now(),now(),:deleted)
                """),
            {
                "id": ids[name],
                "workspace": ids[workspace],
                "name": name,
                "deleted": deleted,
            },
        )
    for name, owner, deleted, linked in (
        ("mapped", "owner", False, None),
        ("target_collision", "owner", False, None),
        ("owner_conflict", "owner", False, None),
        ("org_conflict", "org_mismatch_owner", False, None),
        ("archived", "owner", False, None),
        ("deleted_workspace_project", "owner", False, None),
        ("deleted_collection", "owner", False, None),
        ("deleted_project", "owner", True, None),
        ("unresolved", "owner", False, None),
        ("already_linked", "foreign_owner", False, "already_linked_collection"),
        (
            "personal_cross_org_link",
            "owner",
            False,
            "personal_foreign_collection",
        ),
        ("claimant", "owner", False, "target_collision"),
    ):
        db.execute(
            text("""
                INSERT INTO research_projects
                    (id,name,owner_id,collection_id,status,
                     created_at,updated_at,is_deleted)
                VALUES
                    (:id,:name,:owner,:linked,'active',now(),now(),:deleted)
                """),
            {
                "id": ids[name],
                "name": name,
                "owner": ids[owner],
                "linked": ids[linked] if linked else None,
                "deleted": deleted,
            },
        )
    return ids


def test_backfill_report_conflict_guard_apply_and_idempotency(
    backfill_engine: Engine,
) -> None:
    with backfill_engine.begin() as connection:
        ids = _seed(connection)
        first = BACKFILL.build_report(connection)
        assert [row["research_engine_project_id"] for row in first["rows"]] == sorted(
            row["research_engine_project_id"] for row in first["rows"]
        )
        reasons = {row["reason"] for row in first["rows"]}
        assert {
            "identical_id_owner_scope",
            "owner_mismatch",
            "organization_mismatch",
            "workspace_deleted",
            "collection_deleted",
            "project_deleted",
            "no_identical_id_collection",
            "already_linked",
            "collection_already_linked",
        } <= reasons
        mapped_row = next(
            row
            for row in first["rows"]
            if row["research_engine_project_id"] == str(ids["mapped"])
        )
        assert mapped_row["project_id"] == str(ids["mapped"])
        assert mapped_row["collection_id"] == mapped_row["project_id"]
        assert any(
            row["research_engine_project_id"] == str(ids["personal_cross_org_link"])
            and row["reason"] == "linked_organization_mismatch"
            for row in first["rows"]
        )
        assert any(
            row["research_engine_project_id"] == str(ids["already_linked"])
            and row["reason"] == "already_linked"
            for row in first["rows"]
        )

        refused, exit_code = BACKFILL.run_backfill(connection, apply=True)
        assert exit_code == 2
        assert refused["applied"] == 0
        assert (
            connection.execute(
                text("SELECT collection_id FROM research_projects WHERE id=:project"),
                {"project": ids["mapped"]},
            ).scalar_one()
            is None
        )

        connection.execute(
            text("""
                UPDATE research_projects SET is_deleted=true
                WHERE id IN (
                    :owner_conflict,:org_conflict,:target_collision,:claimant,
                    :deleted_workspace_project,:deleted_collection
                    ,:personal_cross_org_link
                )
                """),
            {
                "owner_conflict": ids["owner_conflict"],
                "org_conflict": ids["org_conflict"],
                "target_collision": ids["target_collision"],
                "claimant": ids["claimant"],
                "deleted_workspace_project": ids["deleted_workspace_project"],
                "deleted_collection": ids["deleted_collection"],
                "personal_cross_org_link": ids["personal_cross_org_link"],
            },
        )
        applied, exit_code = BACKFILL.run_backfill(connection, apply=True)
        assert exit_code == 0
        assert applied["applied"] == 2
        assert (
            connection.execute(
                text("SELECT collection_id FROM research_projects WHERE id=:project"),
                {"project": ids["mapped"]},
            ).scalar_one()
            == ids["mapped"]
        )

        rerun, exit_code = BACKFILL.run_backfill(connection, apply=True)
        assert exit_code == 0
        assert rerun["applied"] == 0
        assert any(
            row["research_engine_project_id"] == str(ids["mapped"])
            and row["reason"] == "already_linked"
            for row in rerun["rows"]
        )
        connection.execute(
            text("UPDATE collections SET is_deleted=true WHERE id=:collection"),
            {"collection": ids["already_linked_collection"]},
        )
        invalid_link = BACKFILL.build_report(connection)
        assert any(
            row["research_engine_project_id"] == str(ids["already_linked"])
            and row["reason"] == "linked_collection_deleted"
            for row in invalid_link["rows"]
        )
        connection.execute(
            text("UPDATE collections SET is_deleted=false WHERE id=:collection"),
            {"collection": ids["already_linked_collection"]},
        )
        guarded, exit_code = BACKFILL.run_backfill(connection, require_resolved=True)
        assert exit_code == 3
        assert guarded["guard"] == "active_projects_invalid"

        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text("DELETE FROM collections WHERE id=:collection"),
                {"collection": ids["mapped"]},
            )


def test_concurrent_backfill_commands_apply_mapping_once(
    backfill_engine: Engine,
) -> None:
    with backfill_engine.begin() as connection:
        ids = _seed(connection)
        connection.execute(
            text("""
                UPDATE research_projects SET is_deleted=true
                WHERE id <> :mapped
                """),
            {"mapped": ids["mapped"]},
        )

    advisory_locks: list[str] = []

    def record_advisory_lock(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "pg_advisory_xact_lock" in statement:
            advisory_locks.append(statement)

    event.listen(backfill_engine, "before_cursor_execute", record_advisory_lock)

    def apply_once() -> tuple[int, int]:
        with backfill_engine.begin() as connection:
            report, exit_code = BACKFILL.run_backfill(connection, apply=True)
            return int(report["applied"]), exit_code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _unused: apply_once(), range(2)))
    finally:
        event.remove(backfill_engine, "before_cursor_execute", record_advisory_lock)
    assert sorted(outcomes) == [(0, 0), (1, 0)]
    assert len(advisory_locks) == 2
