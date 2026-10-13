"""``discover_papers``: one deterministic discovery round (GOO-435, S5c).

The channels (library keyword, library semantic, arXiv) are replaced by fakes
that record how they were called, so these tests pin the orchestration: the
arguments, which calls run and with what window, how failures are isolated, how
results are deduplicated, fused, cut and capped, and what scope the library
searches are given. The SQL of the channels runs on PostgreSQL in
``tests/integration/test_discover_papers_postgres.py``; the grant scope and the
identity query run here on SQLite.

Mutation checks (2026-10-13), each guard removed in turn, the named test seen
failing on the defect, the source restored byte-identical and the tests green
again:

- tenancy, the grant scope (``read_tools._discover_papers``): searching the
  library without ``authorized_scope`` fails
  ``test_a_project_grant_searches_only_its_project``,
  ``test_a_workspace_grant_searches_its_live_projects_and_no_other`` and
  ``test_a_deleted_workspace_is_denied_before_any_search``;
- tenancy, the identity query (``_library_candidates``): removing
  ``project_scope_condition`` or the ``Document.organization_id`` predicate
  fails ``test_the_identity_query_drops_documents_outside_the_scope``;
- the citation gate: making ``_cite_gate`` a pass-through fails
  ``test_the_citation_gate_drops_an_id_no_successful_call_returned``;
- the window: ``_plan`` passing no ``after``/``before`` fails
  ``test_the_window_is_passed_into_every_channel`` and
  ``test_one_open_bound_is_passed_as_open``; sending ``recency_days=365`` to
  arXiv fails ``test_the_window_is_passed_into_every_channel`` and
  ``test_without_a_window_nothing_is_cut_by_date``;
- the relative cut (``RELATIVE_CUT = 0``): fails ``test_a_dominant_paper_cuts_
  the_weak_tail_but_keeps_five``, ``test_the_cut_is_relative_to_the_best_score``
  and ``test_an_exact_name_query_is_not_padded_to_fifteen``;
- deduplication: dropping the arXiv id as an identity key fails
  ``test_an_arxiv_paper_already_in_the_library_is_shown_as_the_library_copy``
  and three more; preferring the last member over the library copy fails
  seven;
- arXiv syntax stripping (``_arxiv_terms``): returning the raw query fails
  ``test_arxiv_gets_plain_keywords_only``;
- the missing-table test (``_embeddings_table_missing`` always false) fails
  ``test_a_missing_table_is_unavailable_logged_once_and_the_savepoint_isolates_it``
  and the parametrized sqlstate cases.

Focused command: ``pytest -q backend/tests/unit/services/integrations/
test_discover_papers.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, DocumentType
from src.models.organization import Organization
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.integration_context import IntegrationContext
from src.schemas.integration_tools import ToolInvocation
from src.services.integrations import discover_papers as dp
from src.services.integrations import read_tools
from src.services.integrations.context import IntegrationAccessDenied
from src.services.integrations.discover_papers import (
    CallRecord,
    Candidate,
    DiscoverArgumentError,
    RankedPaper,
    acronym_terms,
    discover,
    parse_arguments,
    rank_papers,
    select_papers,
)
from src.services.integrations.read_tools import (
    MAX_RESULT_BYTES,
    ToolArgumentError,
    invoke_read,
    list_read_tools,
)

pytestmark = pytest.mark.unit

USER, ORG, OTHER_ORG, WORKSPACE, OTHER_WORKSPACE = (uuid4() for _ in range(5))
PROJECT, OTHER_PROJECT = UUID(int=1), UUID(int=2)
FOREIGN_PROJECT, DELETED_PROJECT = UUID(int=3), UUID(int=4)
DOC_IN, DOC_SIBLING, DOC_FOREIGN, DOC_DELETED_PROJECT, DOC_OTHER_ORG = (
    uuid4() for _ in range(5)
)
DOC_GONE = uuid4()
TODAY = date(2026, 10, 13)
TOOLS_READ = frozenset({"tools:read"})


# --- builders --------------------------------------------------------------------


def paper(
    ident: str,
    *,
    source: str = "library",
    title: str | None = None,
    published: date | None = date(2024, 5, 1),
    date_source: str | None = None,
    arxiv_id: str | None = None,
    doi: str | None = None,
    authors: tuple[str, ...] = ("A. Author",),
    snippet: str = "snippet",
    citations: int | None = None,
) -> Candidate:
    return Candidate(
        id=ident,
        source=source,  # type: ignore[arg-type]
        title=title or f"Distinct title number {ident}",
        published=published,
        date_source=date_source
        or (
            None if published is None else "arxiv" if source == "arxiv" else "metadata"
        ),
        authors=authors,
        snippet=snippet,
        arxiv_id=arxiv_id or (ident if source == "arxiv" else None),
        doi=doi,
        citations=citations,
    )


def arxiv_row(ident: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "id": ident,
        "title": f"arXiv title number {ident}",
        "authors": ["X. Writer"],
        "abstract": "An abstract.",
        "published": "2024-05-01T00:00:00Z",
    }
    row.update(overrides)
    return row


def request(**overrides: Any) -> dp.DiscoverRequest:
    arguments: dict[str, Any] = {
        "keywords": ["llm agents"],
        "question": "how do llm agents plan?",
        "difficulty": 5,
    }
    arguments.update(overrides)
    return parse_arguments(arguments)


@dataclass
class Fakes:
    """The three channels, recording each call.

    ``keyword`` / ``arxiv`` map a call's query to its answer (a list, an
    exception to raise, or a callable); ``semantic`` is one answer.
    """

    keyword: dict[str, Any] = field(default_factory=dict)
    semantic: Any = field(default_factory=list)
    arxiv: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def _answer(self, answer: Any, arguments: dict[str, Any]) -> Any:
        if callable(answer):
            answer = answer(arguments)
            if asyncio.iscoroutine(answer):
                answer = await answer
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def library_keyword(self, **kwargs: Any) -> list[Candidate]:
        self.calls.append(("library_keyword", kwargs))
        return await self._answer(self.keyword.get(kwargs["query"], []), kwargs)

    async def library_semantic(self, **kwargs: Any) -> list[Candidate]:
        self.calls.append(("library_semantic", kwargs))
        return await self._answer(self.semantic, kwargs)

    async def arxiv_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("arxiv", arguments))
        answer = await self._answer(self.arxiv.get(arguments["query"], []), arguments)
        return answer if isinstance(answer, dict) else {"papers": answer}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "Fakes":
        monkeypatch.setattr(dp, "_library_keyword", self.library_keyword)
        monkeypatch.setattr(dp, "_library_semantic", self.library_semantic)
        return self

    def named(self, name: str) -> list[dict[str, Any]]:
        return [arguments for call, arguments in self.calls if call == name]


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Fakes:
    return Fakes().install(monkeypatch)


async def run(
    fakes: Fakes,
    req: dp.DiscoverRequest | None = None,
    project_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    return await discover(
        req or request(),
        organization_id=ORG,
        project_ids=[PROJECT] if project_ids is None else project_ids,
        arxiv_search=fakes.arxiv_search,
        today=TODAY,
    )


def ids(payload: dict[str, Any]) -> list[str]:
    return [item["id"] for item in payload["papers"]]


def status_of(payload: dict[str, Any]) -> dict[str, str]:
    return {call["name"]: call["status"] for call in payload["calls"]}


# --- arguments ---------------------------------------------------------------------


def test_arguments_are_normalized() -> None:
    parsed = parse_arguments(
        {
            "keywords": ["  GRPO ", "reward   models", "GRPO"],
            "question": " What  is GRPO? ",
            "difficulty": 10,
            "sources": ["arxiv", "library", "arxiv"][:2],
        }
    )

    assert parsed.keywords == ("GRPO", "reward models")
    assert parsed.question == "What is GRPO?"
    assert parsed.sources == ("library", "arxiv")  # fixed order
    assert parsed.prioritize == "default"
    assert (parsed.published_after, parsed.published_before) == (None, None)
    assert parsed.difficulty == 10


def test_a_one_day_window_is_valid() -> None:
    parsed = request(published_after="2020-06-01", published_before="2020-06-01")

    assert parsed.published_after == parsed.published_before == date(2020, 6, 1)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"keywords": []}, "keywords must be 1-8 strings"),
        ({"keywords": ["a"] * 9}, "keywords must be 1-8 strings"),
        ({"keywords": "grpo"}, "keywords must be 1-8 strings"),
        ({"keywords": [5]}, "keywords must be 1-8 strings"),
        ({"keywords": ["   "]}, "each keyword must be 1-100 characters"),
        ({"keywords": ["x" * 101]}, "each keyword must be 1-100 characters"),
        ({"question": ""}, "question must be 1-500 characters"),
        ({"question": "q" * 501}, "question must be 1-500 characters"),
        ({"question": 5}, "question must be 1-500 characters"),
        ({"difficulty": 0}, "difficulty must be an integer from 1 to 10"),
        ({"difficulty": 11}, "difficulty must be an integer from 1 to 10"),
        ({"difficulty": True}, "difficulty must be an integer from 1 to 10"),
        ({"difficulty": "5"}, "difficulty must be an integer from 1 to 10"),
        ({"difficulty": 5.0}, "difficulty must be an integer from 1 to 10"),
        (
            {"prioritize": "popular"},
            "prioritize must be default, recency or historical",
        ),
        ({"published_after": "2020-13-01"}, "published_after must be a date like"),
        ({"published_after": "2020-02-30"}, "published_after must be a date like"),
        ({"published_after": "20200101"}, "published_after must be a date like"),
        ({"published_after": "2020-1-1"}, "published_after must be a date like"),
        ({"published_after": 20200101}, "published_after must be a date like"),
        ({"published_before": "soon"}, "published_before must be a date like"),
        (
            {"published_after": "2021-01-01", "published_before": "2020-12-31"},
            "published_after must not be later than published_before",
        ),
        ({"sources": []}, "sources must list library and/or arxiv"),
        ({"sources": ["web"]}, "sources must list library and/or arxiv"),
        ({"sources": "library"}, "sources must list library and/or arxiv"),
        (
            {"sources": ["library", "arxiv", "library"]},
            "sources must list library and/or arxiv",
        ),
    ],
)
def test_bad_arguments_are_refused_with_a_safe_reason(
    override: dict[str, Any], message: str
) -> None:
    with pytest.raises(DiscoverArgumentError, match=message) as refused:
        request(**override)

    # The reason names the argument, never the value the model sent.
    for value in override.values():
        if isinstance(value, str) and len(value) > 3 and value not in message:
            assert value not in str(refused.value)


@pytest.mark.parametrize(
    ("difficulty", "depth", "rounds"),
    [(1, 10, 0), (3, 10, 0), (4, 20, 1), (7, 20, 1), (8, 30, 2), (10, 30, 2)],
)
def test_difficulty_sets_the_depth_and_the_follow_ups_the_harness_may_spend(
    difficulty: int, depth: int, rounds: int
) -> None:
    parsed = request(difficulty=difficulty)

    assert (parsed.depth, parsed.followup_rounds_allowed) == (depth, rounds)


# --- acronym recovery ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("keywords", "expected"),
    [
        (["GRPO", "reward models"], ["GRPO"]),
        (["GRPO"], []),  # nothing else to recover from
        (["GRPO", "PPO"], []),  # only acronyms: the main call is that search
        (["Rl", "reward models"], []),  # one capital is not an acronym
        (["GPT-4", "agents"], ["GPT-4"]),
        (["agents (GRPO)"], ["GRPO"]),  # edge punctuation is not part of the token
        (["GRPO and PPO for agents"], ["GRPO", "PPO"]),
        (["LoRA", "adapters"], ["LoRA"]),  # three capitals, mixed case
        (["A", "agents"], []),  # too short
        (["ABCDEFGHIJK", "agents"], []),  # 11 characters
        (["ABCDEFGHIJ", "agents"], ["ABCDEFGHIJ"]),  # 10 characters
        (["3D", "scenes"], []),  # starts with a digit
        (["RL_HF", "agents"], []),  # not letters, digits and hyphens only
        (["agents", "GRPO", "GRPO"], ["GRPO"]),  # once
        (["reward models"], []),
    ],
)
def test_acronym_recovery_rule(keywords: list[str], expected: list[str]) -> None:
    assert acronym_terms(keywords) == expected


async def test_an_acronym_among_other_words_adds_one_keyword_call_per_source(
    fakes: Fakes,
) -> None:
    payload = await run(fakes, request(keywords=["GRPO", "reward models"]))

    assert [call["name"] for call in payload["calls"]] == [
        "library_keyword",
        "library_semantic",
        "arxiv_keyword",
        "library_keyword_acronyms",
        "arxiv_keyword_acronyms",
    ]
    queries = {call["name"]: call["query"] for call in payload["calls"]}
    assert (
        queries["library_keyword"] == queries["arxiv_keyword"] == "GRPO reward models"
    )
    assert (
        queries["library_keyword_acronyms"]
        == queries["arxiv_keyword_acronyms"]
        == "GRPO"
    )
    assert queries["library_semantic"] == "how do llm agents plan?"


async def test_a_lone_acronym_triggers_no_recovery_call(fakes: Fakes) -> None:
    payload = await run(fakes, request(keywords=["GRPO"]))

    assert [call["name"] for call in payload["calls"]] == [
        "library_keyword",
        "library_semantic",
        "arxiv_keyword",
    ]
    assert len(fakes.calls) == 3


async def test_recovery_calls_follow_the_requested_sources(fakes: Fakes) -> None:
    payload = await run(
        fakes, request(keywords=["GRPO", "reward models"], sources=["arxiv"])
    )

    assert [call["name"] for call in payload["calls"]] == [
        "arxiv_keyword",
        "arxiv_keyword_acronyms",
    ]
    assert (
        fakes.named("library_keyword") == [] and fakes.named("library_semantic") == []
    )


async def test_papers_found_only_by_the_acronym_call_are_returned(fakes: Fakes) -> None:
    fakes.keyword["GRPO reward models"] = []
    fakes.keyword["GRPO"] = [paper("grpo-paper")]
    payload = await run(fakes, request(keywords=["GRPO", "reward models"]))

    assert ids(payload) == ["grpo-paper"]
    assert payload["papers"][0]["found_by"] == ["library_keyword_acronyms"]


# --- the window and the depth reach every channel ------------------------------------


async def test_the_window_is_passed_into_every_channel(fakes: Fakes) -> None:
    await run(
        fakes,
        request(
            keywords=["GRPO", "reward models"],
            published_after="2020-01-01",
            published_before="2020-12-31",
            difficulty=9,
        ),
    )

    library = fakes.named("library_keyword") + fakes.named("library_semantic")
    assert len(library) == 3  # keyword, semantic and the acronym keyword call
    for arguments in library:
        assert arguments["after"] == date(2020, 1, 1)
        assert arguments["before"] == date(2020, 12, 31)
        assert arguments["limit"] == 30
        assert arguments["organization_id"] == ORG
    arxiv = fakes.named("arxiv")
    assert len(arxiv) == 2
    for arguments in arxiv:
        # Applied in the arXiv query; recency_days=0 switches off the tool's
        # default twelve-month lookback.
        assert arguments["published_after"] == "2020-01-01"
        assert arguments["published_before"] == "2020-12-31"
        assert arguments["recency_days"] == 0
        assert arguments["max_results"] == 5  # the tool's own cap


async def test_without_a_window_nothing_is_cut_by_date(fakes: Fakes) -> None:
    await run(fakes)

    for arguments in fakes.named("library_keyword") + fakes.named("library_semantic"):
        assert arguments["after"] is None and arguments["before"] is None
    (arxiv,) = fakes.named("arxiv")
    assert "published_after" not in arxiv and "published_before" not in arxiv
    assert arxiv["recency_days"] == 0


async def test_one_open_bound_is_passed_as_open(fakes: Fakes) -> None:
    await run(fakes, request(published_before="2019-12-31"))

    (arxiv,) = fakes.named("arxiv")
    assert arxiv["published_before"] == "2019-12-31" and "published_after" not in arxiv
    assert fakes.named("library_semantic")[0]["after"] is None


async def test_a_2020_window_returns_the_2020_paper_the_channels_found(
    fakes: Fakes,
) -> None:
    # The channels apply the window where they read; whatever they return is
    # what the window admits. The round must not filter it a second time (by
    # date, or by the default prioritize) and lose the paper.
    rag = paper(
        "2005.11401",
        source="arxiv",
        title="Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
        published=date(2020, 12, 7),
    )
    fakes.semantic = [
        paper("doc-1", published=date(2020, 3, 1), date_source="metadata")
    ]
    fakes.arxiv["retrieval augmented generation"] = {
        "papers": [arxiv_row(rag.id, title=rag.title, published="2020-12-07T00:00:00Z")]
    }
    payload = await run(
        fakes,
        request(
            keywords=["retrieval augmented generation"],
            published_after="2020-01-01",
            published_before="2020-12-31",
        ),
    )

    assert set(ids(payload)) == {"2005.11401", "doc-1"}
    assert payload["window"] == {"after": "2020-01-01", "before": "2020-12-31"}
    assert {item["published"] for item in payload["papers"]} == {
        "2020-12-07",
        "2020-03-01",
    }


async def test_arxiv_gets_plain_keywords_only(fakes: Fakes) -> None:
    # search_arxiv passes syntax through untouched; a keyword must not be able
    # to close the date filter the tool composes around the query.
    await run(
        fakes,
        request(keywords=['agents") OR (all:secret', "AND", "ti:planning"]),
    )

    (arxiv,) = fakes.named("arxiv")
    assert arxiv["query"] == "agents all secret ti planning"
    for symbol in '"():[]':
        assert symbol not in arxiv["query"]


# --- one failure never sinks the others ---------------------------------------------------


async def test_a_failing_channel_is_recorded_and_the_others_still_answer(
    fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    fakes.keyword["llm agents"] = RuntimeError("password=hunter2 host=db.internal")
    fakes.semantic = [paper("doc-1")]
    fakes.arxiv["llm agents"] = [arxiv_row("2401.00001")]
    with caplog.at_level(logging.WARNING):
        payload = await run(fakes)

    assert ids(payload) == ["doc-1", "2401.00001"]
    keyword = payload["calls"][0]
    assert keyword == {
        "name": "library_keyword",
        "query": "llm agents",
        "status": "error",
        "count": 0,
        "reason": "upstream_error",
    }
    assert status_of(payload) == {
        "library_keyword": "error",
        "library_semantic": "ok",
        "arxiv_keyword": "ok",
    }
    # Internal detail stays in the logs, never in the result.
    assert "hunter2" not in json.dumps(payload) and "db.internal" not in json.dumps(
        payload
    )
    assert any("hunter2" in str(r.exc_info) for r in caplog.records if r.exc_info)


async def test_a_slow_channel_times_out_without_stalling_the_round(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang(_arguments: dict[str, Any]) -> list[Candidate]:
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(dp, "LIBRARY_TIMEOUT_SECONDS", 0.05)
    fakes.keyword["llm agents"] = hang
    fakes.semantic = [paper("doc-1")]
    payload = await asyncio.wait_for(run(fakes), timeout=5)

    assert ids(payload) == ["doc-1"]
    keyword = payload["calls"][0]
    assert (keyword["status"], keyword["reason"]) == ("error", "timeout")


async def test_an_unavailable_channel_is_not_an_error(fakes: Fakes) -> None:
    fakes.semantic = dp.ChannelUnavailable("disabled")
    fakes.keyword["llm agents"] = [paper("doc-1")]
    payload = await run(fakes)

    semantic = payload["calls"][1]
    assert semantic == {
        "name": "library_semantic",
        "query": "how do llm agents plan?",
        "status": "unavailable",
        "count": 0,
        "reason": "disabled",
    }
    assert ids(payload) == ["doc-1"]
    assert "error" not in payload


async def test_an_arxiv_error_payload_is_a_failed_call_with_no_upstream_text(
    fakes: Fakes,
) -> None:
    fakes.arxiv["llm agents"] = {"error": "ArXiv rate limited (HTTP 429) token=abc"}
    fakes.keyword["llm agents"] = [paper("doc-1")]
    payload = await run(fakes)

    assert status_of(payload)["arxiv_keyword"] == "error"
    assert ids(payload) == ["doc-1"]
    assert "429" not in json.dumps(payload) and "abc" not in json.dumps(payload)


async def test_stale_cached_arxiv_results_still_count(fakes: Fakes) -> None:
    fakes.arxiv["llm agents"] = {
        "papers": [arxiv_row("2401.00001")],
        "cached": True,
        "stale": True,
        "warning": "ArXiv is unavailable; returned cached results.",
    }
    payload = await run(fakes, request(sources=["arxiv"]))

    assert ids(payload) == ["2401.00001"]


async def test_every_call_failing_is_one_stable_error(fakes: Fakes) -> None:
    fakes.keyword["llm agents"] = RuntimeError("boom")
    fakes.semantic = dp.ChannelUnavailable("not_provisioned")
    fakes.arxiv["llm agents"] = ConnectionError("dns secret.internal")
    payload = await run(fakes)

    assert payload["error"] == "upstream_unavailable"
    assert [call["status"] for call in payload["calls"]] == [
        "error",
        "unavailable",
        "error",
    ]
    assert "secret.internal" not in json.dumps(payload)


async def test_empty_answers_are_a_successful_round_with_no_papers(
    fakes: Fakes,
) -> None:
    fakes.semantic = dp.ChannelUnavailable("disabled")
    payload = await run(fakes)

    assert payload["papers"] == [] and "error" not in payload
    assert status_of(payload) == {
        "library_keyword": "empty",
        "library_semantic": "unavailable",
        "arxiv_keyword": "empty",
    }


# --- deduplication ---------------------------------------------------------------------


async def test_an_arxiv_paper_already_in_the_library_is_shown_as_the_library_copy(
    fakes: Fakes,
) -> None:
    fakes.keyword["llm agents"] = [
        paper("doc-rag", arxiv_id="2005.11401", title="RAG for NLP tasks")
    ]
    # The version suffix and the title differ: the arXiv id alone matches them.
    fakes.arxiv["llm agents"] = [
        arxiv_row("2005.11401v4", title="Retrieval-Augmented Generation")
    ]
    payload = await run(fakes)

    assert ids(payload) == ["doc-rag"]
    assert payload["papers"][0]["source"] == "library"
    assert payload["papers"][0]["found_by"] == ["library_keyword", "arxiv_keyword"]
    assert payload["papers"][0]["score"] == 1.0


async def test_the_library_copy_wins_even_when_arxiv_ranked_it_higher(
    fakes: Fakes,
) -> None:
    fakes.semantic = [
        paper("other-1"),
        paper("other-2"),
        paper("doc-rag", arxiv_id="2005.11401"),
    ]
    fakes.arxiv["llm agents"] = [arxiv_row("2005.11401")]
    payload = await run(fakes)

    assert "doc-rag" in ids(payload) and "2005.11401" not in ids(payload)


async def test_the_same_paper_with_differently_written_titles_is_one_paper(
    fakes: Fakes,
) -> None:
    fakes.keyword["llm agents"] = [
        paper(
            "doc-1",
            title="Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks",
        )
    ]
    fakes.arxiv["llm agents"] = [
        arxiv_row(
            "2005.99999",
            title="RETRIEVAL AUGMENTED generation, for knowledge intensive NLP tasks.",
        )
    ]
    payload = await run(fakes)

    assert ids(payload) == ["doc-1"]


async def test_accents_and_case_do_not_hide_a_duplicate_title(fakes: Fakes) -> None:
    fakes.keyword["llm agents"] = [
        paper("doc-1", title="Étude sur les modèles de langage")
    ]
    fakes.arxiv["llm agents"] = [
        arxiv_row("2401.00002", title="etude sur les modeles de langage")
    ]
    payload = await run(fakes)

    assert ids(payload) == ["doc-1"]


async def test_two_documents_with_one_doi_are_one_paper(fakes: Fakes) -> None:
    fakes.keyword["llm agents"] = [
        paper("doc-1", title="First title of the paper", doi="10.1000/xyz123"),
        paper("doc-2", title="A very different title", doi="10.1000/XYZ123"),
    ]
    payload = await run(fakes)

    assert ids(payload) == ["doc-1"]


async def test_one_document_found_by_two_calls_appears_once_with_both_calls(
    fakes: Fakes,
) -> None:
    fakes.keyword["llm agents"] = [paper("doc-1")]
    fakes.semantic = [paper("doc-1")]
    payload = await run(fakes)

    assert ids(payload) == ["doc-1"]
    assert payload["papers"][0]["found_by"] == ["library_keyword", "library_semantic"]


async def test_short_or_different_titles_are_not_merged(fakes: Fakes) -> None:
    fakes.arxiv["llm agents"] = [
        arxiv_row("2401.00001", title="Introduction"),
        arxiv_row("2401.00002", title="Introduction"),
        arxiv_row("2401.00003", title="Planning with language models"),
    ]
    payload = await run(fakes, request(sources=["arxiv"]))

    assert ids(payload) == ["2401.00001", "2401.00002", "2401.00003"]


async def test_a_merged_paper_takes_the_better_date_and_the_missing_fields(
    fakes: Fakes,
) -> None:
    fakes.keyword["llm agents"] = [
        paper(
            "doc-1",
            arxiv_id="2401.00001",
            published=date(2026, 9, 1),
            date_source="created_at",
            authors=(),
            snippet="",
        )
    ]
    fakes.arxiv["llm agents"] = [
        arxiv_row(
            "2401.00001v2", published="2024-01-02T00:00:00Z", abstract="Real abstract."
        )
    ]
    payload = await run(fakes)

    (merged,) = payload["papers"]
    assert merged["id"] == "doc-1" and merged["source"] == "library"
    # The ingest day is a fallback; the arXiv submission date is the real one.
    assert (merged["published"], merged["date_source"]) == ("2024-01-02", "arxiv")
    assert merged["authors"] == ["X. Writer"] and merged["snippet"] == "Real abstract."


# --- fusion, priority, cut ----------------------------------------------------------------


async def test_reciprocal_rank_fusion_across_calls(fakes: Fakes) -> None:
    a, b, c = paper("A"), paper("B"), paper("C")
    fakes.keyword["llm agents"] = [a, b]
    fakes.semantic = [c, a]
    payload = await run(fakes)

    assert ids(payload) == ["A", "C", "B"]
    scores = {item["id"]: item["score"] for item in payload["papers"]}
    fused_a, fused_c, fused_b = 1 / 61 + 1 / 62, 1 / 61, 1 / 62
    assert scores["A"] == 1.0
    assert scores["C"] == pytest.approx(fused_c / fused_a, abs=1e-3)
    assert scores["B"] == pytest.approx(fused_b / fused_a, abs=1e-3)


def _records(*lists: list[Candidate]) -> list[CallRecord]:
    return [
        CallRecord(f"call_{index}", "q", "ok", candidates=candidates)
        for index, candidates in enumerate(lists)
    ]


def test_recency_moves_a_newer_paper_up_and_excludes_nothing() -> None:
    old = paper("old", published=date(2014, 1, 1))
    new = paper("new", published=date(2026, 9, 1))
    records = _records([old, new])

    by_mode = {
        mode: [r.paper.id for r in rank_papers(records, mode, TODAY)]  # type: ignore[arg-type]
        for mode in ("default", "recency", "historical")
    }

    assert by_mode["default"] == ["old", "new"]
    assert by_mode["recency"] == ["new", "old"]
    assert by_mode["historical"] == ["old", "new"]
    assert all(len(order) == 2 for order in by_mode.values())


def test_historical_moves_an_older_paper_up_and_citations_help() -> None:
    new = paper("new", published=date(2026, 9, 1))
    old = paper("old", published=date(2012, 1, 1))
    cited = paper("cited", published=date(2026, 8, 1), citations=50_000)
    uncited = paper("uncited", published=date(2026, 8, 1))
    records = _records([new, old], [uncited, cited])

    default = [r.paper.id for r in rank_papers(records, "default", TODAY)]
    historical = [r.paper.id for r in rank_papers(records, "historical", TODAY)]

    assert default.index("new") < default.index("old")
    assert historical.index("old") < historical.index("new")
    assert default.index("uncited") < default.index("cited")
    assert historical.index("cited") < historical.index("uncited")


def test_the_priority_nudge_is_light() -> None:
    # Ten places down one list is more than the nudge can make up.
    first = paper("first", published=date(2014, 1, 1))
    rest = [paper(f"p{index}", published=date(2014, 1, 1)) for index in range(1, 30)]
    newest = paper("newest", published=TODAY)
    records = _records([first, *rest, newest])

    order = [r.paper.id for r in rank_papers(records, "recency", TODAY)]

    assert order[0] == "first"
    assert order.index("newest") < len(order) - 1  # but it did move up


def test_equal_scores_fall_back_to_the_favoured_age_then_undated_last() -> None:
    a = paper("a", published=date(2020, 1, 1))
    b = paper("b", published=date(2024, 1, 1))
    undated = paper("undated", published=None)
    records = _records([a], [b], [undated])  # three lists, each a rank-1 hit

    default = [r.paper.id for r in rank_papers(records, "default", TODAY)]
    recency = [r.paper.id for r in rank_papers(records, "recency", TODAY)]
    historical = [r.paper.id for r in rank_papers(records, "historical", TODAY)]

    assert default == ["a", "b", "undated"]  # discovery order
    assert recency == ["b", "a", "undated"]
    assert historical == ["a", "b", "undated"]


def _ranked(*scores: float) -> list[RankedPaper]:
    return [
        RankedPaper(paper(f"p{index}"), score, ("call",))
        for index, score in enumerate(scores)
    ]


def test_a_dominant_paper_cuts_the_weak_tail_but_keeps_five() -> None:
    kept = select_papers(_ranked(1.0, *([0.05] * 20)))

    assert [r.paper.id for r in kept] == ["p0", "p1", "p2", "p3", "p4"]


def test_the_cut_is_relative_to_the_best_score() -> None:
    kept = select_papers(_ranked(1.0, 0.9, 0.5, 0.31, 0.3, 0.29, 0.28, 0.1))

    assert len(kept) == 5  # 0.3 itself stays, 0.29 does not


def test_fewer_than_five_candidates_are_all_returned() -> None:
    assert len(select_papers(_ranked(1.0, 0.01, 0.01))) == 3
    assert select_papers([]) == []
    assert len(select_papers(_ranked(1.0))) == 1


def test_the_list_is_capped_at_fifteen() -> None:
    assert len(select_papers(_ranked(*([1.0] * 40)))) == 15


async def test_an_exact_name_query_is_not_padded_to_fifteen(fakes: Fakes) -> None:
    # One paper dominates: the keyword search, the semantic search and arXiv all
    # return it first. Thirty weak semantic neighbours follow.
    star = paper("star", arxiv_id="2501.00001", title="Q2D-Web: a web agent benchmark")
    fakes.keyword["Q2D-Web"] = [star]
    fakes.semantic = [star] + [paper(f"weak-{n}") for n in range(30)]
    fakes.arxiv["Q2D-Web"] = [arxiv_row("2501.00001")]
    payload = await run(fakes, request(keywords=["Q2D-Web"]))

    top = 3 / 61
    expected = 1 + sum(1 for rank in range(2, 32) if 1 / (60 + rank) >= 0.3 * top)
    assert len(payload["papers"]) == expected == 7
    assert ids(payload)[0] == "star"
    assert payload["papers"][0]["found_by"] == [
        "library_keyword",
        "library_semantic",
        "arxiv_keyword",
    ]


async def test_a_broad_query_is_capped_at_fifteen(fakes: Fakes) -> None:
    fakes.keyword["llm agents"] = [paper(f"k-{n}") for n in range(40)]
    payload = await run(fakes)

    assert len(payload["papers"]) == 15
    assert [item["score"] for item in payload["papers"]] == sorted(
        (item["score"] for item in payload["papers"]), reverse=True
    )


# --- the citation gate --------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["merged", "failures", "windowed", "capped"])
async def test_every_returned_id_came_from_a_successful_call(
    fakes: Fakes, scenario: str
) -> None:
    returned: set[str] = set()
    if scenario == "merged":
        library = [paper("doc-1", arxiv_id="2401.00001"), paper("doc-2")]
        arxiv = [arxiv_row("2401.00001v3"), arxiv_row("2401.00009")]
        fakes.keyword["llm agents"] = library
        fakes.semantic = [paper("doc-2"), paper("doc-3")]
        fakes.arxiv["llm agents"] = arxiv
        returned = {"doc-1", "doc-2", "doc-3", "2401.00001v3", "2401.00009"}
    elif scenario == "failures":
        fakes.keyword["llm agents"] = RuntimeError("down")
        fakes.semantic = [paper("doc-5")]
        fakes.arxiv["llm agents"] = TimeoutError()
        returned = {"doc-5"}
    elif scenario == "windowed":
        fakes.semantic = [paper("doc-6", published=date(2020, 1, 1))]
        returned = {"doc-6"}
    else:
        fakes.keyword["llm agents"] = [paper(f"k-{n}") for n in range(40)]
        returned = {f"k-{n}" for n in range(40)}
    payload = await run(fakes)

    assert payload["papers"], scenario
    assert set(ids(payload)) <= returned


def test_the_citation_gate_drops_an_id_no_successful_call_returned() -> None:
    seen = paper("seen")
    ghost = paper("ghost")
    failed_call = CallRecord("failed", "q", "error", reason="timeout")
    records = _records([seen])
    selected = [
        RankedPaper(seen, 1.0, ("call_0",)),
        RankedPaper(ghost, 0.9, ("failed",)),
    ]

    gated = dp._cite_gate(selected, records + [failed_call])

    assert [r.paper.id for r in gated] == ["seen"]


# --- output ------------------------------------------------------------------------------


async def test_the_payload_shape(fakes: Fakes) -> None:
    fakes.keyword["llm agents"] = [
        paper(
            "doc-1",
            authors=tuple(f"Author {n}" for n in range(9)),
            snippet="s" * 900,
            title="T" * 900,
        )
    ]
    payload = await run(
        fakes,
        request(
            prioritize="recency",
            difficulty=8,
            published_after="2020-01-01",
        ),
    )

    assert set(payload) == {
        "papers",
        "calls",
        "window",
        "prioritize",
        "difficulty",
        "followup_rounds_allowed",
    }
    assert payload["prioritize"] == "recency" and payload["difficulty"] == 8
    assert payload["followup_rounds_allowed"] == 2
    assert payload["window"] == {"after": "2020-01-01", "before": None}
    (item,) = payload["papers"]
    assert set(item) == {
        "id",
        "source",
        "title",
        "published",
        "date_source",
        "authors",
        "snippet",
        "score",
        "found_by",
    }
    assert len(item["authors"]) <= 5 and len(item["snippet"]) <= 300
    assert item["published"] == "2024-05-01" and item["score"] == 1.0
    for call in payload["calls"]:
        assert {"name", "query", "status", "count"} <= set(call)
        assert call["status"] in {"ok", "empty", "unavailable", "error"}
    assert payload["calls"][0]["count"] == 1


# --- the channels ------------------------------------------------------------------------


class _FakeDbError(Exception):
    def __init__(self, **attributes: Any) -> None:
        super().__init__("relation does not exist")
        for name, value in attributes.items():
            setattr(self, name, value)


def _db_error(orig: BaseException) -> ProgrammingError:
    return ProgrammingError("SELECT 1", {}, orig)


@pytest.mark.parametrize(
    ("orig", "missing"),
    [
        (_FakeDbError(sqlstate="42P01"), True),
        (_FakeDbError(pgcode="42P01"), True),
        (_FakeDbError(sqlstate="42704"), True),  # type "vector" does not exist
        (_FakeDbError(sqlstate="42601"), False),  # syntax error
        (_FakeDbError(sqlstate="57014"), False),  # statement timeout
        (Exception('relation "document_embeddings" does not exist'), True),
        (Exception('relation "documents" does not exist'), False),
        (Exception("connection reset"), False),
    ],
)
def test_a_missing_embeddings_table_is_told_from_other_database_errors(
    orig: BaseException, missing: bool
) -> None:
    assert dp._embeddings_table_missing(_db_error(orig)) is missing


def test_the_sqlstate_is_read_from_the_driver_cause_too() -> None:
    orig = Exception("driver wrapper")
    orig.__cause__ = _FakeDbError(sqlstate="42P01")

    assert dp._embeddings_table_missing(_db_error(orig)) is True


class _FakeSession:
    def __init__(self) -> None:
        self.savepoints = 0

    @asynccontextmanager
    async def begin_nested(self) -> AsyncIterator[None]:
        self.savepoints += 1
        yield


def _fake_tool_session(session: Any) -> Callable[[], Any]:
    @asynccontextmanager
    async def opener() -> AsyncIterator[Any]:
        yield session

    return opener


@pytest.fixture
def semantic_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "SEMANTIC_SEARCH_ENABLED", True)
    monkeypatch.setattr(dp, "_unprovisioned_logged", False)
    yield


def _semantic_kwargs() -> dict[str, Any]:
    return {
        "query": "question",
        "organization_id": ORG,
        "project_ids": [PROJECT],
        "after": None,
        "before": None,
        "limit": 10,
    }


async def test_semantic_is_unavailable_when_the_flag_is_off_and_opens_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SEMANTIC_SEARCH_ENABLED", False)

    def forbidden() -> Any:
        raise AssertionError("a session was opened for a disabled channel")

    monkeypatch.setattr(dp, "tool_session", forbidden)
    with pytest.raises(dp.ChannelUnavailable) as unavailable:
        await dp._library_semantic(**_semantic_kwargs())
    record = await dp._run_call(
        dp._CallSpec(
            "library_semantic",
            "question",
            1.0,
            lambda: dp._library_semantic(**_semantic_kwargs()),
        )
    )

    assert unavailable.value.reason == "disabled"
    assert (record.status, record.reason) == ("unavailable", "disabled")


async def test_a_missing_table_is_unavailable_logged_once_and_the_savepoint_isolates_it(
    monkeypatch: pytest.MonkeyPatch,
    semantic_on: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(dp, "tool_session", _fake_tool_session(session))
    monkeypatch.setattr(
        dp,
        "semantic_search",
        AsyncMock(side_effect=_db_error(_FakeDbError(sqlstate="42P01"))),
    )
    with caplog.at_level(logging.DEBUG, logger=dp.logger.name):
        for _ in range(3):
            with pytest.raises(dp.ChannelUnavailable) as unavailable:
                await dp._library_semantic(**_semantic_kwargs())
            assert unavailable.value.reason == "not_provisioned"

    assert session.savepoints == 3  # every attempt ran inside its own SAVEPOINT
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "document_embeddings" in warnings[0].getMessage()


async def test_any_other_database_error_is_an_error_not_unavailable(
    monkeypatch: pytest.MonkeyPatch, semantic_on: None
) -> None:
    monkeypatch.setattr(dp, "tool_session", _fake_tool_session(_FakeSession()))
    monkeypatch.setattr(
        dp,
        "semantic_search",
        AsyncMock(side_effect=_db_error(_FakeDbError(sqlstate="57014"))),
    )
    record = await dp._run_call(
        dp._CallSpec(
            "library_semantic",
            "question",
            1.0,
            lambda: dp._library_semantic(**_semantic_kwargs()),
        )
    )

    assert (record.status, record.reason) == ("error", "upstream_error")


async def test_arxiv_candidates_are_normalized() -> None:
    search = AsyncMock(
        return_value={
            "papers": [
                arxiv_row(
                    "2005.11401v4",
                    title="  A   spaced   title ",
                    authors=["A", "B", "C"],
                    abstract="x" * 600,
                    published="2020-05-22 17:41:13+00:00",
                ),
                {"title": "no id"},
                arxiv_row("math/0309136v1", published="not a date"),
            ]
        }
    )
    found = await dp._arxiv_keyword(
        arxiv_search=search, query="rag", after=None, before=None, limit=30
    )

    assert [c.id for c in found] == ["2005.11401v4", "math/0309136v1"]
    first, second = found
    assert first.title == "A spaced title" and first.authors == ("A", "B", "C")
    assert len(first.snippet) == 300 and first.snippet.endswith("…")
    assert (first.published, first.date_source) == (date(2020, 5, 22), "arxiv")
    assert (first.arxiv_id, second.arxiv_id) == ("2005.11401", "math/0309136")
    assert (second.published, second.date_source) == (None, None)


async def test_arxiv_with_no_usable_keyword_is_not_called() -> None:
    search = AsyncMock()

    assert (
        await dp._arxiv_keyword(
            arxiv_search=search, query='"():', after=None, before=None, limit=10
        )
        == []
    )
    search.assert_not_awaited()


# --- the grant scope, on SQLite ------------------------------------------------------------


def _document_row(document_id: UUID, title: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = dict(
        id=document_id,
        organization_id=ORG,
        title=title,
        filename=f"{title}.txt",
        file_path=f"/tmp/{title}.txt",
        file_size_bytes=1,
        mime_type="text/plain",
        document_type=DocumentType.TEXT,
        arxiv_id=None,
        document_metadata=None,
    )
    row.update(extra)
    return row


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        tables: list[Any] = [
            Organization,
            User,
            Workspace,
            WorkspaceMember,
            Collection,
            Document,
            CollectionDocument,
        ]
        for model in tables:
            await conn.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await session.execute(
            insert(Organization).values(
                [
                    dict(id=ORG, name="Test", storage_limit_bytes=1000000),
                    dict(id=OTHER_ORG, name="Other", storage_limit_bytes=1000000),
                ]
            )
        )
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, 'discover@example.test', 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
            ),
            {"id": str(USER), "org": str(ORG)},
        )
        await session.execute(
            insert(Workspace).values(
                [
                    dict(id=WORKSPACE, name="W", owner_id=USER, organization_id=ORG),
                    dict(
                        id=OTHER_WORKSPACE,
                        name="Other W",
                        owner_id=USER,
                        organization_id=ORG,
                    ),
                ]
            )
        )
        await session.execute(
            insert(Collection).values(
                [
                    dict(id=PROJECT, name="Project", workspace_id=WORKSPACE),
                    dict(id=OTHER_PROJECT, name="Other", workspace_id=WORKSPACE),
                    dict(
                        id=FOREIGN_PROJECT, name="Foreign", workspace_id=OTHER_WORKSPACE
                    ),
                    dict(
                        id=DELETED_PROJECT,
                        name="Gone",
                        workspace_id=WORKSPACE,
                        is_deleted=True,
                    ),
                ]
            )
        )
        await session.execute(
            insert(Document).values(
                [
                    _document_row(
                        DOC_IN,
                        "in-scope",
                        arxiv_id="2005.11401",
                        document_metadata={
                            "authors": ["P. Lewis", {"name": "E. Perez"}, 7, ""],
                            "doi": "https://doi.org/10.1000/ABC",
                            "citation_count": 123,
                        },
                    ),
                    _document_row(DOC_SIBLING, "sibling"),
                    _document_row(DOC_FOREIGN, "foreign"),
                    _document_row(DOC_DELETED_PROJECT, "deleted-project"),
                    _document_row(
                        DOC_OTHER_ORG, "other-org", organization_id=OTHER_ORG
                    ),
                    _document_row(DOC_GONE, "gone", is_deleted=True),
                ]
            )
        )
        await session.execute(
            insert(CollectionDocument).values(
                [
                    dict(collection_id=PROJECT, document_id=DOC_IN),
                    dict(collection_id=OTHER_PROJECT, document_id=DOC_SIBLING),
                    dict(collection_id=FOREIGN_PROJECT, document_id=DOC_FOREIGN),
                    dict(
                        collection_id=DELETED_PROJECT, document_id=DOC_DELETED_PROJECT
                    ),
                    # A foreign-organization document linked into this project.
                    dict(collection_id=PROJECT, document_id=DOC_OTHER_ORG),
                    dict(collection_id=PROJECT, document_id=DOC_GONE),
                ]
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


def _context(project: bool, scopes: frozenset[str] = TOOLS_READ) -> IntegrationContext:
    return IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT if project else None,
        workspace_id=None if project else WORKSPACE,
        grant_id=uuid4(),
        scopes=scopes,
    )


def _invocation(**arguments: Any) -> ToolInvocation:
    arguments.setdefault("keywords", ["llm agents"])
    arguments.setdefault("question", "how do llm agents plan?")
    arguments.setdefault("difficulty", 5)
    return ToolInvocation(
        tool_name="discover_papers", arguments=arguments, invocation_id=uuid4()
    )


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch, fakes: Fakes) -> Fakes:
    monkeypatch.setattr(read_tools, "_tool_search_arxiv", fakes.arxiv_search)
    return fakes


async def test_a_project_grant_searches_only_its_project(
    db: AsyncSession, gateway: Fakes
) -> None:
    result = await invoke_read(db, _context(project=True), _invocation())

    assert result.is_error is False
    for arguments in gateway.named("library_keyword") + gateway.named(
        "library_semantic"
    ):
        assert arguments["project_ids"] == [PROJECT]
        assert arguments["organization_id"] == ORG


async def test_a_workspace_grant_searches_its_live_projects_and_no_other(
    db: AsyncSession, gateway: Fakes
) -> None:
    await invoke_read(db, _context(project=False), _invocation())

    scope = gateway.named("library_semantic")[0]["project_ids"]
    # Both live projects of the grant's workspace; not the user's other
    # workspace, not the soft-deleted project.
    assert scope == sorted([PROJECT, OTHER_PROJECT])
    assert FOREIGN_PROJECT not in scope and DELETED_PROJECT not in scope


async def test_an_arxiv_only_round_reads_no_library_scope(
    db: AsyncSession, gateway: Fakes
) -> None:
    result = await invoke_read(
        db, _context(project=False), _invocation(sources=["arxiv"])
    )

    assert result.is_error is False
    assert gateway.named("library_keyword") == [] == gateway.named("library_semantic")
    assert len(gateway.named("arxiv")) == 1


async def test_a_deleted_workspace_is_denied_before_any_search(
    db: AsyncSession, gateway: Fakes
) -> None:
    from sqlalchemy import update

    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(is_deleted=True)
    )
    await db.commit()

    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(db, _context(project=False), _invocation())
    assert gateway.calls == []


async def test_the_tool_needs_tools_read(db: AsyncSession, gateway: Fakes) -> None:
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(
            db,
            _context(project=True, scopes=frozenset({"library:read"})),
            _invocation(),
        )
    assert gateway.calls == []


async def test_bad_arguments_are_a_422_with_a_safe_reason(
    db: AsyncSession, gateway: Fakes
) -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        ({"published_after": "2020-13-45"}, "published_after must be a date"),
        (
            {"published_after": "2021-01-01", "published_before": "2020-01-01"},
            "must not be later",
        ),
        ({"keywords": ["a"] * 9}, "keywords must be 1-8 strings"),
        ({"difficulty": 11}, "difficulty must be an integer from 1 to 10"),
    ]
    for arguments, message in cases:
        with pytest.raises(ToolArgumentError, match=message):
            await invoke_read(db, _context(project=True), _invocation(**arguments))
    with pytest.raises(ToolArgumentError, match="unknown arguments"):
        await invoke_read(
            db, _context(project=True), _invocation(project_id=str(PROJECT))
        )
    assert gateway.calls == []


async def test_the_identity_query_drops_documents_outside_the_scope(
    db: AsyncSession,
) -> None:
    def hit(document_id: UUID) -> Any:
        return SimpleNamespace(
            document_id=str(document_id),
            title=f"title {document_id}",
            publication_date=date(2020, 1, 1),
            publication_date_source="metadata",
            snippet="snippet",
        )

    hits = [
        hit(document)
        for document in (
            DOC_IN,
            DOC_SIBLING,  # another project of the same workspace
            DOC_FOREIGN,  # another workspace
            DOC_DELETED_PROJECT,  # a soft-deleted project
            DOC_OTHER_ORG,  # another organization, linked into the project
            DOC_GONE,  # soft-deleted
            uuid4(),  # unknown
        )
    ]
    found = await dp._library_candidates(
        db, hits, organization_id=ORG, project_ids=[PROJECT]
    )

    assert [candidate.id for candidate in found] == [str(DOC_IN)]
    (candidate,) = found
    assert candidate.source == "library"
    assert candidate.arxiv_id == "2005.11401"
    assert candidate.doi == "10.1000/abc"
    assert candidate.authors == ("P. Lewis", "E. Perez")
    assert candidate.citations == 123
    both = await dp._library_candidates(
        db, hits, organization_id=ORG, project_ids=[PROJECT, OTHER_PROJECT]
    )
    assert {c.id for c in both} == {str(DOC_IN), str(DOC_SIBLING)}
    assert (
        await dp._library_candidates(db, hits, organization_id=ORG, project_ids=[])
        == []
    )


async def test_library_text_is_redacted_like_the_other_library_tools(
    db: AsyncSession,
) -> None:
    hit = SimpleNamespace(
        document_id=str(DOC_IN),
        title="Contact me@example.com about this",
        publication_date=None,
        publication_date_source="created_at",
        snippet="Write to author@example.org " + "x" * 400,
    )
    (candidate,) = await dp._library_candidates(
        db, [hit], organization_id=ORG, project_ids=[PROJECT]
    )

    assert "example.com" not in candidate.title
    assert "example.org" not in candidate.snippet and len(candidate.snippet) <= 300


# --- the result envelope ----------------------------------------------------------------------


async def test_source_refs_name_documents_and_arxiv_papers_correctly(
    db: AsyncSession, gateway: Fakes
) -> None:
    gateway.keyword["llm agents"] = [paper(str(DOC_IN))]
    gateway.arxiv["llm agents"] = [arxiv_row("2401.00001")]
    result = await invoke_read(db, _context(project=True), _invocation())

    assert result.is_error is False
    assert result.source_refs == [
        {"document_id": str(DOC_IN)},
        {"arxiv_id": "2401.00001"},
    ]
    # Never a library document id under the arXiv key.
    assert {"arxiv_id": str(DOC_IN)} not in result.source_refs


async def test_a_failed_round_is_a_tool_error_with_no_refs(
    db: AsyncSession, gateway: Fakes
) -> None:
    gateway.keyword["llm agents"] = RuntimeError("boom")
    gateway.semantic = dp.ChannelUnavailable("disabled")
    gateway.arxiv["llm agents"] = RuntimeError("boom")
    result = await invoke_read(db, _context(project=True), _invocation())

    assert result.is_error is True and result.source_refs == []
    assert result.content[0]["error"] == "upstream_unavailable"
    assert [call["status"] for call in result.content[0]["calls"]] == [
        "error",
        "unavailable",
        "error",
    ]


def _long(prefix: str, glyph: str, size: int) -> str:
    return (prefix + glyph * size)[:size]


@pytest.mark.parametrize(
    ("glyph", "truncates"),
    [("a", False), ("語", False), ("\x01", True)],
    ids=["ascii", "multibyte", "escaped"],
)
async def test_the_result_stays_under_the_cap_with_long_snippets(
    db: AsyncSession, gateway: Fakes, glyph: str, truncates: bool
) -> None:
    gateway.keyword["llm agents"] = [
        paper(
            f"doc-{n}",
            title=_long(f"{n}", glyph, 300),
            snippet=_long("", glyph, 300),
            authors=tuple(_long(f"{m}", glyph, 100) for m in range(5)),
        )
        for n in range(40)
    ]
    # Every field is bounded, which keeps 15 ASCII or CJK papers under the cap;
    # only text that JSON escapes to six bytes a character (control characters)
    # needs the trimming. The envelope is what is measured, with the refs
    # derived from the papers.
    result = await invoke_read(db, _context(project=True), _invocation())

    assert result.is_error is False
    envelope = {
        "content": result.content,
        "is_error": result.is_error,
        "source_refs": result.source_refs,
    }
    assert read_tools._wire_size(envelope) <= MAX_RESULT_BYTES
    papers = result.content[0]["papers"]
    assert 1 <= len(papers) <= 15
    assert len(result.source_refs) == len(papers)
    assert bool(result.content[0].get("truncated")) is truncates
    if not truncates:
        assert len(papers) == 15


# --- the catalogue ----------------------------------------------------------------------------


def test_the_tool_is_in_the_catalogue_with_the_loop_rules() -> None:
    tool = next(t for t in list_read_tools() if t.name == "discover_papers")
    schema = tool.input_schema

    assert read_tools.TOOL_SCOPES["discover_papers"] == "tools:read"
    assert "discover_papers" in read_tools._PROJECTLESS_TOOLS
    assert schema["required"] == ["keywords", "question", "difficulty"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "keywords",
        "question",
        "difficulty",
        "prioritize",
        "published_after",
        "published_before",
        "sources",
    }
    properties = schema["properties"]
    assert (properties["keywords"]["minItems"], properties["keywords"]["maxItems"]) == (
        1,
        8,
    )
    assert properties["keywords"]["items"]["maxLength"] == 100
    assert properties["question"]["maxLength"] == 500
    assert (
        properties["difficulty"]["minimum"],
        properties["difficulty"]["maximum"],
    ) == (1, 10)
    assert properties["prioritize"]["enum"] == ["default", "recency", "historical"]
    assert properties["sources"]["items"]["enum"] == ["library", "arxiv"]
    description = tool.description
    for phrase in (
        "never synonyms or expansions",
        "same values in every call of the loop",
        "followup_rounds_allowed",
        "ONE concrete gap",
        "never a rephrase",
        "5-15 paper ids",
        "at most two such loops per user turn",
    ):
        assert phrase in description.replace("\n", " "), phrase


def test_a_workspace_grant_gets_no_project_selector_on_it() -> None:
    tool = next(
        t for t in list_read_tools(workspace_bound=True) if t.name == "discover_papers"
    )

    assert "project_id" not in tool.input_schema["properties"]
