"""Safety contracts for the frozen project-skill loader tool."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest


def _snapshot_registry_fields() -> dict:
    from src.services.agent.tools import TOOL_REGISTRY

    metadata = TOOL_REGISTRY.metadata_snapshot()
    return {
        "tool_registry_hash": metadata["hash"],
        "tool_registry_version": metadata["version"],
        "tool_metadata": {
            "descriptors": TOOL_REGISTRY.frozen_descriptor_metadata(
                conditions={"project_skill_catalog"}
            )
        },
    }


@pytest.mark.asyncio
async def test_loader_requires_server_snapshot_context_and_never_accepts_ids_from_model() -> (
    None
):
    from src.services.agent.tools import load_project_skill

    schema = load_project_skill.args_schema.model_json_schema()
    assert "skill_name" in schema["properties"]
    assert not {
        "runtime_snapshot_id",
        "project_id",
        "user_id",
    } & set(schema["properties"])

    with patch(
        "src.core.config.get_settings",
        return_value=SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=True),
    ):
        result = await load_project_skill.ainvoke({"skill_name": "literature-review"})

    assert result["error_type"] == "runtime_snapshot_required"


@pytest.mark.asyncio
async def test_loader_rejects_a_snapshot_without_frozen_loader_membership() -> None:
    from src.services.agent.runtime_snapshot import load_project_skill_from_snapshot
    from src.services.agent.tools import TOOL_REGISTRY

    snapshot_id, user_id, project_id = uuid4(), uuid4(), uuid4()
    registry_metadata = TOOL_REGISTRY.metadata_snapshot()
    row = SimpleNamespace(
        id=snapshot_id,
        user_id=user_id,
        project_id=project_id,
        expires_at=None,
        tool_registry_hash=registry_metadata["hash"],
        tool_registry_version=registry_metadata["version"],
        tool_metadata={"descriptors": TOOL_REGISTRY.frozen_descriptor_metadata()},
        skill_catalog=[
            {
                "version_id": str(uuid4()),
                "name": "literature-review",
                "version": 1,
                "content_hash": "a" * 64,
            }
        ],
        loaded_skill_versions=[],
    )
    session = AsyncMock()
    session.get.return_value = row

    with patch(
        "src.services.agent.runtime_snapshot.get_settings",
        return_value=SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=True),
    ):
        result = await load_project_skill_from_snapshot(
            session,
            snapshot_id=str(snapshot_id),
            user_id=str(user_id),
            project_id=str(project_id),
            skill_name="literature-review",
        )

    assert result["error_type"] == "tool_not_in_snapshot"
    assert session.get.await_count == 1
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_loader_returns_exact_frozen_version_and_records_unique_load() -> None:
    from src.services.agent.runtime_snapshot import load_project_skill_from_snapshot

    snapshot_id = uuid4()
    user_id = uuid4()
    project_id = uuid4()
    version_id = uuid4()
    row = SimpleNamespace(
        id=snapshot_id,
        user_id=user_id,
        project_id=project_id,
        expires_at=None,
        **_snapshot_registry_fields(),
        skill_catalog=[
            {
                "version_id": str(version_id),
                "name": "literature-review",
                "version": 2,
                "content_hash": "61d63d5b5fb1a48c0cb7dc3dcc53763eb1290295bf6e52d9606687a3ceb6271c",
            }
        ],
        loaded_skill_versions=[],
    )
    version = SimpleNamespace(
        id=version_id,
        parsed_name="literature-review",
        version=2,
        content_hash="61d63d5b5fb1a48c0cb7dc3dcc53763eb1290295bf6e52d9606687a3ceb6271c",
        instructions="Use only the frozen version.",
    )
    session = AsyncMock()
    session.add = Mock()
    session.get.side_effect = [row, version]
    session.scalar = AsyncMock(return_value=project_id)
    session.commit = AsyncMock()
    settings = SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=True)

    with patch(
        "src.services.agent.runtime_snapshot.get_settings", return_value=settings
    ):
        result = await load_project_skill_from_snapshot(
            session,
            snapshot_id=str(snapshot_id),
            user_id=str(user_id),
            project_id=str(project_id),
            skill_name="literature-review",
        )

    assert result["instructions"] == "Use only the frozen version."
    assert result["version"] == 2
    assert row.loaded_skill_versions == [
        {
            "version_id": str(version_id),
            "name": "literature-review",
            "content_hash": "61d63d5b5fb1a48c0cb7dc3dcc53763eb1290295bf6e52d9606687a3ceb6271c",
            "token_count": 7,
        }
    ]
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_loader_rejects_wrong_project_and_unknown_name() -> None:
    from src.services.agent.runtime_snapshot import load_project_skill_from_snapshot

    snapshot_id = uuid4()
    user_id = uuid4()
    project_id = uuid4()
    row = SimpleNamespace(
        id=snapshot_id,
        user_id=user_id,
        project_id=project_id,
        expires_at=None,
        skill_catalog=[],
        loaded_skill_versions=[
            {"name": "one", "token_count": 1},
            {"name": "two", "token_count": 1},
            {"name": "three", "token_count": 1},
        ],
    )
    session = AsyncMock()
    session.get = AsyncMock(return_value=row)
    settings = SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=True)

    with patch(
        "src.services.agent.runtime_snapshot.get_settings", return_value=settings
    ):
        cross_project = await load_project_skill_from_snapshot(
            session,
            snapshot_id=str(snapshot_id),
            user_id=str(user_id),
            project_id=str(uuid4()),
            skill_name="missing",
        )
        unknown = await load_project_skill_from_snapshot(
            session,
            snapshot_id=str(snapshot_id),
            user_id=str(user_id),
            project_id=str(project_id),
            skill_name="missing",
        )

    assert cross_project["error_type"] == "runtime_snapshot_unavailable"
    assert unknown["error_type"] == "skill_not_in_snapshot"


@pytest.mark.asyncio
async def test_loader_rejects_a_fourth_unique_skill_before_reading_the_version() -> (
    None
):
    from src.services.agent.runtime_snapshot import load_project_skill_from_snapshot

    snapshot_id = uuid4()
    user_id = uuid4()
    project_id = uuid4()
    row = SimpleNamespace(
        id=snapshot_id,
        user_id=user_id,
        project_id=project_id,
        expires_at=None,
        **_snapshot_registry_fields(),
        skill_catalog=[
            {
                "version_id": str(uuid4()),
                "name": "four",
                "version": 1,
                "content_hash": "d" * 64,
            }
        ],
        loaded_skill_versions=[
            {"name": "one", "token_count": 1},
            {"name": "two", "token_count": 1},
            {"name": "three", "token_count": 1},
        ],
    )
    session = AsyncMock()
    session.get = AsyncMock(return_value=row)
    settings = SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=True)

    with patch(
        "src.services.agent.runtime_snapshot.get_settings", return_value=settings
    ):
        result = await load_project_skill_from_snapshot(
            session,
            snapshot_id=str(snapshot_id),
            user_id=str(user_id),
            project_id=str(project_id),
            skill_name="four",
        )

    assert result["error_type"] == "project_skill_load_limit"
    assert session.get.await_count == 1


@pytest.mark.asyncio
async def test_loader_enforces_aggregate_token_limit_before_returning_instructions() -> (
    None
):
    from src.services.agent.runtime_snapshot import load_project_skill_from_snapshot

    snapshot_id = uuid4()
    user_id = uuid4()
    project_id = uuid4()
    version_id = uuid4()
    row = SimpleNamespace(
        id=snapshot_id,
        user_id=user_id,
        project_id=project_id,
        expires_at=None,
        **_snapshot_registry_fields(),
        skill_catalog=[
            {
                "version_id": str(version_id),
                "name": "large-skill",
                "version": 1,
                "content_hash": "f8efef2f7535e6a9651bd6d128c8f39e3121d4da8d4f1672350a3b8d40c71ecc",
            }
        ],
        loaded_skill_versions=[{"name": "prior", "token_count": 12_000}],
    )
    version = SimpleNamespace(
        id=version_id,
        parsed_name="large-skill",
        version=1,
        content_hash="f8efef2f7535e6a9651bd6d128c8f39e3121d4da8d4f1672350a3b8d40c71ecc",
        instructions="still must not be returned",
    )
    session = AsyncMock()
    session.get.side_effect = [row, version]
    session.scalar = AsyncMock(return_value=project_id)
    settings = SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=True)

    with patch(
        "src.services.agent.runtime_snapshot.get_settings", return_value=settings
    ):
        result = await load_project_skill_from_snapshot(
            session,
            snapshot_id=str(snapshot_id),
            user_id=str(user_id),
            project_id=str(project_id),
            skill_name="large-skill",
        )

    assert result["error_type"] == "project_skill_token_limit"


@pytest.mark.asyncio
async def test_loader_rejects_mutated_instructions_with_a_stale_stored_hash() -> None:
    from src.services.agent.runtime_snapshot import load_project_skill_from_snapshot

    snapshot_id, user_id, project_id, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    expected_hash = "61d63d5b5fb1a48c0cb7dc3dcc53763eb1290295bf6e52d9606687a3ceb6271c"
    row = SimpleNamespace(
        id=snapshot_id,
        user_id=user_id,
        project_id=project_id,
        expires_at=None,
        **_snapshot_registry_fields(),
        skill_catalog=[
            {
                "version_id": str(version_id),
                "name": "literature-review",
                "version": 2,
                "content_hash": expected_hash,
            }
        ],
        loaded_skill_versions=[],
    )
    version = SimpleNamespace(
        id=version_id,
        parsed_name="literature-review",
        version=2,
        content_hash=expected_hash,
        instructions="mutated after approval",
    )
    session = AsyncMock()
    session.get.side_effect = [row, version]
    session.scalar = AsyncMock(return_value=project_id)
    with patch(
        "src.services.agent.runtime_snapshot.get_settings",
        return_value=SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=True),
    ):
        result = await load_project_skill_from_snapshot(
            session,
            snapshot_id=str(snapshot_id),
            user_id=str(user_id),
            project_id=str(project_id),
            skill_name="literature-review",
        )
    assert result["error_type"] == "skill_version_unavailable"


@pytest.mark.asyncio
async def test_loader_rejects_snapshot_version_owned_by_another_project() -> None:
    from src.services.agent.runtime_snapshot import load_project_skill_from_snapshot

    snapshot_id, user_id, project_id, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    content_hash = "61d63d5b5fb1a48c0cb7dc3dcc53763eb1290295bf6e52d9606687a3ceb6271c"
    row = SimpleNamespace(
        id=snapshot_id,
        user_id=user_id,
        project_id=project_id,
        expires_at=None,
        **_snapshot_registry_fields(),
        skill_catalog=[
            {
                "version_id": str(version_id),
                "name": "literature-review",
                "version": 2,
                "content_hash": content_hash,
            }
        ],
        loaded_skill_versions=[],
    )
    version = SimpleNamespace(
        id=version_id,
        parsed_name="literature-review",
        version=2,
        content_hash=content_hash,
        instructions="Use only the frozen version.",
    )
    session = AsyncMock()
    session.get.side_effect = [row, version]
    session.scalar = AsyncMock(return_value=uuid4())
    with patch(
        "src.services.agent.runtime_snapshot.get_settings",
        return_value=SimpleNamespace(PROJECT_SKILL_RUNTIME_ENABLED=True),
    ):
        result = await load_project_skill_from_snapshot(
            session,
            snapshot_id=str(snapshot_id),
            user_id=str(user_id),
            project_id=str(project_id),
            skill_name="literature-review",
        )
    assert result["error_type"] == "skill_version_unavailable"
