"""Topology snapshot tests for the agent graphs.

Safety net for the subgraph-factory restructure (behavior-preserving by
contract): pins the exact node sets and edge lists — including which edges
are conditional — of the three specialist subgraphs and the main graph.
Any refactor that changes wiring, adds/removes a node, or flips an edge
between direct and conditional fails here before it reaches an LLM.

Snapshots were recorded from the pre-refactor code via
``compiled.get_graph(xray=1)``. ``to_json()`` is deliberately avoided — it
embeds unstable ids. If a topology change is *intentional*, re-record the
literals below and say so in the PR description.
"""

from typing import Any

from src.services.agent._builders import build_agent_graph
from src.services.agent.subgraphs.data_agent import build_data_subgraph
from src.services.agent.subgraphs.research_agent import build_research_subgraph
from src.services.agent.subgraphs.writing_agent import build_writing_subgraph


def _snapshot(compiled: Any) -> tuple[list[str], list[tuple[str, str, bool]]]:
    """Return (sorted node names, sorted (source, target, conditional) edges)."""
    graph = compiled.get_graph(xray=1)
    nodes = sorted(graph.nodes.keys())
    edges = sorted((e.source, e.target, bool(e.conditional)) for e in graph.edges)
    return nodes, edges


RESEARCH_NODES = [
    "__end__",
    "__start__",
    "research_compactor_node",
    "research_force_synthesis_node",
    "research_interrupt_node",
    "research_llm_node",
    "research_planner_node",
    "research_reflection_gate",
    "research_tool_node",
]

RESEARCH_EDGES = [
    ("__start__", "research_planner_node", False),
    ("research_compactor_node", "research_llm_node", False),
    ("research_force_synthesis_node", "research_reflection_gate", False),
    ("research_interrupt_node", "research_reflection_gate", True),
    ("research_interrupt_node", "research_tool_node", True),
    ("research_llm_node", "research_force_synthesis_node", True),
    ("research_llm_node", "research_interrupt_node", True),
    ("research_llm_node", "research_reflection_gate", True),
    ("research_llm_node", "research_tool_node", True),
    ("research_planner_node", "__end__", True),
    ("research_planner_node", "research_llm_node", True),
    ("research_reflection_gate", "__end__", True),
    ("research_reflection_gate", "research_llm_node", True),
    ("research_tool_node", "__end__", True),
    ("research_tool_node", "research_compactor_node", True),
    ("research_tool_node", "research_force_synthesis_node", True),
]

WRITING_NODES = [
    "__end__",
    "__start__",
    "writing_compactor_node",
    "writing_force_synthesis_node",
    "writing_interrupt_node",
    "writing_llm_node",
    "writing_planner_node",
    "writing_reflection_gate",
    "writing_tool_node",
]

WRITING_EDGES = [
    ("__start__", "writing_planner_node", False),
    ("writing_compactor_node", "writing_llm_node", False),
    ("writing_force_synthesis_node", "writing_reflection_gate", False),
    ("writing_interrupt_node", "writing_reflection_gate", True),
    ("writing_interrupt_node", "writing_tool_node", True),
    ("writing_llm_node", "writing_force_synthesis_node", True),
    ("writing_llm_node", "writing_interrupt_node", True),
    ("writing_llm_node", "writing_reflection_gate", True),
    ("writing_llm_node", "writing_tool_node", True),
    ("writing_planner_node", "__end__", True),
    ("writing_planner_node", "writing_llm_node", True),
    ("writing_reflection_gate", "__end__", True),
    ("writing_reflection_gate", "writing_llm_node", True),
    ("writing_tool_node", "__end__", True),
    ("writing_tool_node", "writing_compactor_node", True),
    ("writing_tool_node", "writing_force_synthesis_node", True),
]

# Data has no interrupt node — no destructive tools in that subgraph.
DATA_NODES = [
    "__end__",
    "__start__",
    "data_compactor_node",
    "data_force_synthesis_node",
    "data_llm_node",
    "data_planner_node",
    "data_reflection_gate",
    "data_tool_node",
]

