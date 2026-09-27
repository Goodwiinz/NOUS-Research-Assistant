"""PostgreSQL durability coverage for rejected draft candidates."""

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models import Base, DraftReview, GeneratedDraft
from src.services.research.draft_generation_service import DraftGenerationService


@pytest.fixture
def draft_review_postgres_url(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str]]:
    configured = os.getenv("DRAFT_REVIEW_DATABASE_URL")
    sync_url = (
        configured or str(request.getfixturevalue("postgres_container")["url"])
    ).replace("+asyncpg", "")
    schema = f"test_draft_review_{uuid4().hex}"
    admin = create_engine(sync_url)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    schema_engine = create_engine(
        sync_url, connect_args={"options": f"-csearch_path={schema}"}
    )
    Base.metadata.create_all(schema_engine)
    schema_engine.dispose()
    try:
        async_url = sync_url.replace("postgresql://", "postgresql+asyncpg://")
        yield async_url, schema
    finally:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.requires_postgres
async def test_blocked_revision_review_survives_and_current_draft_is_unchanged(
    draft_review_postgres_url: tuple[str, str],
) -> None:
    ids = {
        name: uuid4()
        for name in (
            "org",
            "user",
            "workspace",
            "project",
            "draft",
            "document",
            "link",
            "draft_citation",
        )
    }
    common: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "is_deleted": False,
    }
    url, schema = draft_review_postgres_url
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    statements = [
        "INSERT INTO organizations (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,is_active,created_at,updated_at,is_deleted) VALUES (:org,'review-org','FREE',0,1,true,:created_at,:updated_at,:is_deleted)",
        "INSERT INTO users (id,email,password_hash,first_name,last_name,role,is_active,login_count,organization_id,created_at,updated_at,is_deleted) VALUES (:user,'review@test','x','A','B','USER',true,0,:org,:created_at,:updated_at,:is_deleted)",
        "INSERT INTO workspaces (id,name,is_archived,is_public,owner_id,organization_id,created_at,updated_at,is_deleted) VALUES (:workspace,'W',false,false,:user,:org,:created_at,:updated_at,:is_deleted)",
        "INSERT INTO collections (id,workspace_id,name,created_at,updated_at,is_deleted) VALUES (:project,:workspace,'P',:created_at,:updated_at,:is_deleted)",
        "INSERT INTO documents (id,title,filename,file_path,file_size_bytes,mime_type,document_type,processing_status,processing_retry_count,is_embedded,is_indexed,is_public,organization_id,content_summary,created_at,updated_at,is_deleted) VALUES (:document,'Paper','paper.pdf','paper.pdf',1,'application/pdf','PDF','COMPLETED',0,false,false,false,:org,'Source summary.',:created_at,:updated_at,:is_deleted)",
        "INSERT INTO collection_documents (id,collection_id,document_id,sort_order,created_at,updated_at,is_deleted) VALUES (:link,:project,:document,0,:created_at,:updated_at,:is_deleted)",
        "INSERT INTO generated_drafts (id,project_id,version,title,content,themes,is_current,created_at,updated_at,is_deleted) VALUES (:draft,:project,1,'Valid','Original supported statement [Doc 1].','[]',true,:created_at,:updated_at,:is_deleted)",
        "INSERT INTO draft_citations (id,draft_id,citation_index,document_id,context,created_at,updated_at,is_deleted) VALUES (:draft_citation,:draft,1,:document,'Referenced as [Doc 1]',:created_at,:updated_at,:is_deleted)",
    ]
    async with engine.begin() as connection:
        for statement in statements:
            await connection.execute(text(statement), {**ids, **common})

    candidate = "Unsupported revised statement [Doc 1]."
    review = {
        "docs_skipped": 0,
        "coverage": {"complete": True},
        "uncited_assertions": [],
        "verdicts": [
            {
                "doc_index": 1,
                "verdict": "major",
                "evidence": "Not supported.",
                "location": "document summary",
            }
        ],
    }
    async with Session() as session:
        service = DraftGenerationService(session)
        with (
            patch.object(
                service, "_build_revision_with_llm", AsyncMock(return_value=candidate)
            ),
            patch.object(service, "_review_citations", AsyncMock(return_value=review)),
            pytest.raises(ValueError, match="blocked persistence"),
        ):
            await service.revise_draft(
                project_id=ids["project"], instructions="Unsupported revision"
            )

    async with Session() as session:
        persisted = (
            await session.execute(
                select(DraftReview).where(DraftReview.project_id == ids["project"])
            )
        ).scalar_one()
        current = (
            await session.execute(
                select(GeneratedDraft).where(
                    GeneratedDraft.project_id == ids["project"],
                    GeneratedDraft.is_current.is_(True),
                )
            )
        ).scalar_one()
        assert persisted.outcome == "blocked"
        assert persisted.candidate_content == candidate
        assert (
            persisted.candidate_content_hash
            == hashlib.sha256(candidate.encode()).hexdigest()
        )
        assert current.id == ids["draft"]
        assert current.content == "Original supported statement [Doc 1]."
    await engine.dispose()
