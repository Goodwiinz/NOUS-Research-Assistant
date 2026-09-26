"""Code-owned descriptors for the agent's LangChain tools and policies.

The registry deliberately stores only code-defined tool wrappers.  It is the
single source of truth for model binding, graph routing, and execution policy;
database records may reference descriptor metadata but never executable code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Iterable

from langchain_core.tools import BaseTool

from src.services.agent.observability import record_project_skill_event


class AgentIntent(StrEnum):
    """Top-level intents with curated tool bindings."""

    RESEARCH = "research"
    WRITING = "writing"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    GENERAL = "general"


class AgentSubgraph(StrEnum):
    """Specialized graph branches that bind their own tool subsets."""

    RESEARCH = "research"
    WRITING = "writing"
    DATA = "data"


class ToolPolicyTag(StrEnum):
    """Server-owned execution policy flags."""

    DESTRUCTIVE = "destructive"
    SLOW = "slow"
    NO_OUTER_RETRY = "no_outer_retry"
    CONTEXT_FREE = "context_free"
    CONTEXT_REQUIRED = "context_required"


class ToolEffectMode(StrEnum):
    """Business-effect boundary used by durable operation orchestration."""

    READ_ONLY = "read_only"
    LOCAL_TRANSACTION = "local_transaction"
    EXTERNAL = "external"


@dataclass(frozen=True)
class ToolDescriptor:
    """Immutable metadata for one decorated LangChain tool wrapper."""

    name: str
    tool: BaseTool
    intents: frozenset[AgentIntent]
    subgraphs: frozenset[AgentSubgraph]
    policy_tags: frozenset[ToolPolicyTag]
    effect_mode: ToolEffectMode = ToolEffectMode.READ_ONLY
    enabled: bool = True
    exposed_in_all_tools: bool = True
    availability_condition: str | None = None
    subgraph_positions: tuple[tuple[AgentSubgraph, int], ...] = ()


class ToolRegistry:
    """Validated, deterministic view over code-defined tool descriptors."""

    METADATA_VERSION = "2"

    def __init__(self, descriptors: Iterable[ToolDescriptor]) -> None:
        self._descriptors = tuple(descriptors)
        self._validate()
        self._by_name = {
            descriptor.name: descriptor for descriptor in self._descriptors
        }
        self._by_intent = {
            intent: tuple(
                descriptor
                for descriptor in self._descriptors
                if descriptor.enabled and intent in descriptor.intents
            )
            for intent in AgentIntent
        }
        self._by_subgraph = {
            subgraph: tuple(
                descriptor
                for descriptor in sorted(
                    self._descriptors,
                    key=lambda item: dict(item.subgraph_positions).get(subgraph, 0),
                )
                if descriptor.enabled and subgraph in descriptor.subgraphs
            )
            for subgraph in AgentSubgraph
        }
        self._metadata_hash = self._build_metadata_hash()

    def _validate(self) -> None:
        names: set[str] = set()
        positions_by_subgraph: dict[AgentSubgraph, set[int]] = {
            subgraph: set() for subgraph in AgentSubgraph
        }
        for descriptor in self._descriptors:
            if not isinstance(descriptor, ToolDescriptor):
                raise TypeError("registry entries must be ToolDescriptor instances")
            if not descriptor.name:
                raise ValueError("tool descriptor names must not be empty")
            if descriptor.name in names:
                raise ValueError(f"duplicate tool descriptor name: {descriptor.name}")
            names.add(descriptor.name)
            if not isinstance(descriptor.tool, BaseTool):
                raise TypeError("tool descriptors require a LangChain BaseTool")
            if descriptor.tool.name != descriptor.name:
                raise ValueError(
                    "tool descriptor name must match the LangChain tool name"
                )
            if not isinstance(descriptor.enabled, bool):
                raise TypeError("tool descriptor enabled must be a bool")
            if not isinstance(descriptor.exposed_in_all_tools, bool):
                raise TypeError("tool descriptor exposed_in_all_tools must be a bool")
            if descriptor.availability_condition is not None and (
                not isinstance(descriptor.availability_condition, str)
                or not descriptor.availability_condition
            ):
                raise TypeError(
                    "tool descriptor availability_condition must be a non-empty string"
                )
            positions = dict(descriptor.subgraph_positions)
            if len(positions) != len(descriptor.subgraph_positions) or not all(
                isinstance(subgraph, AgentSubgraph) and isinstance(position, int)
                for subgraph, position in descriptor.subgraph_positions
            ):
                raise TypeError("subgraph positions must be unique enum/int pairs")
            if set(positions) != descriptor.subgraphs:
                raise ValueError(
                    "every descriptor subgraph must have exactly one position"
                )
            for subgraph, position in positions.items():
                if position < 0:
                    raise ValueError("subgraph positions must be non-negative")
                if position in positions_by_subgraph[subgraph]:
                    raise ValueError(
                        f"duplicate subgraph position for {subgraph.value}: {position}"
                    )
                positions_by_subgraph[subgraph].add(position)
            if not all(
                isinstance(intent, AgentIntent) for intent in descriptor.intents
            ):
                raise TypeError(
                    "tool descriptor intents must contain AgentIntent values"
                )
            if not all(
                isinstance(subgraph, AgentSubgraph) for subgraph in descriptor.subgraphs
            ):
                raise TypeError(
                    "tool descriptor subgraphs must contain AgentSubgraph values"
                )
            if not all(
                isinstance(tag, ToolPolicyTag) for tag in descriptor.policy_tags
            ):
                raise TypeError(
                    "tool descriptor policy_tags must contain ToolPolicyTag values"
                )
            if not isinstance(descriptor.effect_mode, ToolEffectMode):
                raise TypeError("tool descriptor effect_mode must be a ToolEffectMode")
            if descriptor.effect_mode != ToolEffectMode.READ_ONLY:
                required_tags = {
                    ToolPolicyTag.DESTRUCTIVE,
                    ToolPolicyTag.NO_OUTER_RETRY,
                }
                if not required_tags.issubset(descriptor.policy_tags):
                    raise ValueError(
                        "mutating tool effects require destructive and no_outer_retry policies"
                    )
                if ToolPolicyTag.CONTEXT_FREE in descriptor.policy_tags:
                    raise ValueError("mutating tool effects cannot be context-free")
            if (
                ToolPolicyTag.NO_OUTER_RETRY in descriptor.policy_tags
                and ToolPolicyTag.SLOW not in descriptor.policy_tags
                and ToolPolicyTag.DESTRUCTIVE not in descriptor.policy_tags
            ):
                raise ValueError(
                    "no_outer_retry policy requires the slow or destructive policy"
                )

    def _build_metadata_hash(self) -> str:
        metadata = [
            {
                "name": descriptor.name,
                "intents": sorted(intent.value for intent in descriptor.intents),
                "subgraphs": sorted(
                    subgraph.value for subgraph in descriptor.subgraphs
                ),
                "policy_tags": sorted(tag.value for tag in descriptor.policy_tags),
                "effect_mode": descriptor.effect_mode.value,
                "enabled": descriptor.enabled,
                "exposed_in_all_tools": descriptor.exposed_in_all_tools,
                "availability_condition": descriptor.availability_condition,
                "subgraph_positions": [
                    (subgraph.value, position)
                    for subgraph, position in descriptor.subgraph_positions
                ],
            }
            for descriptor in self._descriptors
        ]
        canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()

    def all_tools(self) -> tuple[BaseTool, ...]:
        """Return enabled LangChain wrappers in declaration order."""
        return tuple(
            descriptor.tool
            for descriptor in self._descriptors
            if descriptor.enabled and descriptor.exposed_in_all_tools
        )

    @property
    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        """All registered descriptors in declaration order, including hidden tools."""
        return self._descriptors

    def descriptor(self, name: str) -> ToolDescriptor | None:
        """Return a descriptor by its server-owned tool name, if registered."""
        return self._by_name.get(name)

    def descriptors_for_intent(self, intent: str) -> tuple[ToolDescriptor, ...]:
        """Return enabled descriptors for a known top-level intent."""
        try:
            return self._by_intent[AgentIntent(intent)]
        except ValueError:
            return ()

    def descriptors_for_subgraph(self, subgraph: str) -> tuple[ToolDescriptor, ...]:
        """Return enabled descriptors for a known specialized subgraph."""
        try:
            return self._by_subgraph[AgentSubgraph(subgraph)]
        except ValueError:
            return ()

    def has_policy(self, name: str, tag: ToolPolicyTag) -> bool:
        """Whether a registered descriptor carries a server-owned policy tag."""
        descriptor = self._by_name.get(name)
        return bool(descriptor and tag in descriptor.policy_tags)

    def has_policy_in_subgraph(
        self, name: str, tag: ToolPolicyTag, subgraph: str
    ) -> bool:
        """Whether an enabled subgraph-visible descriptor carries a policy tag."""
        descriptor = self._by_name.get(name)
        try:
            selected_subgraph = AgentSubgraph(subgraph)
        except ValueError:
            return False
        return bool(
            descriptor
            and descriptor.enabled
            and selected_subgraph in descriptor.subgraphs
            and tag in descriptor.policy_tags
        )

    def metadata_snapshot(self) -> dict[str, str]:
        """Return stable registry metadata suitable for durable run snapshots."""
        return {"version": self.METADATA_VERSION, "hash": self._metadata_hash}

    def available_descriptor_names(
        self, *, conditions: set[str] | frozenset[str] = frozenset()
    ) -> tuple[str, ...]:
        """Return descriptors available for a specific frozen runtime context."""
        return tuple(
            descriptor.name
            for descriptor in self._descriptors
            if descriptor.enabled
            and (
                descriptor.availability_condition is None
                or descriptor.availability_condition in conditions
            )
        )

    def record_parity_shadow(self, expected_tool_names: Iterable[str]) -> bool:
        """Record a safe registry parity result without logging tool arguments."""
        matches = tuple(expected_tool_names) == self.available_descriptor_names()
        record_project_skill_event(
            "registry_parity", "match" if matches else "mismatch"
        )
        return matches

    def frozen_descriptor_metadata(
        self, *, conditions: set[str] | frozenset[str] = frozenset()
    ) -> list[dict[str, object]]:
        """JSON-safe projection of descriptors available in a durable snapshot."""
        return [
            {
                "name": item.name,
                "intents": sorted(value.value for value in item.intents),
                "subgraphs": sorted(value.value for value in item.subgraphs),
                "policy_tags": sorted(value.value for value in item.policy_tags),
                "effect_mode": item.effect_mode.value,
                "enabled": item.enabled,
                "exposed_in_all_tools": item.exposed_in_all_tools,
                "availability_condition": item.availability_condition,
                "subgraph_positions": [
                    [subgraph.value, position]
                    for subgraph, position in item.subgraph_positions
                ],
            }
            for item in self._descriptors
            if item.name in self.available_descriptor_names(conditions=conditions)
        ]