DATA_EDGES = [
    ("__start__", "data_planner_node", False),
    ("data_compactor_node", "data_llm_node", False),
    ("data_force_synthesis_node", "data_reflection_gate", False),
    ("data_llm_node", "data_force_synthesis_node", True),
    ("data_llm_node", "data_reflection_gate", True),
    ("data_llm_node", "data_tool_node", True),
    ("data_planner_node", "__end__", True),
    ("data_planner_node", "data_llm_node", True),
    ("data_reflection_gate", "__end__", True),
    ("data_reflection_gate", "data_llm_node", True),
    ("data_tool_node", "__end__", True),
    ("data_tool_node", "data_compactor_node", True),
    ("data_tool_node", "data_force_synthesis_node", True),
]

# xray=1 expands the three subgraphs in place ("<subgraph>:<node>"), so the
# main snapshot also pins how the subgraphs are embedded, not just the
# parent-level wiring.
MAIN_NODES = [
    "__end__",
    "__start__",
    "compactor_node",
    "data_subgraph:__end__",
    "data_subgraph:data_compactor_node",
    "data_subgraph:data_force_synthesis_node",
    "data_subgraph:data_llm_node",
    "data_subgraph:data_planner_node",
    "data_subgraph:data_reflection_gate",
    "data_subgraph:data_tool_node",
    "force_synthesis_node",
    "interrupt_node",
    "llm_node",
    "memory_save_node",
    "planner_node",
    "preprocessing_node",
    "reflection_gate",
    "research_subgraph:__end__",
    "research_subgraph:research_compactor_node",
    "research_subgraph:research_force_synthesis_node",
    "research_subgraph:research_interrupt_node",
    "research_subgraph:research_llm_node",
    "research_subgraph:research_planner_node",
    "research_subgraph:research_reflection_gate",
    "research_subgraph:research_tool_node",
    "tool_node",
    "writing_subgraph:__end__",
    "writing_subgraph:writing_compactor_node",
    "writing_subgraph:writing_force_synthesis_node",
    "writing_subgraph:writing_interrupt_node",
    "writing_subgraph:writing_llm_node",
    "writing_subgraph:writing_planner_node",
    "writing_subgraph:writing_reflection_gate",
    "writing_subgraph:writing_tool_node",
]

