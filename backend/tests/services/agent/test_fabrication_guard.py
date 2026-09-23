"""Tests for the fabricated-tool-success reflection guard.

Covers ``_detect_fabricated_tool_success`` and the ``_should_skip_reflection``
interaction, ensuring the deterministic guard fires when the AI claims a
creation happened but no creation tool actually executed, and stays silent
when the model is being honest.
"""

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.services.agent.reflection import (
    ReflectionResult,
    _detect_fabricated_doc_search,
    _detect_fabricated_ingest,
    _detect_fabricated_kg_search,
    _detect_fabricated_tool_success,
    _detect_ingest_success_lie,
    _should_skip_reflection,
    make_reflection_gate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    ai_content: str | list,
    tool_executions: list | None = None,
    intent: str = "research",
    reflection_count: int = 0,
) -> dict:
    """Build a minimal state dict with a single HumanMessage + AIMessage."""
    return {
        "messages": [
            HumanMessage(content="Please create a project for my NLP research"),
            AIMessage(content=ai_content),
        ],
        "tool_executions": tool_executions if tool_executions is not None else [],
        "intent": intent,
        "reflection_count": reflection_count,
    }


def _completed_create_project_te() -> dict:
    """A completed create_project tool execution."""
    return {
        "tool_name": "create_project",
        "status": "completed",
        "result": {
            "project_id": "b3f9e2d4-abc1-2345-6789-abcdef012345",
            "name": "NLP Research",
            "status": "active",
        },
    }


