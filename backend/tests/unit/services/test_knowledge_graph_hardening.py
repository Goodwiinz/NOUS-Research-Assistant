"""
Tests for Knowledge Graph service hardening:
  - Tenant isolation (source_document_ids filtering)
  - Integration contracts (search, adapters)
  - Serialization (metadata/evidence roundtrip)
  - Entity identity (name validation)
"""

import json
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_neo4j_entity(
    entity_id: str = None,
    name: str = "Test Entity",
    entity_type: str = "PERSON",
    source_document_id: str = None,
    metadata: str = "{}",
):
    """Create a mock Neo4j node record for an entity."""
    return {
        "id": entity_id or str(uuid.uuid4()),
        "name": name,
        "type": entity_type,
        "confidence_score": 0.9,
        "extraction_method": "llm_extraction",
        "position": None,
        "context": "test context",
        "metadata": metadata,
        "source_document_id": source_document_id,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }


# ---------------------------------------------------------------------------
# Phase 1: Tenant Isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """Verify source_document_ids filtering in service methods."""

    @patch(
        "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService.get_session"
    )
    def test_get_entity_scoped_returns_none_for_wrong_org(self, mock_session):
        """get_entity with source_document_ids should return None when entity
        belongs to a different org's document."""
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        # Entity belongs to doc-A, but caller passes doc-B scope
        mock_result = MagicMock()
        mock_result.single.return_value = None
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_ctx.run.return_value = mock_result
        mock_session.return_value = mock_ctx

        svc = KnowledgeGraphService()
        result = svc.get_entity("entity-1", source_document_ids=["doc-B"])
        assert result is None

        # Verify the Cypher includes source_document_ids filtering
        call_args = mock_ctx.run.call_args
        query = call_args[0][0]
        assert "source_document_id IN $source_document_ids" in query
        assert call_args[0][1]["source_document_ids"] == ["doc-B"]

    @patch(
        "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService.get_session"
    )
    def test_search_entities_scoped(self, mock_session):
        """search_entities with source_document_ids should include the filter."""
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_ctx.run.return_value = mock_result
        mock_session.return_value = mock_ctx

        svc = KnowledgeGraphService()
        svc.search_entities("test", source_document_ids=["doc-X"])

        call_args = mock_ctx.run.call_args
        query = call_args[0][0]
        assert "source_document_id IN $source_document_ids" in query

    @patch(
        "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService.get_session"
    )
    def test_delete_entity_scoped(self, mock_session):
        """delete_entity with source_document_ids should scope the MATCH."""
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        mock_result = MagicMock()
        mock_result.single.return_value = {"deleted_count": 0}
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_ctx.run.return_value = mock_result
        mock_session.return_value = mock_ctx

        svc = KnowledgeGraphService()
        result = svc.delete_entity("entity-1", source_document_ids=["doc-B"])
        assert result is False

        call_args = mock_ctx.run.call_args
        query = call_args[0][0]
        assert "source_document_id IN $source_document_ids" in query

    @patch(
        "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService.get_session"
    )
    def test_get_all_entities_no_null_escape(self, mock_session):
        """get_all_entities should NOT include IS NULL escape hatch."""
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iter([]))
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_ctx.run.return_value = mock_result
        mock_session.return_value = mock_ctx

        svc = KnowledgeGraphService()
        svc.get_all_entities(limit=10, offset=0, source_document_ids=["doc-A"])

        call_args = mock_ctx.run.call_args
        query = call_args[0][0]
        assert "IS NULL" not in query


# ---------------------------------------------------------------------------
# Phase 2: Integration Contracts
# ---------------------------------------------------------------------------


class TestIntegrationContracts:
    """Verify adapter methods match caller expectations."""

    def test_search_method_exists(self):
        """KnowledgeGraphService should have a search() method."""
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        assert hasattr(KnowledgeGraphService, "search")

    def test_create_entity_node_adapter_exists(self):
        """KnowledgeGraphService should have create_entity_node()."""
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        assert hasattr(KnowledgeGraphService, "create_entity_node")

    @patch(
        "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService.search_entities"
    )
    def test_search_returns_search_response(self, mock_search_entities):
        """search() should return a SearchResponse with .results attribute."""
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        mock_search_entities.return_value = []

        svc = KnowledgeGraphService()
        result = svc.search(
            search_request=MagicMock(query="test", limit=10, offset=0),
            user_id="user-1",
            organization_id="org-1",
        )

        assert hasattr(result, "results")
        assert hasattr(result, "search_time_ms")
        assert hasattr(result, "total_results")
        assert isinstance(result.results, list)


