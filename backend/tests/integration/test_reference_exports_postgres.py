"""Real PostgreSQL proof for GOO-317 CSL JSON and RIS reference exports.

Runs on GOO-301's ``screening_factory`` schema (the migration chain, not
``Base.metadata``) with GOO-312's ``_setup`` seed (owner O, foreign-org user
F). ``records_v1.json`` becomes saved ``Citation``/``DraftCitation`` rows on
one draft; a GOO-315 candidate snapshots them. Every file is checked by the
independent oracles (the vendored CSL-data schema and ``ris_reader.py``).

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-317 section):
release references re-reading the live ``Citation`` makes step 3 fail.

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_reference_exports_postgres.py``.
"""

import hashlib
import importlib.util
import io
import json
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, AsyncIterator, TypeAlias, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft7Validator
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research import drafts as drafts_api
from src.api.research import manuscript_releases as releases_api
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.citation import Citation
from src.models.draft_citation import DraftCitation
from src.models.generated_draft import GeneratedDraft
from src.services.artifacts.storage import LocalArtifactStorage
from src.services.research import manuscript_release_service as mr
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.shared.manuscript_release_schemas import CandidateCreate
from tests.integration.test_run_manifest_postgres import _setup
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/references"
CONTENT = "## Results\nAlpha holds [Doc 1] and [Doc 3].\n"
KEYS = [f"doc{n}" for n in range(1, 7)]
# references.bib of records_v1 captured before GOO-317 touched export_draft
# (tests/unit/api/test_draft_export_formats.py GOLDEN["latex.bib"]).
BIB_GOLDEN = "58217ffee4698cbf3ac3b948fb041eedd405a1f5822830762cff665335bb1673"