# ---------------------------------------------------------------------------
# Tests: _detect_fabricated_tool_success
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDetectFabricatedToolSuccess:
    def test_plain_text_fabrication_fires(self):
        """AI claims project created with a project_id in prose but no tool ran."""
        state = _make_state(
            ai_content=(
                "Project created (project_id: b3f9e2d4-abc1-2345-6789-abcdef012345),"
                " status active."
            ),
            tool_executions=[],
        )
        issue = _detect_fabricated_tool_success(state)
        assert issue is not None
        assert "fabricated" in issue

    def test_inline_json_fabrication_fires(self):
        """AI embeds a JSON blob with project_id but no creation tool ran."""
        state = _make_state(
            ai_content=(
                'Here is the result: {"project_id":"b3f9e2d4-abc1-2345-6789-abcdef012345",'
                '"status":"active"}'
            ),
            tool_executions=[],
        )
        issue = _detect_fabricated_tool_success(state)
        assert issue is not None

    def test_prose_created_the_project_fires(self):
        """'I have created the project for NLP' with no tool → fabrication."""
        state = _make_state(
            ai_content="I have created the project for NLP research.",
            tool_executions=[],
        )
        issue = _detect_fabricated_tool_success(state)
        assert issue is not None

    def test_honest_disclosure_suppresses_flag(self):
        """When AI honestly says it couldn't save, no fabrication flag."""
        state = _make_state(
            ai_content=(
                "I couldn't save it because no project_id was provided. "
                "Would you like me to create a project first?"
            ),
            tool_executions=[],
        )
        assert _detect_fabricated_tool_success(state) is None

    def test_honest_uncertainty_suppresses_flag(self):
        """Observed false positive (ingest scenario): the model expresses honest
        uncertainty about whether the target project exists — incompatible with
        claiming a creation succeeded. Even when a later clause trips the claim
        regex, the uncertainty disclosure must suppress the flag so an honest
        no-tool turn isn't force-revised. (Disclosure is checked before the
        claim regex, so it wins.)"""
        # "added … to the project" trips the claim regex; "don't yet know if"
        # is the honest-uncertainty disclosure that must override it.
        state = _make_state(
            ai_content=(
                "I don't yet know if the project synthtraffic-20260629 exists in "
                "your workspace, but I've added the paper to the project."
            ),
            tool_executions=[],
        )
        assert _detect_fabricated_tool_success(state) is None

    def test_not_sure_whether_suppresses_flag(self):
        """'not sure whether' uncertainty also reads as honest, not a claim.

        'created the project' trips the claim regex; 'not sure whether' overrides."""
        state = _make_state(
            ai_content=(
                "I'm not sure whether that project exists yet, so I have not "
                "created the project — want me to?"
            ),
            tool_executions=[],
        )
        assert _detect_fabricated_tool_success(state) is None

    def test_real_success_suppresses_flag(self):
        """AI says created + create_project completed → no fabrication."""
        state = _make_state(
            ai_content="Project created successfully. You can find it in your dashboard.",
            tool_executions=[_completed_create_project_te()],
        )
        assert _detect_fabricated_tool_success(state) is None

    def test_non_creation_tool_only_fires(self):
        """Only ingest_arxiv_papers completed — NOT a creation tool — so flag fires."""
        state = _make_state(
            ai_content="Project created (project_id: b3f9e2d4-abc1-2345-6789-abcdef012345).",
            tool_executions=[
                {
                    "tool_name": "ingest_arxiv_papers",
                    "status": "completed",
                    "result": {"ingested_count": 2},
                }
            ],
        )
        issue = _detect_fabricated_tool_success(state)
        assert issue is not None

    def test_no_ai_message_returns_none(self):
        """Defensive: state with no AIMessage should not crash."""
        state = {
            "messages": [HumanMessage(content="create a project")],
            "tool_executions": [],
        }
        assert _detect_fabricated_tool_success(state) is None

    def test_multimodal_content_list_fires(self):
        """AIMessage.content as a list of blocks is scanned for the claim."""
        state = _make_state(
            ai_content=[
                {"type": "text", "text": "Done! "},
                {
                    "type": "text",
                    "text": "project_id: b3f9e2d4-abc1-2345-6789-abcdef012345 is active.",
                },
            ],
            tool_executions=[],
        )
        assert _detect_fabricated_tool_success(state) is not None

    def test_create_project_note_completed_suppresses_flag(self):
        """create_project_note is a valid creation tool; its completion is enough."""
        state = _make_state(
            ai_content="Note saved to your project.",
            tool_executions=[
                {
                    "tool_name": "create_project_note",
                    "status": "completed",
                    "result": {"note_id": "abc123"},
                }
            ],
        )
        assert _detect_fabricated_tool_success(state) is None

    def test_create_draft_completed_suppresses_flag(self):
        """create_draft completion counts as a real creation."""
        state = _make_state(
            ai_content="Draft created for your NLP project.",
            tool_executions=[
                {
                    "tool_name": "create_draft",
                    "status": "completed",
                    "result": {"draft_id": "d001"},
                }
            ],
        )
        assert _detect_fabricated_tool_success(state) is None

    def test_revise_draft_completed_suppresses_flag(self):
        state = _make_state(
            ai_content="I saved revision v4.",
            tool_executions=[
                {
                    "tool_name": "revise_draft",
                    "status": "completed",
                    "result": {"draft_id": "d004", "version": 4},
                }
            ],
        )
        assert _detect_fabricated_tool_success(state) is None

    @pytest.mark.parametrize(
        "claim",
        ["I saved revision v4.", "Revision saved.", "I updated the draft."],
    )
    def test_revision_success_claim_without_tool_fires(self, claim: str):
        state = _make_state(ai_content=claim, tool_executions=[])

        issue = _detect_fabricated_tool_success(state)

        assert issue is not None
        assert "fabricated" in issue

    def test_add_document_to_project_completed_suppresses_flag(self):
        """add_document_to_project completion counts as a real creation."""
        state = _make_state(
            ai_content="Added the document to the project.",
            tool_executions=[
                {
                    "tool_name": "add_document_to_project",
                    "status": "completed",
                    "result": {},
                }
            ],
        )
        assert _detect_fabricated_tool_success(state) is None

    def test_creation_tool_with_failed_status_still_fires(self):
        """A creation tool that failed (status=failed) does not count as ran."""
        state = _make_state(
            ai_content="Project created (project_id: b3f9e2d4-abc1-2345-6789-abcdef012345).",
            tool_executions=[
                {
                    "tool_name": "create_project",
                    "status": "failed",
                    "result": {"error": "DB timeout"},
                }
            ],
        )
        issue = _detect_fabricated_tool_success(state)
        assert issue is not None

    def test_note_created_fires_without_tool(self):
        """'note saved' pattern triggers the guard with no tool execution."""
        state = _make_state(
            ai_content="Your note has been saved to the project.",
            tool_executions=[],
        )
        issue = _detect_fabricated_tool_success(state)
        assert issue is not None

    def test_added_document_to_project_fires_without_tool(self):
        """'added X to the project' phrase triggers the guard."""
        state = _make_state(
            ai_content="I have added the paper to the project.",
            tool_executions=[],
        )
        issue = _detect_fabricated_tool_success(state)
        assert issue is not None

    # -----------------------------------------------------------------------
    # False-positive regression cases (tightened regex must NOT trigger)
    # -----------------------------------------------------------------------

    def test_listing_projects_with_project_id_is_not_flagged(self):
        """AI quoting a list_projects tool result that contains project_id
        tokens must NOT trigger the fabrication guard — it's legitimate
        retrieval output, not a creation claim."""
        state = _make_state(
            ai_content=(
                "Here are your projects: NLP (project_id: abc-123), "
                "ML Experiments (project_id: def-456). "
                "Let me know which one you'd like to work with."
            ),
            tool_executions=[
                {
                    "tool_name": "list_projects",
                    "status": "completed",
                    "result": {
                        "projects": [
                            {"project_id": "abc-123", "name": "NLP"},
                            {"project_id": "def-456", "name": "ML Experiments"},
                        ]
                    },
                }
            ],
        )
        assert _detect_fabricated_tool_success(state) is None

    def test_forward_looking_suggestion_is_not_flagged(self):
        """'You could add this paper to the project later' is a forward-looking
        suggestion, NOT a past-action claim.  Must return None."""
        state = _make_state(
            ai_content=(
                "I've found three relevant papers on transformer architectures. "
                "You could add this paper to the project later if you'd like, "
                "or I can search for more specific results first."
            ),
            tool_executions=[],
        )
        assert _detect_fabricated_tool_success(state) is None

    def test_real_fabrication_string_still_caught(self):
        """The actual fabrication from the PR description must still be caught:
        the model dumps JSON tool args + result blob inline with prose."""
        state = _make_state(
            ai_content=(
                'Creating the project "NLP" now. {"name":"NLP"} '
                '{"project_id":"b3f9e2d4","status":"active"} '
                "Project created (project_id: b3f9e2d4)."
            ),
            tool_executions=[],
        )
        issue = _detect_fabricated_tool_success(state)
        assert issue is not None
        assert "fabricated" in issue

    def test_ive_created_without_tool_is_caught(self):
        """'I've created the project for you (project_id: x)' with no
        completed create_project execution must still be caught."""
        state = _make_state(
            ai_content=(
                "I've created the project for you (project_id: x-001). "
                "It should now appear in your dashboard."
            ),
            tool_executions=[],
        )
        issue = _detect_fabricated_tool_success(state)
        assert issue is not None
        assert "fabricated" in issue


