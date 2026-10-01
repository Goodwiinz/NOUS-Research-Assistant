"""Draft export route formats (GOO-317).

The real ``DraftGenerationService.export_draft`` runs behind the real route;
only the draft/citation loads and the GOO-307 gate are stubbed. Markdown and
LaTeX bytes are pinned to hashes captured before CSL JSON/RIS existed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft7Validator

from src.api.research import drafts as drafts_api
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.services.research import draft_release_service
from src.services.research.draft_generation_service import DraftGenerationService
from src.services.research.release_rules import GateResult

pytestmark = pytest.mark.unit

PROJECT, DRAFT = uuid4(), uuid4()
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/references"
CONTENT = "## Draft Ω\n\n## 1. Results\nAlpha holds [Doc 1] and [Doc 3].\n"


def _saved(record: dict[str, Any], index: int) -> Any:
    citation = SimpleNamespace(
        document_title=record["title"],
        authors=record["authors"],
        year=record["year"],
        venue=record["venue"],
        doi=record["doi"],
        arxiv_id=record["arxiv_id"],
        abstract=None,
        document_type=record["type"],
    )
    return SimpleNamespace(
        citation=citation,
        document=None,
        citation_index=index,
        snippet=record["snippet"],
    )


def _citations() -> list[Any]:
    raw = (FIXTURES / "records_v1.json").read_text(encoding="utf-8")
    records = json.loads(raw)["records"]
    return [_saved(r, int(r["key"].removeprefix("doc"))) for r in records]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    draft = SimpleNamespace(id=DRAFT, title="Draft Ω/1", content=CONTENT)

    async def owned(*_: object) -> None:
        return None

    async def get_draft(_self: object, *_: object, **__: object) -> Any:
        return draft

    async def get_citations(_self: object, *_: object, **__: object) -> list[Any]:
        return _citations()

    async def header(*_: object) -> tuple[GateResult, str]:
        return GateResult((), {}, (), (), ()), "> Status: unreleased"

    monkeypatch.setattr(drafts_api, "_validate_project_ownership", owned)
    monkeypatch.setattr(DraftGenerationService, "get_draft", get_draft)
    monkeypatch.setattr(DraftGenerationService, "get_draft_citations", get_citations)
    monkeypatch.setattr(draft_release_service, "export_header", header)
    app = FastAPI()
    app.include_router(drafts_api.router)
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _export(client: TestClient, query: str) -> Any:
    return client.post(f"/api/v1/projects/{PROJECT}/drafts/{DRAFT}/export?{query}")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Captured before GOO-317 touched export_draft (route + service as of af10ffa21).
GOLDEN = {
    "markdown": "66e7b2b35458a2491d66805cbea11da31876e24641250fd2c64393228449e835",
    "markdown-apa": "314c4344f774c03b41333020f3bb23aff6c7cb0618551a28bd01e1d796ea59d6",
    "latex.tex": "afda2c9349621f662f6144f65b0f607c32cd148d8c81952650e2e0a8454495fc",
    "latex.bib": "58217ffee4698cbf3ac3b948fb041eedd405a1f5822830762cff665335bb1673",
}


def test_markdown_and_latex_bytes_unchanged(client: TestClient) -> None:
    markdown = _export(client, "format=markdown")
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.headers["content-disposition"] == (
        'attachment; filename="Draft_1.md"'
    )
    assert _sha(markdown.content) == GOLDEN["markdown"]
    apa = _export(client, "bib_format=apa")
    assert _sha(apa.content) == GOLDEN["markdown-apa"]
    latex = _export(client, "format=latex")
    assert latex.status_code == 200
    assert latex.headers["content-type"] == "application/zip"
    assert latex.headers["content-disposition"] == 'attachment; filename="Draft_1.zip"'
    with zipfile.ZipFile(io.BytesIO(latex.content)) as archive:
        assert archive.namelist() == ["Draft_1.tex", "references.bib"]
        assert _sha(archive.read("Draft_1.tex")) == GOLDEN["latex.tex"]
        assert _sha(archive.read("references.bib")) == GOLDEN["latex.bib"]


def test_unknown_format_still_400(client: TestClient) -> None:
    response = _export(client, "format=docx")
    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported format: docx"


def _ris_reader() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ris_reader", FIXTURES / "ris_reader.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_csl_and_ris_attachments_mime_and_filename(client: TestClient) -> None:
    csl = _export(client, "format=csl-json&include_bibliography=false")
    assert csl.status_code == 200
    assert csl.headers["content-type"] == "application/vnd.citationstyles.csl+json"
    assert csl.headers["content-disposition"] == (
        'attachment; filename="references.json"'
    )
    items = json.loads(csl.content.decode("utf-8"))
    schema = json.loads((FIXTURES / "csl-data.schema.json").read_text("utf-8"))
    assert not list(Draft7Validator(schema).iter_errors(items))
    assert [i["id"] for i in items] == [f"doc{n}" for n in range(1, 7)]
    assert items[0]["type"] == "article-journal"  # from Citation.document_type
    ris = _export(client, "format=ris&bib_format=apa")
    assert ris.status_code == 200
    assert ris.headers["content-type"] == "application/x-research-info-systems"
    assert ris.headers["content-disposition"] == 'attachment; filename="references.ris"'
    records = _ris_reader().parse(ris.content.decode("utf-8"))
    assert [r["ID"] for r in records] == [[f"doc{n}"] for n in range(1, 7)]
    # absent: doc2 year/venue/identifier, doc3 venue, doc4 title; doc5
    # type_unmapped/venue/identifier.
    assert csl.headers["x-reference-omissions"] == "8"
    assert ris.headers["x-reference-omissions"] == "8"
    snippet = "This evidence snippet must never become a title."
    assert snippet not in csl.text and snippet not in ris.text


def test_zero_citations_give_empty_reference_files(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def none(_self: object, *_: object, **__: object) -> list[Any]:
        return []

    monkeypatch.setattr(DraftGenerationService, "get_draft_citations", none)
    csl = _export(client, "format=csl-json")
    assert (csl.status_code, csl.text) == (200, "[]\n")
    ris = _export(client, "format=ris")
    assert (ris.status_code, ris.content) == (200, b"")
    assert ris.headers["x-reference-omissions"] == "0"
