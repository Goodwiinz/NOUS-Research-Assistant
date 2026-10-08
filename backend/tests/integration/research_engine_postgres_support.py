"""Bounded canonical-project schema support for research-engine PG tests."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from src.models.base import Base
from src.models.collection import Collection
from src.models.research_project_role import ResearchProjectRoleAssignment
from src.models.research_protocol import (
    ResearchProtocol,
    ResearchProtocolVersion,
    ResearchQuestion,
    ResearchQuestionVersion,
)
from src.models.workspace import Workspace, WorkspaceMember
from src.services.research_engine.protocol_service import (
    canonical_hash,
    protocol_content,
)


@dataclass(frozen=True)
class CanonicalProjectScope:
    workspace_id: UUID
    collection_id: UUID


@dataclass(frozen=True)
class ApprovedProtocolBinding:
    protocol_version_id: UUID
    effective_plan_hash: str


_PROTOCOL_SNAPSHOT = {
    "eligibility": {"population": "fixture"},
    "sources_search": {"databases": ["fixture"]},
    "selection": {"reviewers": 1},
    "extraction": {"fields": ["outcome"]},
    "appraisal_synthesis": {"method": "fixture"},
    "outcomes": {"primary": "fixture"},
    "reviewer_mode": {"mode": "single"},
}


async def create_research_engine_tables(
    connection: Any, *engine_models: type[Any]
) -> None:
    """Create canonical access, protocol, and requested engine tables in order."""
    await connection.exec_driver_sql(
        'CREATE TABLE "organizations" (id UUID PRIMARY KEY)'
    )
    await connection.exec_driver_sql(
        'CREATE TABLE "users" ('
        "id UUID PRIMARY KEY, organization_id UUID NULL REFERENCES organizations(id)"
        ")"
    )
    models = (
        Workspace,
        WorkspaceMember,
        Collection,
        ResearchProjectRoleAssignment,
        ResearchQuestion,
        ResearchQuestionVersion,
        ResearchProtocol,
        ResearchProtocolVersion,
        *engine_models,
    )
    tables = list(dict.fromkeys(cast(Any, model).__table__ for model in models))
    await connection.run_sync(
        lambda sync_connection: Base.metadata.create_all(
            sync_connection,
            tables=tables,
        )
    )


async def seed_canonical_project_scope(
    executor: Any,
    *,
    owner_id: UUID,
    organization_id: UUID,
    additional_user_organizations: Mapping[UUID, UUID] | None = None,
    additional_organization_ids: tuple[UUID, ...] = (),
    workspace_id: UUID | None = None,
    collection_id: UUID | None = None,
    reviewer_ids: tuple[UUID, ...] = (),
) -> CanonicalProjectScope:
    """Seed an owner workspace and Collection plus fixture principals.

    Also seeds REVIEWER assignments for ``reviewer_ids`` (reviewers must be
    the owner or a member).
    """
    workspace_id = workspace_id or uuid4()
    collection_id = collection_id or uuid4()
    organization_ids = dict.fromkeys((organization_id, *additional_organization_ids))
    for candidate_id in organization_ids:
        await executor.execute(
            text("INSERT INTO organizations (id) VALUES (:id)"),
            {"id": candidate_id},
        )
    users = {owner_id: organization_id, **(additional_user_organizations or {})}
    for user_id, user_organization_id in users.items():
        await executor.execute(
            text(
                "INSERT INTO users (id, organization_id) VALUES (:id, :organization_id)"
            ),
            {"id": user_id, "organization_id": user_organization_id},
        )
    await executor.execute(
        text("""INSERT INTO workspaces (
                   id, created_at, updated_at, is_deleted, deleted_at,
                   name, description, is_archived, is_public,
                   owner_id, organization_id
               ) VALUES (
                   :id, now(), now(), false, NULL,
                   'Research fixture', NULL, false, false,
                   :owner_id, :organization_id
               )"""),
        {
            "id": workspace_id,
            "owner_id": owner_id,
            "organization_id": organization_id,
        },
    )
    await executor.execute(
        text("""INSERT INTO collections (
                   id, created_at, updated_at, is_deleted, deleted_at,
                   workspace_id, name, description, color, icon,
                   project_type, research_status, research_goals,
                   deadline, tags, is_private
               ) VALUES (
                   :id, now(), now(), false, NULL,
                   :workspace_id, 'Research fixture', NULL, NULL, NULL,
                   'research', 'active', NULL, NULL, '[]'::jsonb, true
               )"""),
        {"id": collection_id, "workspace_id": workspace_id},
    )
    for reviewer_id in reviewer_ids:
        # Role literal stays inline: asyncpg binds a varchar, which PostgreSQL
        # will not implicitly cast to the researchprojectrole enum.
        await executor.execute(
            text("""INSERT INTO research_project_role_assignments (
                       id, created_at, updated_at, is_deleted, deleted_at,
                       collection_id, user_id, role, assigned_by_id
                   ) VALUES (
                       :id, now(), now(), false, NULL,
                       :collection_id, :user_id, 'reviewer', :assigned_by_id
                   )"""),
            {
                "id": uuid4(),
                "collection_id": collection_id,
                "user_id": reviewer_id,
                "assigned_by_id": owner_id,
            },
        )
    return CanonicalProjectScope(workspace_id, collection_id)


async def seed_approved_protocol_binding(
    executor: Any,
    *,
    blueprint_id: UUID,
    collection_id: UUID,
    author_id: UUID,
    steps: list[dict[str, Any]],
    parameters: dict[str, Any],
    blueprint_version: int = 1,
    snapshot: Mapping[str, Any] = _PROTOCOL_SNAPSHOT,
    hypothesis: str | None = None,
) -> ApprovedProtocolBinding:
    """Bind an isolated run fixture to the exact approved blueprint plan."""
    question_id = uuid4()
    question_version_id = uuid4()
    protocol_id = uuid4()
    protocol_version_id = uuid4()
    execution_plan = {
        "blueprint_id": str(blueprint_id),
        "blueprint_version": blueprint_version,
        "steps": deepcopy(steps),
        "parameters": deepcopy(parameters),
    }
    content_hash = canonical_hash(
        protocol_content(
            question_version_id,
            blueprint_id,
            snapshot,
            execution_plan,
        )
    )
    question_hash = canonical_hash(
        {
            "question": "Fixture research question",
            "hypothesis": hypothesis,
            "scope": None,
            "framework": {},
            "canonicalization_version": "research-protocol-v1",
        }
    )
    await executor.execute(
        text("""INSERT INTO research_questions (
                   id, created_at, updated_at, is_deleted, deleted_at,
                   collection_id, current_version_id
               ) VALUES (
                   :id, now(), now(), false, NULL, :collection_id, NULL
               )"""),
        {"id": question_id, "collection_id": collection_id},
    )
    await executor.execute(
        text("""INSERT INTO research_question_versions (
                   id, question_id, version, parent_version_id, question,
                   hypothesis, scope, framework, content_hash, author_user_id,
                   created_at
               ) VALUES (
                   :id, :question_id, 1, NULL, 'Fixture research question',
                   :hypothesis, NULL, CAST(:framework AS jsonb), :content_hash,
                   :author_user_id, now()
               )"""),
        {
            "id": question_version_id,
            "question_id": question_id,
            "hypothesis": hypothesis,
            "framework": json.dumps({}),
            "content_hash": question_hash,
            "author_user_id": author_id,
        },
    )
    await executor.execute(
        text(
            "UPDATE research_questions SET current_version_id=:version_id "
            "WHERE id=:question_id"
        ),
        {"version_id": question_version_id, "question_id": question_id},
    )
    await executor.execute(
        text("""INSERT INTO research_protocols (
                   id, created_at, updated_at, is_deleted, deleted_at,
                   collection_id, name, current_draft_version_id,
                   current_approved_version_id
               ) VALUES (
                   :id, now(), now(), false, NULL, :collection_id,
                   'Fixture protocol', NULL, NULL
               )"""),
        {"id": protocol_id, "collection_id": collection_id},
    )
    await executor.execute(
        text("""INSERT INTO research_protocol_versions (
                   id, protocol_id, version, parent_version_id,
                   question_version_id, blueprint_id, execution_plan, snapshot,
                   content_hash, status, change_kind, amendment_reason,
                   author_user_id, approved_by_user_id, approved_at,
                   superseded_at, created_at
               ) VALUES (
                   :id, :protocol_id, 1, NULL, :question_version_id,
                   :blueprint_id, CAST(:execution_plan AS jsonb),
                   CAST(:snapshot AS jsonb), :content_hash, 'approved',
                   'initial', NULL, :author_user_id, :author_user_id,
                   now(), NULL, now()
               )"""),
        {
            "id": protocol_version_id,
            "protocol_id": protocol_id,
            "question_version_id": question_version_id,
            "blueprint_id": blueprint_id,
            "execution_plan": json.dumps(execution_plan),
            "snapshot": json.dumps(snapshot),
            "content_hash": content_hash,
            "author_user_id": author_id,
        },
    )
    await executor.execute(
        text("""UPDATE research_protocols
               SET current_draft_version_id=:version_id,
                   current_approved_version_id=:version_id
               WHERE id=:protocol_id"""),
        {"version_id": protocol_version_id, "protocol_id": protocol_id},
    )
    effective_hash = canonical_hash(
        {
            "protocol_content_hash": content_hash,
            "execution_plan": execution_plan,
        }
    )
    return ApprovedProtocolBinding(protocol_version_id, effective_hash)