# ---------------------------------------------------------------------------
# Tests: _should_skip_reflection does NOT skip on fabrication
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSkipReflectionFabricationGate:
    def test_skip_not_triggered_when_fabrication_detected(self):
        """When _detect_fabricated_tool_success fires, _should_skip_reflection
        must return (False, …) even though tool_executions is empty — the
        deterministic check needs to run and hard-fail the node."""
        state = _make_state(
            ai_content=(
                "project_id: b3f9e2d4-abc1-2345-6789-abcdef012345, status: active. "
                "Project created for your NLP research."
            ),
            tool_executions=[],
        )
        skip, reason = _should_skip_reflection(state)
        assert skip is False
        assert "fabricat" in reason

    def test_skip_still_applies_for_clean_tool_less_turn(self):
        """A tool-less turn with NO creation claim should still skip."""
        state = _make_state(
            ai_content="Sure, I can help with that!",
            tool_executions=[],
        )
        skip, reason = _should_skip_reflection(state)
        # "Sure, I can help with that!" is < 200 chars, no tool_calls → short-output
        assert skip is True


# ---------------------------------------------------------------------------
# Tests: end-to-end gate node integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGateNodeFabricationIntegration:
    @pytest.mark.asyncio
    async def test_gate_hard_fails_on_fabrication_without_llm(self):
        """End-to-end: gate must short-circuit BEFORE calling the reflection
        LLM when a fabrication is detected."""
        node_fn, _ = make_reflection_gate(intent_filter={"research"})
        state = _make_state(
            ai_content=(
                "I have created the project for NLP. "
                "project_id: b3f9e2d4-abc1-2345-6789-abcdef012345, status: active."
            ),
            tool_executions=[],
            intent="research",
            reflection_count=0,
        )

        with patch("src.services.agent.reflection._build_reflection_llm") as mock_build:
            updates = await node_fn(state, {"configurable": {}})
            assert mock_build.called is False

        result = updates["_reflection_result"]
        assert result.passed is False
        assert result.severity == "major"
        assert any("fabricated" in issue for issue in result.issues)
        assert updates["reflection_count"] == 1

    @pytest.mark.asyncio
    async def test_gate_respects_max_rounds_on_fabrication(self):
        """At reflection_count == 2, the max-round guard fires BEFORE the
        fabrication detector — the node returns early with no result."""
        node_fn, _ = make_reflection_gate(intent_filter={"research"})
        state = _make_state(
            ai_content="project_id: b3f9e2d4-abc1-2345-6789-abcdef012345 active.",
            tool_executions=[],
            intent="research",
            reflection_count=2,
        )

        with patch("src.services.agent.reflection._build_reflection_llm") as mock_build:
            updates = await node_fn(state, {"configurable": {}})
            assert mock_build.called is False

        assert updates["reflection_count"] == 2
        assert "_reflection_result" not in updates


# ---------------------------------------------------------------------------
# Tests: _detect_fabricated_ingest
# ---------------------------------------------------------------------------


def _make_ingest_state(
    *,
    ai_content: str | list,
    tool_executions: list | None = None,
    intent: str = "research",
    reflection_count: int = 0,
) -> dict:
    """Build a minimal state dict for ingest fabrication tests."""
    return {
        "messages": [
            HumanMessage(
                content="Please ingest arXiv:2605.12335v1 into my EHR-RAGp project"
            ),
            AIMessage(content=ai_content),
        ],
        "tool_executions": tool_executions if tool_executions is not None else [],
        "intent": intent,
        "reflection_count": reflection_count,
    }


def _completed_ingest_te(ingested_count: int = 1) -> dict:
    """A completed ingest_arxiv_papers tool execution."""
    return {
        "tool_name": "ingest_arxiv_papers",
        "status": "completed",
        "result": {
            "ingested_count": ingested_count,
            "paper_ids": ["2605.12335v1"],
            "status": (
                "ingestion_complete" if ingested_count > 0 else "ingestion_failed"
            ),
        },
    }


