"""Contract tests for the code-owned agent tool registry."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import patch

import pytest
from langchain_core.tools import BaseTool, tool

from src.services.agent.tool_registry import (
    AgentIntent,
    AgentSubgraph,
    ToolDescriptor,
    ToolPolicyTag,
    ToolRegistry,
)


@tool
def alpha_tool(value: str) -> str:
    """Return the input unchanged."""
    return value


@tool
def beta_tool(value: str) -> str:
    """Return the input unchanged."""
    return value


def _descriptor(
    *,
    name: str = "alpha_tool",
    tool_object: BaseTool = alpha_tool,
    intents: frozenset[AgentIntent] = frozenset({AgentIntent.GENERAL}),
    subgraphs: frozenset[AgentSubgraph] = frozenset(),
    policy_tags: frozenset[ToolPolicyTag] = frozenset(),
    enabled: bool = True,
    exposed_in_all_tools: bool = True,
    availability_condition: str | None = None,
    subgraph_positions: tuple[tuple[AgentSubgraph, int], ...] | None = None,
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        tool=tool_object,
        intents=intents,
        subgraphs=subgraphs,
        policy_tags=policy_tags,
        enabled=enabled,
        exposed_in_all_tools=exposed_in_all_tools,
        availability_condition=availability_condition,
        subgraph_positions=(
            tuple((subgraph, 0) for subgraph in subgraphs)
            if subgraph_positions is None
            else subgraph_positions
        ),
    )


@pytest.mark.unit
class TestToolRegistry:
    def test_returns_one_tool_object_per_descriptor_in_declaration_order(self) -> None:
        registry = ToolRegistry(
            [_descriptor(name="beta_tool", tool_object=beta_tool), _descriptor()]
        )

        assert registry.all_tools() == (beta_tool, alpha_tool)

    def test_indexes_known_intents_and_subgraphs(self) -> None:
        registry = ToolRegistry(
            [
                _descriptor(
                    intents=frozenset({AgentIntent.RESEARCH}),
                    subgraphs=frozenset({AgentSubgraph.RESEARCH}),
                ),
                _descriptor(
                    name="beta_tool",
                    tool_object=beta_tool,
                    intents=frozenset({AgentIntent.WRITING}),
                    subgraphs=frozenset({AgentSubgraph.WRITING}),
                ),
            ]
        )

        assert [item.name for item in registry.descriptors_for_intent("research")] == [
            "alpha_tool"
        ]
        assert [item.name for item in registry.descriptors_for_subgraph("writing")] == [
            "beta_tool"
        ]
        assert registry.descriptors_for_intent("unknown") == ()
        assert registry.descriptors_for_subgraph("unknown") == ()

    def test_policy_tags_are_queryable(self) -> None:
        registry = ToolRegistry(
            [
                _descriptor(
                    policy_tags=frozenset(
                        {
                            ToolPolicyTag.DESTRUCTIVE,
                            ToolPolicyTag.SLOW,
                            ToolPolicyTag.NO_OUTER_RETRY,
                        }
                    )
                )
            ]
        )

        assert registry.has_policy("alpha_tool", ToolPolicyTag.DESTRUCTIVE)
        assert registry.has_policy("alpha_tool", ToolPolicyTag.SLOW)
        assert registry.has_policy("alpha_tool", ToolPolicyTag.NO_OUTER_RETRY)
        assert not registry.has_policy("missing", ToolPolicyTag.DESTRUCTIVE)

    def test_descriptor_metadata_is_immutable_and_snapshot_is_stable(self) -> None:
        descriptor = _descriptor(policy_tags=frozenset({ToolPolicyTag.SLOW}))
        registry = ToolRegistry([descriptor])

        with pytest.raises(FrozenInstanceError):
            descriptor.name = "renamed"  # type: ignore[misc]

        assert (
            registry.metadata_snapshot()
            == ToolRegistry([descriptor]).metadata_snapshot()
        )
        assert set(registry.metadata_snapshot()) == {"version", "hash"}

    def test_metadata_hash_includes_all_tools_compatibility_visibility(self) -> None:
        exposed = ToolRegistry([_descriptor()])
        hidden = ToolRegistry([_descriptor(exposed_in_all_tools=False)])

        assert exposed.metadata_snapshot()["hash"] != hidden.metadata_snapshot()["hash"]

    def test_conditionally_available_descriptors_are_absent_without_a_condition(
        self,
    ) -> None:
        registry = ToolRegistry(
            [_descriptor(availability_condition="project_skill_catalog")]
        )

        assert registry.available_descriptor_names() == ()
        assert registry.frozen_descriptor_metadata() == []
        assert registry.available_descriptor_names(
            conditions={"project_skill_catalog"}
        ) == ("alpha_tool",)

    def test_parity_shadow_records_only_match_or_mismatch(self) -> None:
        registry = ToolRegistry([_descriptor()])

        with patch(
            "src.services.agent.tool_registry.record_project_skill_event"
        ) as record_event:
            assert registry.record_parity_shadow(("different",)) is False

        record_event.assert_called_once_with("registry_parity", "mismatch")

    def test_rejects_duplicate_names_and_invalid_descriptor_contracts(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            ToolRegistry([_descriptor(), _descriptor(tool_object=beta_tool)])

        with pytest.raises(ValueError, match="must match"):
            ToolRegistry([_descriptor(name="not_the_tool_name")])

        with pytest.raises(TypeError, match="AgentIntent"):
            ToolRegistry([_descriptor(intents=frozenset({"general"}))])  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="no_outer_retry"):
            ToolRegistry(
                [_descriptor(policy_tags=frozenset({ToolPolicyTag.NO_OUTER_RETRY}))]
            )

    def test_rejects_incomplete_or_ambiguous_subgraph_positions(self) -> None:
        research = frozenset({AgentSubgraph.RESEARCH})

        with pytest.raises(ValueError, match="every descriptor subgraph"):
            ToolRegistry([_descriptor(subgraphs=research, subgraph_positions=())])

        with pytest.raises(ValueError, match="non-negative"):
            ToolRegistry(
                [
                    _descriptor(
                        subgraphs=research,
                        subgraph_positions=((AgentSubgraph.RESEARCH, -1),),
                    )
                ]
            )

        with pytest.raises(ValueError, match="duplicate subgraph position"):
            ToolRegistry(
                [
                    _descriptor(subgraphs=research),
                    _descriptor(
                        name="beta_tool",
                        tool_object=beta_tool,
                        subgraphs=research,
                    ),
                ]
            )


@pytest.mark.unit
class TestProductionToolRegistryParity:
    LEGACY_ALL_TOOL_NAMES = [
        "search_arxiv",
        "ingest_arxiv_papers",
        "search_documents",
        "create_project",
        "list_projects",
        "add_document_to_project",
        "create_project_note",
        "list_project_documents",
        "summarize_document",
        "compare_documents",
        "extract_entities",
        "search_knowledge_graph",
        "explore_entity_neighborhood",
        "find_entity_paths",
        "get_graph_stats",
        "create_draft",
        "revise_draft",
        "export_bibliography",
        "execute_code",
        "search_external_database",
        "list_external_databases",
        "forget_memory",
    ]

    def test_all_tools_keeps_legacy_order_and_wrappers(self) -> None:
        from src.services.agent.tools import ALL_TOOLS, TOOL_REGISTRY

        assert [tool.name for tool in ALL_TOOLS] == self.LEGACY_ALL_TOOL_NAMES
        assert tuple(ALL_TOOLS) == TOOL_REGISTRY.all_tools()
        assert [
            descriptor.tool
            for descriptor in TOOL_REGISTRY.descriptors
            if descriptor.exposed_in_all_tools
        ] == ALL_TOOLS

    def test_intent_and_subgraph_bindings_keep_legacy_names(self) -> None:
        from src.services.agent.tools import TOOL_REGISTRY

        expected_intents = {
            "research": {
                "search_arxiv",
                "ingest_arxiv_papers",
                "search_documents",
                "create_project",
                "list_projects",
                "add_document_to_project",
                "list_project_documents",
                "execute_code",
            },
            "writing": {
                "create_draft",
                "revise_draft",
                "get_current_draft",
                "create_project_note",
                "export_bibliography",
                "summarize_document",
                "compare_documents",
            },
            "knowledge_graph": {
                "extract_entities",
                "search_knowledge_graph",
                "explore_entity_neighborhood",
                "find_entity_paths",
                "get_graph_stats",
                "search_documents",
            },
            "general": {
                "search_arxiv",
                "ingest_arxiv_papers",
                "search_documents",
                # content-level retrieval: general is the classifier's
                # weak-evidence fallback and must be a superset of the
                # specialist lanes (live miss 2026-08-12).
                "do_kb_retrieve",
                "compare_documents",
                "create_project",
                "list_projects",
                "add_document_to_project",
                "list_project_documents",
                "create_project_note",
                "summarize_document",
                "search_knowledge_graph",
                "forget_memory",
                "search_external_database",
                "list_external_databases",
            },
        }
        expected_subgraphs = {
            "research": {
                "search_arxiv",
                "ingest_arxiv_papers",
                "search_documents",
                "do_kb_retrieve",
                "create_project",
                "list_projects",
                "add_document_to_project",
                "list_project_documents",
                "create_project_note",
                "execute_code",
            },
            "writing": {
                "create_draft",
                "revise_draft",
                "get_current_draft",
                "create_project_note",
                "export_bibliography",
                "summarize_document",
                "compare_documents",
                "search_arxiv",
                "ingest_arxiv_papers",
                # create_project_note/create_draft need a project_id, and
                # without this the writing executor could only ask the user
                # for one — measured on dev at 5 runs out of 5.
                "list_projects",
                # summarize_document is writing-only and its recoverable hint
                # names this tool; without the binding the advice is dead.
                "list_project_documents",
                "create_project",
                "add_document_to_project",
                # the literature-review project skill instructs multi-database
                # search, and lit-review requests classify as writing.
                "search_external_database",
                "list_external_databases",
            },
            "data": {
                # list_project_documents' _missing_project_error names this.
                "list_projects",
                "extract_entities",
                "search_knowledge_graph",
                "explore_entity_neighborhood",
                "find_entity_paths",
                "get_graph_stats",
                "search_documents",
                # search_documents' zero-hit suggestion names this tool; bound
                # to research only, that advice was dead in the data lane.
                "do_kb_retrieve",
                "list_project_documents",
            },
        }

        for intent, names in expected_intents.items():
            assert {
                item.name for item in TOOL_REGISTRY.descriptors_for_intent(intent)
            } == names
        for subgraph, names in expected_subgraphs.items():
            assert {
                item.name for item in TOOL_REGISTRY.descriptors_for_subgraph(subgraph)
            } == names

        assert [
            item.name for item in TOOL_REGISTRY.descriptors_for_subgraph("research")
        ] == [
            "search_arxiv",
            "ingest_arxiv_papers",
            "search_documents",
            "do_kb_retrieve",
            "create_project",
            "list_projects",
            "add_document_to_project",
            "list_project_documents",
            "create_project_note",
            # Appended at position 9 so the existing order is untouched.
            "execute_code",
        ]
        assert [
            item.name for item in TOOL_REGISTRY.descriptors_for_subgraph("writing")
        ] == [
            "create_draft",
            "create_project_note",
            "export_bibliography",
            "summarize_document",
            "compare_documents",
            "search_arxiv",
            "ingest_arxiv_papers",
            # Appended at positions 7 and 8 so the existing order is untouched.
            "list_projects",
            "list_project_documents",
            "create_project",
            "add_document_to_project",
            # Appended at positions 11 and 12 so the existing order is untouched.
            "search_external_database",
            "list_external_databases",
            "revise_draft",
            "get_current_draft",
        ]
        assert [
            item.name for item in TOOL_REGISTRY.descriptors_for_subgraph("data")
        ] == [
            "extract_entities",
            "search_knowledge_graph",
            "explore_entity_neighborhood",
            "find_entity_paths",
            "get_graph_stats",
            "search_documents",
            "list_project_documents",
            "list_projects",
            # Appended at position 8 so the existing order is untouched.
            "do_kb_retrieve",
        ]

    def test_research_only_tool_is_registered_but_hidden_from_legacy_all_tools(
        self,
    ) -> None:
        from src.services.agent.tools import ALL_TOOLS, TOOL_REGISTRY

        descriptor = TOOL_REGISTRY.descriptor("do_kb_retrieve")

        assert descriptor is not None
        assert not descriptor.exposed_in_all_tools
        assert "do_kb_retrieve" not in {tool.name for tool in ALL_TOOLS}
        assert "do_kb_retrieve" in {
            item.name for item in TOOL_REGISTRY.descriptors_for_subgraph("research")
        }

    def test_forget_memory_is_reachable_via_general_route(self) -> None:
        """forget_memory must be bound to a real intent, not orphaned.

        With intents=∅ + subgraphs=∅ it was reachable only via the
        unknown-intent ALL_TOOLS path, which the live graph never takes
        (classify_intent_with_fallback is Literal-typed to the four real
        intents; _get_tools_for_intent returns ALL_TOOLS only for an intent
        OUTSIDE AgentIntent). That made it uncallable in production. GENERAL is
        the fix; DESTRUCTIVE keeps it HITL-gated.
        """
        from src.services.agent._nodes_llm import _get_tools_for_intent
        from src.services.agent.tool_registry import ToolPolicyTag
        from src.services.agent.tools import TOOL_REGISTRY

        assert "forget_memory" in {
            item.name for item in TOOL_REGISTRY.descriptors_for_intent("general")
        }
        # the actual runtime binding the general route uses
        assert "forget_memory" in {
            tool.name for tool in _get_tools_for_intent("general")
        }
        # still destructive -> still behind the HITL interrupt
        assert (
            ToolPolicyTag.DESTRUCTIVE
            in TOOL_REGISTRY.descriptor("forget_memory").policy_tags
        )

    def test_execute_code_is_reachable_via_research_subgraph(self) -> None:
        """execute_code must be bound to a subgraph, not just an intent.

        With subgraphs=∅ it was in no subgraph's tool set, so the research/data
        specialist LLM nodes (which bind ``descriptors_for_subgraph``, not
        intents) could never see it, and the general path only handles GENERAL
        intent — making it uncallable in production despite carrying
        intents={RESEARCH, KNOWLEDGE_GRAPH}. RESEARCH subgraph membership is the
        fix, not DATA: data_agent has ``has_interrupt=False`` (no destructive
        tools by design), so a DESTRUCTIVE tool bound there would run with no
        HITL confirmation. RESEARCH has ``has_interrupt=True``, and its
        ``should_continue`` interrupt check keys off subgraph-scoped policy tags
        (``has_policy_in_subgraph``), so execute_code stays gated there.
        """
        from src.services.agent.tool_registry import ToolPolicyTag
        from src.services.agent.tools import TOOL_REGISTRY

        assert "execute_code" in {
            item.name for item in TOOL_REGISTRY.descriptors_for_subgraph("research")
        }
        # not reachable via data — that subgraph has no HITL interrupt node
        assert "execute_code" not in {
            item.name for item in TOOL_REGISTRY.descriptors_for_subgraph("data")
        }
        # still destructive -> still behind research's HITL interrupt
        assert (
            ToolPolicyTag.DESTRUCTIVE
            in TOOL_REGISTRY.descriptor("execute_code").policy_tags
        )
        assert TOOL_REGISTRY.has_policy_in_subgraph(
            "execute_code", ToolPolicyTag.DESTRUCTIVE, "research"
        )

    def test_external_database_connectors_are_reachable_via_general_route(
        self,
    ) -> None:
        """search_external_database/list_external_databases must reach a route.

        Same dead-metadata pattern as forget_memory (51fd5adc): intents=∅ +
        subgraphs=∅ meant both were reachable only via the unknown-intent
        ALL_TOOLS path the live graph never takes. GENERAL is the fix.
        """
        from src.services.agent._nodes_llm import _get_tools_for_intent
        from src.services.agent.tools import TOOL_REGISTRY

        for name in ("search_external_database", "list_external_databases"):
            assert name in {
                item.name for item in TOOL_REGISTRY.descriptors_for_intent("general")
            }
            assert name in {tool.name for tool in _get_tools_for_intent("general")}
            # …and via writing, where lit-review requests are classified.
            assert name in {
                item.name for item in TOOL_REGISTRY.descriptors_for_subgraph("writing")
            }

    def test_policy_tags_keep_legacy_execution_policy(self) -> None:
        from src.services.agent.tool_registry import ToolPolicyTag
        from src.services.agent.tools import TOOL_REGISTRY

        assert {
            descriptor.name
            for descriptor in TOOL_REGISTRY.descriptors
            if ToolPolicyTag.DESTRUCTIVE in descriptor.policy_tags
        } == {
            "ingest_arxiv_papers",
            "add_document_to_project",
            "create_project",
            "create_project_note",
            "create_draft",
            "revise_draft",
            "execute_code",
            "forget_memory",
        }
        assert {
            descriptor.name
            for descriptor in TOOL_REGISTRY.descriptors
            if ToolPolicyTag.SLOW in descriptor.policy_tags
        } == {
            "ingest_arxiv_papers",
            "create_draft",
            "revise_draft",
            "compare_documents",
            "search_arxiv",
        }
        assert {
            descriptor.name
            for descriptor in TOOL_REGISTRY.descriptors
            if ToolPolicyTag.NO_OUTER_RETRY in descriptor.policy_tags
        } == {
            "search_arxiv",
            "ingest_arxiv_papers",
            "create_project",
            "add_document_to_project",
            "create_project_note",
            "create_draft",
            "revise_draft",
            "execute_code",
            "forget_memory",
        }
