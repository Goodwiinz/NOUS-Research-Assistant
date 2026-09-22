from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.models.document import Document
from src.models.draft_citation import DraftCitation
from src.models.generated_draft import GeneratedDraft
from src.services.agent.tools_impl import _tool_revise_draft
from src.services.research.draft_generation_service import DraftGenerationService

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _document(title: str, text: str = "evidence") -> MagicMock:
    doc = MagicMock(spec=Document)
    doc.id = uuid4()
    doc.title = title
    doc.content_text = text
    doc.content_summary = "summary"
    return doc


def _draft(version: int, content: str, *, current: bool) -> MagicMock:
    draft = MagicMock(spec=GeneratedDraft)
    draft.id = uuid4()
    draft.version = version
    draft.title = "Literature Review"
    draft.content = content
    draft.themes = ["attention"]
    draft.is_current = current
    draft.citations = []
    return draft


def _citation(index: int, document_id, *, context: str = "") -> MagicMock:
    citation = MagicMock(spec=DraftCitation)
    citation.citation_index = index
    citation.document_id = document_id
    citation.context = context
    return citation


class _RevisionSession:
    def __init__(self, base: MagicMock, documents: list[MagicMock], max_version: int):
        self.base = base
        self.documents = documents
        self.max_version = max_version
        self.added: list[object] = []
        self.execute = AsyncMock(side_effect=self._execute)
        self.commit = AsyncMock(side_effect=self._commit)
        self.rollback = AsyncMock()
        self.flush = AsyncMock(side_effect=self._flush)
        self.fail_persistence = False
        self.commits = 0
        self.current_id = base.id

    async def _execute(self, statement):
        sql = str(statement)
        result = MagicMock()
        if "FROM collections" in sql and "FOR UPDATE" in sql:
            result.scalar_one_or_none.return_value = uuid4()
        elif "SELECT generated_drafts.id" in sql and "is_current" in sql:
            result.scalar_one_or_none.return_value = self.current_id
        elif "max(generated_drafts.version)" in sql:
            result.scalar.return_value = self.max_version
        elif "FROM documents" in sql:
            result.scalars.return_value.all.return_value = self.documents
        elif sql.lstrip().upper().startswith("UPDATE"):
            pass
        else:
            result.scalar_one_or_none.return_value = self.base
        return result

    def add(self, value: object) -> None:
        self.added.append(value)

    async def _flush(self) -> None:
        if self.fail_persistence and any(
            isinstance(value, GeneratedDraft) for value in self.added
        ):
            raise RuntimeError("write failed")
        for value in self.added:
            if isinstance(value, GeneratedDraft) and value.id is None:
                value.id = uuid4()

    async def _commit(self) -> None:
        self.commits += 1


async def _service(
    base: MagicMock,
    documents: list[MagicMock],
    max_version: int = 3,
) -> tuple[DraftGenerationService, _RevisionSession]:
    if not base.citations:
        base.citations = [
            _citation(
                index, documents[index - 1].id, context=f"Referenced as [Doc {index}]"
            )
            for index in DraftGenerationService._citation_indices(base.content)
            if index <= len(documents)
        ]
    session = _RevisionSession(base, documents, max_version)
    with patch.object(
        DraftGenerationService,
        "_init_openai_client",
        return_value=(MagicMock(), "gpt-5-test"),
    ):
        service = DraftGenerationService(session)
    return service, session


async def test_explicit_base_version_is_used_even_when_later_version_is_current() -> (
    None
):
    base_v2 = _draft(2, "## Review\n\nOriginal v2 prose [Doc 1].", current=False)
    service, session = await _service(base_v2, [_document("Paper")])
    service._build_revision_with_llm = AsyncMock(
        return_value="## Review\n\nRevised v2 prose [Doc 1]."
    )

    result = await service.revise_draft(
        project_id=uuid4(),
        instructions="Clarify the claim",
        base_version=2,
    )

    assert result["base_version"] == 2
    assert result["version"] == 4
    assert (
        service._build_revision_with_llm.await_args.kwargs["base_content"]
        == base_v2.content
    )
    assert session.commits == 2  # release read transaction, then persist