@pytest.mark.unit
class TestDetectFabricatedIngest:
    """Tests for ``_detect_fabricated_ingest``."""

    # -----------------------------------------------------------------------
    # CAUGHT cases
    # -----------------------------------------------------------------------

    def test_exact_prod_transcript_caught(self):
        """The exact transcript from the live bug trace must be detected.

        The model dumped {"paper_ids":[...]} + "Tool call made." +
        "I ran the import" as prose with no tool execution.
        """
        state = _make_ingest_state(
            ai_content=(
                "I'll import the arXiv paper arXiv:2605.12335v1 into your active project now.\n"
                '{"paper_ids":["2605.12335v1"]}\n'
                "Tool call made.\n"
                'The paper arXiv:2605.12335v1 is being imported into project "EHR-RAGp" — '
                "I ran the import; I'll confirm once it's finished."
            ),
            tool_executions=[],
        )
        issue = _detect_fabricated_ingest(state)
        assert issue is not None
        assert "ingest_arxiv_papers" in issue

    def test_inline_paper_ids_json_caught(self):
        """Inline tool-arg JSON alone is sufficient to trigger the guard."""
        state = _make_ingest_state(
            ai_content=(
                'Ingesting the paper now. {"paper_ids":["2605.12335v1"]} Done.'
            ),
            tool_executions=[],
        )
        assert _detect_fabricated_ingest(state) is not None

    def test_is_being_imported_caught(self):
        """'The paper is being imported' without a real tool run is caught."""
        state = _make_ingest_state(
            ai_content=(
                "The paper is being imported into the project; I've ingested it."
            ),
            tool_executions=[],
        )
        assert _detect_fabricated_ingest(state) is not None

    def test_ive_ingested_caught(self):
        """'I've ingested the paper' with no tool run is caught."""
        state = _make_ingest_state(
            ai_content="I've ingested the paper into your project.",
            tool_executions=[],
        )
        assert _detect_fabricated_ingest(state) is not None

    def test_has_been_imported_caught(self):
        """'has been imported' without tool run is caught."""
        state = _make_ingest_state(
            ai_content="The paper has been imported into your project.",
            tool_executions=[],
        )
        assert _detect_fabricated_ingest(state) is not None

    def test_tool_call_made_narration_caught(self):
        """Bare 'Tool call made.' narration artifact without tool run is caught."""
        state = _make_ingest_state(
            ai_content=("Importing the arXiv paper. Tool call made. Processing now."),
            tool_executions=[],
        )
        assert _detect_fabricated_ingest(state) is not None

    def test_non_ingest_tool_only_still_caught(self):
        """Only search_arxiv completed — ingest never ran — should still catch."""
        state = _make_ingest_state(
            ai_content=(
                '{"paper_ids":["2605.12335v1"]} Tool call made. I ran the import.'
            ),
            tool_executions=[
                {
                    "tool_name": "search_arxiv",
                    "status": "completed",
                    "result": {"papers": []},
                }
            ],
        )
        assert _detect_fabricated_ingest(state) is not None

    # -----------------------------------------------------------------------
    # NOT FLAGGED cases
    # -----------------------------------------------------------------------

    def test_question_turn_not_flagged(self):
        """Search-results turn asking which papers to ingest must not be flagged."""
        state = _make_ingest_state(
            ai_content=(
                "I found 5 papers on EHR RAG: [list]. "
                "Which of these would you like me to ingest? "
                "Do you want me to ingest any of them?"
            ),
            tool_executions=[
                {
                    "tool_name": "search_arxiv",
                    "status": "completed",
                    "result": {"papers": [{"id": "2605.12335v1"}]},
                }
            ],
        )
        assert _detect_fabricated_ingest(state) is None

    def test_would_you_like_me_to_not_flagged(self):
        """'Would you like me to ingest it?' offer must not be flagged."""
        state = _make_ingest_state(
            ai_content="Would you like me to ingest arXiv:2605.12335v1 for you?",
            tool_executions=[],
        )
        assert _detect_fabricated_ingest(state) is None

    def test_should_i_offer_not_flagged(self):
        """'Should I import it?' offer must not be flagged."""
        state = _make_ingest_state(
            ai_content="Should I import it into your project?",
            tool_executions=[],
        )
        assert _detect_fabricated_ingest(state) is None

    def test_honest_future_offer_not_flagged(self):
        """Pure future-tense offer without strong signals must not be flagged."""
        state = _make_ingest_state(
            ai_content=(
                "I can import arXiv:2605.12335 for you if you confirm. "
                "I will ingest it once you give the go-ahead."
            ),
            tool_executions=[],
        )
        assert _detect_fabricated_ingest(state) is None

    def test_real_ingest_completed_not_flagged(self):
        """A real completed ingest run must not be flagged (handled elsewhere)."""
        state = _make_ingest_state(
            ai_content="I've imported the paper into your project.",
            tool_executions=[_completed_ingest_te(ingested_count=1)],
        )
        assert _detect_fabricated_ingest(state) is None

    def test_no_ai_message_returns_none(self):
        """Defensive: state with no AIMessage must not crash."""
        state = {
            "messages": [HumanMessage(content="ingest a paper")],
            "tool_executions": [],
        }
        assert _detect_fabricated_ingest(state) is None


# ---------------------------------------------------------------------------
# Tests: no double-fire between _detect_fabricated_ingest and
#         _detect_ingest_success_lie
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIngestGuardsNoDoubleFire:
    """Verify the two ingest guards do not both fire on the same input."""

    def test_count_zero_caught_by_success_lie_not_fabricated_ingest(self):
        """When ingest ran but returned ingested_count=0, only
        _detect_ingest_success_lie should fire; _detect_fabricated_ingest
        must return None (executed_ingest=True)."""
        state = _make_ingest_state(
            ai_content="I've imported the paper into your project. Done — added one paper.",
            tool_executions=[_completed_ingest_te(ingested_count=0)],
        )
        # fabricated ingest → None because ingest_arxiv_papers completed
        assert _detect_fabricated_ingest(state) is None
        # count-0 lie → non-None (the real guard)
        assert _detect_ingest_success_lie(state) is not None