def _sha(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _records() -> list[dict[str, Any]]:
    raw = (FIXTURES / "records_v1.json").read_text(encoding="utf-8")
    return cast(list[dict[str, Any]], json.loads(raw)["records"])


def _ris_reader() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ris_reader", FIXTURES / "ris_reader.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_csl(text: str) -> list[dict[str, Any]]:
    items = json.loads(text)
    schema = json.loads((FIXTURES / "csl-data.schema.json").read_text("utf-8"))
    errors = [e.message for e in Draft7Validator(schema).iter_errors(items)]
    assert not errors, errors
    return cast(list[dict[str, Any]], items)


def _client(w: Any, user: str = "O") -> AsyncClient:
    app = FastAPI()
    app.include_router(drafts_api.router)
    app.include_router(releases_api.router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with w.factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    org = w.ids["foreign_org" if user == "F" else "org"]
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=w.ids[user], organization_id=org
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _seed_draft(w: Any) -> tuple[UUID, dict[str, UUID]]:
    citations: dict[str, UUID] = {}
    async with w.factory() as db:
        draft = GeneratedDraft(
            project_id=w.p1, version=1, title="Refs", content=CONTENT, is_current=True
        )
        db.add(draft)
        await db.flush()
        for record in _records():
            citation = Citation(
                document_title=record["title"],
                document_type=record["type"],
                authors=record["authors"],
                year=record["year"],
                venue=record["venue"],
                doi=record["doi"],
                arxiv_id=record["arxiv_id"],
                snippet=record["snippet"],
            )
            db.add(citation)
            await db.flush()
            db.add(
                DraftCitation(
                    draft_id=draft.id,
                    citation_index=int(record["key"].removeprefix("doc")),
                    citation_id=citation.id,
                    snippet=record["snippet"],
                )
            )
            citations[record["case"]] = cast(UUID, citation.id)
        await db.commit()
        return cast(UUID, draft.id), citations


def _zip_members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in sorted(archive.namelist())}


async def _download(client: AsyncClient, base: str, fmt: str) -> bytes:
    response = await client.get(f"{base}?format={fmt}")
    assert response.status_code == 200, response.text
    return cast(bytes, response.content)


async def test_release_and_draft_reference_exports_reconcile_and_stay_immutable(
    screening_factory: Factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalArtifactStorage(tmp_path / "artifacts")
    monkeypatch.setattr(mr, "get_artifact_storage", lambda: storage)
    w = await _setup(screening_factory, tmp_path)
    draft_id, citations = await _seed_draft(w)
    exports = f"/api/v1/projects/{w.p1}/drafts/{draft_id}/export"
    async with _client(w) as client:
        markdown = (await client.post(f"{exports}?format=markdown")).content
        latex = (await client.post(f"{exports}?format=latex")).content

    # 1. Build a candidate; download its CSL JSON and RIS.
    body = CandidateCreate(
        draft_id=draft_id, expected_content_hash=_sha(CONTENT), idempotency_key="c1"
    )
    async with w.factory() as db:
        context = await resolve_project(db, w.p1, w.ids["O"], ResearchAction.EDIT)
        candidate, _ = await mr.create_candidate(db, context, w.ids["O"], body)
    base = f"/api/v1/projects/{w.p1}/manuscript-releases/{candidate.id}/references"
    async with _client(w) as client:
        before = {f: await _download(client, base, f) for f in ("csl-json", "ris")}
        bib_before = await _download(client, base, "bibtex")
        report = (await client.get(f"{base}?format=csl-json&report=true")).json()
    csl = _valid_csl(before["csl-json"].decode("utf-8"))
    ris = _ris_reader().parse(before["ris"].decode("utf-8"))
    async with w.factory() as db:
        snapshot = (await mr._release(db, w.p1, candidate.id)).snapshot
    assert [r["key"] for r in snapshot["references"]] == KEYS
    assert [i["id"] for i in csl] == KEYS
    assert [r["ID"] for r in ris] == [[k] for k in KEYS]
    for record, item, entry in zip(_records(), csl, ris):
        assert item.get("DOI") == record["doi"] == entry.get("DO", [None])[0]
        arxiv = record["arxiv_id"]
        assert item.get("archive_location") == arxiv
        assert entry.get("AN", [None])[0] == (f"arXiv:{arxiv}" if arxiv else None)
        assert [a["literal"] for a in item["author"]] == record["authors"]
        assert entry["AU"] == record["authors"]
    assert report["records"] == 6
    omissions = {(o["key"], o["field"], o["reason"]) for o in report["omissions"]}
    assert ("doc2", "year", "absent") in omissions  # corporate
    assert ("doc4", "title", "absent") in omissions  # untitled_with_snippet
    assert ("doc5", "type", "type_unmapped") in omissions  # unknown_type
    snippet = _records()[3]["snippet"]
    assert snippet not in before["csl-json"].decode() + before["ris"].decode()

    # 2. Package members, and verify reconciles BibTeX, CSL and RIS.
    async with w.factory() as db:
        context = await resolve_project(db, w.p1, w.ids["O"])
        data, _sha256 = await mr.package_bytes(db, context, candidate.id)
        verification = await mr.verify(db, context, candidate.id)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    assert json.loads(members["references.json"])["body"] == csl
    assert members["references.ris"] == before["ris"]
    assert members["references.bib"] == bib_before
    assert json.loads(members["references.omissions.json"])["body"] == (
        report["omissions"]
    )
    assert verification.references_ok and verification.bundle_ok
    assert [m.key for m in verification.reference_mapping] == KEYS

    # 4. Back-compat (before the edit, which ad-hoc exports read live):
    # Markdown/LaTeX unchanged by the candidate; the zip's BibTeX is the
    # pre-GOO-317 golden and equals the release's references.bib.
    async with _client(w) as client:
        assert (await client.post(f"{exports}?format=markdown")).content == markdown
        latex_again = (await client.post(f"{exports}?format=latex")).content
    # Zip local headers carry a 2-second DOS mtime, so compare the members.
    assert _zip_members(latex_again) == _zip_members(latex)
    with zipfile.ZipFile(io.BytesIO(latex)) as archive:
        assert _sha(archive.read("references.bib")) == BIB_GOLDEN
        assert archive.read("references.bib") == bib_before

    # 3. Immutability: edit the journal citation; release bytes stay put,
    # a fresh ad-hoc draft export shows the new values.
    async with w.factory() as db:
        await db.execute(
            update(Citation)
            .where(Citation.id == citations["journal"])
            .values(document_title="Edited title", doi="10.1000/edited")
        )
        await db.commit()
    async with _client(w) as client:
        for fmt, data_before in before.items():
            assert await _download(client, base, fmt) == data_before, fmt
        assert await _download(client, base, "bibtex") == bib_before
        fresh = await client.post(f"{exports}?format=csl-json")
    assert fresh.status_code == 200
    edited = _valid_csl(fresh.text)[0]
    assert (edited["title"], edited["DOI"]) == ("Edited title", "10.1000/edited")
    async with w.factory() as db:
        context = await resolve_project(db, w.p1, w.ids["O"])
        assert (await mr.verify(db, context, candidate.id)).references_ok

    # 5. Tenancy: a foreign-org user cannot read either export.
    async with _client(w, "F") as foreign:
        assert (await foreign.get(f"{base}?format=ris")).status_code == 404
        refused = await foreign.post(f"{exports}?format=csl-json")
        assert refused.status_code == 404, refused.text
