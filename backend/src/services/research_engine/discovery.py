"""Bounded paper discovery with conservative identity matching and evidence snapshots."""

import asyncio
import copy
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.services.research_engine.connectors.base import SourceConnector, SourceDocument

SEARCH_TIMEOUT_SECONDS = 90.0


def _identifiers(source: SourceDocument) -> dict[str, str]:
    values = dict(source.metadata)
    if source.external_id:
        values.setdefault(
            "doi" if source.connector_type == "crossref" else source.connector_type,
            source.external_id,
        )
    if source.connector_type == "pubmed":
        values.setdefault("pmid", source.external_id)
    identifiers = {}
    for kind in (
        "doi",
        "pmid",
        "pmcid",
        "arxiv",
        "openalex",
        "semantic_scholar",
        "rag_store",
    ):
        value = values.get(kind)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if kind == "doi":
            value = re.sub(
                r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I
            ).lower()
            if not re.fullmatch(r"10\.\d{4,9}/\S+", value):
                continue
        elif kind == "arxiv":
            value = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/", "", value)
            value = re.sub(r"\.pdf$", "", value)
            # Preserve the exact revision, including v1/v2 suffixes.
        elif kind in ("pmid", "pmcid", "openalex"):
            value = value.rstrip("/").rsplit("/", 1)[-1]
        identifiers[kind] = value
    return identifiers


def prepare_sources(documents: list[SourceDocument]) -> list[SourceDocument]:
    """Merge verified identifier matches; never infer identity from a title.

    Conflicting shared identifiers prevent a merge, including arXiv revisions.
    Original provider snapshots are retained so enrichment does not erase origin.
    """
    merged: list[SourceDocument] = []
    now = datetime.now(timezone.utc).isoformat()
    for original in documents:
        if not original.title.strip():
            continue
        source = copy.deepcopy(original)
        ids = _identifiers(source)
        provenance = {**asdict(source), "retrieved_at": now}
        source.metadata["identifiers"] = ids
        source.metadata["provenance"] = [provenance]
        # Local documents remain distinct from public metadata even with a DOI.
        for candidate in list(merged):
            if (candidate.connector_type == "rag_store") != (
                source.connector_type == "rag_store"
            ):
                continue
            other = candidate.metadata["identifiers"]
            shared = ids.keys() & other.keys()
            if not shared or any(ids[key] != other[key] for key in shared):
                continue
            ids = {**other, **ids}
            candidate.metadata["identifiers"] = ids
            candidate.metadata["provenance"].extend(source.metadata["provenance"])
            for attribute in ("abstract", "full_text", "url", "authors"):
                if not getattr(candidate, attribute):
                    setattr(candidate, attribute, getattr(source, attribute))
            source = candidate
            merged.remove(candidate)
        source.compute_hash()
        merged.append(source)
    return merged


def source_records(documents: list[SourceDocument]) -> list[dict[str, Any]]:
    """JSON-safe evidence passed through step persistence and run resume."""
    return [
        {
            **asdict(doc),
            "source_id": str(uuid4()),
            "evidence_level": (
                ("excerpt" if doc.connector_type == "rag_store" else "full_text")
                if doc.full_text
                else "abstract" if doc.abstract else "metadata"
            ),
        }
        for doc in documents
    ]


async def search_sources(
    connectors: dict[str, SourceConnector], names: list[str], query: str, limit: int
) -> tuple[list[SourceDocument], dict[str, Any]]:
    """Return successful providers and explicitly record incomplete coverage."""
    names = list(dict.fromkeys(names))
    unknown = [name for name in names if name not in connectors]
    if unknown:
        raise ValueError("Unknown research source: " + ", ".join(unknown))
    if not names:
        raise ValueError("Select at least one research source")
    if not 1 <= limit <= 200:
        raise ValueError("max_results must be between 1 and 200 per provider")

    async def search(name: str) -> tuple[list[SourceDocument], dict[str, Any]]:
        try:
            docs = await asyncio.wait_for(
                connectors[name].search(query, max_results=limit),
                SEARCH_TIMEOUT_SECONDS,
            )
            return docs, {"status": "ok", "returned": len(docs), "limit": limit}
        except Exception as exc:
            # Exceptions can contain credential-bearing URLs. Never serialize them.
            return [], {"status": "failed", "error_type": type(exc).__name__}

    results = await asyncio.gather(*(search(name) for name in names))
    providers = {name: result[1] for name, result in zip(names, results)}
    if all(item["status"] == "failed" for item in providers.values()):
        raise RuntimeError("All selected research providers failed")
    docs = prepare_sources([doc for result, _ in results for doc in result])
    return docs, {
        "partial": any(item["status"] == "failed" for item in providers.values()),
        "providers": providers,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "exhaustive": False,
    }
