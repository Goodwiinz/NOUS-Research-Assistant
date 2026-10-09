"""Browser selection freezes only approved versions under renewable consent."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.agent_runtime_snapshot import AgentRuntimeSnapshot
from src.models.integration_context_selection import IntegrationContextSelection
from src.models.integration_grant import IntegrationGrantRequest
from src.models.project_skill import (
    ProjectSkill,
    ProjectSkillVersion,
    ProjectSkillVersionScan,
)
from src.models.workspace import Workspace
from src.services.integrations import selected_context as service
from tests.unit.services.integrations.test_selected_context import (  # noqa: F401
    CONSENT,
    KEEP,
    PROJECT,
    RENEWED,
    USER,
    WORKSPACE,
    _grant_context,
    _memory_ids,
    _user,
    db,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
async def skill_tables(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = await db.connection()
    for model in (
        ProjectSkill,
        ProjectSkillVersion,
        ProjectSkillVersionScan,
        AgentRuntimeSnapshot,
    ):
        await conn.run_sync(model.__table__.create)
    monkeypatch.setattr(settings, "PROJECT_SKILL_CATALOG_ENABLED", True)
    monkeypatch.setattr(settings, "PROJECT_SKILL_RUNTIME_ENABLED", True)
    await db.commit()


async def skill(
    db: AsyncSession,
    name: str = "review",
    *,
    passed: bool = True,
    project_id: UUID = PROJECT,
    instructions: str = "Use the approved rubric.",
) -> UUID:
    row = ProjectSkill(project_id=project_id, normalized_name=name, created_by_id=USER)
    db.add(row)
    await db.flush()
    version = ProjectSkillVersion(
        skill_id=row.id,
        version=1,
        instructions=instructions,
        parsed_name=name,
        description="Review rubric",
        content_hash=sha256(instructions.encode()).hexdigest(),
        author_id=USER,
    )
    db.add(version)
    await db.flush()
    db.add(
        ProjectSkillVersionScan(
            version_id=version.id,
            scan_state="passed" if passed else "blocked",
            scanner_version="test",
            scanned_by_id=USER,
        )
    )
    row.active_version_id = version.id
    await db.commit()
    return UUID(str(version.id))


async def test_freezes_only_explicit_skill_and_preserves_memory_only_save(
    db: AsyncSession,
) -> None:
    chosen = await skill(db)
    secret = await skill(
        db, "private-rubric", instructions="Unselected secret instructions"
    )
    before = await service.context_options(db, await _user(db), CONSENT)
    assert before.selected_skill_version_ids == []
    saved = await service.save_selection(
        db, await _user(db), CONSENT, [KEEP], skill_version_ids=[chosen]
    )
    assert saved.selected_skill_version_ids == [chosen]
    selection = await db.scalar(select(IntegrationContextSelection))
    assert selection is not None
    snapshot_id = selection.runtime_snapshot_id
    snapshot: Any = await db.get(AgentRuntimeSnapshot, snapshot_id)
    assert [v["version_id"] for v in snapshot.skill_catalog] == [str(chosen)]
    assert snapshot.thread_id is None and snapshot.job_id is None
    result = await service.read_selected_context(db, _grant_context(RENEWED))
    assert _memory_ids(result) == [str(KEEP)]
    assert result.content[0]["skills"][0]["version_id"] == str(chosen)
    assert str(secret) not in str(result) and "instructions" not in str(result)
    loaded = await service.load_selected_skill(db, _grant_context(), "review")
    assert loaded.content[0]["instructions"] == "Use the approved rubric."
    assert loaded.source_refs[0]["version_id"] == str(chosen)
    assert (
        await service.load_selected_skill(db, _grant_context(), "private-rubric")
    ).is_error
    await service.save_selection(db, await _user(db), CONSENT, [])
    assert selection.runtime_snapshot_id == snapshot_id
    assert selection.skill_version_ids == [str(chosen)]
    assert len(snapshot.loaded_skill_versions) == 1


async def test_live_activation_does_not_change_frozen_skill(db: AsyncSession) -> None:
    chosen = await skill(db)
    await service.save_selection(
        db, await _user(db), CONSENT, [], skill_version_ids=[chosen]
    )
    version: Any = await db.get(ProjectSkillVersion, chosen)
    changed = ProjectSkillVersion(
        skill_id=version.skill_id,
        version=2,
        instructions="New rubric",
        parsed_name="review",
        description="Changed",
        content_hash=sha256(b"New rubric").hexdigest(),
        author_id=USER,
    )
    db.add(changed)
    await db.flush()
    await db.execute(
        update(ProjectSkill)
        .where(ProjectSkill.id == version.skill_id)
        .values(active_version_id=changed.id)
    )
    await db.commit()
    result = await service.load_selected_skill(db, _grant_context(RENEWED), "review")
    assert result.content[0]["version_id"] == str(chosen)
    assert result.content[0]["instructions"] == "Use the approved rubric."


async def test_expired_snapshot_requires_explicit_browser_refresh(
    db: AsyncSession,
) -> None:
    chosen = await skill(db)
    await service.save_selection(
        db, await _user(db), CONSENT, [KEEP], skill_version_ids=[chosen]
    )
    selection = await db.scalar(select(IntegrationContextSelection))
    assert selection is not None
    before = selection.runtime_snapshot_id
    await db.execute(
        update(AgentRuntimeSnapshot).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    )
    await db.commit()
    options = await service.context_options(db, await _user(db), CONSENT)
    assert options.skill_snapshot_status == "unavailable"
    assert (await service.read_selected_context(db, _grant_context())).is_error
    await service.save_selection(db, await _user(db), CONSENT, [KEEP])
    assert selection.runtime_snapshot_id == before
    refreshed = await service.save_selection(
        db,
        await _user(db),
        CONSENT,
        [KEEP],
        skill_version_ids=[chosen],
        refresh_skills=True,
    )
    assert refreshed.skill_snapshot_status == "ready"
    assert selection.runtime_snapshot_id != before


@pytest.mark.parametrize(
    "condition", ["foreign", "blocked", "deleted-workspace", "revoked-consent"]
)
async def test_selection_cannot_expand_authority(
    db: AsyncSession, condition: str
) -> None:
    chosen = await skill(
        db,
        project_id=uuid4() if condition == "foreign" else PROJECT,
        passed=condition != "blocked",
    )
    if condition == "deleted-workspace":
        await db.execute(
            update(Workspace).where(Workspace.id == WORKSPACE).values(is_deleted=True)
        )
    if condition == "revoked-consent":
        await db.execute(
            update(IntegrationGrantRequest)
            .where(IntegrationGrantRequest.id == CONSENT)
            .values(consent_revoked_at=datetime.now(timezone.utc))
        )
    await db.commit()
    with pytest.raises((service.ContextSelectionInvalid, service.ContextNotFound)):
        await service.save_selection(
            db, await _user(db), CONSENT, [KEEP], skill_version_ids=[chosen]
        )
    assert await db.scalar(select(func.count()).select_from(AgentRuntimeSnapshot)) == 0


async def test_expired_choices_stay_visible_after_live_pointer_changes(
    db: AsyncSession,
) -> None:
    chosen = await skill(db)
    await service.save_selection(
        db, await _user(db), CONSENT, [KEEP], skill_version_ids=[chosen]
    )
    await db.execute(
        update(AgentRuntimeSnapshot).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    )
    await db.execute(update(ProjectSkill).values(active_version_id=None))
    await db.commit()
    options = await service.context_options(db, await _user(db), CONSENT)
    assert options.skill_snapshot_status == "unavailable"
    assert [item.version_id for item in options.skills] == [chosen]


async def test_load_limits_survive_renewal_and_memory_only_saves(
    db: AsyncSession,
) -> None:
    ids = [await skill(db, f"rubric-{index}") for index in range(4)]
    await service.save_selection(
        db, await _user(db), CONSENT, [], skill_version_ids=ids
    )
    for index in range(3):
        assert not (
            await service.load_selected_skill(db, _grant_context(), f"rubric-{index}")
        ).is_error
    await service.save_selection(db, await _user(db), CONSENT, [KEEP])
    fourth = await service.load_selected_skill(db, _grant_context(RENEWED), "rubric-3")
    assert fourth.is_error and fourth.content[0]["error"] == "project_skill_load_limit"
    assert not (
        await service.load_selected_skill(db, _grant_context(RENEWED), "rubric-0")
    ).is_error
    snapshot: Any = await db.scalar(select(AgentRuntimeSnapshot))
    assert len(snapshot.loaded_skill_versions) == 3


@pytest.mark.parametrize(
    "instructions, error",
    [
        ("x" * 48004, "project_skill_token_limit"),
        ("界" * 41000, "skill_result_too_large"),
    ],
    ids=["tokens", "wire"],
)
async def test_instruction_token_and_utf8_wire_limits_are_durable(
    db: AsyncSession, instructions: str, error: str
) -> None:
    chosen = await skill(db, instructions=instructions)
    await service.save_selection(
        db, await _user(db), CONSENT, [], skill_version_ids=[chosen]
    )
    result = await service.load_selected_skill(db, _grant_context(), "review")
    assert result.is_error and result.content[0]["error"] == error
    snapshot: Any = await db.scalar(select(AgentRuntimeSnapshot))
    assert snapshot.loaded_skill_versions == []


@pytest.mark.parametrize(
    "condition",
    [
        "revoked-grant",
        "revoked-consent",
        "foreign-user",
        "foreign-organization",
        "deleted-project",
        "deleted-workspace",
        "missing-snapshot",
        "tampered-version",
    ],
)
async def test_load_rechecks_live_authority_and_snapshot(
    db: AsyncSession, condition: str
) -> None:
    from src.models.collection import Collection
    from src.models.integration_grant import IntegrationGrant
    from tests.unit.services.integrations.test_selected_context import GRANT

    chosen = await skill(db)
    await service.save_selection(
        db, await _user(db), CONSENT, [KEEP], skill_version_ids=[chosen]
    )
    context = _grant_context()
    if condition == "revoked-grant":
        await db.execute(
            update(IntegrationGrant)
            .where(IntegrationGrant.id == GRANT)
            .values(revoked_at=datetime.now(timezone.utc))
        )
    elif condition == "revoked-consent":
        await db.execute(
            update(IntegrationGrantRequest)
            .where(IntegrationGrantRequest.id == CONSENT)
            .values(consent_revoked_at=datetime.now(timezone.utc))
        )
    elif condition == "foreign-user":
        context = context.model_copy(update={"user_id": uuid4()})
    elif condition == "foreign-organization":
        context = context.model_copy(update={"organization_id": uuid4()})
    elif condition == "deleted-project":
        await db.execute(
            update(Collection).where(Collection.id == PROJECT).values(is_deleted=True)
        )
    elif condition == "deleted-workspace":
        await db.execute(
            update(Workspace).where(Workspace.id == WORKSPACE).values(is_deleted=True)
        )
    elif condition == "missing-snapshot":
        await db.execute(
            update(IntegrationContextSelection).values(runtime_snapshot_id=None)
        )
    else:
        await db.execute(
            update(ProjectSkillVersion)
            .where(ProjectSkillVersion.id == chosen)
            .values(instructions="Mutated instructions")
        )
    await db.commit()
    result = await service.load_selected_skill(db, context, "review")
    assert result.is_error and "instructions" not in str(result.content)
    snapshot: Any = await db.scalar(select(AgentRuntimeSnapshot))
    assert snapshot.loaded_skill_versions == []


async def test_failed_snapshot_write_rolls_back_memory_and_skill_selection(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.agent import runtime_snapshot

    chosen = await skill(db)
    await service.save_selection(db, await _user(db), CONSENT, [KEEP])
    original = runtime_snapshot.create_runtime_snapshot

    async def fail_after_flush(*args: Any, **kwargs: Any) -> Any:
        await original(*args, **kwargs)
        raise RuntimeError("snapshot persistence failed")

    monkeypatch.setattr(runtime_snapshot, "create_runtime_snapshot", fail_after_flush)
    with pytest.raises(RuntimeError):
        await service.save_selection(
            db, await _user(db), CONSENT, [], skill_version_ids=[chosen]
        )
    selection = await db.scalar(select(IntegrationContextSelection))
    assert selection is not None
    assert selection.memory_ids == [str(KEEP)] and selection.skill_version_ids == []
    assert await db.scalar(select(func.count()).select_from(AgentRuntimeSnapshot)) == 0


async def test_active_snapshot_refresh_cannot_reset_load_receipts(
    db: AsyncSession,
) -> None:
    chosen = await skill(db)
    await service.save_selection(
        db, await _user(db), CONSENT, [], skill_version_ids=[chosen]
    )
    await service.load_selected_skill(db, _grant_context(), "review")
    with pytest.raises(service.ContextSelectionInvalid):
        await service.save_selection(
            db,
            await _user(db),
            CONSENT,
            [],
            skill_version_ids=[chosen],
            refresh_skills=True,
        )
    snapshot: Any = await db.scalar(select(AgentRuntimeSnapshot))
    assert len(snapshot.loaded_skill_versions) == 1


async def test_load_commit_failure_never_returns_instructions_or_receipt(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = await skill(db)
    await service.save_selection(
        db, await _user(db), CONSENT, [], skill_version_ids=[chosen]
    )

    async def fail_commit() -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError):
        await service.load_selected_skill(db, _grant_context(), "review")
    snapshot: Any = await db.scalar(select(AgentRuntimeSnapshot))
    assert snapshot.loaded_skill_versions == []


async def test_explicit_empty_skill_selection_preserves_memories(
    db: AsyncSession,
) -> None:
    chosen = await skill(db)
    await service.save_selection(
        db, await _user(db), CONSENT, [KEEP], skill_version_ids=[chosen]
    )
    result = await service.save_selection(
        db, await _user(db), CONSENT, [KEEP], skill_version_ids=[]
    )
    assert (
        result.skill_snapshot_status == "none"
        and result.selected_skill_version_ids == []
    )
    read = await service.read_selected_context(db, _grant_context())
    assert not read.is_error and _memory_ids(read) == [str(KEEP)]
    assert (await service.load_selected_skill(db, _grant_context(), "review")).is_error


async def test_result_budget_includes_catalog_and_all_provenance(
    db: AsyncSession,
) -> None:
    from src.models.project_memory import ProjectMemory

    chosen = await skill(db)
    await service.save_selection(
        db, await _user(db), CONSENT, [KEEP], skill_version_ids=[chosen]
    )
    await db.execute(
        update(ProjectMemory)
        .where(ProjectMemory.id == KEEP)
        .values(content="x" * (service.MAX_RESULT_BYTES - 100))
    )
    await db.commit()
    result = await service.read_selected_context(db, _grant_context())
    assert (
        service._wire_bytes(result.model_dump(mode="json")) <= service.MAX_RESULT_BYTES
    )
    assert result.content[0]["truncated"] is True
    assert result.source_refs == [
        {
            "version_id": str(chosen),
            "content_hash": result.content[0]["skills"][0]["content_hash"],
        }
    ]