# ---------------------------------------------------------------------------
# Phase 3: Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """Verify JSON serialization roundtrips correctly."""

    def test_parse_metadata_from_json_string(self):
        """_parse_metadata should parse json.dumps() output."""
        from src.services.knowledge_graph.knowledge_graph_service import _parse_metadata

        data = {"key": "value", "nested": {"a": 1}}
        result = _parse_metadata(json.dumps(data))
        assert result == data

    def test_parse_metadata_from_python_repr(self):
        """_parse_metadata should handle legacy str() format."""
        from src.services.knowledge_graph.knowledge_graph_service import _parse_metadata

        result = _parse_metadata("{'key': 'value'}")
        assert result == {"key": "value"}

    def test_parse_metadata_from_dict(self):
        """_parse_metadata should pass through native dicts."""
        from src.services.knowledge_graph.knowledge_graph_service import _parse_metadata

        data = {"key": "value"}
        assert _parse_metadata(data) == data

    def test_parse_metadata_empty(self):
        """_parse_metadata should handle empty/null values."""
        from src.services.knowledge_graph.knowledge_graph_service import _parse_metadata

        assert _parse_metadata(None) == {}
        assert _parse_metadata("") == {}
        assert _parse_metadata("{}") == {}

    def test_parse_evidence_from_json_string(self):
        """_parse_evidence should parse json.dumps() output."""
        from src.services.knowledge_graph.knowledge_graph_service import _parse_evidence

        data = ["evidence1", "evidence2"]
        result = _parse_evidence(json.dumps(data))
        assert result == data

    def test_parse_evidence_from_list(self):
        """_parse_evidence should pass through native lists."""
        from src.services.knowledge_graph.knowledge_graph_service import _parse_evidence

        data = ["a", "b"]
        assert _parse_evidence(data) == data

    def test_parse_evidence_empty(self):
        """_parse_evidence should handle empty/null values."""
        from src.services.knowledge_graph.knowledge_graph_service import _parse_evidence

        assert _parse_evidence(None) == []
        assert _parse_evidence("") == []
        assert _parse_evidence("[]") == []


# ---------------------------------------------------------------------------
# Phase 4: Entity Identity
# ---------------------------------------------------------------------------


class TestEntityIdentity:
    """Verify name validation in entity creation."""

    def test_create_entity_rejects_blank_name(self):
        """create_entity should raise ValueError for blank names."""
        from src.models.graph import CreateEntityRequest, EntityType, ExtractionMethod
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        svc = KnowledgeGraphService()
        request = CreateEntityRequest(
            name="",
            entity_type=EntityType.PERSON,
        )
        with pytest.raises(ValueError, match="blank"):
            svc.create_entity(request)

    def test_create_entity_rejects_whitespace_name(self):
        """create_entity should raise ValueError for whitespace-only names."""
        from src.models.graph import CreateEntityRequest, EntityType, ExtractionMethod
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        svc = KnowledgeGraphService()
        request = CreateEntityRequest(
            name="   ",
            entity_type=EntityType.PERSON,
        )
        with pytest.raises(ValueError, match="blank"):
            svc.create_entity(request)

    @patch(
        "src.services.knowledge_graph.knowledge_graph_service.KnowledgeGraphService.get_session"
    )
    def test_create_entity_strips_whitespace(self, mock_session):
        """create_entity should strip leading/trailing whitespace from names."""
        from src.models.graph import CreateEntityRequest, EntityType
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        mock_node = MagicMock()
        # create_entity now MERGEs and reads the resolved id from the RETURN.
        mock_node.__getitem__ = MagicMock(
            return_value="11111111-1111-1111-1111-111111111111"
        )
        mock_result = MagicMock()
        mock_result.single.return_value = mock_node
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_ctx.run.return_value = mock_result
        mock_session.return_value = mock_ctx

        svc = KnowledgeGraphService()
        request = CreateEntityRequest(
            name="  John Doe  ",
            entity_type=EntityType.PERSON,
        )
        result = svc.create_entity(request)

        # Verify the name passed to Neo4j was stripped
        call_args = mock_ctx.run.call_args
        params = call_args[0][1]
        assert params["name"] == "John Doe"


