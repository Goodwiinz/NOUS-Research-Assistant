"""Agent KG tools return an error payload, not "no entities", during a Neo4j outage.

Audit #2123 item 4: the service used to swallow driver errors, so the tools
reported an empty graph. Now that the service raises, each tool must still
return ``tool_error_payload`` (never raise into the graph) and must not claim
success.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from neo4j.exceptions import ServiceUnavailable

from src.models.user import User
from src.services.agent import tools_impl

pytestmark = pytest.mark.unit

# The dispatcher only reads ``id`` / ``organization_id``; a namespace stands in.
_USER = cast(User, SimpleNamespace(id=uuid4(), organization_id=uuid4()))


def _raiser(*_a: Any, **_k: Any) -> Any:
    raise ServiceUnavailable("bolt://neo4j:hunter2@10.0.0.7 unreachable")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name, args, method",
    [
        ("search_knowledge_graph", {"query": "neurons"}, "search_entities"),
        ("explore_entity_neighborhood", {"entity_id": "e1"}, "get_neighborhood"),
        ("get_graph_stats", {}, "get_graph_analytics"),
    ],
)
async def test_kg_tool_returns_error_payload_when_neo4j_down(
    tool_name: str, args: dict, method: str
) -> None:
    fake_service = SimpleNamespace(**{method: _raiser})
    with patch(
        "src.services.knowledge_graph.knowledge_graph_service.knowledge_graph_service",
        fake_service,
    ):
        result = await tools_impl._dispatch_tool(tool_name, args, current_user=_USER)

    assert isinstance(result, dict)
    assert result.get("error"), f"{tool_name} did not report an error: {result}"
    assert "hunter2" not in str(result)
    # Must not look like a successful empty graph.
    assert result.get("entities") is None
    assert result.get("total") is None
    assert result.get("total_entities") is None