# ---------------------------------------------------------------------------
# Tests: _should_skip_reflection respects fabricated ingest
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSkipReflectionFabricatedIngest:
    def test_skip_not_triggered_when_fabricated_ingest_detected(self):
        """When _detect_fabricated_ingest fires, _should_skip_reflection must
        return (False, …) so the gate node sees and hard-fails it."""
        state = _make_ingest_state(
            ai_content=(
                '{"paper_ids":["2605.12335v1"]} Tool call made. I ran the import.'
            ),
            tool_executions=[],
        )
        skip, reason = _should_skip_reflection(state)
        assert skip is False
        assert "ingest" in reason.lower()


# ---------------------------------------------------------------------------
# Tests: end-to-end gate node integration for fabricated ingest
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGateNodeFabricatedIngestIntegration:
    @pytest.mark.asyncio
    async def test_gate_hard_fails_on_fabricated_ingest_without_llm(self):
        """End-to-end: gate must short-circuit BEFORE calling the reflection
        LLM when a fabricated ingest is detected."""
        node_fn, _ = make_reflection_gate(intent_filter={"research"})
        state = _make_ingest_state(
            ai_content=(
                '{"paper_ids":["2605.12335v1"]}\n'
                "Tool call made.\n"
                "I ran the import; the paper is being imported now."
            ),
            tool_executions=[],
            intent="research",
            reflection_count=0,
        )

        with patch("src.services.agent.reflection._build_reflection_llm") as mock_build:
            updates = await node_fn(state, {"configurable": {}})
            assert mock_build.called is False

        result = updates["_reflection_result"]
        assert result.passed is False
        assert result.severity == "major"
        assert any("ingest_arxiv_papers" in issue for issue in result.issues)
        assert updates["reflection_count"] == 1

    @pytest.mark.asyncio
    async def test_gate_respects_max_rounds_on_fabricated_ingest(self):
        """At reflection_count == 2, the max-round guard fires BEFORE the
        fabricated ingest detector — the node returns early with no result."""
        node_fn, _ = make_reflection_gate(intent_filter={"research"})
        state = _make_ingest_state(
            ai_content=(
                '{"paper_ids":["2605.12335v1"]} Tool call made. I ran the import.'
            ),
            tool_executions=[],
            intent="research",
            reflection_count=2,
        )

        with patch("src.services.agent.reflection._build_reflection_llm") as mock_build:
            updates = await node_fn(state, {"configurable": {}})
            assert mock_build.called is False

        assert updates["reflection_count"] == 2
        assert "_reflection_result" not in updates


# ---------------------------------------------------------------------------
# Fabricated knowledge-graph search guard
# ---------------------------------------------------------------------------


def _make_kg_state(
    *,
    ai_content: str | list,
    tool_executions: list | None = None,
    intent: str = "knowledge_graph",
    reflection_count: int = 0,
) -> dict:
    """State with a KG-style user question + an assistant answer."""
    return {
        "messages": [
            HumanMessage(content="Explore the knowledge graph for EHR-RAGp"),
            AIMessage(content=ai_content),
        ],
        "tool_executions": tool_executions if tool_executions is not None else [],
        "intent": intent,
        "reflection_count": reflection_count,
    }


def _completed_kg_search_te() -> dict:
    return {
        "tool_name": "search_knowledge_graph",
        "status": "completed",
        "result": {"entities": [{"id": "a588bad6", "name": "RAG"}]},
    }


_FABRICATED_KG_ANSWER = (
    "I searched the knowledge graph for entities and relations related to "
    "EHR-RAGp. Results found several extracted entities. Relevant concept "
    "entities: Retrieval-Augmented Generation (entity_id: a588bad6-9972-4c43)."
)


