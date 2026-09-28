"""PostgreSQL-backed proof for the downloaded draft bibliography artifact."""

import io
import os
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from pybtex.database import parse_string
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.api.research.drafts import export_draft
from src.models import Base
from src.models.citation import Citation
from src.models.collection import Collection
from src.models.document import Document, DocumentType, ProcessingStatus
from src.models.draft_citation import DraftCitation
from src.models.generated_draft import GeneratedDraft
from src.models.organization import Organization
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.requires_postgres
async def test_downloaded_latex_bibliography_matches_saved_draft_and_metadata(
    request: pytest.FixtureRequest,
) -> None:
    configured_url = os.getenv("BIBLIOGRAPHY_DATABASE_URL") or str(
        request.getfixturevalue("postgres_container")["url"]
    )

    parsed_url = make_url(configured_url)
    sync_url = parsed_url.set(drivername="postgresql+psycopg2")
    async_url = parsed_url.set(drivername="postgresql+asyncpg")
    schema = f"test_draft_bibliography_{uuid4().hex}"
    admin_engine = create_engine(sync_url)
    with admin_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

    engine = create_async_engine(
        async_url,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async_session = sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with async_session() as db:
            user_id = uuid4()
            await db.execute(
                text("""
                    INSERT INTO users
                        (id, email, password_hash, first_name, last_name, role,
                         is_active, login_count, created_at, updated_at, is_deleted)
                    VALUES
                        (:id, :email, 'unused', 'Test', 'Owner', 'USER', true, 0,
                         now(), now(), false)
                    """),
                {"id": user_id, "email": f"{user_id}@example.test"},
            )
            organization = Organization(
                name=f"Bibliography {uuid4()}", storage_limit_bytes=1_000_000
            )
            db.add(organization)
            await db.flush()
            await db.execute(
                text("UPDATE users SET organization_id=:org WHERE id=:user"),
                {"org": organization.id, "user": user_id},
            )
            collaborator_id = uuid4()
            outsider_id = uuid4()
            foreign_organization = Organization(
                name=f"Foreign bibliography {uuid4()}", storage_limit_bytes=1_000_000
            )
            db.add(foreign_organization)
            await db.flush()
            await db.execute(
                text("""
                    INSERT INTO users
                        (id, email, password_hash, first_name, last_name, role,
                         is_active, login_count, organization_id, created_at,
                         updated_at, is_deleted)
                    VALUES
                        (:collaborator, :collaborator_email, 'unused', 'Test',
                         'Collaborator', 'USER', true, 0, :organization,
                         now(), now(), false),
                        (:outsider, :outsider_email, 'unused', 'Test', 'Outsider',
                         'USER', true, 0, :foreign_organization,
                         now(), now(), false)
                    """),
                {
                    "collaborator": collaborator_id,
                    "collaborator_email": f"{collaborator_id}@example.test",
                    "outsider": outsider_id,
                    "outsider_email": f"{outsider_id}@example.test",
                    "organization": organization.id,
                    "foreign_organization": foreign_organization.id,
                },
            )
            workspace = Workspace(
                name="Bibliography workspace",
                owner_id=user_id,
                organization_id=organization.id,
            )
            db.add(workspace)
            await db.flush()
            db.add(
                WorkspaceMember(
                    workspace_id=workspace.id,
                    user_id=collaborator_id,
                    role=WorkspaceRole.VIEWER,
                    invited_by_id=user_id,
                )
            )
            project = Collection(workspace_id=workspace.id, name="Artifact project")
            db.add(project)
            await db.flush()
            document = Document(
                title='Canonical "Quoted" {Result} α',
                filename="paper.pdf",
                file_path="paper.pdf",
                file_size_bytes=123,
                mime_type="application/pdf",
                document_type=DocumentType.PDF,
                processing_status=ProcessingStatus.COMPLETED,
                organization_id=organization.id,
                uploaded_by_user_id=user_id,
            )
            db.add(document)
            await db.flush()
            citation = Citation(
                document_id=document.id,
                document_title=document.title,
                authors=["Sofía Kovalevskaya", "Grace Hopper"],
                year=2026,
                venue="Journal of R&D",
                doi="10.1000/example",
                snippet='"This quote is evidence, never the title."',
            )
            db.add(citation)
            await db.flush()
            draft = GeneratedDraft(
                project_id=project.id,
                version=1,
                title="Saved draft",
                content="## Findings\n\nGrounded result [Doc 4].",
                themes=["artifacts"],
                word_count=5,
                citation_count=1,
                is_current=True,
            )
            db.add(draft)
            await db.flush()
            db.add(
                DraftCitation(
                    draft_id=draft.id,
                    citation_index=4,
                    document_id=document.id,
                    citation_id=citation.id,
                    snippet='"This quote is evidence, never the title."',
                    context="Grounded result [Doc 4].",
                )
            )
            await db.commit()

            markdown_response = await export_draft(
                project_id=project.id,
                draft_id=draft.id,
                format="markdown",
                include_bibliography=True,
                bib_format="apa",
                current_user=SimpleNamespace(id=collaborator_id),
                db=db,
            )

            response = await export_draft(
                project_id=project.id,
                draft_id=draft.id,
                format="latex",
                include_bibliography=True,
                bib_format="bibtex",
                current_user=SimpleNamespace(id=user_id),
                db=db,
            )

            with pytest.raises(HTTPException) as denied:
                await export_draft(
                    project_id=project.id,
                    draft_id=draft.id,
                    format="markdown",
                    include_bibliography=True,
                    bib_format="apa",
                    current_user=SimpleNamespace(id=outsider_id),
                    db=db,
                )

        assert isinstance(markdown_response, Response)
        markdown = bytes(markdown_response.body).decode()
        assert "Grounded result [Doc 4]." in markdown
        assert "[Doc 4]" in markdown
        assert 'Canonical "Quoted" {Result} α' in markdown
        assert "Kovalevskaya" in markdown
        assert "2026" in markdown
        assert "Journal of R&D" in markdown
        assert "https://doi.org/10.1000/example" in markdown
        assert "This quote is evidence" not in markdown
        assert denied.value.status_code == 404
        assert isinstance(response, Response)
        with zipfile.ZipFile(io.BytesIO(bytes(response.body))) as archive:
            tex_name = next(
                name for name in archive.namelist() if name.endswith(".tex")
            )
            latex = archive.read(tex_name).decode()
            bibtex = archive.read("references.bib").decode()

        parsed = parse_string(bibtex, "bibtex")
        entry = parsed.entries["doc4"]
        assert "Grounded result [Doc 4]." == draft.content.split("\n\n", 1)[1]
        assert r"Grounded result \cite{doc4}." in latex
        assert entry.fields["title"] == 'Canonical "Quoted" {Result} α'
        assert [str(person) for person in entry.persons["author"]] == [
            "Kovalevskaya, Sofía",
            "Hopper, Grace",
        ]
        assert entry.fields["year"] == "2026"
        assert entry.fields["journal"] == r"Journal of R\&D"
        assert entry.fields["doi"] == "10.1000/example"
        assert r"Journal of R\&D" in bibtex
        assert "This quote is evidence" not in entry.fields["title"]
    finally:
        await engine.dispose()
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_engine.dispose()