@pytest.mark.unit
class TestRelationshipDatetimeCoercion:
    def test_to_native_dt_converts_neo4j_datetime(self):
        from datetime import datetime, timezone

        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        class _FakeNeo4jDT:
            def to_native(self):
                return datetime(2026, 1, 1, tzinfo=timezone.utc)

        out = KnowledgeGraphService._to_native_dt(_FakeNeo4jDT())
        assert isinstance(out, datetime)
        # passthrough for native / None
        assert KnowledgeGraphService._to_native_dt(None) is None
        native = datetime(2025, 5, 5)
        assert KnowledgeGraphService._to_native_dt(native) is native


# ---------------------------------------------------------------------------
# #50 read-flip: org-scoping predicate (OR-transition)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEntityScopePredicate:
    """`_entity_scope_predicate` prefers the indexed organization_id but OR's the
    legacy source_document_id IN-list, so the same query stays correct on
    environments whose backfill has not run yet."""

    def _fn(self):
        from src.services.knowledge_graph.knowledge_graph_service import (
            _entity_scope_predicate,
        )

        return _entity_scope_predicate

    def test_unscoped_returns_none(self):
        params = {}
        assert self._fn()("e", None, None, params) is None
        assert params == {}

    def test_org_only_uses_indexed_equality(self):
        params = {}
        pred = self._fn()("e", None, "org-1", params)
        assert pred == "(e.organization_id = $organization_id)"
        assert params == {"organization_id": "org-1"}

    def test_doc_only_preserves_legacy_in_list(self):
        params = {}
        pred = self._fn()("e", ["d1", "d2"], None, params)
        assert pred == "(e.source_document_id IN $source_document_ids)"
        assert params == {"source_document_ids": ["d1", "d2"]}

    def test_both_ors_org_and_docs_for_cross_env_safety(self):
        params = {}
        pred = self._fn()("e", ["d1"], "org-1", params)
        assert pred == (
            "(e.organization_id = $organization_id "
            "OR e.source_document_id IN $source_document_ids)"
        )
        assert params == {"organization_id": "org-1", "source_document_ids": ["d1"]}

    def test_alias_is_respected(self):
        params = {}
        pred = self._fn()("source", None, "org-1", params)
        assert pred == "(source.organization_id = $organization_id)"


@pytest.mark.unit
class TestTwoEndpointScope:
    """`_two_endpoint_scope` scopes relationship/traversal queries by BOTH endpoint
    aliases — same OR semantics as `_entity_scope_predicate`, so legacy NULL-org
    relationships stay deletable/traversable, not just listable."""

    def _fn(self):
        from src.services.knowledge_graph.knowledge_graph_service import (
            _two_endpoint_scope,
        )

        return _two_endpoint_scope

    def test_unscoped(self):
        frag, params = self._fn()("start", "related", None, None)
        assert frag == ""
        assert params == {}

    def test_org_wide_uses_indexed_equality_on_both_endpoints(self):
        frag, params = self._fn()("start", "related", None, "org-1")
        assert frag == (
            "\n  AND (start.organization_id = $organization_id)"
            "\n  AND (related.organization_id = $organization_id)"
        )
        assert params == {"organization_id": "org-1"}

    def test_project_only_uses_doc_list_on_both_endpoints(self):
        frag, params = self._fn()("source", "target", ["d1", "d2"], None)
        assert frag == (
            "\n  AND (source.source_document_id IN $source_document_ids)"
            "\n  AND (target.source_document_id IN $source_document_ids)"
        )
        assert params == {"source_document_ids": ["d1", "d2"]}

    def test_org_ors_with_docs_when_both_supplied(self):
        """Org-exclusive precedence here (while the entity sibling OR'd) made
        legacy NULL-org relationships listable but undeletable/untraversable."""
        frag, params = self._fn()("start", "end", ["d1"], "org-1")
        assert frag == (
            "\n  AND (start.organization_id = $organization_id"
            " OR start.source_document_id IN $source_document_ids)"
            "\n  AND (end.organization_id = $organization_id"
            " OR end.source_document_id IN $source_document_ids)"
        )
        assert params == {"organization_id": "org-1", "source_document_ids": ["d1"]}


# ---------------------------------------------------------------------------
# Schema bootstrap on first connect (entity_fulltext_idx)
# ---------------------------------------------------------------------------