MAIN_EDGES = [
    ("__start__", "preprocessing_node", False),
    ("compactor_node", "llm_node", False),
    ("data_subgraph:__end__", "memory_save_node", False),
    ("data_subgraph:data_compactor_node", "data_subgraph:data_llm_node", False),
    (
        "data_subgraph:data_force_synthesis_node",
        "data_subgraph:data_reflection_gate",
        False,
    ),
    (
        "data_subgraph:data_llm_node",
        "data_subgraph:data_force_synthesis_node",
        True,
    ),
    ("data_subgraph:data_llm_node", "data_subgraph:data_reflection_gate", True),
    ("data_subgraph:data_llm_node", "data_subgraph:data_tool_node", True),
    ("data_subgraph:data_planner_node", "data_subgraph:__end__", True),
    ("data_subgraph:data_planner_node", "data_subgraph:data_llm_node", True),
    ("data_subgraph:data_reflection_gate", "data_subgraph:__end__", True),
    ("data_subgraph:data_reflection_gate", "data_subgraph:data_llm_node", True),
    ("data_subgraph:data_tool_node", "data_subgraph:__end__", True),
    ("data_subgraph:data_tool_node", "data_subgraph:data_compactor_node", True),
    (
        "data_subgraph:data_tool_node",
        "data_subgraph:data_force_synthesis_node",
        True,
    ),
    ("force_synthesis_node", "reflection_gate", False),
    ("interrupt_node", "reflection_gate", True),
    ("interrupt_node", "tool_node", True),
    ("llm_node", "force_synthesis_node", True),
    ("llm_node", "interrupt_node", True),
    ("llm_node", "reflection_gate", True),
    ("llm_node", "tool_node", True),
    ("memory_save_node", "__end__", False),
    ("planner_node", "llm_node", True),
    ("planner_node", "memory_save_node", True),
    ("preprocessing_node", "data_subgraph:data_planner_node", True),
    ("preprocessing_node", "planner_node", True),
    ("preprocessing_node", "research_subgraph:research_planner_node", True),
    ("preprocessing_node", "writing_subgraph:writing_planner_node", True),
    ("reflection_gate", "llm_node", True),
    ("reflection_gate", "memory_save_node", True),
    ("research_subgraph:__end__", "memory_save_node", False),
    (
        "research_subgraph:research_compactor_node",
        "research_subgraph:research_llm_node",
        False,
    ),
    (
        "research_subgraph:research_force_synthesis_node",
        "research_subgraph:research_reflection_gate",
        False,
    ),
    (
        "research_subgraph:research_interrupt_node",
        "research_subgraph:research_reflection_gate",
        True,
    ),
    (
        "research_subgraph:research_interrupt_node",
        "research_subgraph:research_tool_node",
        True,
    ),
    (
        "research_subgraph:research_llm_node",
        "research_subgraph:research_force_synthesis_node",
        True,
    ),
    (
        "research_subgraph:research_llm_node",
        "research_subgraph:research_interrupt_node",
        True,
    ),
    (
        "research_subgraph:research_llm_node",
        "research_subgraph:research_reflection_gate",
        True,
    ),
    (
        "research_subgraph:research_llm_node",
        "research_subgraph:research_tool_node",
        True,
    ),
    (
        "research_subgraph:research_planner_node",
        "research_subgraph:__end__",
        True,
    ),
    (
        "research_subgraph:research_planner_node",
        "research_subgraph:research_llm_node",
        True,
    ),
    (
        "research_subgraph:research_reflection_gate",
        "research_subgraph:__end__",
        True,
    ),
    (
        "research_subgraph:research_reflection_gate",
        "research_subgraph:research_llm_node",
        True,
    ),
    (
        "research_subgraph:research_tool_node",
        "research_subgraph:__end__",
        True,
    ),
    (
        "research_subgraph:research_tool_node",
        "research_subgraph:research_compactor_node",
        True,
    ),
    (
        "research_subgraph:research_tool_node",
        "research_subgraph:research_force_synthesis_node",
        True,
    ),
    ("tool_node", "compactor_node", True),
    ("tool_node", "force_synthesis_node", True),
    ("tool_node", "memory_save_node", True),
    ("writing_subgraph:__end__", "memory_save_node", False),
    (
        "writing_subgraph:writing_compactor_node",
        "writing_subgraph:writing_llm_node",
        False,
    ),
    (
        "writing_subgraph:writing_force_synthesis_node",
        "writing_subgraph:writing_reflection_gate",
        False,
    ),
    (
        "writing_subgraph:writing_interrupt_node",
        "writing_subgraph:writing_reflection_gate",
        True,
    ),
    (
        "writing_subgraph:writing_interrupt_node",
        "writing_subgraph:writing_tool_node",
        True,
    ),
    (
        "writing_subgraph:writing_llm_node",
        "writing_subgraph:writing_force_synthesis_node",
        True,
    ),
    (
        "writing_subgraph:writing_llm_node",
        "writing_subgraph:writing_interrupt_node",
        True,
    ),
    (
        "writing_subgraph:writing_llm_node",
        "writing_subgraph:writing_reflection_gate",
        True,
    ),
    (
        "writing_subgraph:writing_llm_node",
        "writing_subgraph:writing_tool_node",
        True,
    ),
    (
        "writing_subgraph:writing_planner_node",
        "writing_subgraph:__end__",
        True,
    ),
    (
        "writing_subgraph:writing_planner_node",
        "writing_subgraph:writing_llm_node",
        True,
    ),
    ("writing_subgraph:writing_reflection_gate", "writing_subgraph:__end__", True),
    (
        "writing_subgraph:writing_reflection_gate",
        "writing_subgraph:writing_llm_node",
        True,
    ),
    (
        "writing_subgraph:writing_tool_node",
        "writing_subgraph:__end__",
        True,
    ),
    (
        "writing_subgraph:writing_tool_node",
        "writing_subgraph:writing_compactor_node",
        True,
    ),
    (
        "writing_subgraph:writing_tool_node",
        "writing_subgraph:writing_force_synthesis_node",
        True,
    ),
]


def test_research_subgraph_topology() -> None:
    nodes, edges = _snapshot(build_research_subgraph().compile())
    assert nodes == RESEARCH_NODES
    assert edges == RESEARCH_EDGES


def test_writing_subgraph_topology() -> None:
    nodes, edges = _snapshot(build_writing_subgraph().compile())
    assert nodes == WRITING_NODES
    assert edges == WRITING_EDGES


def test_data_subgraph_topology() -> None:
    nodes, edges = _snapshot(build_data_subgraph().compile())
    assert nodes == DATA_NODES
    assert edges == DATA_EDGES


def test_main_graph_topology() -> None:
    nodes, edges = _snapshot(build_agent_graph().compile())
    assert nodes == MAIN_NODES
    assert edges == MAIN_EDGES
