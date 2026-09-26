"""PostgreSQL-backed HTTP proof for bounded draft source scope."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, AsyncIterator, cast
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.research.drafts import router as drafts_router
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.draft_citation import DraftCitation
from src.models.generated_draft import GeneratedDraft
from src.models.organization import Organization
from src.models.user import User
from src.models.workspace import Workspace
from src.services.research.draft_generation_service import DraftGenerationService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_postgres,
    pytest.mark.asyncio,
]


@dataclass(frozen=True)
class _DraftDatabase:
    factory: async_sessionmaker[AsyncSession]
    user_id: UUID
    project_id: UUID
    document_ids: tuple[UUID, UUID, UUID]


def _async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    raise ValueError("ORCHESTRATION_TEST_DATABASE_URL must be a PostgreSQL URL")


def _ensure_fixture_encryption() -> Any:
    from src.core import encryption

    try:
        return encryption.get_field_encryption()
    except encryption.EncryptionError:
        manager = object.__new__(encryption.KeyManager)
        manager.master_key_env_var = "TASK_4_TEST_ONLY"
        manager._keys = {}
        manager._master_key = b"\x00" * 32
        manager.generate_key(encryption.EncryptionKeyType.DATA)
        aes = encryption.AESEncryption(manager)
        field_encryption = encryption.FieldEncryption(aes)
        encryption._key_manager = manager
        encryption._aes_encryption = aes
        encryption._field_encryption = field_encryption
        return field_encryption


@asynccontextmanager
async def _postgres_draft_schema(dsn: str) -> AsyncIterator[_DraftDatabase]:
    field_encryption = _ensure_fixture_encryption()
    schema = "draft_scope_" + uuid4().hex
    admin_engine = create_async_engine(_async_dsn(dsn))
    scoped_engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

        scoped_engine = create_async_engine(
            _async_dsn(dsn),
            connect_args={"server_settings": {"search_path": schema}},
        )
        async with scoped_engine.begin() as connection:
            # Citation is nullable for source-backed drafts. Its production
            # table references chat_messages, so the bounded schema needs only
            # the primary key targeted by DraftCitation.citation_id.
            await connection.exec_driver_sql(
                'CREATE TABLE "citations" (id UUID PRIMARY KEY)'
            )
            for model in (
                Organization,
                User,
                Workspace,
                Collection,
                Document,
                CollectionDocument,
                GeneratedDraft,
                DraftCitation,
            ):
                await connection.run_sync(cast(Any, model).__table__.create)

        factory = async_sessionmaker(scoped_engine, expire_on_commit=False)
        user_id = uuid4()
        organization_id = uuid4()
        project_id = uuid4()
        workspace_id = uuid4()
        document_ids = (uuid4(), uuid4(), uuid4())
        async with factory() as setup:
            await setup.execute(
                text("""INSERT INTO organizations (
                        id, created_at, updated_at, is_deleted, name, storage_tier,
                        storage_used_bytes, storage_limit_bytes, is_active
                    ) VALUES (
                        :id, now(), now(), false, :name, 'FREE', 0, 10737418240, true
                    )"""),
                {"id": organization_id, "name": f"draft-{schema}"},
            )
            await setup.execute(
                text("""INSERT INTO users (
                        id, created_at, updated_at, is_deleted, email, password_hash,
                        first_name, last_name, role, is_active, organization_id, login_count
                    ) VALUES (
                        :id, now(), now(), false, :email, 'unused-test-hash',
                        :first_name, :last_name, 'USER', true, :organization_id, 0
                    )"""),
                {
                    "id": user_id,
                    "email": f"{user_id}@example.test",
                    "first_name": field_encryption.encrypt_field("Task", "first_name"),
                    "last_name": field_encryption.encrypt_field("Four", "last_name"),
                    "organization_id": organization_id,
                },
            )
            setup.add(
                Workspace(
                    id=workspace_id,
                    name="draft source scope test",
                    owner_id=user_id,
                    organization_id=organization_id,
                )
            )
            setup.add(
                Collection(
                    id=project_id,
                    name="bounded source project",
                    workspace_id=workspace_id,
                )
            )
            titles = ("Selected source A", "Uncited selected source B", "New source C")
            contents = (
                "Evidence unique to source A.",
                "Evidence unique to source B.",
                "Private evidence from newly attached source C.",
            )
            for index, (document_id, title, content) in enumerate(
                zip(document_ids, titles, contents)
            ):
                setup.add(
                    Document(
                        id=document_id,
                        title=title,
                        filename=f"{document_id}.txt",
                        file_path=f"/unused/{document_id}.txt",
                        file_size_bytes=len(content),
                        mime_type="text/plain",
                        document_type=DocumentType.TEXT,
                        processing_status=ProcessingStatus.COMPLETED,
                        content_text=content,
                        content_summary=content,
                        organization_id=organization_id,
                        is_deleted=False,
                    )
                )
                if index < 2:
                    setup.add(
                        CollectionDocument(
                            collection_id=project_id,
                            document_id=document_id,
                            is_deleted=False,
                        )
                    )
            await setup.commit()

        yield _DraftDatabase(factory, user_id, project_id, document_ids)
    finally:
        if scoped_engine is not None:
            await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        await admin_engine.dispose()


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.on_create: Any = None
        self.responses = [
            "A finding from the first selected source [Doc 1].",
            "A constrained synthesis from source A [Doc 1].",
            "A revised finding from source A [Doc 1]. A source B finding [Doc 2].",
            "A stale-scope finding from source A [Doc 1].",
        ]

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.on_create is not None:
            await self.on_create()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=self.responses[
                            min(len(self.calls) - 1, len(self.responses) - 1)
                        ]
                    )
                )
            ]
        )


async def test_http_generation_uses_selected_scope_and_persists_postgres_rows() -> None:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")

    async with _postgres_draft_schema(dsn) as database:

        async def override_db() -> AsyncIterator[AsyncSession]:
            async with database.factory() as session:
                yield session

        app = FastAPI()
        app.include_router(drafts_router)
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=database.user_id
        )
        completions = _FakeCompletions()
        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        async def passing_review(
            _db: AsyncSession, content: str, _documents: list[Document]
        ) -> dict[str, Any]:
            indices = DraftGenerationService._citation_indices(content)
            return {
                "verdicts": [
                    {
                        "doc_index": index,
                        "verdict": "exact",
                        "evidence": f"Grounded source evidence {index}.",
                        "page_number": index,
                        "location": f"Page {index}",
                    }
                    for index in indices
                ],
                "summary": {
                    "exact": len(indices),
                    "minor": 0,
                    "major": 0,
                    "unverified": 0,
                },
                "docs_checked": len(indices),
                "docs_skipped": 0,
            }

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            try:
                with (
                    patch(
                        "src.services.research.draft_generation_service.AsyncSessionLocal",
                        database.factory,
                    ),
                    patch(
                        "src.services.research.draft_generation_service.get_redis",
                        new=AsyncMock(return_value=None),
                    ),
                    patch.object(
                        DraftGenerationService,
                        "_init_openai_client",
                        return_value=(fake_client, "gpt-4o-test"),
                    ),
                    patch.object(
                        DraftGenerationService,
                        "_review_citations",
                        new=AsyncMock(side_effect=passing_review),
                    ),
                ):
                    selected_ids = tuple(sorted(database.document_ids[:2]))
                    response = await client.post(
                        f"/api/v1/projects/{database.project_id}/drafts",
                        params=[
                            ("themes", "bounded evidence"),
                            ("document_ids", str(selected_ids[0])),
                            ("document_ids", str(selected_ids[1])),
                        ],
                    )
                    assert response.status_code == 202, response.text
                    started = response.json()
                    assert started["selection_mode"] == "explicit"
                    assert started["document_ids"] == sorted(
                        str(document_id) for document_id in selected_ids
                    )

                    final_status = (
                        await DraftGenerationService.wait_for_terminal_status(
                            started["task_id"], timeout_seconds=10, poll_interval=0.05
                        )
                    )
                    assert final_status["status"] == "completed", final_status
                    assert final_status["selection_mode"] == "explicit"
                    assert final_status["document_ids"] == started["document_ids"]
                    assert len(final_status["generation_request_hash"]) == 64

                    status_response = await client.get(
                        f"/api/v1/projects/{database.project_id}/drafts/status",
                        params={"task_id": started["task_id"]},
                    )
                    assert status_response.status_code == 200
                    assert status_response.json()["selection_mode"] == "explicit"
                    assert (
                        status_response.json()["document_ids"]
                        == started["document_ids"]
                    )

                    assert len(completions.calls) == 1
                    user_prompt = completions.calls[0]["messages"][1]["content"]
                    assert "Selected source A" in user_prompt
                    assert "Uncited selected source B" in user_prompt
                    assert "New source C" not in user_prompt
                    assert (
                        "Private evidence from newly attached source C"
                        not in user_prompt
                    )
                    assert (
                        '<untrusted_content source="document_evidence">' in user_prompt
                    )

                    async with database.factory() as verify:
                        draft = (
                            await verify.execute(
                                select(GeneratedDraft).where(
                                    GeneratedDraft.project_id == database.project_id
                                )
                            )
                        ).scalar_one()
                        citation_rows = list(
                            (
                                await verify.execute(
                                    select(DraftCitation)
                                    .where(DraftCitation.draft_id == draft.id)
                                    .order_by(DraftCitation.citation_index)
                                )
                            )
                            .scalars()
                            .all()
                        )
                    params = draft.generation_params
                    assert params["source_scope_version"] == 1
                    assert params["selection_mode"] == "explicit"
                    assert params["document_ids"] == started["document_ids"]
                    assert (
                        params["generation_request_hash"]
                        == final_status["generation_request_hash"]
                    )
                    assert params["instructions"] is None
                    assert len(citation_rows) == 1
                    assert {row.document_id for row in citation_rows} == {
                        selected_ids[0]
                    }
                    assert {row.citation_index for row in citation_rows} == {1}
                    assert database.document_ids[2] not in {
                        row.document_id for row in citation_rows
                    }

                    # C is attached only after version 1 has been saved. The
                    # next create_draft call still keeps the original A/B
                    # selection and leaves B uncited in its generated prose.
                    async with database.factory() as attach:
                        attach.add(
                            CollectionDocument(
                                collection_id=database.project_id,
                                document_id=database.document_ids[2],
                                is_deleted=False,
                            )
                        )
                        await attach.commit()

                    # Exercise the actual agent tool implementation with a
                    # user instruction while keeping the fake at the model
                    # boundary. Its Task 2 dispatch recorder shape remains
                    # unchanged; the terminal status carries effective scope.
                    from src.services.agent.tools_impl import _tool_create_draft

                    recorder_payloads: list[dict[str, Any]] = []

                    async def record_dispatch(payload: dict[str, Any]) -> None:
                        recorder_payloads.append(payload)

                    exact_instruction = (
                        "Use only the requested sources and keep the synthesis concise."
                    )
                    async with database.factory() as tool_session:
                        tool_result = await _tool_create_draft(
                            {
                                "project_id": str(database.project_id),
                                "themes": ["bounded evidence"],
                                "document_ids": [str(item) for item in selected_ids],
                                "instructions": exact_instruction,
                            },
                            tool_session,
                            SimpleNamespace(id=database.user_id),
                            dispatch_recorder=record_dispatch,
                        )
                    assert tool_result["status"] == "completed", tool_result
                    assert tool_result["selection_mode"] == "explicit"
                    assert tool_result["document_ids"] == started["document_ids"]
                    assert recorder_payloads == [
                        {
                            "task_id": tool_result["task_id"],
                            "project_id": str(database.project_id),
                            "project_name": "bounded source project",
                            "user_id": str(database.user_id),
                            "status": "pending",
                            "message": "Draft generation started",
                        }
                    ]
                    assert len(completions.calls) == 2
                    second_prompt = completions.calls[1]["messages"][1]["content"]
                    assert exact_instruction in second_prompt
                    assert (
                        "Private evidence from newly attached source C"
                        not in second_prompt
                    )

                    async with database.factory() as verify:
                        drafts = list(
                            (
                                await verify.execute(
                                    select(GeneratedDraft)
                                    .where(
                                        GeneratedDraft.project_id == database.project_id
                                    )
                                    .order_by(GeneratedDraft.version)
                                )
                            )
                            .scalars()
                            .all()
                        )
                        second_citations = list(
                            (
                                await verify.execute(
                                    select(DraftCitation)
                                    .where(DraftCitation.draft_id == drafts[1].id)
                                    .order_by(DraftCitation.citation_index)
                                )
                            )
                            .scalars()
                            .all()
                        )
                    assert len(drafts) == 2
                    assert (
                        drafts[1].generation_params["instructions"] == exact_instruction
                    )
                    assert drafts[1].generation_params["selection_mode"] == "explicit"
                    assert (
                        drafts[1].generation_params["document_ids"]
                        == started["document_ids"]
                    )
                    assert len(second_citations) == 1
                    assert second_citations[0].document_id == selected_ids[0]

                    # Revising the saved version uses its durable A/B scope,
                    # reconstructs the original [Doc 1] -> A identity, and
                    # assigns uncited B to [Doc 2]; newly attached C stays out.
                    from src.services.agent.tools_impl import _tool_revise_draft

                    async with database.factory() as revision_session:
                        revision_result = await _tool_revise_draft(
                            {
                                "project_id": str(database.project_id),
                                "instructions": "Clarify the relation between sources.",
                            },
                            revision_session,
                            SimpleNamespace(id=database.user_id),
                        )
                    assert revision_result["version"] == 3, revision_result
                    assert len(completions.calls) == 3
                    revision_prompt = completions.calls[2]["messages"][1]["content"]
                    assert (
                        "Original generation instructions (user task instructions):"
                        in revision_prompt
                    )
                    assert exact_instruction in revision_prompt
                    assert (
                        "Current revision instructions (user task instructions):"
                        in revision_prompt
                    )
                    assert "Clarify the relation between sources." in revision_prompt
                    assert "Selected source A" in revision_prompt
                    assert "Uncited selected source B" in revision_prompt
                    assert "New source C" not in revision_prompt
                    assert (
                        "Private evidence from newly attached source C"
                        not in revision_prompt
                    )

                    async with database.factory() as verify:
                        revision = (
                            await verify.execute(
                                select(GeneratedDraft).where(
                                    GeneratedDraft.project_id == database.project_id,
                                    GeneratedDraft.version == 3,
                                )
                            )
                        ).scalar_one()
                        revision_citations = list(
                            (
                                await verify.execute(
                                    select(DraftCitation)
                                    .where(DraftCitation.draft_id == revision.id)
                                    .order_by(DraftCitation.citation_index)
                                )
                            )
                            .scalars()
                            .all()
                        )
                    assert revision.generation_params["selection_mode"] == "explicit"
                    assert revision.is_current is True

                    # Remove selected B while the model call is in flight. The
                    # model saw the originally authorized A/B snapshot, but
                    # the generated result must not be promoted after B is no
                    # longer an active project source.
                    async def detach_selected_b() -> None:
                        async with database.factory() as detach:
                            link = (
                                await detach.execute(
                                    select(CollectionDocument).where(
                                        CollectionDocument.collection_id
                                        == database.project_id,
                                        CollectionDocument.document_id
                                        == database.document_ids[1],
                                    )
                                )
                            ).scalar_one()
                            link.is_deleted = True
                            await detach.commit()

                    completions.on_create = detach_selected_b
                    late_response = await client.post(
                        f"/api/v1/projects/{database.project_id}/drafts",
                        params=[
                            ("themes", "late deletion check"),
                            ("document_ids", str(selected_ids[0])),
                            ("document_ids", str(selected_ids[1])),
                        ],
                    )
                    assert late_response.status_code == 202, late_response.text
                    late_status = await DraftGenerationService.wait_for_terminal_status(
                        late_response.json()["task_id"],
                        timeout_seconds=10,
                        poll_interval=0.05,
                    )
                    assert late_status["status"] == "failed", late_status
                    assert len(completions.calls) == 4
                    late_prompt = completions.calls[3]["messages"][1]["content"]
                    assert "Uncited selected source B" in late_prompt

                    async with database.factory() as verify:
                        all_drafts = list(
                            (
                                await verify.execute(
                                    select(GeneratedDraft)
                                    .where(
                                        GeneratedDraft.project_id == database.project_id
                                    )
                                    .order_by(GeneratedDraft.version)
                                )
                            )
                            .scalars()
                            .all()
                        )
                    assert [draft.version for draft in all_drafts] == [1, 2, 3]
                    assert all_drafts[-1].is_current is True
                    assert all(draft.is_current is False for draft in all_drafts[:-1])
                    assert (
                        revision.generation_params["document_ids"]
                        == started["document_ids"]
                    )
                    assert revision.generation_params["generation_request_hash"] == (
                        drafts[1].generation_params["generation_request_hash"]
                    )
                    assert [row.citation_index for row in revision_citations] == [1, 2]
                    assert [row.document_id for row in revision_citations] == list(
                        selected_ids
                    )
                    assert [row.citation_index for row in second_citations] == [1]
                    assert [row.document_id for row in second_citations] == [
                        selected_ids[0]
                    ]
            finally:
                app.dependency_overrides.clear()
                pending = [
                    task
                    for task in DraftGenerationService._background_tasks
                    if not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
