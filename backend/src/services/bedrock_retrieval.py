"""Fail-closed retrieval from the shared Bedrock knowledge base."""

from __future__ import annotations

from typing import Any
from uuid import UUID


def retrieve_chunks(
    kb_id: str,
    query: str,
    org_id: Any,
    top_k: int,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply mandatory tenant filtering, then verify every returned chunk."""
    import boto3

    organization_id = str(UUID(str(org_id)))
    organization_filter: dict[str, Any] = {
        "equals": {"key": "organization_id", "value": organization_id}
    }
    vector_filter = (
        {"andAll": [organization_filter, filters]}
        if filters is not None
        else organization_filter
    )
    response = boto3.client("bedrock-agent-runtime", region_name="us-east-1").retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": top_k,
                "filter": vector_filter,
            }
        },
    )
    chunks = []
    for raw in response.get("retrievalResults", []):
        metadata = raw.get("metadata") or {}
        if metadata.get("organization_id") != organization_id:
            continue
        try:
            document_id = str(UUID(str(metadata.get("document_id"))))
        except (ValueError, TypeError, AttributeError):
            continue
        text = (raw.get("content") or {}).get("text")
        if not isinstance(text, str) or not text:
            continue
        chunks.append(
            {
                "text": text,
                "score": float(raw.get("score") or 0),
                "document_id": document_id,
                "metadata": {**metadata, "score_source": "upstream"},
            }
        )
    return chunks