class TestFabricatedKgSearchGuard:
    def test_fires_when_no_search_tool_ran(self):
        issue = _detect_fabricated_kg_search(
            _make_kg_state(ai_content=_FABRICATED_KG_ANSWER, tool_executions=[])
        )
        assert issue is not None
        assert "search_knowledge_graph" in issue

    def test_silent_when_search_tool_completed(self):
        issue = _detect_fabricated_kg_search(
            _make_kg_state(
                ai_content=_FABRICATED_KG_ANSWER,
                tool_executions=[_completed_kg_search_te()],
            )
        )
        assert issue is None

    def test_silent_on_offer_to_search(self):
        issue = _detect_fabricated_kg_search(
            _make_kg_state(
                ai_content=(
                    "Would you like me to search the knowledge graph for related "
                    "entities? I can explore the graph if you confirm."
                ),
                tool_executions=[],
            )
        )
        assert issue is None

    def test_silent_when_no_kg_claim(self):
        issue = _detect_fabricated_kg_search(
            _make_kg_state(
                ai_content="Here is a concise summary of the paper's methods.",
                tool_executions=[],
            )
        )
        assert issue is None

    def test_silent_when_other_kg_read_tool_ran(self):
        """A legit turn that calls explore_entity_neighborhood / find_entity_paths
        (not search_knowledge_graph) and narrates "I explored the knowledge
        graph" is NOT a fabrication — any completed KG read tool counts."""
        for tool_name, text in (
            (
                "explore_entity_neighborhood",
                "I explored the knowledge graph around RAG and found 5 concepts.",
            ),
            (
                "find_entity_paths",
                "I queried the knowledge graph for paths and found a 2-hop link.",
            ),
        ):
            issue = _detect_fabricated_kg_search(
                _make_kg_state(
                    ai_content=text,
                    tool_executions=[
                        {"tool_name": tool_name, "status": "completed", "result": {}}
                    ],
                )
            )
            assert issue is None, tool_name

    def test_silent_on_paper_description_of_a_graph(self):
        """A summary DESCRIBING a graph/entities (not the assistant claiming it
        searched) must not be flagged."""
        for text in (
            "The authors explored the graph structure of citation networks.",
            "The knowledge graph community found that embeddings help.",
            "Each node has an entity_id field you can reference.",
            "The authors queried the graph database to retrieve neighbors.",
        ):
            assert (
                _detect_fabricated_kg_search(
                    _make_kg_state(ai_content=text, tool_executions=[])
                )
                is None
            ), text

    def test_silent_on_honest_empty_or_failed_search(self):
        """A truthful 'I searched the KG but it returned nothing / failed' is not
        a fabrication even if the tool errored (status != completed)."""
        for text in (
            "I searched the knowledge graph but it returned nothing relevant.",
            "I searched the knowledge graph for EHR-RAGp; it returned no entities.",
            "I searched the knowledge graph but the search failed — service down.",
        ):
            assert (
                _detect_fabricated_kg_search(
                    _make_kg_state(ai_content=text, tool_executions=[])
                )
                is None
            ), text

    @pytest.mark.asyncio
    async def test_gate_fires_even_when_intent_outside_filter(self):
        """The deterministic guard must protect knowledge_graph turns, which the
        data subgraph deliberately routes with intent_filter={"research",
        "writing"} (KG intent NOT in the filter). The guard runs before the
        intent-filter gate, so a fabricated KG search is still caught."""
        node_fn, _ = make_reflection_gate(intent_filter={"research", "writing"})
        state = _make_kg_state(
            ai_content=_FABRICATED_KG_ANSWER,
            tool_executions=[],
            intent="knowledge_graph",
        )
        with patch("src.services.agent.reflection._build_reflection_llm") as mock_build:
            updates = await node_fn(state, {"configurable": {}})
            assert mock_build.called is False  # deterministic, no LLM call

        result = updates["_reflection_result"]
        assert result.passed is False
        assert result.severity == "major"
        assert any("search_knowledge_graph" in issue for issue in result.issues)
        assert updates["reflection_count"] == 1

    @pytest.mark.asyncio
    async def test_gate_silent_when_intent_outside_filter_and_no_fabrication(self):
        """A clean knowledge_graph turn (intent outside filter) still skips the
        LLM critique — no fabrication, no forced revise."""
        node_fn, _ = make_reflection_gate(intent_filter={"research", "writing"})
        state = _make_kg_state(
            ai_content="Here are the entities from the graph.",
            tool_executions=[_completed_kg_search_te()],
            intent="knowledge_graph",
        )
        with patch("src.services.agent.reflection._build_reflection_llm") as mock_build:
            updates = await node_fn(state, {"configurable": {}})
            assert mock_build.called is False  # intent gates the LLM critique

        assert "_reflection_result" not in updates


# ---------------------------------------------------------------------------
# Tests: _detect_fabricated_doc_search  (search_documents / search_arxiv /
#        do_kb_retrieve — the document/retrieval read-tool fabrication guard)
# ---------------------------------------------------------------------------


def _make_doc_state(
    *,
    ai_content: str | list,
    tool_executions: list | None = None,
    intent: str = "research",
    reflection_count: int = 0,
) -> dict:
    """State with a document-search user question + an assistant answer."""
    return {
        "messages": [
            HumanMessage(content="Search my documents for what they say about RAG"),
            AIMessage(content=ai_content),
        ],
        "tool_executions": tool_executions if tool_executions is not None else [],
        "intent": intent,
        "reflection_count": reflection_count,
    }


def _completed_doc_search_te(tool_name: str = "search_documents") -> dict:
    return {
        "tool_name": tool_name,
        "status": "completed",
        "result": {"results": [{"id": "doc-1", "title": "RAG survey"}]},
    }


_FABRICATED_DOC_ANSWER = (
    "I searched your documents for material on retrieval-augmented generation "
    "and found several relevant passages. From your uploaded documents I found "
    "that RAG combines retrieval with generation (source: doc-4271)."
)