class TestSchemaOnConnect:
    """The fulltext index backing search_entities must be created on connect.

    Nothing else calls _ensure_schema on the hot path, so without this a fresh
    Neo4j never gets entity_fulltext_idx and every search degrades to a CONTAINS
    full-scan (observed live: 'There is no such fulltext schema index').
    """

    @pytest.fixture(autouse=True)
    def _isolated_breaker(self):
        # Other tests can leave the process-wide Neo4j breaker open. Tests of
        # open/closed behavior below explicitly supply their own breaker.
        with patch(
            "src.services.knowledge_graph.knowledge_graph_service.get_circuit_breaker",
            return_value=None,
        ):
            yield

    def _fresh_service(self):
        from src.services.knowledge_graph.knowledge_graph_service import (
            KnowledgeGraphService,
        )

        # Clear the shared singleton so _connect takes the slow (create) path,
        # and reset the schema latch so _maybe_ensure_schema actually attempts.
        KnowledgeGraphService._driver_instance = None
        KnowledgeGraphService._schema_ensured = False
        return KnowledgeGraphService

    def _reset(self, cls):
        cls._driver_instance = None
        cls._schema_ensured = False

    @patch("src.services.knowledge_graph.knowledge_graph_service.GraphDatabase")
    def test_connect_runs_ensure_schema(self, mock_graphdb):
        KnowledgeGraphService = self._fresh_service()
        try:
            with patch.object(KnowledgeGraphService, "_ensure_schema") as mock_schema:
                svc = KnowledgeGraphService()
                svc._connect()
                mock_schema.assert_called_once()
                # A successful bootstrap latches so later calls don't re-run it.
                assert KnowledgeGraphService._schema_ensured is True
        finally:
            self._reset(KnowledgeGraphService)

    @patch("src.services.knowledge_graph.knowledge_graph_service.GraphDatabase")
    def test_schema_failure_does_not_null_driver(self, mock_graphdb):
        """A schema hiccup must leave a healthy connection intact (CONTAINS
        fallback still serves queries) and must NOT latch, so it retries."""
        KnowledgeGraphService = self._fresh_service()
        try:
            with patch.object(
                KnowledgeGraphService,
                "_ensure_schema",
                side_effect=RuntimeError("transient schema error"),
            ):
                svc = KnowledgeGraphService()
                svc._connect()
                assert svc.driver is not None
                assert KnowledgeGraphService._driver_instance is not None
                # Failed bootstrap leaves the latch unset for a later retry.
                assert KnowledgeGraphService._schema_ensured is False
        finally:
            self._reset(KnowledgeGraphService)

    def test_maybe_ensure_schema_skips_when_breaker_open(self):
        """Breaker open at first attempt: do NOT run schema, do NOT latch — so a
        later call (once the breaker recovers) still gets a chance to create the
        index instead of being stuck on CONTAINS for the pod's lifetime."""
        KnowledgeGraphService = self._fresh_service()
        open_breaker = MagicMock()
        open_breaker.can_execute.return_value = False
        try:
            with (
                patch(
                    "src.services.knowledge_graph.knowledge_graph_service.get_circuit_breaker",
                    return_value=open_breaker,
                ),
                patch.object(KnowledgeGraphService, "_ensure_schema") as mock_schema,
            ):
                svc = KnowledgeGraphService()
                svc._maybe_ensure_schema()
                mock_schema.assert_not_called()
                assert KnowledgeGraphService._schema_ensured is False
        finally:
            self._reset(KnowledgeGraphService)

    def test_maybe_ensure_schema_runs_when_breaker_closed(self):
        """Once the breaker is closed, the bootstrap runs and latches."""
        KnowledgeGraphService = self._fresh_service()
        closed_breaker = MagicMock()
        closed_breaker.can_execute.return_value = True
        try:
            with (
                patch(
                    "src.services.knowledge_graph.knowledge_graph_service.get_circuit_breaker",
                    return_value=closed_breaker,
                ),
                patch.object(KnowledgeGraphService, "_ensure_schema") as mock_schema,
            ):
                svc = KnowledgeGraphService()
                svc._maybe_ensure_schema()
                mock_schema.assert_called_once()
                assert KnowledgeGraphService._schema_ensured is True
        finally:
            self._reset(KnowledgeGraphService)

    def test_maybe_ensure_schema_reentrancy_guard(self):
        """The nested get_session inside _ensure_schema must not recurse back
        into a second _ensure_schema on the same instance."""
        KnowledgeGraphService = self._fresh_service()
        try:
            with patch.object(KnowledgeGraphService, "_ensure_schema") as mock_schema:
                svc = KnowledgeGraphService()
                svc._ensuring_schema = True  # simulate in-flight bootstrap
                svc._maybe_ensure_schema()
                mock_schema.assert_not_called()
        finally:
            self._reset(KnowledgeGraphService)