async def test_citations_only_preserves_prose_and_persists_visible_doc_index() -> None:
    base = _draft(3, "## Review\n\nBenchmark reached 28.4 BLEU.", current=True)
    documents = [_document("Other"), _document("Attention Is All You Need")]
    service, session = await _service(base, documents)
    service._build_revision_with_llm = AsyncMock(
        return_value="## Review\n\nBenchmark reached 28.4 BLEU [Doc 2]."
    )

    result = await service.revise_draft(
        project_id=uuid4(),
        instructions="Add citations only",
        mode="citations_only",
    )

    citations = [value for value in session.added if isinstance(value, DraftCitation)]
    saved = next(value for value in session.added if isinstance(value, GeneratedDraft))
    assert result["citation_count"] == 1
    assert saved.citation_count == 1
    assert [citation.citation_index for citation in citations] == [2]
    assert citations[0].document_id == documents[1].id


async def test_revision_preserves_base_doc_two_identity_when_order_changes() -> None:
    cited = _document("Originally second")
    newly_first = _document("Now sorts first")
    base = _draft(3, "Claim [Doc 2].", current=True)
    base.citations = [_citation(2, cited.id, context="Referenced as [Doc 2]")]
    service, session = await _service(base, [cited, newly_first])
    service._build_revision_with_llm = AsyncMock(return_value="Clearer claim [Doc 2].")

    await service.revise_draft(project_id=uuid4(), instructions="Clarify")

    documents = service._build_revision_with_llm.await_args.kwargs["document_context"]
    assert f'[Doc 2] "{cited.title}"' in documents
    saved_citation = next(
        value for value in session.added if isinstance(value, DraftCitation)
    )
    assert saved_citation.document_id == cited.id


async def test_legacy_context_marker_overrides_renumbered_citation_index() -> None:
    cited = _document("Legacy cited paper")
    other = _document("Other paper")
    base = _draft(3, "Claim [Doc 2].", current=True)
    base.citations = [_citation(1, cited.id, context="Referenced as [Doc 2]")]
    service, session = await _service(base, [cited, other])
    service._build_revision_with_llm = AsyncMock(return_value="Claim improved [Doc 2].")

    await service.revise_draft(project_id=uuid4(), instructions="Clarify")

    saved_citation = next(
        value for value in session.added if isinstance(value, DraftCitation)
    )
    assert saved_citation.citation_index == 2
    assert saved_citation.document_id == cited.id


async def test_base_doc_zero_fails_before_document_ordering() -> None:
    base = _draft(3, "Invalid claim [Doc 0].", current=True)

    with pytest.raises(ValueError, match="invalid document reference"):
        DraftGenerationService._order_revision_documents(base, [_document("Paper")])


async def test_default_revision_rejects_stale_current_after_model_call() -> None:
    base = _draft(3, "Original [Doc 1].", current=True)
    service, session = await _service(base, [_document("Paper")])
    session.current_id = uuid4()
    service._build_revision_with_llm = AsyncMock(return_value="Improved [Doc 1].")

    with pytest.raises(ValueError, match="Current draft changed"):
        await service.revise_draft(project_id=uuid4(), instructions="Clarify")

    assert session.added == []
    session.rollback.assert_awaited_once()


async def test_revision_uses_project_row_lock_for_version_allocation() -> None:
    base = _draft(3, "Original [Doc 1].", current=True)
    service, session = await _service(base, [_document("Paper")])
    service._build_revision_with_llm = AsyncMock(return_value="Improved [Doc 1].")

    await service.revise_draft(project_id=uuid4(), instructions="Clarify")

    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert any("FROM collections" in sql and "FOR UPDATE" in sql for sql in statements)


