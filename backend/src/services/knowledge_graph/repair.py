"""Idempotent per-document Neo4j re-index core (audit D1 / P2.3).

Extracted from ``scripts/repair_kg.py`` so the same logic drives both:

- the manual CLI (``python -m scripts.repair_kg``), now a thin wrapper, and
- the scheduled satellite reconciler (``src.tasks.reconcile_tasks``), which
  re-drives documents whose ingestion fan-out recorded
  ``neo4j_index_status='failed'``.

The extraction call is also *fixed* here: the script called
``EntityExtractionService.extract_entities(text, doc_id)`` — a method that
does not exist (the service exposes
``extract_entities_and_relationships_from_text(document, text)``), so every
CLI run failed per-document inside its blanket ``except``. The core uses the
real API, re-derives entities + relationships from ``content_text``, and
upserts them into the graph (``BatchEntityRequest(upsert=True)``) — safe to
re-run any number of times.

Tenancy: every entity/relationship request carries the document's
``organization_id`` (the script omitted it, writing unscoped graph nodes).

Never raises: failures are reported in :class:`GraphRepairOutcome` so callers
(the reconciler, the CLI) can record honest per-document status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from src.models.document import Document
from src.models.graph import (
    BatchEntityRequest,
    CreateEntityRequest,
    CreateRelationshipRequest,
    EntityType,
    ExtractionMethod,
    RelationshipType,
)
from src.services.knowledge_graph.knowledge_graph_service import (
    GraphExtractor,
    knowledge_graph_service,
)

logger = logging.getLogger(__name__)

__all__ = ["GraphRepairOutcome", "repair_document_graph", "safe_enum_value"]


def safe_enum_value(enum_cls, value, default):
    """Safely convert a string to an enum member (case-insensitive fallback)."""
    try:
        return enum_cls(value)
    except ValueError:
        for member in enum_cls:
            if member.value.lower() == str(value).lower():
                return member
        return default


# models.entity.ExtractionMethod values -> graph ExtractionMethod. The spaCy
# pipeline emits SPACY/REGEX; anything else degrades to RULE_BASED (the
# fallback the original script used).
_EXTRACTION_METHOD_MAP = {
    "spacy": ExtractionMethod.SPACY_NER,
    "regex": ExtractionMethod.PATTERN_MATCHING,
}


@dataclass(frozen=True)
class GraphRepairOutcome:
    """Truthful result of one document's graph re-index attempt."""

    document_id: str
    entities_found: int = 0
    relationships_found: int = 0
    entities_created: int = 0
    relationships_created: int = 0
    errors: int = 0
    skipped_reason: Optional[str] = None  # e.g. "no_content"
    error_message: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True only for a clean, complete re-index.

        A vacuous run (nothing extractable, no errors) is ok — the graph is
        not missing anything. A skip (no content) or any error keeps the
        document reconcilable.
        """
        return (
            self.error_message is None
            and self.skipped_reason is None
            and self.errors == 0
        )


def _entity_requests(document: Document, entities, org_id: Optional[str]):
    requests = []
    for entity in entities:
        raw_type = (
            entity.entity_type.value
            if hasattr(entity.entity_type, "value")
            else str(entity.entity_type)
        )
        raw_method = (
            entity.extraction_method.value
            if hasattr(entity.extraction_method, "value")
            else str(entity.extraction_method)
        )
        try:
            requests.append(
                CreateEntityRequest(
                    name=entity.name,
                    entity_type=safe_enum_value(EntityType, raw_type, EntityType.OTHER),
                    confidence_score=min(1.0, max(0.0, entity.confidence or 0.7)),
                    extraction_method=_EXTRACTION_METHOD_MAP.get(
                        str(raw_method).lower(), ExtractionMethod.RULE_BASED
                    ),
                    position=None,
                    context=(entity.properties or {}).get("context_window"),
                    metadata={
                        "source": "graph_repair",
                        "document_id": str(document.id),
                    },
                    source_document_id=str(document.id),
                    organization_id=org_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - skip one bad row, keep going
            logger.warning(
                "graph repair: skipping invalid entity for document %s: %s",
                document.id,
                exc,
            )
    return requests


def _relationship_requests(
    document: Document,
    relationships,
    created_id_by_name: dict,
    org_id: Optional[str],
):
    requests = []
    for rel in relationships:
        source = rel.get("source_entity")
        target = rel.get("target_entity")
        if source is None or target is None:
            continue
        source_id = created_id_by_name.get((source.name or "").strip().lower())
        target_id = created_id_by_name.get((target.name or "").strip().lower())
        if not source_id or not target_id:
            continue
        confidence = min(1.0, max(0.0, float(rel.get("confidence", 0.7))))
        evidence = rel.get("evidence") or ""
        try:
            requests.append(
                CreateRelationshipRequest(
                    source_entity_id=source_id,
                    target_entity_id=target_id,
                    relationship_type=safe_enum_value(
                        RelationshipType,
                        rel.get("relationship_type"),
                        RelationshipType.RELATED_TO,
                    ),
                    strength=confidence,
                    confidence_score=confidence,
                    context=evidence or None,
                    evidence=[evidence] if evidence else [],
                    metadata={
                        "source": "graph_repair",
                        "document_id": str(document.id),
                        "pattern_matched": rel.get("pattern_matched") or "",
                    },
                    source_document_id=str(document.id),
                    organization_id=org_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - skip one bad row, keep going
            logger.warning(
                "graph repair: skipping invalid relationship for document %s: %s",
                document.id,
                exc,
            )
    return requests


def repair_document_graph(
    document: Document,
    *,
    extraction_service=None,
    kg_service=None,
) -> GraphRepairOutcome:
    """Re-derive one document's entities + relationships and upsert into Neo4j.

    Idempotent: the batch write uses ``upsert=True``, so replays converge on
    the same graph instead of duplicating nodes. Never raises — the outcome
    (including any error) is reported in :class:`GraphRepairOutcome`.

    ``extraction_service`` defaults to a fresh spaCy
    ``EntityExtractionService`` (imported lazily: the spaCy model load is
    heavy and must not run at module import in API pods). Pass a shared
    instance when repairing many documents.
    """
    doc_id = str(document.id)
    kg = kg_service or knowledge_graph_service

    content = document.content_text or ""
    if not content.strip():
        return GraphRepairOutcome(document_id=doc_id, skipped_reason="no_content")

    if extraction_service is None:
        from src.services.processing.entity_extraction_service import (
            EntityExtractionService,
        )

        extraction_service = EntityExtractionService()

    org_id = str(document.organization_id) if document.organization_id else None

    try:
        entities, relationships = (
            extraction_service.extract_entities_and_relationships_from_text(
                document, content
            )
        )
    except Exception as exc:  # noqa: BLE001 - report, never raise
        logger.warning("graph repair: extraction failed for %s: %s", doc_id, exc)
        return GraphRepairOutcome(document_id=doc_id, error_message=str(exc))

    entity_requests = _entity_requests(document, entities, org_id)
    if not entity_requests:
        # Nothing extractable — a vacuous but honest success (no drift left).
        return GraphRepairOutcome(
            document_id=doc_id,
            entities_found=len(entities),
            relationships_found=len(relationships),
        )

    try:
        entity_result = kg.create_entities_batch(
            BatchEntityRequest(
                entities=entity_requests,
                relationships=[],
                upsert=True,
                document_id=doc_id,
            ),
            extractor=GraphExtractor.SPACY,
        )
    except Exception as exc:  # noqa: BLE001 - report, never raise
        logger.warning("graph repair: entity batch failed for %s: %s", doc_id, exc)
        return GraphRepairOutcome(
            document_id=doc_id,
            entities_found=len(entities),
            relationships_found=len(relationships),
            error_message=str(exc),
        )

    errors = len(entity_result.errors)

    # Relationship endpoints reference extracted entity names; resolve them to
    # the graph node ids the batch just created/upserted (same approach as
    # kg_extract_entities_job).
    created_id_by_name = {
        (created.name or "").strip().lower(): created.id
        for created in entity_result.created_entities
    }
    relationship_requests = _relationship_requests(
        document, relationships, created_id_by_name, org_id
    )

    relationships_created = 0
    if relationship_requests:
        try:
            rel_result = kg.create_entities_batch(
                BatchEntityRequest(
                    entities=[],
                    relationships=relationship_requests,
                    upsert=True,
                    document_id=doc_id,
                ),
                extractor=GraphExtractor.SPACY,
            )
            relationships_created = len(rel_result.created_relationships)
            errors += len(rel_result.errors)
        except Exception as exc:  # noqa: BLE001 - report, never raise
            logger.warning(
                "graph repair: relationship batch failed for %s: %s", doc_id, exc
            )
            return GraphRepairOutcome(
                document_id=doc_id,
                entities_found=len(entities),
                relationships_found=len(relationships),
                entities_created=len(entity_result.created_entities),
                errors=errors,
                error_message=str(exc),
            )

    # Upserts only append; drop the membership/edges this document no longer
    # emits (KN2 follow-up). Skipped after write errors (the outcome is
    # already not ok, so the document is retried); a failed reconcile also
    # keeps the document reconcilable.
    if not errors:
        try:
            kg.reconcile_document_graph(
                doc_id,
                org_id,
                extractor=GraphExtractor.SPACY,
                entities=entity_requests,
                relationships=relationship_requests,
            )
        except Exception as exc:  # noqa: BLE001 - report, never raise
            logger.warning("graph repair: reconcile failed for %s: %s", doc_id, exc)
            return GraphRepairOutcome(
                document_id=doc_id,
                entities_found=len(entities),
                relationships_found=len(relationships),
                entities_created=len(entity_result.created_entities),
                relationships_created=relationships_created,
                errors=errors,
                error_message=str(exc),
            )

    return GraphRepairOutcome(
        document_id=doc_id,
        entities_found=len(entities),
        relationships_found=len(relationships),
        entities_created=len(entity_result.created_entities),
        relationships_created=relationships_created,
        errors=errors,
    )
