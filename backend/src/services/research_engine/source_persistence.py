"""Build run-owned source rows for the caller's step-completion transaction."""

from typing import Any
from uuid import UUID

from src.models.research_source import ResearchSource


def research_source_rows(run_id: UUID, output: dict[str, Any]) -> list[ResearchSource]:
    """Persist source snapshots with the same IDs used in downstream evidence."""
    return [
        ResearchSource(
            id=UUID(record["source_id"]),
            run_id=run_id,
            connector_type=record["connector_type"],
            external_id=record.get("external_id"),
            title=record["title"][:500],
            authors=record.get("authors"),
            abstract=record.get("abstract"),
            url=record.get("url"),
            content_hash=record.get("content_hash"),
            metadata_={
                **record.get("metadata", {}),
                "evidence_level": record["evidence_level"],
            },
        )
        for record in output.get("source_records", [])
    ]