@pytest.mark.parametrize(
    ("mode", "output", "message"),
    [
        ("citations_only", "Changed prose [Doc 1].", "changed draft prose"),
        ("citations_only", "Original prose.", "at least one resolved"),
        ("revise", "Original prose [Doc 2].", "unresolved document"),
        ("revise", "## Result\n\nI can’t revise this. Please provide v2.", "refusal"),
        ("revise", "Too short [Doc 1].", "collapsed"),
    ],
)
async def test_invalid_revisions_never_persist(
    mode: str, output: str, message: str
) -> None:
    base_content = (
        "Original prose. " * 120 if message == "collapsed" else "Original prose."
    )
    base = _draft(3, base_content, current=True)
    service, session = await _service(base, [_document("Paper")])
    service._build_revision_with_llm = AsyncMock(return_value=output)

    with pytest.raises(ValueError, match=message):
        await service.revise_draft(project_id=uuid4(), instructions="Revise", mode=mode)

    assert session.added == []
    assert base.is_current is True
    assert session.commits == 1


async def test_model_failure_leaves_current_pointer_unchanged() -> None:
    base = _draft(3, "Original prose.", current=True)
    service, session = await _service(base, [_document("Paper")])
    service._build_revision_with_llm = AsyncMock(side_effect=RuntimeError("model down"))

    with pytest.raises(RuntimeError, match="model down"):
        await service.revise_draft(project_id=uuid4(), instructions="Revise")

    assert session.added == []
    assert base.is_current is True
    assert session.commits == 1


async def test_persistence_failure_rolls_back_without_changing_base() -> None:
    base = _draft(3, "Original prose [Doc 1].", current=True)
    service, session = await _service(base, [_document("Paper")])
    session.fail_persistence = True
    service._build_revision_with_llm = AsyncMock(
        return_value="Improved complete prose [Doc 1]."
    )

    with pytest.raises(RuntimeError, match="write failed"):
        await service.revise_draft(project_id=uuid4(), instructions="Revise")

    session.rollback.assert_awaited_once()
    assert base.is_current is True
    assert session.commits == 1


async def test_document_context_includes_evidence_beyond_500_and_is_bounded() -> None:
    evidence = "a" * 600 + "BENCHMARK_EVIDENCE" + "b" * 40_000
    context = DraftGenerationService._build_document_context(
        [_document("Paper", evidence)], max_chars=2_000
    )

    assert "BENCHMARK_EVIDENCE" in context
    assert len(context) <= 2_000


async def test_citation_syntax_must_be_canonical() -> None:
    with pytest.raises(ValueError, match="at least one resolved"):
        DraftGenerationService._validate_citations(
            "Claim [Doc  1].", [_document("Paper")], require_one=True
        )


async def test_citation_only_allows_marker_inserted_between_words() -> None:
    base = "Vaswani et al. introduced the transformer."
    revised = "Vaswani et al. [Doc 1] introduced the transformer."

    DraftGenerationService._validate_revision_content(
        revised, base, [_document("Paper")], "citations_only"
    )


async def test_agent_tool_returns_completed_revision_metadata() -> None:
    project = SimpleNamespace(id=uuid4(), name="Transformers")
    current_user = MagicMock()
    expected = {
        "draft_id": str(uuid4()),
        "status": "completed",
        "version": 4,
        "base_version": 2,
        "word_count": 900,
        "citation_count": 2,
        "change_summary": "Revised v2.",
    }
    service = MagicMock()
    service.revise_draft = AsyncMock(return_value=expected)

    with (
        patch(
            "src.services.agent.tools_impl._verify_project_ownership",
            new=AsyncMock(return_value=project),
        ),
        patch(
            "src.services.research.draft_generation_service.DraftGenerationService",
            return_value=service,
        ),
    ):
        result = await _tool_revise_draft(
            {
                "project_id": str(project.id),
                "instructions": "Tighten the argument",
                "base_version": 2,
                "mode": "revise",
            },
            MagicMock(),
            current_user,
        )

    assert result == {
        **expected,
        "project_id": str(project.id),
        "project_name": "Transformers",
    }
    service.revise_draft.assert_awaited_once_with(
        project_id=project.id,
        instructions="Tighten the argument",
        base_version=2,
        mode="revise",
    )
