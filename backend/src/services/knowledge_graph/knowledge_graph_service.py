"""
Knowledge Graph Service for managing Neo4j graph database operations
"""

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from neo4j import Driver, GraphDatabase, Session
from neo4j.exceptions import ServiceUnavailable, SessionExpired

from src.core.circuit_breaker import ServiceUnavailableError as CircuitBreakerError
from src.core.circuit_breaker import get_circuit_breaker
from src.core.config import settings
from src.models.graph import (
    BatchEntityRequest,
    BatchEntityResponse,
    CreateEntityRequest,
    CreateRelationshipRequest,
    Entity,
    EntityResponse,
    EntityType,
    ExtractionMethod,
    GraphAnalytics,
    GraphHealthStatus,
    GraphPath,
    GraphSearchRequest,
    GraphSearchResponse,
    GraphVisualizationData,
    GraphVisualizationEdge,
    GraphVisualizationNode,
    Relationship,
    RelationshipResponse,
    RelationshipType,
    UpdateEntityRequest,
)

logger = logging.getLogger(__name__)


class RelationshipScopeError(RuntimeError):
    """Raised when scoped relationship creation finds no endpoint pair."""


def _parse_metadata(metadata_val) -> Dict[str, Any]:
    """Parse metadata from Neo4j (may be JSON string, Python repr, or dict)."""
    if not metadata_val or metadata_val == "{}":
        return {}
    if isinstance(metadata_val, dict):
        return metadata_val
    try:
        parsed = json.loads(metadata_val)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        pass
    # Fallback for legacy data stored via str() instead of json.dumps().
    # ast.literal_eval is safe here — it only handles Python literals
    # (strings, numbers, dicts, lists, tuples, booleans, None), NOT
    # arbitrary code execution. It is explicitly designed as a safe
    # alternative to eval() for parsing Python literal structures.
    try:
        import ast

        parsed = ast.literal_eval(metadata_val)  # noqa: S307 — safe, literal-only
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, SyntaxError):
        return {}


def _parse_evidence(evidence_val) -> List[str]:
    """Parse evidence from Neo4j (may be JSON string or native list)."""
    if not evidence_val or evidence_val == "[]":
        return []
    if isinstance(evidence_val, list):
        return evidence_val
    try:
        parsed = json.loads(evidence_val)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _convert_datetime(dt) -> datetime:
    """Convert Neo4j datetime to Python datetime"""
    if dt is None:
        return datetime.utcnow()
    if isinstance(dt, datetime):
        return dt
    if isinstance(dt, str):
        # Handle ISO format strings from Neo4j
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if hasattr(dt, "to_native"):
        return dt.to_native()
    try:
        return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    except (AttributeError, TypeError):
        return datetime.utcnow()


import re as _re

_LUCENE_SPECIAL = _re.compile(r'(&&|\|\||[+\-!(){}\[\]^"~*?:\\/])')


def _to_lucene_prefix(query: str) -> str:
    """Escape Lucene special chars in a plain-text query and prefix-match each
    token (token* ) for substring-like recall via the fulltext index."""
    tokens = []
    for tok in query.split():
        esc = _LUCENE_SPECIAL.sub(r"\\\1", tok)
        if esc:
            tokens.append(esc + "*")
    return " ".join(tokens) if tokens else query


def _entity_scope_predicate(
    alias: str,
    source_document_ids: Optional[List[str]],
    organization_id: Optional[str],
    params: Dict[str, Any],
) -> Optional[str]:
    """Build the org-scoping WHERE predicate for an Entity alias (#50 read-flip).

    Prefers the indexed `organization_id` (stamped on writes + the one-time
    backfill) — but OR's it with the legacy `source_document_id IN $ids` list so
    the SAME query stays correct on environments whose backfill has not run yet
    (organization_id NULL there). Once every environment is backfilled the IN-list
    arg can be dropped, leaving a pure indexed equality.

    Mutates `params` with whichever bind vars it uses. Returns the predicate (no
    leading WHERE/AND), or None when unscoped (both inputs None).
    Relationships have no organization_id, so this is entity-only.
    """
    preds: List[str] = []
    if organization_id is not None:
        preds.append(f"{alias}.organization_id = $organization_id")
        params["organization_id"] = organization_id
    if source_document_ids is not None:
        preds.append(f"{alias}.source_document_id IN $source_document_ids")
        params["source_document_ids"] = source_document_ids
    if not preds:
        # Both scopes None → unscoped, cross-tenant query. Callers should always
        # pass an organization_id; warn loudly so a dropped scope is visible.
        logger.warning(
            "_entity_scope_predicate called with no organization_id or "
            "source_document_ids — query will span all organizations"
        )
        return None
    return "(" + " OR ".join(preds) + ")"


def _two_endpoint_scope(
    alias_a: str,
    alias_b: str,
    source_document_ids: Optional[List[str]],
    organization_id: Optional[str],
) -> tuple[str, Dict[str, Any]]:
    """Scope a relationship/traversal query by BOTH endpoint Entity aliases (#50).

    Same OR semantics as ``_entity_scope_predicate``: when both scopes are
    supplied each endpoint matches `org = $org OR source_document_id IN $ids`.
    Org-exclusive precedence here used to make legacy NULL-org relationships
    listable (via the OR'ing sibling) but undeletable and untraversable.

    Returns a fragment with a LEADING "\\n  AND ..." (empty when unscoped) plus
    the bind params to merge in.
    """
    preds: List[str] = []
    params: Dict[str, Any] = {}
    if organization_id is not None:
        preds.append("{alias}.organization_id = $organization_id")
        params["organization_id"] = organization_id
    if source_document_ids is not None:
        preds.append("{alias}.source_document_id IN $source_document_ids")
        params["source_document_ids"] = source_document_ids
    if preds:
        joined = "(" + " OR ".join(preds) + ")"
        frag = (
            f"\n  AND {joined.format(alias=alias_a)}"
            f"\n  AND {joined.format(alias=alias_b)}"
        )
        return frag, params
    # Neither scope supplied → the traversal spans EVERY organization's
    # entities. Warn loudly (mirrors _entity_scope_predicate) so a dropped
    # tenant scope on a path/neighborhood query is auditable, not silent.
    logger.warning(
        "_two_endpoint_scope (%s/%s) called with no organization_id or "
        "source_document_ids — traversal will span ALL organizations (#50)",
        alias_a,
        alias_b,
    )
    return "", {}


def _safe_relationship_type(value: Optional[str]) -> RelationshipType:
    """Safely convert string to RelationshipType, falling back to RELATED_TO"""
    if not value:
        return RelationshipType.RELATED_TO
    try:
        return RelationshipType(value)
    except ValueError:
        logger.debug(f"Unknown RelationshipType '{value}', using RELATED_TO")
        return RelationshipType.RELATED_TO


def _safe_entity_type(value: Optional[str]) -> EntityType:
    """Safely convert string to EntityType, falling back to OTHER for unknown values"""
    if not value:
        return EntityType.OTHER
    try:
        return EntityType(value)
    except ValueError:
        logger.debug(f"Unknown EntityType '{value}', using OTHER")
        return EntityType.OTHER


def _safe_extraction_method(value: Optional[str]) -> ExtractionMethod:
    """Safely convert string to ExtractionMethod, falling back to UNKNOWN for None/unknown values"""
    if not value:
        return ExtractionMethod.UNKNOWN

    # Map common aliases
    alias_map = {
        "llm": "llm_extraction",
        "spacy": "spacy_ner",
        "pattern": "pattern_matching",
        "rules": "rule_based",
    }
    normalized = alias_map.get(value.lower(), value)

    try:
        return ExtractionMethod(normalized)
    except ValueError:
        logger.debug(f"Unknown ExtractionMethod '{value}', using UNKNOWN")
        return ExtractionMethod.UNKNOWN


