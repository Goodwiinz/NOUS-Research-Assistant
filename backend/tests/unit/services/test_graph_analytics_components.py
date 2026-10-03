"""GOO-332: connected_components / largest_component_size must be exact."""

from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService

pytestmark = pytest.mark.unit


class _Result(list[Any]):
    def single(self) -> Any:
        return self[0] if self else None


class _FakeSession:
    """Answers the analytics queries from an in-memory graph."""

    def __init__(self, nodes: list[str], edges: list[tuple[str, str]]) -> None:
        self.nodes = nodes
        self.edges = edges

    def run(self, query: str, params: Any = None) -> _Result:
        if "RETURN e.type as type" in query:
            return _Result([{"type": "concept", "count": len(self.nodes)}])
        if "RETURN r.type as type" in query:
            return _Result([{"type": "related", "count": len(self.edges)}])
        if "neighbors" in query:
            return _Result(
                [
                    {"id": n, "neighbors": [t for s, t in self.edges if s == n]}
                    for n in self.nodes
                ]
            )
        raise AssertionError(f"unexpected query: {query}")


def _service(nodes: list[str], edges: list[tuple[str, str]]) -> KnowledgeGraphService:
    svc = KnowledgeGraphService.__new__(KnowledgeGraphService)

    @contextmanager
    def get_session(database: str = "neo4j") -> Iterator[_FakeSession]:
        yield _FakeSession(nodes, edges)

    svc.get_session = get_session  # type: ignore[method-assign,assignment]
    return svc


@pytest.mark.parametrize(
    ("nodes", "edges", "components", "largest"),
    [
        # Two separate pairs plus one isolated node: 3 components, largest 2.
        (["a", "b", "c", "d", "e"], [("a", "b"), ("c", "d")], 3, 2),
        # Chain of 4 and a triangle: 2 components, largest 4.
        (
            ["a", "b", "c", "d", "x", "y", "z"],
            [("a", "b"), ("b", "c"), ("d", "c"), ("x", "y"), ("y", "z"), ("z", "x")],
            2,
            4,
        ),
        # Only isolated nodes: every node is its own component.
        (["a", "b"], [], 2, 1),
        ([], [], 0, 0),
    ],
)
def test_component_counts_are_exact(
    nodes: list[str], edges: list[tuple[str, str]], components: int, largest: int
) -> None:
    analytics = _service(nodes, edges).get_graph_analytics(organization_id="org-1")

    assert analytics.connected_components == components
    assert analytics.largest_component_size == largest