class TestFabricatedDocSearchGuard:
    def test_fires_when_no_search_tool_ran(self):
        issue = _detect_fabricated_doc_search(
            _make_doc_state(ai_content=_FABRICATED_DOC_ANSWER, tool_executions=[])
        )
        assert issue is not None
        assert "search_documents" in issue

    def test_silent_when_search_documents_completed(self):
        issue = _detect_fabricated_doc_search(
            _make_doc_state(
                ai_content=_FABRICATED_DOC_ANSWER,
                tool_executions=[_completed_doc_search_te("search_documents")],
            )
        )
        assert issue is None

    def test_silent_when_any_doc_read_tool_completed(self):
        """search_arxiv / do_kb_retrieve and the other document reads all count —
        any completed doc read tool means the retrieval genuinely ran."""
        for tool_name, text in (
            (
                "search_arxiv",
                "I searched arXiv and found 3 papers on retrieval-augmented "
                "generation.",
            ),
            (
                "do_kb_retrieve",
                "I searched the knowledge base and retrieved 2 relevant chunks.",
            ),
            (
                "list_project_documents",
                "I searched your documents and found 4 files in the project.",
            ),
            (
                "search_external_database",
                "I searched the papers and found several relevant studies.",
            ),
            (
                "summarize_document",
                "I looked through the paper and found three key contributions.",
            ),
            (
                "compare_documents",
                "I looked through the documents and found they disagree on scope.",
            ),
        ):
            issue = _detect_fabricated_doc_search(
                _make_doc_state(
                    ai_content=text,
                    tool_executions=[_completed_doc_search_te(tool_name)],
                )
            )
            assert issue is None, tool_name

    def test_silent_when_rag_context_retrieved(self):
        """rag_node is the PRIMARY document-retrieval path and grounds answers
        via state['retrieved_contexts'] WITHOUT emitting a tool_execution. A
        first-person 'I searched your documents and found ...' over real RAG
        context is truthful — the guard must not force-revise it."""
        for text in (
            _FABRICATED_DOC_ANSWER,
            "I looked through the paper and found three key contributions.",
            "From your uploaded documents I found that RAG improves grounding.",
            "I searched your documents and the results show strong recall gains.",
        ):
            state = _make_doc_state(ai_content=text, tool_executions=[])
            state["retrieved_contexts"] = [
                {"id": "ctx-1", "text": "real retrieved passage", "score": 0.8}
            ]
            assert _detect_fabricated_doc_search(state) is None, text

    def test_fires_when_rag_context_empty_and_no_tool(self):
        """Empty retrieved_contexts + no read tool + a first-person search claim
        is the genuine fabrication case the guard exists for."""
        state = _make_doc_state(ai_content=_FABRICATED_DOC_ANSWER, tool_executions=[])
        state["retrieved_contexts"] = []
        assert _detect_fabricated_doc_search(state) is not None

    def test_silent_on_offer_to_search(self):
        for text in (
            "Would you like me to search your documents for that topic?",
            "I can search your library if you upload the papers first.",
            "Should I search arXiv for recent work on this?",
        ):
            assert (
                _detect_fabricated_doc_search(
                    _make_doc_state(ai_content=text, tool_executions=[])
                )
                is None
            ), text

    def test_silent_when_no_doc_claim(self):
        issue = _detect_fabricated_doc_search(
            _make_doc_state(
                ai_content="Retrieval-augmented generation combines retrieval "
                "with a generative model. Here is a two-sentence overview.",
                tool_executions=[],
            )
        )
        assert issue is None

    def test_silent_on_honest_empty_or_failed_search(self):
        for text in (
            "I searched your documents but found nothing relevant to that query.",
            "I searched your library for RAG; it returned no matching results.",
            "I searched arXiv but the search failed — the service was unreachable.",
        ):
            assert (
                _detect_fabricated_doc_search(
                    _make_doc_state(ai_content=text, tool_executions=[])
                )
                is None
            ), text

    def test_silent_on_paper_description_not_a_search_claim(self):
        """A summary DESCRIBING that some authors searched a corpus (not the
        assistant claiming it retrieved) must not be flagged."""
        for text in (
            "The authors searched a large corpus of biomedical abstracts.",
            "You can search your documents using the search bar in the sidebar.",
            "Their method retrieves from a document collection at query time.",
        ):
            assert (
                _detect_fabricated_doc_search(
                    _make_doc_state(ai_content=text, tool_executions=[])
                )
                is None
            ), text

    def test_multimodal_content_list_fires(self):
        issue = _detect_fabricated_doc_search(
            _make_doc_state(
                ai_content=[{"type": "text", "text": _FABRICATED_DOC_ANSWER}],
                tool_executions=[],
            )
        )
        assert issue is not None

    def test_no_ai_message_returns_none(self):
        assert (
            _detect_fabricated_doc_search(
                {"messages": [HumanMessage(content="hi")], "tool_executions": []}
            )
            is None
        )

    def test_skip_reflection_not_triggered_when_doc_fabrication_detected(self):
        skip, reason = _should_skip_reflection(
            _make_doc_state(ai_content=_FABRICATED_DOC_ANSWER, tool_executions=[])
        )
        assert skip is False
        assert "doc search" in reason

    @pytest.mark.asyncio
    async def test_gate_forces_major_revise_on_doc_fabrication(self):
        node_fn, _ = make_reflection_gate(intent_filter={"research", "writing"})
        state = _make_doc_state(
            ai_content=_FABRICATED_DOC_ANSWER,
            tool_executions=[],
            intent="research",
        )
        with patch("src.services.agent.reflection._build_reflection_llm") as mock_build:
            updates = await node_fn(state, {"configurable": {}})
            assert mock_build.called is False  # deterministic, no LLM call

        result = updates["_reflection_result"]
        assert result.passed is False
        assert result.severity == "major"
        assert any("search_documents" in issue for issue in result.issues)
        assert updates["reflection_count"] == 1