class KnowledgeGraphService:
    """Service for managing knowledge graph operations using Neo4j"""

    _driver_instance: Optional[Driver] = None
    _driver_lock = threading.Lock()
    # Process-wide latch: flips True only after _ensure_schema succeeds. Stays
    # False if the first attempt was blocked (e.g. the neo4j circuit breaker was
    # open at startup), so a later call retries instead of leaving the pod
    # permanently on the unindexed CONTAINS fallback.
    _schema_ensured: bool = False

    def __init__(self):
        self.driver: Optional[Driver] = None
        self.uri = settings.NEO4J_URI
        self.user = settings.NEO4J_USER
        self.password = settings.NEO4J_PASSWORD
        # Explicit database name for queries that need it (dbms.database.details)
        # — never read the private per-session database attribute.
        self.database = getattr(settings, "NEO4J_DATABASE", "neo4j") or "neo4j"
        # Re-entrancy guard: _maybe_ensure_schema -> _ensure_schema -> get_session
        # would otherwise recurse back into _maybe_ensure_schema on the same
        # instance. Per-instance flag keeps that nested call a no-op.
        self._ensuring_schema = False
        # Do not connect immediately to avoid import-time side effects
        # self._connect()

    async def __aenter__(self):
        self._connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # We don't close the shared driver here anymore
        pass

    @classmethod
    def close_driver(cls):
        """Close the shared driver instance"""
        with cls._driver_lock:
            if cls._driver_instance:
                cls._driver_instance.close()
                cls._driver_instance = None
                logger.info("Closed shared Neo4j driver")

    def _connect(self):
        """Establish connection to Neo4j database (using shared driver)"""
        # Fast path: driver already exists
        if KnowledgeGraphService._driver_instance:
            self.driver = KnowledgeGraphService._driver_instance
            return

        # Slow path: create driver with lock to prevent race conditions
        with KnowledgeGraphService._driver_lock:
            # Double-checked locking
            if KnowledgeGraphService._driver_instance:
                self.driver = KnowledgeGraphService._driver_instance
                return

            try:
                KnowledgeGraphService._driver_instance = GraphDatabase.driver(
                    self.uri,
                    auth=(self.user, self.password),
                    max_connection_lifetime=3600,
                    max_connection_pool_size=50,
                )
                self.driver = KnowledgeGraphService._driver_instance

                # Test connection
                with self.driver.session() as session:
                    session.run("RETURN 1")
                logger.info(f"Connected to Neo4j at {self.uri}")

                # Ensure constraints + indexes (incl. the entity_fulltext_idx
                # backing search_entities) exist now that the driver is up.
                # Nothing else calls _ensure_schema on the hot path, so on a
                # fresh Neo4j the fulltext index was never created and every
                # search silently fell back to an unindexed CONTAINS scan.
                # Idempotent (IF NOT EXISTS). Goes through _maybe_ensure_schema
                # so that if the breaker is open right now the latch stays unset
                # and get_session retries it once the breaker recovers.
                self._maybe_ensure_schema()
            except Exception as e:
                logger.error(f"Failed to connect to Neo4j: {e}")
                # Don't raise here, let the caller handle it or retry later
                self.driver = None
                KnowledgeGraphService._driver_instance = None

    @contextmanager
    def get_session(self, database: str = "neo4j") -> Session:
        """Context manager for database sessions with circuit breaker protection"""
        # Check circuit breaker before attempting connection
        breaker = get_circuit_breaker("neo4j")
        if breaker and not breaker.can_execute():
            raise CircuitBreakerError(
                "neo4j", "Neo4j circuit breaker is open - service unavailable"
            )

        if not self.driver:
            self._connect()

        if not self.driver:
            if breaker:
                breaker.record_failure()
            raise RuntimeError("Neo4j driver not initialized")

        # Retry the one-time schema bootstrap if a prior attempt was blocked
        # (e.g. breaker open at first connect). No-op once the latch is set or
        # while a schema-ensure is already in flight on this instance.
        self._maybe_ensure_schema()

        session = self.driver.session(database=database)
        try:
            yield session
            # Record success on clean exit
            if breaker:
                breaker.record_success()
        except (ServiceUnavailable, SessionExpired) as e:
            # Record failure for connection-related errors
            if breaker:
                breaker.record_failure(e)
            raise
        finally:
            session.close()

    def _maybe_ensure_schema(self):
        """Run the one-time schema bootstrap, retrying until it actually lands.

        ``_connect`` caches the shared driver as soon as its ``RETURN 1`` probe
        succeeds — and that probe bypasses the circuit breaker. ``_ensure_schema``
        does not: it goes through ``get_session``, which raises
        ``CircuitBreakerError`` while the neo4j breaker is open. So if the breaker
        is open at first connect (Neo4j briefly flaky, then recovers), the driver
        is cached but schema creation is skipped — and the ``_connect`` fast path
        never re-runs it, leaving the pod permanently on the unindexed CONTAINS
        fallback. Calling this from ``get_session`` retries the bootstrap on a
        later request once the breaker closes.

        Lock-free by design: the DDL is idempotent (``IF NOT EXISTS``) and Neo4j
        serializes schema changes, so a rare concurrent double-run is harmless —
        cheaper than ordering a new lock against ``_driver_lock``. The per-instance
        ``_ensuring_schema`` guard stops the nested ``get_session`` call inside
        ``_ensure_schema`` from recursing back in.
        """
        if KnowledgeGraphService._schema_ensured or self._ensuring_schema:
            return
        # Don't attempt (and don't latch) while the breaker is open — retry later.
        breaker = get_circuit_breaker("neo4j")
        if breaker and not breaker.can_execute():
            return

        self._ensuring_schema = True
        try:
            self._ensure_schema()
            KnowledgeGraphService._schema_ensured = True
        except Exception as schema_err:  # noqa: BLE001
            # Best-effort: a schema hiccup must not break callers — queries still
            # work via the CONTAINS fallback, and the latch stays unset so a
            # later call retries.
            logger.warning("Neo4j schema init deferred: %s", schema_err)
        finally:
            self._ensuring_schema = False

    def _ensure_schema(self):
        """Ensure database schema constraints and indexes exist"""
        if not self.driver:
            self._connect()

        if not self.driver:
            logger.warning("Skipping schema initialization - no connection to Neo4j")
            return

        try:
            with self.get_session() as session:
                # Create constraints
                constraints = [
                    "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
                    # Identity for idempotent MERGE: one node per
                    # (canonical_key, type, organization_id) so re-ingest dedups
                    # WITHOUT collapsing two orgs' same name+type entities onto
                    # one shared node (tenant isolation). Migrate from the old
                    # (canonical_key, type) constraint: drop it, then create the
                    # composite. The new key is a strict superset so existing
                    # nodes stay unique; the tolerant loop below swallows a
                    # missing-drop / already-exists on either statement, and a
                    # failed migration cannot crash the schema bootstrap.
                    "DROP CONSTRAINT entity_canonical_unique IF EXISTS",
                    "CREATE CONSTRAINT entity_canonical_org_unique IF NOT EXISTS FOR (e:Entity) REQUIRE (e.canonical_key, e.type, e.organization_id) IS UNIQUE",
                    "CREATE CONSTRAINT document_id_unique IF NOT EXISTS FOR (d:Document) REQUIRE d.id IS UNIQUE",
                    "CREATE INDEX entity_name_index IF NOT EXISTS FOR (e:Entity) ON (e.name)",
                    "CREATE INDEX entity_type_index IF NOT EXISTS FOR (e:Entity) ON (e.type)",
                    # Hottest filter in the service (org/tenant scoping). Without this the
                    # `source_document_id IN [...]` predicate is a full label scan.
                    "CREATE INDEX entity_source_doc_index IF NOT EXISTS FOR (e:Entity) ON (e.source_document_id)",
                    "CREATE INDEX document_title_index IF NOT EXISTS FOR (d:Document) ON (d.title)",
                    "CREATE INDEX relationship_strength_index IF NOT EXISTS FOR ()-[r:RELATED_TO]-() ON (r.strength)",
                    # get_all_relationships orders by r.created_at for pagination.
                    "CREATE INDEX relationship_created_at_index IF NOT EXISTS FOR ()-[r:RELATED_TO]-() ON (r.created_at)",
                    # Fulltext index backs search_entities (replaces the unindexed CONTAINS scan).
                    "CREATE FULLTEXT INDEX entity_fulltext_idx IF NOT EXISTS FOR (e:Entity) ON EACH [e.name]",
                    # Direct tenant scoping (replaces the org-doc-id IN-list once backfilled).
                    "CREATE INDEX entity_organization_index IF NOT EXISTS FOR (e:Entity) ON (e.organization_id)",
                ]

                # The tenant-isolation constraint is critical: if it can't be
                # created we must NOT latch _schema_ensured (else the failure is
                # permanently masked and the constraint never retried). Other
                # constraints/indexes stay best-effort.
                _CRITICAL = "entity_canonical_org_unique"
                critical_failed = False
                for constraint in constraints:
                    try:
                        session.run(constraint)
                        logger.debug(f"Applied schema: {constraint}")
                    except Exception as e:
                        if "already exists" not in str(e).lower():
                            logger.warning(f"Failed to apply constraint: {e}")
                            if _CRITICAL in constraint:
                                critical_failed = True

                if critical_failed:
                    # Raise so _maybe_ensure_schema leaves the latch unset and a
                    # later connect retries the migration.
                    raise RuntimeError(
                        "tenant-isolation constraint "
                        f"{_CRITICAL} not created; will retry on next connect"
                    )

                logger.info("Database schema ensured")
        except Exception as e:
            logger.error(f"Failed to ensure database schema: {e}")
            raise

    def close(self):
        """
        Close Neo4j driver connection.

        NOTE: With Singleton pattern, we DO NOT close the shared driver here.
        The driver should remain open for the application lifetime.
        Use close_driver() class method for explicit shutdown.
        """
        # if self.driver:
        #     self.driver.close()
        #     logger.info("Neo4j connection closed")
        pass

    # Entity Management
    def create_entity(self, request: CreateEntityRequest) -> EntityResponse:
        """Create a new entity in the knowledge graph"""
        if not request.name or not request.name.strip():
            raise ValueError("Entity name cannot be blank or whitespace-only")
        request.name = request.name.strip()

        start_time = time.time()
        entity_id = str(uuid.uuid4())
        # Stable identity for dedup. Re-ingesting the same name+type, or two
        # documents naming the same entity, MERGE onto one node instead of
        # CREATEing a fresh UUID each time (the old behavior duplicated nodes
        # endlessly and broke cross-document linking).
        canonical_key = request.name.strip().lower()

        try:
            with self.get_session() as session:
                # organization_id is part of the MERGE identity so two orgs'
                # same name+type entities get separate nodes (tenant isolation).
                # Coalesced to "" because Cypher cannot MERGE on a null key; the
                # stored property therefore equals the key (never re-set to null
                # in ON CREATE/ON MATCH, which would desync key and property and
                # cause duplicate nodes on the next MERGE).
                query = f"""
                MERGE (e:Entity:{request.entity_type.value} {{canonical_key: $canonical_key, type: $entity_type, organization_id: $org_key}})
                ON CREATE SET
                    e.id = $id,
                    e.name = $name,
                    e.confidence_score = $confidence_score,
                    e.extraction_method = $extraction_method,
                    e.position = $position,
                    e.context = $context,
                    e.metadata = $metadata,
                    e.source_document_id = $source_document_id,
                    e.created_at = datetime(),
                    e.updated_at = datetime()
                ON MATCH SET
                    e.name = $name,
                    e.updated_at = datetime(),
                    e.confidence_score = CASE
                        WHEN $confidence_score > e.confidence_score THEN $confidence_score
                        ELSE e.confidence_score END
                RETURN e.id AS resolved_id, e.created_at AS created_at, e.updated_at AS updated_at
                """

                result = session.run(
                    query,
                    {
                        "id": entity_id,
                        "canonical_key": canonical_key,
                        "name": request.name,
                        "entity_type": request.entity_type.value,
                        "confidence_score": request.confidence_score,
                        "extraction_method": request.extraction_method.value,
                        "position": request.position,
                        "context": request.context,
                        "metadata": (
                            json.dumps(request.metadata) if request.metadata else "{}"
                        ),
                        "source_document_id": request.source_document_id,
                        "org_key": getattr(request, "organization_id", None) or "",
                    },
                )

                node = result.single()
                if not node:
                    raise RuntimeError("Failed to create entity")

                # Use the node's resolved id (existing on MATCH, new on CREATE) —
                # never assume the freshly generated UUID was persisted.
                resolved_id = node["resolved_id"]
                processing_time = time.time() - start_time
                logger.info(
                    f"Upserted entity: {request.name} ({resolved_id}) in {processing_time:.3f}s"
                )

                return EntityResponse(
                    id=resolved_id,
                    name=request.name,
                    entity_type=request.entity_type,
                    confidence_score=request.confidence_score,
                    extraction_method=request.extraction_method,
                    position=request.position,
                    context=request.context,
                    metadata=request.metadata,
                    source_document_id=request.source_document_id,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )

        except Exception as e:
            logger.error(f"Error creating entity {request.name}: {e}")
            raise

    def get_entity(
        self,
        entity_id: str,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> Optional[EntityResponse]:
        """Retrieve an entity by ID, optionally scoped to organization documents"""
        try:
            with self.get_session() as session:
                params: Dict[str, Any] = {"entity_id": entity_id}
                scope = _entity_scope_predicate(
                    "e", source_document_ids, organization_id, params
                )
                where = f"\n                    WHERE {scope}" if scope else ""
                query = f"""
                    MATCH (e:Entity {{id: $entity_id}}){where}
                    RETURN e
                    """

                result = session.run(query, params)
                node = result.single()

                if not node:
                    return None

                e = node["e"]
                return EntityResponse(
                    id=e["id"],
                    name=e["name"],
                    entity_type=_safe_entity_type(e["type"]),
                    confidence_score=e["confidence_score"],
                    extraction_method=_safe_extraction_method(e["extraction_method"]),
                    position=e.get("position"),
                    context=e.get("context"),
                    metadata=_parse_metadata(e.get("metadata", "{}")),
                    source_document_id=e.get("source_document_id"),
                    created_at=_convert_datetime(e["created_at"]),
                    updated_at=(
                        _convert_datetime(e["updated_at"])
                        if e.get("updated_at")
                        else None
                    ),
                )
        except Exception as e:
            logger.error(f"Error retrieving entity {entity_id}: {e}")
            return None

    def update_entity(
        self,
        entity_id: str,
        request: UpdateEntityRequest,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> Optional[EntityResponse]:
        """Update an existing entity, optionally scoped to organization documents"""
        try:
            with self.get_session() as session:
                # Build update parameters dynamically
                update_fields = []
                params = {"entity_id": entity_id, "updated_at": datetime.utcnow()}
                scope = _entity_scope_predicate(
                    "e", source_document_ids, organization_id, params
                )

                if request.name is not None:
                    update_fields.append("e.name = $name")
                    params["name"] = request.name

                if request.confidence_score is not None:
                    update_fields.append("e.confidence_score = $confidence_score")
                    params["confidence_score"] = request.confidence_score

                if request.metadata is not None:
                    update_fields.append("e.metadata = $metadata")
                    params["metadata"] = (
                        json.dumps(request.metadata) if request.metadata else "{}"
                    )

                if not update_fields:
                    return self.get_entity(
                        entity_id,
                        source_document_ids=source_document_ids,
                        organization_id=organization_id,
                    )

                update_fields.append("e.updated_at = $updated_at")
                set_clause = ", ".join(update_fields)

                tenant_filter = f"\nWHERE {scope}" if scope else ""
                query = f"""
                MATCH (e:Entity {{id: $entity_id}}){tenant_filter}
                SET {set_clause}
                RETURN e
                """

                result = session.run(query, params)
                node = result.single()

                if not node:
                    return None

                e = node["e"]
                return EntityResponse(
                    id=e["id"],
                    name=e["name"],
                    entity_type=_safe_entity_type(e["type"]),
                    confidence_score=e["confidence_score"],
                    extraction_method=_safe_extraction_method(e["extraction_method"]),
                    position=e.get("position"),
                    context=e.get("context"),
                    metadata=_parse_metadata(e.get("metadata", "{}")),
                    source_document_id=e.get("source_document_id"),
                    created_at=_convert_datetime(e["created_at"]),
                    updated_at=(
                        _convert_datetime(e["updated_at"])
                        if e.get("updated_at")
                        else None
                    ),
                )
        except Exception as e:
            logger.error(f"Error updating entity {entity_id}: {e}")
            return None

    def delete_entity(
        self,
        entity_id: str,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> bool:
        """Delete an entity and all its relationships, optionally scoped to organization documents"""
        try:
            with self.get_session() as session:
                params: Dict[str, Any] = {"entity_id": entity_id}
                scope = _entity_scope_predicate(
                    "e", source_document_ids, organization_id, params
                )
                where = f"\n                    WHERE {scope}" if scope else ""
                query = f"""
                    MATCH (e:Entity {{id: $entity_id}}){where}
                    DETACH DELETE e
                    RETURN count(e) as deleted_count
                    """

                result = session.run(query, params)
                deleted_count = result.single()["deleted_count"]

                if deleted_count > 0:
                    logger.info(f"Deleted entity {entity_id}")
                    return True
                else:
                    logger.warning(f"Entity {entity_id} not found for deletion")
                    return False
        except Exception as e:
            logger.error(f"Error deleting entity {entity_id}: {e}")
            return False

    def delete_document_graph(
        self,
        document_id: str,
        organization_id: Optional[str] = None,
    ) -> Tuple[int, int]:
        """Best-effort reference-count deletion of a document's graph (audit D2).

        Deleting a document soft-deletes its Postgres ``Entity`` rows but
        historically left the mirrored Neo4j graph untouched, so a deleted
        document's graph data orphaned forever.

        :Entity nodes are NOT per-document: both creation paths MERGE on
        ``(canonical_key, type, organization_id)`` — no document component — so
        one node is *shared* by every document in the org that names the entity,
        and ``e.source_document_id`` (set only ON CREATE) records merely the
        FIRST creator. A node-anchored ``DETACH DELETE`` would therefore destroy
        other documents' relationships whenever the deleted doc happened to be
        the node's creator. Only RELATED_TO relationships carry true per-document
        provenance (``r.source_document_id`` is part of their MERGE key).

        So this deletes in two steps, inside one transaction:

        1. DELETE this document's *relationships* (``r.source_document_id`` =
           the doc id) — the per-document part of the graph.
        2. DELETE only entity *nodes* that this document created
           (``e.source_document_id``) AND that are now fully orphaned
           (``COUNT { (e)--() } = 0`` — no remaining relationships from any
           document; Neo4j 5.x syntax, dev runs 5.26). Plain ``DELETE`` (not
           ``DETACH``) so if the orphan guard ever regresses, Neo4j raises on a
           still-connected node instead of silently destroying shared data.

        A node created by this doc but still connected via another doc survives
        (that doc still references it) — its ``source_document_id`` then points
        at a deleted document; harmless for reads, and a future reconciler can
        re-stamp it. A node created by another doc keeps living even if all of
        this doc's relationships to it are removed in step 1.

        ``source_document_id`` is globally unique to one document in one org, so
        both steps are already tenant-isolated; the ``organization_id``
        predicates are defense-in-depth for property-based graph tenancy, kept
        broad on legacy NULL-org nodes/edges so pre-backfill data for this doc
        is still reaped. They are AND'd with the doc anchor (never OR — that
        would widen the delete to the whole org's graph); this deliberately does
        NOT reuse ``_entity_scope_predicate``, whose OR semantics are for read
        breadth.

        Returns ``(deleted_relationships, deleted_nodes)``. Raises on driver /
        circuit breaker failure; callers isolate it (best-effort at the delete
        endpoint).
        """
        params: Dict[str, Any] = {"source_document_id": str(document_id)}
        rel_org_filter = ""
        node_org_filter = ""
        if organization_id is not None:
            params["organization_id"] = str(organization_id)
            rel_org_filter = (
                "\n                    WHERE (r.organization_id = $organization_id"
                "\n                        OR r.organization_id IS NULL)"
            )
            node_org_filter = (
                "\n                          (e.organization_id = $organization_id"
                "\n                        OR e.organization_id IS NULL)"
                "\n                      AND"
            )
        # Step 1: this document's relationships (per-document provenance edges).
        rel_query = f"""
                    MATCH ()-[r:RELATED_TO {{source_document_id: $source_document_id}}]->(){rel_org_filter}
                    DELETE r
                    RETURN count(r) as deleted_count
                    """
        # Step 2: nodes this doc created that no document references any more.
        # The COUNT {{ (e)--() }} = 0 orphan guard is load-bearing: without it,
        # deleting a creator doc destroys nodes still wired to other docs.
        node_query = f"""
                    MATCH (e:Entity {{source_document_id: $source_document_id}})
                    WHERE{node_org_filter} COUNT {{ (e)--() }} = 0
                    DELETE e
                    RETURN count(e) as deleted_count
                    """

        with self.get_session() as session:
            # One explicit transaction: step 2's orphan check must observe
            # step 1's deletions, and a failure between steps must not strand
            # a half-cleaned graph (the Transaction context manager commits on
            # clean exit, rolls back on error).
            with session.begin_transaction() as tx:
                rel_record = tx.run(rel_query, params).single()
                deleted_relationships = rel_record["deleted_count"] if rel_record else 0
                node_record = tx.run(node_query, params).single()
                deleted_nodes = node_record["deleted_count"] if node_record else 0

            logger.info(
                "Deleted %d graph relationships and %d orphaned entities "
                "for document %s (org=%s)",
                deleted_relationships,
                deleted_nodes,
                document_id,
                organization_id,
            )
            return deleted_relationships, deleted_nodes

    def search_entities(
        self,
        query: str,
        entity_types: Optional[List[EntityType]] = None,
        limit: int = 50,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> List[EntityResponse]:
        """Search for entities by name or properties, optionally scoped to organization documents"""
        try:
            with self.get_session() as session:
                query_str = (query or "").strip()
                # Shared scope/type filters (reference `e`).
                filters: List[str] = []
                params: Dict[str, Any] = {"limit": limit}
                scope = _entity_scope_predicate(
                    "e", source_document_ids, organization_id, params
                )
                if scope:
                    filters.append(scope)
                if entity_types:
                    filters.append("e.type IN $entity_types")
                    params["entity_types"] = [t.value for t in entity_types]

                result = None
                if query_str:
                    # Use the fulltext index instead of an unindexed `CONTAINS`
                    # full label scan. Escape Lucene specials and prefix-match
                    # for substring-like recall. Falls back to CONTAINS on error.
                    lucene = _to_lucene_prefix(query_str)
                    ft_where = (" WHERE " + " AND ".join(filters)) if filters else ""
                    ft_query = f"""
                    CALL db.index.fulltext.queryNodes('entity_fulltext_idx', $lucene)
                    YIELD node AS e, score
                    {ft_where}
                    RETURN e
                    ORDER BY score DESC
                    LIMIT $limit
                    """
                    try:
                        result = list(
                            session.run(ft_query, {**params, "lucene": lucene})
                        )
                    except Exception as ft_err:
                        logger.warning(
                            "Fulltext search failed, falling back to CONTAINS: %s",
                            ft_err,
                        )
                        result = None

                if result is None:
                    # Empty query (list mode) or fulltext fallback.
                    conditions = list(filters)
                    if query_str:
                        conditions.insert(0, "e.name CONTAINS $query")
                        params["query"] = query_str
                    where_clause = (
                        (" WHERE " + " AND ".join(conditions)) if conditions else ""
                    )
                    search_query = f"""
                    MATCH (e:Entity)
                    {where_clause}
                    RETURN e
                    ORDER BY e.confidence_score DESC
                    LIMIT $limit
                    """
                    result = session.run(search_query, params)

                entities = []

                for node in result:
                    e = node["e"]
                    entities.append(
                        EntityResponse(
                            id=e["id"],
                            name=e["name"],
                            entity_type=_safe_entity_type(e["type"]),
                            confidence_score=e["confidence_score"],
                            extraction_method=_safe_extraction_method(
                                e["extraction_method"]
                            ),
                            position=e.get("position"),
                            context=e.get("context"),
                            metadata=_parse_metadata(e.get("metadata", "{}")),
                            source_document_id=e.get("source_document_id"),
                            created_at=_convert_datetime(e["created_at"]),
                            updated_at=(
                                _convert_datetime(e["updated_at"])
                                if e.get("updated_at")
                                else None
                            ),
                        )
                    )

                return entities
        except Exception as e:
            logger.error(f"Error searching entities: {e}")
            return []

    def get_all_entities(
        self,
        limit: int = 100,
        offset: int = 0,
        entity_types: Optional[List[EntityType]] = None,
        source_document_ids: Optional[List[str]] = None,
        connected_only: bool = False,
        organization_id: Optional[str] = None,
    ) -> List[EntityResponse]:
        """Get all entities with pagination and optional filtering"""
        try:
            with self.get_session() as session:
                # Build query conditions
                conditions = []
                params = {"limit": limit, "offset": offset}

                if entity_types:
                    # Filter by entity type property
                    type_values = [t.value for t in entity_types]
                    conditions.append("e.type IN $entity_types")
                    params["entity_types"] = type_values

                scope = _entity_scope_predicate(
                    "e", source_document_ids, organization_id, params
                )
                if scope:
                    conditions.append(scope)

                where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

                # When connected_only, only return entities involved in relationships
                if connected_only:
                    match_clause = "MATCH (e:Entity)-[:RELATED_TO]-()"
                    return_distinct = "RETURN DISTINCT e"
                else:
                    match_clause = "MATCH (e:Entity)"
                    return_distinct = "RETURN e"

                query = f"""
                {match_clause}
                {where_clause}
                {return_distinct}
                ORDER BY e.created_at DESC, e.id
                SKIP $offset
                LIMIT $limit
                """

                result = session.run(query, params)
                entities = []

                for node in result:
                    e = node["e"]
                    # Handle legacy nodes that might not have all properties
                    labels = e.labels if hasattr(e, "labels") else []
                    entity_type = EntityType.PERSON  # Default

                    # Determine entity type from labels or properties
                    if "type" in e:
                        entity_type = _safe_entity_type(e["type"])
                    elif labels:
                        # Map label to entity type
                        label_map = {
                            "Person": EntityType.PERSON,
                            "Organization": EntityType.ORGANIZATION,
                            "Location": EntityType.LOCATION,
                            "Concept": EntityType.CONCEPT,
                            "Event": EntityType.EVENT,
                            "Product": EntityType.PRODUCT,
                            "Date": EntityType.DATE,
                            "Technology": EntityType.TECHNOLOGY,
                            "Document": EntityType.DOCUMENT,
                        }
                        for label in labels:
                            if label in label_map:
                                entity_type = label_map[label]
                                break

                    entities.append(
                        EntityResponse(
                            id=e.get("id", str(uuid.uuid4())),
                            name=e.get("name", "Unknown"),
                            entity_type=entity_type,
                            confidence_score=e.get("confidence_score", 0.8),
                            extraction_method=_safe_extraction_method(
                                e.get("extraction_method")
                            ),
                            position=e.get("position"),
                            context=e.get("context"),
                            metadata=_parse_metadata(e.get("metadata", "{}")),
                            source_document_id=e.get("source_document_id"),
                            created_at=_convert_datetime(
                                e.get("created_at", datetime.utcnow())
                            ),
                            updated_at=(
                                _convert_datetime(e["updated_at"])
                                if e.get("updated_at")
                                else None
                            ),
                        )
                    )

                return entities
        except Exception as e:
            logger.error(f"Error getting all entities: {e}")
            raise  # Re-raise to see actual error, don't silently return empty

    def count_entities(
        self,
        entity_types: Optional[List[EntityType]] = None,
        source_document_ids: Optional[List[str]] = None,
        connected_only: bool = False,
        organization_id: Optional[str] = None,
    ) -> int:
        """Count total entities with optional filtering"""
        try:
            with self.get_session() as session:
                conditions = []
                params: Dict[str, Any] = {}

                if entity_types:
                    type_values = [t.value for t in entity_types]
                    conditions.append("e.type IN $entity_types")
                    params["entity_types"] = type_values

                scope = _entity_scope_predicate(
                    "e", source_document_ids, organization_id, params
                )
                if scope:
                    conditions.append(scope)

                where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

                if connected_only:
                    match_clause = "MATCH (e:Entity)-[:RELATED_TO]-()"
                    count_expr = "count(DISTINCT e)"
                else:
                    match_clause = "MATCH (e:Entity)"
                    count_expr = "count(e)"

                query = f"""
                {match_clause}
                {where_clause}
                RETURN {count_expr} as total
                """

                result = session.run(query, params)
                record = result.single()
                return record["total"] if record else 0
        except Exception as e:
            logger.error(f"Error counting entities: {e}")
            return 0

    def get_all_relationships(
        self,
        limit: int = 500,
        offset: int = 0,
        relationship_types: Optional[List[RelationshipType]] = None,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> List[RelationshipResponse]:
        """Get all relationships with pagination, optionally org-scoped."""
        # Cap limit to prevent OOM on large graphs
        limit = min(limit, 200)
        try:
            with self.get_session() as session:
                conditions = []
                params = {"limit": limit, "offset": offset}

                if relationship_types:
                    type_values = [t.value for t in relationship_types]
                    conditions.append("r.type IN $relationship_types")
                    params["relationship_types"] = type_values

                if organization_id is not None:
                    # Scope by ENDPOINT org (not edge org) so legacy edges that
                    # predate edge-org stamping but connect correctly-stamped
                    # entities still surface for the owning tenant.
                    conditions.append(
                        "source.organization_id = $organization_id"
                        " AND target.organization_id = $organization_id"
                    )
                    params["organization_id"] = organization_id
                elif source_document_ids is not None:
                    # Use relationship-level source_document_id for filtering
                    # (more reliable than entity node property for scoping)
                    conditions.append("r.source_document_id IN $source_document_ids")
                    params["source_document_ids"] = source_document_ids

                where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

                query = f"""
                MATCH (source:Entity)-[r:RELATED_TO]->(target:Entity)
                {where_clause}
                RETURN r.id AS rid,
                       source.id AS source_id,
                       target.id AS target_id,
                       r.type AS rel_type,
                       r.strength AS strength,
                       r.confidence_score AS confidence,
                       r.source_document_id AS doc_id,
                       r.created_at AS created_at
                ORDER BY r.created_at DESC, rid
                SKIP $offset
                LIMIT $limit
                """

                result = session.run(query, params)
                relationships = []

                for record in result:
                    source_id = record["source_id"]
                    target_id = record["target_id"]

                    if not source_id or not target_id:
                        continue

                    relationships.append(
                        RelationshipResponse(
                            id=record["rid"] or str(uuid.uuid4()),
                            source_entity_id=source_id,
                            target_entity_id=target_id,
                            relationship_type=_safe_relationship_type(
                                record["rel_type"] or "RELATED_TO"
                            ),
                            strength=record["strength"] or 0.5,
                            confidence_score=record["confidence"] or 0.8,
                            context=None,
                            evidence=[],
                            metadata={},
                            source_document_id=record["doc_id"],
                            created_at=_convert_datetime(record["created_at"]),
                            updated_at=None,
                        )
                    )

                return relationships
        except Exception as e:
            logger.error(f"Error getting all relationships: {e}", exc_info=True)
            return []

    # Relationship Management
    def create_relationship(
        self,
        request: CreateRelationshipRequest,
        source_document_ids: Optional[List[str]] = None,
    ) -> RelationshipResponse:
        """Create a new relationship between entities, optionally scoped to organization documents"""
        start_time = time.time()
        relationship_id = str(uuid.uuid4())

        try:
            with self.get_session() as session:
                # Scope BOTH endpoints to the caller's org so a relationship can
                # never bind a cross-tenant entity. Prefer the indexed
                # organization_id; fall back to the source_document_id IN-list
                # for callers that still pass doc ids; else unscoped (legacy).
                if request.organization_id:
                    tenant_filter = (
                        "\nWHERE source.organization_id = $organization_id"
                        " AND target.organization_id = $organization_id"
                    )
                elif source_document_ids is not None:
                    tenant_filter = (
                        "\nWHERE source.source_document_id IN $source_document_ids"
                        " AND target.source_document_id IN $source_document_ids"
                    )
                else:
                    tenant_filter = ""
                # MERGE (not CREATE) so re-ingesting the SAME document does not
                # pile up duplicate RELATED_TO edges. The dedupe key is
                # (source, target, type, source_document_id): source_document_id
                # is part of the key so two DIFFERENT documents asserting the
                # same pair keep their own provenance edges (a doc-scoped read
                # filtering r.source_document_id still finds them) rather than
                # one overwriting the other. source_document_id is coalesced to
                # "" in params because Cypher cannot MERGE on a null key value.
                # LIMIT 1 keeps the read to one row; it does NOT consolidate any
                # legacy duplicate edges left by the old CREATE path (that is a
                # separate one-time data cleanup, out of scope here).
                query = f"""
                MATCH (source:Entity {{id: $source_entity_id}})
                MATCH (target:Entity {{id: $target_entity_id}}){tenant_filter}
                MERGE (source)-[r:RELATED_TO {{
                    type: $relationship_type,
                    source_document_id: $source_document_id
                }}]->(target)
                ON CREATE SET
                    r.id = $id,
                    r.strength = $strength,
                    r.confidence_score = $confidence_score,
                    r.context = $context,
                    r.evidence = $evidence,
                    r.metadata = $metadata,
                    r.organization_id = $organization_id,
                    r.created_at = datetime(),
                    r.updated_at = datetime()
                ON MATCH SET
                    r.strength = $strength,
                    r.confidence_score = $confidence_score,
                    r.context = $context,
                    r.evidence = $evidence,
                    r.metadata = $metadata,
                    r.organization_id = coalesce(r.organization_id, $organization_id),
                    r.updated_at = datetime()
                RETURN r, source, target
                LIMIT 1
                """

                # Serialize evidence and metadata to JSON strings (Neo4j only accepts primitives)
                evidence_str = (
                    json.dumps(request.evidence) if request.evidence else "[]"
                )
                metadata_str = (
                    json.dumps(request.metadata) if request.metadata else "{}"
                )

                params: Dict[str, Any] = {
                    "id": relationship_id,
                    "source_entity_id": request.source_entity_id,
                    "target_entity_id": request.target_entity_id,
                    "relationship_type": request.relationship_type.value,
                    "strength": request.strength,
                    "confidence_score": request.confidence_score,
                    "context": request.context,
                    "evidence": evidence_str,
                    "metadata": metadata_str,
                    # Coalesced to "" so it can serve as a MERGE key (Cypher
                    # rejects a null key value).
                    "source_document_id": request.source_document_id or "",
                    # Edge org (non-key property; null allowed for org-less edges).
                    "organization_id": request.organization_id,
                }
                if source_document_ids is not None:
                    params["source_document_ids"] = source_document_ids

                result = session.run(query, params)

                record = result.single()
                if not record:
                    scoped = bool(request.organization_id) or (
                        source_document_ids is not None
                    )
                    if scoped:
                        raise RelationshipScopeError(
                            "create_relationship matched no in-scope endpoint pair"
                        )
                    raise RuntimeError("Failed to create relationship")

                # On MATCH the existing edge keeps its original id, so read it
                # back from the result rather than returning the freshly minted
                # uuid (which is only applied ON CREATE).
                resolved_rel = record["r"]
                resolved_id = resolved_rel.get("id", relationship_id)

                processing_time = time.time() - start_time
                logger.info(
                    f"Upserted relationship: {request.source_entity_id} -> {request.target_entity_id} ({resolved_id}) in {processing_time:.3f}s"
                )

                return RelationshipResponse(
                    id=resolved_id,
                    source_entity_id=request.source_entity_id,
                    target_entity_id=request.target_entity_id,
                    relationship_type=request.relationship_type,
                    strength=request.strength,
                    confidence_score=request.confidence_score,
                    context=request.context,
                    evidence=request.evidence,
                    metadata=request.metadata,
                    source_document_id=request.source_document_id,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
        except Exception as e:
            logger.error(f"Error creating relationship: {e}")
            raise

    def get_relationships(
        self,
        entity_id: str,
        relationship_types: Optional[List[RelationshipType]] = None,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
        strict: bool = False,
    ) -> List[RelationshipResponse]:
        """Get all relationships for an entity, optionally scoped to organization documents

        Read errors are swallowed into ``[]`` by default. Pass ``strict=True``
        when an empty list would be acted on as "no relationships" (entity
        merge deletes the duplicate) so an outage raises instead.
        """
        try:
            with self.get_session() as session:
                conditions = ["(source.id = $entity_id OR target.id = $entity_id)"]
                params: Dict[str, Any] = {"entity_id": entity_id}

                if organization_id is not None:
                    conditions.append("source.organization_id = $organization_id")
                    conditions.append("target.organization_id = $organization_id")
                    params["organization_id"] = organization_id
                elif source_document_ids is not None:
                    conditions.append(
                        "source.source_document_id IN $source_document_ids"
                    )
                    conditions.append(
                        "target.source_document_id IN $source_document_ids"
                    )
                    params["source_document_ids"] = source_document_ids

                if relationship_types:
                    type_values = [t.value for t in relationship_types]
                    # Filter by Neo4j label OR by the type property
                    type_condition = " OR ".join(
                        [f"type(r) = '{t}' OR r.type = '{t}'" for t in type_values]
                    )
                    conditions.append(f"({type_condition})")

                where_clause = " AND ".join(conditions)

                query = f"""
                MATCH (source:Entity)-[r]-(target:Entity)
                WHERE {where_clause}
                RETURN r, type(r) AS rel_label,
                       source.id AS source_id, target.id AS target_id
                """

                result = session.run(query, params)
                relationships = []

                for record in result:
                    r = record["r"]
                    rel_label = record["rel_label"]

                    # Determine relationship type: prefer r.type property,
                    # fall back to the Neo4j relationship label
                    raw_type = r.get("type") or rel_label
                    try:
                        rel_type = RelationshipType(raw_type)
                    except ValueError:
                        rel_type = RelationshipType.RELATED_TO

                    relationships.append(
                        RelationshipResponse(
                            id=r.get(
                                "id",
                                f"{record['source_id']}-{rel_label}-{record['target_id']}",
                            ),
                            source_entity_id=record["source_id"],
                            target_entity_id=record["target_id"],
                            relationship_type=rel_type,
                            strength=r.get("strength", r.get("confidence", 0.5)),
                            confidence_score=r.get(
                                "confidence_score", r.get("confidence", 0.5)
                            ),
                            context=r.get("context"),
                            evidence=_parse_evidence(r.get("evidence", [])),
                            metadata=_parse_metadata(r.get("metadata", "{}")),
                            source_document_id=r.get("source_document_id"),
                            created_at=_convert_datetime(r.get("created_at")),
                            updated_at=(
                                _convert_datetime(r.get("updated_at"))
                                if r.get("updated_at") is not None
                                else None
                            ),
                        )
                    )

                return relationships
        except Exception as e:
            logger.error(f"Error retrieving relationships for {entity_id}: {e}")
            if strict:
                raise
            return []

    @staticmethod
    def _to_native_dt(value):
        """Convert a neo4j.time.DateTime to a native datetime (pydantic rejects
        the neo4j type). Pass through None / already-native values."""
        return value.to_native() if hasattr(value, "to_native") else value

    def _record_to_relationship(self, record) -> "RelationshipResponse":
        """Build a RelationshipResponse from a (r, rel_label, source_id, target_id) row."""
        r = record["r"]
        rel_label = record["rel_label"]
        raw_type = r.get("type") or rel_label
        try:
            rel_type = RelationshipType(raw_type)
        except ValueError:
            rel_type = RelationshipType.RELATED_TO
        return RelationshipResponse(
            id=r.get("id", f"{record['source_id']}-{rel_label}-{record['target_id']}"),
            source_entity_id=record["source_id"],
            target_entity_id=record["target_id"],
            relationship_type=rel_type,
            strength=r.get("strength", r.get("confidence", 0.5)),
            confidence_score=r.get("confidence_score", r.get("confidence", 0.5)),
            context=r.get("context"),
            evidence=_parse_evidence(r.get("evidence", [])),
            metadata=_parse_metadata(r.get("metadata", "{}")),
            source_document_id=r.get("source_document_id"),
            created_at=self._to_native_dt(r.get("created_at")) or datetime.utcnow(),
            updated_at=self._to_native_dt(r.get("updated_at")),
        )

    def get_relationships_among(
        self,
        entity_ids: List[str],
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> List[RelationshipResponse]:
        """Return every relationship whose BOTH endpoints are in entity_ids — in a
        SINGLE query. Replaces the per-entity get_relationships loop + Python
        filter (N+1) used by visualization/search endpoints."""
        if not entity_ids:
            return []
        try:
            with self.get_session() as session:
                conditions = ["source.id IN $entity_ids", "target.id IN $entity_ids"]
                params: Dict[str, Any] = {"entity_ids": list(entity_ids)}
                if organization_id is not None:
                    conditions.append("source.organization_id = $organization_id")
                    conditions.append("target.organization_id = $organization_id")
                    params["organization_id"] = organization_id
                elif source_document_ids is not None:
                    conditions.append(
                        "source.source_document_id IN $source_document_ids"
                    )
                    conditions.append(
                        "target.source_document_id IN $source_document_ids"
                    )
                    params["source_document_ids"] = source_document_ids
                where_clause = " AND ".join(conditions)
                query = f"""
                MATCH (source:Entity)-[r]-(target:Entity)
                WHERE {where_clause}
                RETURN DISTINCT r, type(r) AS rel_label,
                       source.id AS source_id, target.id AS target_id
                """
                return [
                    self._record_to_relationship(rec)
                    for rec in session.run(query, params)
                ]
        except Exception as e:
            logger.error(f"Error retrieving relationships among entities: {e}")
            return []

    def get_relationships_for_entities(
        self,
        entity_ids: List[str],
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> List[RelationshipResponse]:
        """Return every relationship INCIDENT to any entity in entity_ids (either
        endpoint), in a SINGLE query — replaces the per-entity get_relationships
        loop (N+1) in search_graph. Unlike get_relationships_among, the other
        endpoint may be outside the set."""
        if not entity_ids:
            return []
        try:
            with self.get_session() as session:
                conditions = ["(source.id IN $entity_ids OR target.id IN $entity_ids)"]
                params: Dict[str, Any] = {"entity_ids": list(entity_ids)}
                if organization_id is not None:
                    conditions.append("source.organization_id = $organization_id")
                    conditions.append("target.organization_id = $organization_id")
                    params["organization_id"] = organization_id
                elif source_document_ids is not None:
                    conditions.append(
                        "source.source_document_id IN $source_document_ids"
                    )
                    conditions.append(
                        "target.source_document_id IN $source_document_ids"
                    )
                    params["source_document_ids"] = source_document_ids
                where_clause = " AND ".join(conditions)
                query = f"""
                MATCH (source:Entity)-[r]-(target:Entity)
                WHERE {where_clause}
                RETURN DISTINCT r, type(r) AS rel_label,
                       source.id AS source_id, target.id AS target_id
                """
                return [
                    self._record_to_relationship(rec)
                    for rec in session.run(query, params)
                ]
        except Exception as e:
            logger.error(f"Error retrieving relationships for entities: {e}")
            return []

    def get_relationship(
        self,
        relationship_id: str,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> Optional[RelationshipResponse]:
        """Get a single relationship by ID, optionally scoped to organization documents"""
        try:
            with self.get_session() as session:
                params: Dict[str, Any] = {"relationship_id": relationship_id}
                scope_frag, scope_params = _two_endpoint_scope(
                    "source", "target", source_document_ids, organization_id
                )
                params.update(scope_params)
                where = (
                    f"\n                    WHERE true{scope_frag}"
                    if scope_frag
                    else ""
                )
                query = f"""
                    MATCH (source:Entity)-[r:RELATED_TO {{id: $relationship_id}}]->(target:Entity){where}
                    RETURN r, source.id AS source_id, target.id AS target_id
                    """

                result = session.run(query, params)
                record = result.single()

                if not record:
                    return None

                r = record["r"]
                return RelationshipResponse(
                    id=r["id"],
                    source_entity_id=record["source_id"],
                    target_entity_id=record["target_id"],
                    relationship_type=RelationshipType(r["type"]),
                    strength=r["strength"],
                    confidence_score=r["confidence_score"],
                    context=r.get("context"),
                    evidence=_parse_evidence(r.get("evidence", [])),
                    metadata=_parse_metadata(r.get("metadata", "{}")),
                    source_document_id=r.get("source_document_id"),
                    created_at=_convert_datetime(r.get("created_at")),
                    updated_at=(
                        _convert_datetime(r.get("updated_at"))
                        if r.get("updated_at") is not None
                        else None
                    ),
                )
        except Exception as e:
            logger.error(f"Error retrieving relationship {relationship_id}: {e}")
            return None

    def delete_relationship(
        self,
        relationship_id: str,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> bool:
        """Delete a relationship by ID, scoped to the caller's tenant.

        organization_id is the preferred scope (indexed, org-wide);
        source_document_ids remains for legacy project-scoped callers.
        Read and delete now honor the same partition so a relationship
        listed inside an org can always be deleted by it (#R5-M7).
        """
        try:
            with self.get_session() as session:
                params: Dict[str, Any] = {"relationship_id": relationship_id}
                tenant_filter, scope_params = _two_endpoint_scope(
                    "source", "target", source_document_ids, organization_id
                )
                params.update(scope_params)
                query = f"""
                MATCH (source:Entity)-[r:RELATED_TO {{id: $relationship_id}}]-(target:Entity)
                WHERE 1=1{tenant_filter}
                DELETE r
                RETURN count(r) as deleted_count
                """

                result = session.run(query, params)
                deleted_count = result.single()["deleted_count"]

                if deleted_count > 0:
                    logger.info(f"Deleted relationship {relationship_id}")
                    return True
                else:
                    logger.warning(
                        f"Relationship {relationship_id} not found for deletion"
                    )
                    return False
        except Exception as e:
            logger.error(f"Error deleting relationship {relationship_id}: {e}")
            return False

    # Graph Search and Traversal
    def find_related_entities(
        self,
        entity_id: str,
        max_depth: int = 2,
        min_strength: float = 0.1,
        limit: int = 50,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> List[EntityResponse]:
        """Find entities related to a given entity, optionally scoped to organization documents"""
        try:
            with self.get_session() as session:
                tenant_filter, scope_params = _two_endpoint_scope(
                    "start", "related", source_document_ids, organization_id
                )
                # Type the traversal to :RELATED_TO and clamp depth: an untyped
                # `[r*1..N]` follows ANY relationship type and an unbounded N
                # explodes into combinatorial fanout on hub nodes. LIMIT alone
                # (applied after expansion) does not bound the work.
                safe_depth = max(1, min(int(max_depth), 5))
                query = f"""
                MATCH (start:Entity {{id: $entity_id}})
                MATCH (start)-[r:RELATED_TO*1..{safe_depth}]-(related:Entity)
                WHERE related.id <> $entity_id
                  AND all(rel in r WHERE coalesce(rel.strength, 1.0) >= $min_strength){tenant_filter}
                RETURN DISTINCT related
                ORDER BY related.id
                LIMIT $limit
                """

                params: Dict[str, Any] = {
                    "entity_id": entity_id,
                    "min_strength": min_strength,
                    "limit": limit,
                    **scope_params,
                }

                result = session.run(query, params)

                entities = []
                for node in result:
                    e = node["related"]
                    if e["id"] != entity_id:  # Exclude the start entity
                        entities.append(
                            EntityResponse(
                                id=e["id"],
                                name=e["name"],
                                entity_type=_safe_entity_type(e["type"]),
                                confidence_score=e["confidence_score"],
                                extraction_method=_safe_extraction_method(
                                    e["extraction_method"]
                                ),
                                position=e.get("position"),
                                context=e.get("context"),
                                metadata=_parse_metadata(e.get("metadata", "{}")),
                                source_document_id=e.get("source_document_id"),
                                created_at=_convert_datetime(e["created_at"]),
                                updated_at=(
                                    _convert_datetime(e["updated_at"])
                                    if e.get("updated_at")
                                    else None
                                ),
                            )
                        )

                return entities
        except Exception as e:
            logger.error(f"Error finding related entities for {entity_id}: {e}")
            return []

    def get_neighborhood(
        self,
        entity_id: str,
        max_depth: int = 2,
        min_strength: float = 0.1,
        limit: int = 50,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get neighborhood entities and relationships in a single query.

        Returns both entities and the relationships connecting them,
        including paths through intermediate non-Entity nodes.
        """
        try:
            with self.get_session() as session:
                tenant_filter, scope_params = _two_endpoint_scope(
                    "start", "related", source_document_ids, organization_id
                )
                # Type the traversal + clamp depth (untyped `[*1..N]` followed
                # ANY relationship and unbounded N caused combinatorial fanout
                # on hub nodes).
                safe_depth = max(1, min(int(max_depth), 5))
                # Same OR semantics as _two_endpoint_scope: legacy NULL-org
                # nodes reachable via source_document_ids must stay traversable
                # when both scopes are supplied.
                node_preds: List[str] = []
                if organization_id is not None:
                    node_preds.append("n.organization_id = $organization_id")
                if source_document_ids is not None:
                    node_preds.append("n.source_document_id IN $source_document_ids")
                if len(node_preds) > 1:
                    path_node_predicate = "(" + " OR ".join(node_preds) + ")"
                else:
                    path_node_predicate = "".join(node_preds)
                node_scope = (
                    "\n              AND all(n IN nodes(path) "
                    f"WHERE {path_node_predicate})"
                    if path_node_predicate
                    else ""
                )
                query = f"""
                MATCH (start:Entity {{id: $entity_id}})
                MATCH path = (start)-[:RELATED_TO*1..{safe_depth}]-(related:Entity)
                WHERE related.id <> $entity_id
                  AND all(rel in relationships(path)
                      WHERE coalesce(rel.strength, rel.confidence, 1.0) >= $min_strength){tenant_filter}{node_scope}
                WITH DISTINCT related, path, length(path) AS hops
                ORDER BY hops
                WITH related,
                     collect(path)[0] AS shortest_path
                WITH related,
                     shortest_path,
                     [rel in relationships(shortest_path) |
                       {{label: coalesce(rel.type, type(rel)),
                         source_entity_id: startNode(rel).id,
                         target_entity_id: endNode(rel).id,
                         strength: coalesce(rel.strength, rel.confidence, 1.0),
                         confidence: coalesce(rel.confidence_score, rel.confidence, 0.5)}}
                     ] AS path_rels,
                     [n in nodes(shortest_path) | n.id] AS path_node_ids
                RETURN related, path_rels, path_node_ids
                ORDER BY length(shortest_path), related.id
                LIMIT $limit
                """

                params: Dict[str, Any] = {
                    "entity_id": entity_id,
                    "min_strength": min_strength,
                    "limit": limit,
                    **scope_params,
                }

                result = session.run(query, params)

                entities = []
                edges = []
                seen_edge_keys = set()
                seen_entity_ids = {entity_id}
                intermediate_ids: set = set()

                def _node_to_entity(n) -> EntityResponse:
                    return EntityResponse(
                        id=n["id"],
                        name=n["name"],
                        entity_type=_safe_entity_type(n["type"]),
                        confidence_score=n.get(
                            "confidence_score", n.get("confidence", 0.5)
                        ),
                        extraction_method=_safe_extraction_method(
                            n.get("extraction_method", "unknown")
                        ),
                        position=n.get("position"),
                        context=n.get("context"),
                        metadata=_parse_metadata(n.get("metadata", "{}")),
                        source_document_id=n.get("source_document_id"),
                        created_at=_convert_datetime(n.get("created_at")),
                        updated_at=(
                            _convert_datetime(n["updated_at"])
                            if n.get("updated_at")
                            else None
                        ),
                    )

                for record in result:
                    e = record["related"]
                    path_rels = record["path_rels"]
                    path_node_ids = record["path_node_ids"]

                    if e["id"] not in seen_entity_ids:
                        seen_entity_ids.add(e["id"])
                        entities.append(_node_to_entity(e))

                    # Intermediate nodes on the path (between start and related)
                    # — previously dropped. Collect to fetch + include.
                    for nid in path_node_ids[1:-1]:
                        if nid not in seen_entity_ids:
                            intermediate_ids.add(nid)

                    # Emit the REAL per-hop edges (was a single synthetic
                    # start->related edge with product strength + first-hop type).
                    for rel in path_rels:
                        src = rel["source_entity_id"]
                        tgt = rel["target_entity_id"]
                        label = rel.get("label") or "RELATED_TO"
                        ek = (src, tgt, label)
                        if ek in seen_edge_keys:
                            continue
                        seen_edge_keys.add(ek)
                        try:
                            rel_type = RelationshipType(label)
                        except ValueError:
                            rel_type = RelationshipType.RELATED_TO
                        edges.append(
                            RelationshipResponse(
                                id=f"{src}-{label}-{tgt}",
                                source_entity_id=src,
                                target_entity_id=tgt,
                                relationship_type=rel_type,
                                strength=round(rel.get("strength", 1.0), 3),
                                confidence_score=round(rel.get("confidence", 0.5), 3),
                                context=None,
                                evidence=[],
                                metadata={},
                                source_document_id=None,
                                created_at=datetime.utcnow(),
                                updated_at=None,
                            )
                        )

                # Fetch the intermediate nodes' details in ONE batched query.
                missing = [
                    nid for nid in intermediate_ids if nid not in seen_entity_ids
                ]
                if missing:
                    refetch_scope = (
                        f" AND {path_node_predicate}" if path_node_predicate else ""
                    )
                    for row in session.run(
                        "MATCH (n:Entity) WHERE n.id IN $ids"
                        f"{refetch_scope} RETURN n",
                        {"ids": missing, **scope_params},
                    ):
                        entities.append(_node_to_entity(row["n"]))

                return {"entities": entities, "relationships": edges}
        except Exception as e:
            logger.error(f"Error getting neighborhood for {entity_id}: {e}")
            return {"entities": [], "relationships": []}

    def find_paths(
        self,
        source_id: str,
        target_id: str,
        max_depth: int = 3,
        min_strength: float = 0.1,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> List[GraphPath]:
        """Find paths between two entities, optionally scoped to organization documents"""
        try:
            with self.get_session() as session:
                tenant_filter, scope_params = _two_endpoint_scope(
                    "start", "end", source_document_ids, organization_id
                )
                # Type the traversal to :RELATED_TO and clamp depth: an untyped
                # `[*1..N]` follows ANY relationship type and an unbounded N
                # explodes into combinatorial fanout on hub nodes (matches the
                # find_related_entities / get_neighborhood hardening).
                safe_depth = max(1, min(int(max_depth), 5))
                node_scope = (
                    "\n              AND all(n IN nodes(path) "
                    "WHERE n.organization_id = $organization_id)"
                    if organization_id is not None
                    else ""
                )
                query = f"""
                MATCH path = (start:Entity {{id: $source_id}})-[:RELATED_TO*1..{safe_depth}]-(end:Entity {{id: $target_id}})
                WHERE all(rel in relationships(path) WHERE coalesce(rel.strength, 1.0) >= $min_strength){tenant_filter}{node_scope}
                RETURN path, length(path) as path_length
                ORDER BY path_length, reduce(strength = 1.0, rel in relationships(path) | strength * coalesce(rel.strength, 1.0)) DESC
                LIMIT 10
                """

                params: Dict[str, Any] = {
                    "source_id": source_id,
                    "target_id": target_id,
                    "min_strength": min_strength,
                    **scope_params,
                }

                result = session.run(query, params)

                paths = []
                for record in result:
                    path_obj = record["path"]
                    path_length = record["path_length"]

                    # Extract entities and relationships from path
                    entities = []
                    relationships = []
                    total_strength = 1.0

                    for node in path_obj.nodes:
                        e = node
                        entities.append(
                            EntityResponse(
                                id=e["id"],
                                name=e["name"],
                                entity_type=_safe_entity_type(e["type"]),
                                confidence_score=e["confidence_score"],
                                extraction_method=_safe_extraction_method(
                                    e["extraction_method"]
                                ),
                                position=e.get("position"),
                                context=e.get("context"),
                                metadata=_parse_metadata(e.get("metadata", "{}")),
                                source_document_id=e.get("source_document_id"),
                                created_at=_convert_datetime(e["created_at"]),
                                updated_at=(
                                    _convert_datetime(e["updated_at"])
                                    if e.get("updated_at")
                                    else None
                                ),
                            )
                        )

                    for rel in path_obj.relationships:
                        r = rel
                        # source/target are the graph ENDPOINTS, not stored
                        # properties — reading r["source_entity_id"] KeyError'd
                        # and the whole path was swallowed (returned []). Read
                        # the endpoint node ids; .get() the rest with defaults.
                        strength = r.get("strength", r.get("confidence", 0.5))
                        total_strength *= strength
                        relationships.append(
                            RelationshipResponse(
                                id=r.get(
                                    "id", f"{rel.start_node['id']}-{rel.end_node['id']}"
                                ),
                                source_entity_id=rel.start_node["id"],
                                target_entity_id=rel.end_node["id"],
                                relationship_type=_safe_relationship_type(
                                    r.get("type")
                                ),
                                strength=strength,
                                confidence_score=r.get("confidence_score", strength),
                                context=r.get("context"),
                                evidence=_parse_evidence(r.get("evidence", [])),
                                metadata=_parse_metadata(r.get("metadata", "{}")),
                                source_document_id=r.get("source_document_id"),
                                created_at=_convert_datetime(r.get("created_at")),
                                updated_at=(
                                    _convert_datetime(r["updated_at"])
                                    if r.get("updated_at")
                                    else None
                                ),
                            )
                        )

                    paths.append(
                        GraphPath(
                            entities=entities,
                            relationships=relationships,
                            total_strength=total_strength,
                            path_length=path_length,
                            # guard min() on a zero-length (no-relationship) path
                            confidence_score=min(
                                (rel.confidence_score for rel in relationships),
                                default=1.0,
                            ),
                        )
                    )

                return paths
        except Exception as e:
            logger.error(
                f"Error finding paths between {source_id} and {target_id}: {e}"
            )
            return []

    # Batch Operations
    def create_entities_batch(
        self,
        request: BatchEntityRequest,
        source_document_ids: Optional[List[str]] = None,
    ) -> BatchEntityResponse:
        """Create multiple entities and relationships in a batch"""
        start_time = time.time()
        response = BatchEntityResponse()

        try:
            with self.get_session() as session:
                with session.begin_transaction() as tx:
                    # Create entities in ONE UNWIND batch (single round-trip for
                    # all entities); fall back to per-entity on any error.
                    batched = self._batch_merge_entities(tx, request.entities)
                    if batched is not None:
                        response.created_entities.extend(batched)
                    else:
                        for entity_req in request.entities:
                            try:
                                entity = self._create_entity_in_transaction(
                                    tx, entity_req
                                )
                                if entity:
                                    response.created_entities.append(entity)
                            except Exception as e:
                                response.errors.append(
                                    {
                                        "type": "entity_creation_error",
                                        "data": entity_req.dict(),
                                        "error": str(e),
                                    }
                                )

                    # Create relationships
                    for rel_req in request.relationships:
                        try:
                            relationship = self._create_relationship_in_transaction(
                                tx, rel_req
                            )
                            if relationship:
                                response.created_relationships.append(relationship)
                        except Exception as e:
                            response.errors.append(
                                {
                                    "type": "relationship_creation_error",
                                    "data": rel_req.dict(),
                                    "error": str(e),
                                }
                            )

            response.processing_time = time.time() - start_time
            logger.info(
                f"Batch processing completed: {len(response.created_entities)} entities, {len(response.created_relationships)} relationships in {response.processing_time:.3f}s"
            )

            return response
        except Exception as e:
            logger.error(f"Error in batch processing: {e}")
            response.processing_time = time.time() - start_time
            response.errors.append({"type": "batch_processing_error", "error": str(e)})
            return response

    def _batch_merge_entities(self, tx, entities) -> Optional[List[EntityResponse]]:
        """MERGE all entities in a single UNWIND (one round-trip) via
        apoc.merge.node (dynamic :Entity:<type> label). Returns the created
        EntityResponse list, or None if anything goes wrong (caller falls back
        to the per-entity path). Idempotent — same (canonical_key, type) merges.
        """
        valid = [e for e in entities if e.name and e.name.strip()]
        if not valid:
            return []
        rows = []
        for i, e in enumerate(valid):
            e.name = e.name.strip()
            org_key = getattr(e, "organization_id", None) or ""
            rows.append(
                {
                    "idx": i,
                    "etype": e.entity_type.value,
                    "canonical_key": e.name.lower(),
                    "org_key": org_key,
                    "props": {
                        "id": str(uuid.uuid4()),
                        "name": e.name,
                        "confidence_score": e.confidence_score,
                        "extraction_method": e.extraction_method.value,
                        "position": e.position,
                        "context": e.context,
                        "metadata": json.dumps(e.metadata) if e.metadata else "{}",
                        "source_document_id": e.source_document_id,
                        # Equal to the identity-map org so the stored property
                        # never desyncs from the MERGE key.
                        "organization_id": org_key,
                    },
                }
            )
        # organization_id is part of the apoc.merge identity map (matches the
        # entity_canonical_org_unique constraint) so two orgs' same name+type
        # entities are separate nodes. Coalesced to "" since the key can't be null.
        query = """
        UNWIND $rows AS row
        CALL apoc.merge.node(['Entity', row.etype],
            {canonical_key: row.canonical_key, type: row.etype, organization_id: row.org_key},
            row.props) YIELD node
        SET node.created_at = coalesce(node.created_at, datetime()),
            node.updated_at = datetime(),
            node.name = row.props.name,
            node.confidence_score = CASE
                WHEN row.props.confidence_score > coalesce(node.confidence_score, 0.0)
                THEN row.props.confidence_score ELSE node.confidence_score END
        RETURN row.idx AS idx, node.id AS id
        """
        try:
            id_by_idx = {rec["idx"]: rec["id"] for rec in tx.run(query, {"rows": rows})}
        except Exception as e:
            logger.warning(
                "Batch UNWIND entity merge failed, falling back per-entity: %s", e
            )
            return None
        out = []
        for row, ent in zip(rows, valid):
            out.append(
                EntityResponse(
                    id=id_by_idx.get(row["idx"], row["props"]["id"]),
                    name=ent.name,
                    entity_type=ent.entity_type,
                    confidence_score=ent.confidence_score,
                    extraction_method=ent.extraction_method,
                    position=ent.position,
                    context=ent.context,
                    metadata=ent.metadata,
                    source_document_id=ent.source_document_id,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )
        return out

    def _create_entity_in_transaction(
        self, tx, request: CreateEntityRequest
    ) -> Optional[EntityResponse]:
        """Helper to create entity within a transaction"""
        if not request.name or not request.name.strip():
            raise ValueError("Entity name cannot be blank or whitespace-only")
        request.name = request.name.strip()

        entity_id = str(uuid.uuid4())
        canonical_key = request.name.strip().lower()
        # Idempotent MERGE keyed on (canonical_key, type, organization_id) —
        # same identity as create_entity — so batch re-ingest and concurrent
        # ingest of the same entity dedup onto one node (backed by the
        # entity_canonical_org_unique constraint) while keeping two orgs'
        # same-name entities separate. org_key coalesced to "" (Cypher can't
        # MERGE on null); the stored property equals the key.
        query = f"""
        MERGE (e:Entity:{request.entity_type.value} {{canonical_key: $canonical_key, type: $entity_type, organization_id: $org_key}})
        ON CREATE SET
            e.id = $id,
            e.name = $name,
            e.confidence_score = $confidence_score,
            e.extraction_method = $extraction_method,
            e.position = $position,
            e.context = $context,
            e.metadata = $metadata,
            e.source_document_id = $source_document_id,
            e.created_at = datetime(),
            e.updated_at = datetime()
        ON MATCH SET
            e.name = $name,
            e.updated_at = datetime(),
            e.confidence_score = CASE
                WHEN $confidence_score > e.confidence_score THEN $confidence_score
                ELSE e.confidence_score END
        RETURN e.id AS resolved_id
        """

        result = tx.run(
            query,
            {
                "id": entity_id,
                "canonical_key": canonical_key,
                "name": request.name,
                "entity_type": request.entity_type.value,
                "confidence_score": request.confidence_score,
                "extraction_method": request.extraction_method.value,
                "position": request.position,
                "context": request.context,
                "metadata": json.dumps(request.metadata) if request.metadata else "{}",
                "source_document_id": request.source_document_id,
                "org_key": getattr(request, "organization_id", None) or "",
            },
        )

        node = result.single()
        if not node:
            return None

        return EntityResponse(
            id=node["resolved_id"],
            name=request.name,
            entity_type=request.entity_type,
            confidence_score=request.confidence_score,
            extraction_method=request.extraction_method,
            position=request.position,
            context=request.context,
            metadata=request.metadata,
            source_document_id=request.source_document_id,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

    def _create_relationship_in_transaction(
        self, tx, request: CreateRelationshipRequest
    ) -> Optional[RelationshipResponse]:
        """Helper to create relationship within a transaction"""
        relationship_id = str(uuid.uuid4())
        # Scope both endpoints to the caller's org when known (mirrors
        # create_relationship). Batch ingest uses this path, so fall back to the
        # edge's own source_document_id when no org is set — both endpoints must
        # share that document — instead of leaving it fully unscoped. MERGE key
        # unchanged ((type, source_document_id)).
        if request.organization_id:
            endpoint_filter = (
                "\nWHERE source.organization_id = $organization_id"
                " AND target.organization_id = $organization_id"
            )
        elif request.source_document_id:
            endpoint_filter = (
                "\nWHERE source.source_document_id = $source_document_id"
                " AND target.source_document_id = $source_document_id"
            )
        else:
            endpoint_filter = ""
        # MERGE on (source, target, type, source_document_id) for idempotent
        # re-ingest while preserving per-document provenance — see the note in
        # create_relationship. LIMIT 1 keeps the read to one row (it does not
        # consolidate legacy duplicate edges).
        query = f"""
        MATCH (source:Entity {{id: $source_entity_id}})
        MATCH (target:Entity {{id: $target_entity_id}}){endpoint_filter}
        MERGE (source)-[r:RELATED_TO {{
            type: $relationship_type,
            source_document_id: $source_document_id
        }}]->(target)
        ON CREATE SET
            r.id = $id,
            r.strength = $strength,
            r.confidence_score = $confidence_score,
            r.context = $context,
            r.evidence = $evidence,
            r.metadata = $metadata,
            r.organization_id = $organization_id,
            r.created_at = datetime(),
            r.updated_at = datetime()
        ON MATCH SET
            r.strength = $strength,
            r.confidence_score = $confidence_score,
            r.context = $context,
            r.evidence = $evidence,
            r.metadata = $metadata,
            r.organization_id = coalesce(r.organization_id, $organization_id),
            r.updated_at = datetime()
        RETURN r
        LIMIT 1
        """

        # Serialize evidence and metadata to JSON strings (Neo4j only accepts primitives)
        evidence_str = json.dumps(request.evidence) if request.evidence else "[]"
        metadata_str = json.dumps(request.metadata) if request.metadata else "{}"

        result = tx.run(
            query,
            {
                "id": relationship_id,
                "source_entity_id": request.source_entity_id,
                "target_entity_id": request.target_entity_id,
                "relationship_type": request.relationship_type.value,
                "strength": request.strength,
                "confidence_score": request.confidence_score,
                "context": request.context,
                "evidence": evidence_str,
                "metadata": metadata_str,
                # Coalesced to "" so it can serve as a MERGE key.
                "source_document_id": request.source_document_id or "",
                "organization_id": request.organization_id,
            },
        )

        rel = result.single()
        if not rel:
            return None

        # On MATCH the existing edge keeps its original id (the new uuid is only
        # applied ON CREATE), so read it back from the result.
        resolved_id = rel["r"].get("id", relationship_id)

        return RelationshipResponse(
            id=resolved_id,
            source_entity_id=request.source_entity_id,
            target_entity_id=request.target_entity_id,
            relationship_type=request.relationship_type,
            strength=request.strength,
            confidence_score=request.confidence_score,
            context=request.context,
            evidence=request.evidence,
            metadata=request.metadata,
            source_document_id=request.source_document_id,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

    # Analytics and Statistics
    def get_graph_analytics(
        self,
        source_document_ids: Optional[List[str]] = None,
        organization_id: Optional[str] = None,
    ) -> GraphAnalytics:
        """Get comprehensive graph analytics, optionally scoped to organization documents"""
        try:
            with self.get_session() as session:
                # Build tenant-scoped queries. Unlike the traversal helpers this
                # ANDs the two scopes: a doc-id list here comes from an explicit
                # ?project_id= filter and must NARROW the org counts, not act as
                # a fallback (the old elif made the doc-id branch dead, so a
                # project filter silently returned org-wide counts).
                params: Dict[str, Any] = {}
                scope_preds: List[str] = []
                if organization_id is not None:
                    scope_preds.append("{alias}.organization_id = $organization_id")
                    params["organization_id"] = organization_id
                if source_document_ids is not None:
                    scope_preds.append(
                        "{alias}.source_document_id IN $source_document_ids"
                    )
                    params["source_document_ids"] = source_document_ids
                if scope_preds:
                    joined = " AND ".join(scope_preds)
                    entity_where = "WHERE " + joined.format(alias="e")
                    rel_where = "WHERE " + joined.format(alias="source")
                else:
                    # No tenant scope at all — counts span every organization.
                    # Log loudly: a None org reaching here means a caller dropped
                    # scoping, not a legitimate cross-tenant query.
                    logger.warning(
                        "get_graph_analytics running UNSCOPED (no organization_id "
                        "or source_document_ids) — returning all-org counts"
                    )
                    entity_where = ""
                    rel_where = ""

                # Get the type distributions, then DERIVE the totals from them
                # (sum of per-type counts) instead of running two extra full
                # count scans — 5 scans -> 3.
                entity_types_result = session.run(
                    f"MATCH (e:Entity) {entity_where} RETURN e.type as type, count(e) as count",
                    params,
                )
                entity_type_counts = {
                    record["type"]: record["count"] for record in entity_types_result
                }
                total_entities = sum(entity_type_counts.values())

                rel_types_result = session.run(
                    f"MATCH (source:Entity)-[r:RELATED_TO]->() {rel_where} RETURN r.type as type, count(r) as count",
                    params,
                )
                relationship_type_counts = {
                    record["type"]: record["count"] for record in rel_types_result
                }
                total_relationships = sum(relationship_type_counts.values())

                # Calculate average degree
                if total_entities > 0:
                    avg_degree = (2 * total_relationships) / total_entities
                else:
                    avg_degree = 0.0

                # Exact (weakly) connected components via union-find over the
                # scoped edge list. GDS procedures are skipped — they OOM in
                # constrained containers. Neighbours outside the scope are
                # ignored so components never span tenants/projects.
                # ponytail: pulls every scoped edge into memory, O(E); move to
                # GDS WCC on a dedicated instance if org graphs get huge.
                connected_components = 0
                largest_component_size = 0
                clustering_coefficient = 0.0

                try:
                    rows = session.run(
                        f"""
                        MATCH (e:Entity) {entity_where}
                        OPTIONAL MATCH (e)-[:RELATED_TO]->(t:Entity)
                        RETURN e.id AS id, collect(t.id) AS neighbors
                        """,
                        params,
                    )
                    adjacency = {row["id"]: row["neighbors"] for row in rows}
                    parent = {node: node for node in adjacency}

                    def _find(node: Any) -> Any:
                        while parent[node] != node:
                            parent[node] = parent[parent[node]]
                            node = parent[node]
                        return node

                    for node, neighbors in adjacency.items():
                        for neighbor in neighbors:
                            if neighbor in parent:
                                parent[_find(node)] = _find(neighbor)

                    sizes: Dict[Any, int] = {}
                    for node in parent:
                        root = _find(node)
                        sizes[root] = sizes.get(root, 0) + 1
                    connected_components = len(sizes)
                    largest_component_size = max(sizes.values(), default=0)
                except Exception as exc:
                    logger.warning(f"Graph component analysis failed: {exc}")

                return GraphAnalytics(
                    total_entities=total_entities,
                    total_relationships=total_relationships,
                    entity_type_counts=entity_type_counts,
                    relationship_type_counts=relationship_type_counts,
                    average_degree=avg_degree,
                    connected_components=connected_components,
                    largest_component_size=largest_component_size,
                    clustering_coefficient=clustering_coefficient,
                )
        except Exception as e:
            logger.error(f"Error getting graph analytics: {e}")
            return GraphAnalytics()

    def get_health_status(
        self, organization_id: Optional[str] = None
    ) -> GraphHealthStatus:
        """Get health status of the graph database.

        When ``organization_id`` is supplied the node/relationship counts (and
        database size) are scoped to that tenant. These counts feed a
        user-facing panel, so a global ``MATCH (n)`` would leak the whole
        multi-tenant graph's size to every tenant. Called with no org only from
        ops/standalone scripts, which keep the global counts.
        """
        start_time = time.time()
        try:
            with self.get_session() as session:
                # Test basic connectivity
                start_time = time.time()
                session.run("RETURN 1")
                response_time_ms = (time.time() - start_time) * 1000

                # Get database info
                version_result = session.run(
                    "CALL dbms.components() YIELD name, versions RETURN versions[0] as version"
                ).single()
                if organization_id:
                    # Strict org-equality (no source_document_id fallback like
                    # _entity_scope_predicate): this is a best-effort display
                    # panel, and every deployed env is org-backfilled. New writes
                    # always stamp organization_id, so counts stay accurate.
                    node_count_result = session.run(
                        "MATCH (e:Entity {organization_id: $org}) "
                        "RETURN count(e) as count",
                        org=organization_id,
                    ).single()
                    rel_count_result = session.run(
                        "MATCH (:Entity {organization_id: $org})-[r]->"
                        "(:Entity {organization_id: $org}) RETURN count(r) as count",
                        org=organization_id,
                    ).single()
                else:
                    node_count_result = session.run(
                        "MATCH (n) RETURN count(n) as count"
                    ).single()
                    rel_count_result = session.run(
                        "MATCH ()-[r]->() RETURN count(r) as count"
                    ).single()

                # Get indexes and constraints
                index_result = session.run(
                    "SHOW INDEXES YIELD type RETURN count(*) as count"
                ).single()
                constraint_result = session.run(
                    "SHOW CONSTRAINTS YIELD type RETURN count(*) as count"
                ).single()

                # Calculate database size
                database_size = None
                try:
                    size_result = session.run(
                        """
                        CALL dbms.database.details($db_name) YIELD sizeOnDisk
                        RETURN sizeOnDisk
                        """,
                        db_name=self.database,
                    ).single()
                    if size_result and size_result["sizeOnDisk"]:
                        database_size = str(size_result["sizeOnDisk"])
                except Exception:
                    # Fallback: try to get store sizes from dbms.queryJmx
                    try:
                        jmx_result = session.run("""
                            CALL dbms.queryJmx('org.neo4j:*')
                            YIELD name, attributes
                            WHERE name CONTAINS 'Store sizes'
                            RETURN attributes
                            """).single()
                        if jmx_result and jmx_result["attributes"]:
                            total_size = (
                                jmx_result["attributes"]
                                .get("TotalStoreSize", {})
                                .get("value", 0)
                            )
                            if total_size:
                                # Format as human-readable
                                if total_size >= 1024 * 1024 * 1024:
                                    database_size = (
                                        f"{total_size / (1024 * 1024 * 1024):.2f} GB"
                                    )
                                elif total_size >= 1024 * 1024:
                                    database_size = (
                                        f"{total_size / (1024 * 1024):.2f} MB"
                                    )
                                else:
                                    database_size = f"{total_size / 1024:.2f} KB"
                    except Exception as size_error:
                        logger.debug(f"Could not get database size: {size_error}")

                # Calculate uptime
                uptime = None
                try:
                    # Query server start time from JMX
                    uptime_result = session.run("""
                        CALL dbms.queryJmx('java.lang:type=Runtime')
                        YIELD name, attributes
                        RETURN attributes.Uptime.value as uptimeMs
                        """).single()
                    if uptime_result and uptime_result["uptimeMs"]:
                        uptime_ms = uptime_result["uptimeMs"]
                        uptime_seconds = uptime_ms // 1000
                        days = uptime_seconds // 86400
                        hours = (uptime_seconds % 86400) // 3600
                        minutes = (uptime_seconds % 3600) // 60
                        if days > 0:
                            uptime = f"{days}d {hours}h {minutes}m"
                        elif hours > 0:
                            uptime = f"{hours}h {minutes}m"
                        else:
                            uptime = f"{minutes}m"
                except Exception as uptime_error:
                    logger.debug(f"Could not get uptime: {uptime_error}")

                # Whole-DB store size is a cross-tenant signal; don't expose it
                # on the per-tenant health panel.
                if organization_id:
                    database_size = None

                return GraphHealthStatus(
                    status="healthy",
                    neo4j_version=(
                        version_result["version"] if version_result else "unknown"
                    ),
                    database_size=database_size,
                    node_count=node_count_result["count"] if node_count_result else 0,
                    relationship_count=(
                        rel_count_result["count"] if rel_count_result else 0
                    ),
                    index_count=index_result["count"] if index_result else 0,
                    constraint_count=(
                        constraint_result["count"] if constraint_result else 0
                    ),
                    uptime=uptime,
                    last_error=None,
                    response_time_ms=response_time_ms,
                )
        except Exception as e:
            logger.error(f"Error getting health status: {e}")
            return GraphHealthStatus(
                status="unhealthy",
                neo4j_version="unknown",
                node_count=0,
                relationship_count=0,
                index_count=0,
                constraint_count=0,
                response_time_ms=(time.time() - start_time) * 1000,
                last_error=str(e),
            )

    # --- Integration adapter methods ---
    # These adapt the KG service to match contracts expected by search, document
    # processing, and multi-agent callers.

    def search(
        self,
        search_request: Any = None,
        user_id: str = None,
        organization_id: str = None,
        db: Any = None,
    ) -> Any:
        """Search adapter matching the contract used by search.py and hybrid_search_service.

        Returns a SearchResponse-compatible object so the search API can treat
        knowledge-graph search identically to fulltext/vector search.
        """
        from src.models.document import DocumentType
        from src.models.search_schemas import SearchResponse
        from src.models.search_schemas import SearchResult as SearchResultModel
        from src.models.search_schemas import SearchType as SearchTypeEnum

        start_time = time.time()

        # Derive tenant scope
        source_document_ids = None
        if organization_id and db:
            try:
                from src.models.document import Document

                source_document_ids = [
                    str(doc_id)
                    for (doc_id,) in db.query(Document.id)
                    .filter(Document.organization_id == organization_id)
                    .all()
                ]
            except Exception:
                pass

        query_text = search_request.query if search_request else ""
        limit = getattr(search_request, "limit", 20)

        # Pass organization_id directly, not only the derived doc-id list: if the
        # doc-id lookup above threw, source_document_ids is None and passing only
        # that would run unscoped across all tenants. The indexed organization_id
        # equality is the primary scope.
        entities = self.search_entities(
            query_text,
            limit=limit,
            source_document_ids=source_document_ids,
            organization_id=organization_id,
        )

        results = []
        now = datetime.utcnow()
        for entity in entities:
            results.append(
                SearchResultModel(
                    document_id=entity.source_document_id or str(uuid.uuid4()),
                    title=entity.name,
                    document_type=DocumentType.TEXT,
                    content_preview=entity.context or "",
                    snippets=[],
                    relevance_score=min(entity.confidence_score, 1.0),
                    file_size_bytes=0,
                    created_at=entity.created_at or now,
                    updated_at=entity.updated_at or now,
                    processing_status="completed",
                    tags=[entity.entity_type.value],
                    is_public=False,
                    uploaded_by_user_id=user_id or "",
                    organization_id=organization_id or "",
                    metadata={
                        "entity_id": entity.id,
                        "entity_type": entity.entity_type.value,
                    },
                )
            )

        search_time_ms = (time.time() - start_time) * 1000
        return SearchResponse(
            query=query_text,
            search_id=str(uuid.uuid4()),
            search_type=(
                SearchTypeEnum.KNOWLEDGE_GRAPH
                if hasattr(SearchTypeEnum, "KNOWLEDGE_GRAPH")
                else "knowledge_graph"
            ),
            results=results,
            total_results=len(results),
            returned_results=len(results),
            search_time_ms=search_time_ms,
            limit=limit,
            offset=getattr(search_request, "offset", 0),
            has_more=False,
        )

    def create_entity_node(
        self,
        entity_text: str,
        entity_type: str,
        document_id: str = None,
        confidence: float = 0.8,
        metadata: str = "",
        organization_id: str = None,
    ) -> Optional[str]:
        """Adapter for legacy callers that expect create_entity_node.

        Returns the entity ID on success, None on failure.
        """
        try:
            request = CreateEntityRequest(
                name=entity_text.strip(),
                entity_type=_safe_entity_type(entity_type),
                confidence_score=confidence,
                extraction_method=ExtractionMethod.LLM_EXTRACTION,
                context=metadata if isinstance(metadata, str) else None,
                source_document_id=document_id,
                organization_id=organization_id,
            )
            result = self.create_entity(request)
            return result.id if result else None
        except Exception as e:
            logger.error(f"Error in create_entity_node adapter: {e}")
            return None


# Global instance
knowledge_graph_service = KnowledgeGraphService()