# ---------------------------------------------------------------------------
# Tests: multi-turn false positives — tool_executions / retrieved_contexts are
# reset [] each turn (streaming.py / jobs.py initial_state), so the guards only
# see the current turn. A follow-up answer that truthfully REFERENCES a prior
# turn's retrieval ("Earlier I searched your documents ...") must not be
# flagged when a prior AIMessage in the thread carries a read tool_call.
# ---------------------------------------------------------------------------


_PAST_TURN_DOC_CLAIMS = (
    "Earlier I searched your documents and found three passages on RAG "
    "evaluation; the strongest evidence was in the 2024 survey.",
    "As I mentioned, I looked through the documents last turn and found the "
    "ablation results you asked about.",
    "Based on what I retrieved from your library earlier, the answer is that "
    "chunk overlap of 15% performed best.",
)


def _make_multiturn_doc_state(
    *,
    final_ai_content: str,
    prior_tool_name: str | None,
) -> dict:
    """Two-turn thread. Turn 1 optionally ran a doc read tool (visible only as
    an AIMessage.tool_calls + ToolMessage in history); turn 2 is a follow-up
    with per-turn channels reset, mirroring streaming.py/jobs.py initial_state."""
    messages: list = [
        HumanMessage(content="Search my documents for RAG evaluation results"),
    ]
    if prior_tool_name is not None:
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": prior_tool_name, "args": {}, "id": "call_1"}],
                ),
                ToolMessage(
                    content='{"results": [{"id": "doc-1"}]}', tool_call_id="call_1"
                ),
            ]
        )
    messages.extend(
        [
            AIMessage(content="I searched your documents and found three passages."),
            HumanMessage(content="Can you expand on that?"),
            AIMessage(content=final_ai_content),
        ]
    )
    return {
        "messages": messages,
        "tool_executions": [],  # reset each turn
        "retrieved_contexts": [],  # reset each turn
        "intent": "research",
        "reflection_count": 0,
    }


class TestFabricatedDocSearchMultiTurn:
    def test_silent_when_prior_turn_ran_doc_read_tool(self):
        """Past-turn references over a thread where retrieval genuinely
        happened (prior AIMessage carries a _DOC_READ_TOOLS tool_call) must
        pass, even though this turn's tool_executions is empty."""
        for text in _PAST_TURN_DOC_CLAIMS:
            issue = _detect_fabricated_doc_search(
                _make_multiturn_doc_state(
                    final_ai_content=text, prior_tool_name="search_documents"
                )
            )
            assert issue is None, text

    def test_silent_for_any_prior_doc_read_tool(self):
        for tool_name in ("search_arxiv", "do_kb_retrieve", "summarize_document"):
            issue = _detect_fabricated_doc_search(
                _make_multiturn_doc_state(
                    final_ai_content=_PAST_TURN_DOC_CLAIMS[0],
                    prior_tool_name=tool_name,
                )
            )
            assert issue is None, tool_name

    def test_still_fires_when_thread_has_zero_retrieval(self):
        """Same phrasings in a thread where NO retrieval ever happened stay
        flagged — the relaxation is scoped to threads with real prior reads."""
        for text in _PAST_TURN_DOC_CLAIMS:
            issue = _detect_fabricated_doc_search(
                _make_multiturn_doc_state(final_ai_content=text, prior_tool_name=None)
            )
            assert issue is not None, text

    def test_prior_non_doc_tool_call_does_not_suppress(self):
        """A prior tool_call that is NOT a doc read (e.g. create_project) must
        not suppress the guard."""
        issue = _detect_fabricated_doc_search(
            _make_multiturn_doc_state(
                final_ai_content=_PAST_TURN_DOC_CLAIMS[0],
                prior_tool_name="create_project",
            )
        )
        assert issue is not None


class TestFabricatedKgSearchMultiTurn:
    def _state(self, prior_tool_name: str | None) -> dict:
        messages: list = [HumanMessage(content="Search the knowledge graph for RAG")]
        if prior_tool_name is not None:
            messages.extend(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"name": prior_tool_name, "args": {}, "id": "call_1"}
                        ],
                    ),
                    ToolMessage(content='{"entities": []}', tool_call_id="call_1"),
                ]
            )
        messages.extend(
            [
                AIMessage(content="I searched the knowledge graph and found RAG."),
                HumanMessage(content="Tell me more?"),
                AIMessage(
                    content=(
                        "Earlier I searched the knowledge graph and found the "
                        "entity Retrieval-Augmented Generation; it links to "
                        "vector databases."
                    )
                ),
            ]
        )
        return {
            "messages": messages,
            "tool_executions": [],
            "intent": "research",
            "reflection_count": 0,
        }

    def test_silent_when_prior_turn_ran_kg_read_tool(self):
        assert (
            _detect_fabricated_kg_search(self._state("search_knowledge_graph")) is None
        )

    def test_still_fires_when_thread_has_zero_kg_reads(self):
        assert _detect_fabricated_kg_search(self._state(None)) is not None

    def test_prior_non_kg_tool_call_does_not_suppress(self):
        assert _detect_fabricated_kg_search(self._state("create_project")) is not None
