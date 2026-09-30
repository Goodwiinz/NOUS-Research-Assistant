"""Bounded paper discovery with conservative identity matching and evidence snapshots."""

import asyncio
import copy
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from src.services.research_engine.connectors.base import (
    SearchTrace,
    SourceConnector,
    SourceDocument,
)

SEARCH_TIMEOUT_SECONDS = 90.0


def extract_identifiers(source: SourceDocument) -> dict[str, str]:
    values = dict(source.metadata)
    if source.external_id:
        values.setdefault(
            "doi" if source.connector_type == "crossref" else source.connector_type,
            source.external_id,
        )
    if source.connector_type == "pubmed":
        values.setdefault("pmid", source.external_id)
    return extract_identifiers_from_mapping(values)


def extract_identifiers_from_mapping(values: Mapping[str, Any]) -> dict[str, str]:
    """Normalize known identifier kinds; unknown keys and blanks are dropped."""
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


_identifiers = extract_identifiers  # compatibility alias for existing callers/tests


def prepare_sources(documents: list[SourceDocument]) -> list[SourceDocument]:
    """Merge verified identifier matches; never infer identity from a title.

    Conflicting shared identifiers prevent a merge, including arXiv revisions.
    Original provider snapshots are retained so enrichment does not erase origin.
    """
    merged: list[SourceDocument] = []
    seen_identity_pairs: set[tuple[str, str, str]] = set()
    now = datetime.now(timezone.utc).isoformat()
    for original in documents:
        if not original.title.strip():
            continue
        source = copy.deepcopy(original)
        ids = extract_identifiers(source)
        provenance = {**asdict(source), "retrieved_at": now}
        source.metadata["identifiers"] = ids
        source.metadata["provenance"] = [provenance]
        partition = "local" if source.connector_type == "rag_store" else "public"
        identity_pairs = {(partition, kind, value) for kind, value in ids.items()}
        # A merge requires at least one exact identifier match in the same
        # local/public partition. Cluster identifiers only grow, so a source
        # with no previously seen pair cannot match any existing candidate.
        if seen_identity_pairs.isdisjoint(identity_pairs):
            source.compute_hash()
            merged.append(source)
            seen_identity_pairs.update(identity_pairs)
            continue
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
        seen_identity_pairs.update(
            (partition, kind, value) for kind, value in ids.items()
        )
    return merged


def source_records(
    documents: list[SourceDocument], *, id_namespace: str | None = None
) -> list[dict[str, Any]]:
    """JSON-safe evidence passed through step persistence and run resume.

    With ``id_namespace`` the source_id is derived from the provider identity so
    a retried search maps the same record to the same durable row.
    """
    rows = []
    for doc in documents:
        if not doc.content_hash:
            doc.compute_hash()
        provenance = doc.metadata.get("provenance") or []
        primary = provenance[0] if provenance else {}
        provider = primary.get("connector_type", doc.connector_type)
        external_id = primary.get("external_id", doc.external_id)
        identity = (
            f"{provider}:{external_id}"
            if external_id
            else f"{provider}:sha256:{doc.content_hash}"
        )
        source_id = uuid5(UUID(id_namespace), identity) if id_namespace else uuid4()
        rows.append(
            {
                **asdict(doc),
                "source_id": str(source_id),
                "evidence_level": (
                    (
                        "workspace_document"
                        if doc.connector_type == "rag_store"
                        else "full_text"
                    )
                    if doc.full_text
                    else "abstract" if doc.abstract else "metadata_only"
                ),
            }
        )
    return rows


async def search_sources(
    connectors: dict[str, SourceConnector],
    names: list[str],
    query: str,
    limit: int,
    *,
    execution_namespace: str | None = None,
    on_page_update: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None,
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
        trace = SearchTrace(
            execution_id=(
                str(uuid5(NAMESPACE_URL, f"{execution_namespace}:{name}"))
                if execution_namespace
                else str(uuid4())
            ),
            provider=name,
            requested_limit=limit,
            on_page_update=on_page_update,
        )
        try:
            docs, receipt = await asyncio.wait_for(
                SourceConnector.search_with_receipts(
                    connectors[name],
                    query,
                    max_results=limit,
                    provider=name,
                    trace=trace,
                ),
                SEARCH_TIMEOUT_SECONDS,
            )
            return docs, {
                **receipt,
                "returned": len(docs),
                "limit": limit,
            }
        except asyncio.TimeoutError:
            trace.error_type = "TimeoutError"
            await trace.record_failed_request(trace.error_type)
            docs = trace.documents
            return docs, {
                **trace.as_receipt(returned_count=len(docs), status="timed_out"),
                "returned": len(docs),
                "limit": limit,
            }
        except Exception as exc:
            # Exceptions can contain credential-bearing URLs. Never serialize them.
            trace.error_type = type(exc).__name__
            await trace.record_failed_request(trace.error_type)
            docs = trace.documents
            return docs, {
                **trace.as_receipt(returned_count=len(docs)),
                "returned": len(docs),
                "limit": limit,
            }

    results = await asyncio.gather(*(search(name) for name in names))
    providers = {name: result[1] for name, result in zip(names, results)}
    # Every provider's request/page receipts are already checkpointed by the
    # traces above, so failing the run here loses no evidence.
    if all(
        item["status"] != "ok" and item["returned"] == 0 for item in providers.values()
    ):
        raise RuntimeError("All selected research providers failed")
    raw_documents = [doc for result, _ in results for doc in result]
    docs = prepare_sources(raw_documents)
    failed_statuses = {"failed", "partial", "timed_out"}
    return docs, {
        "partial": any(
            item["status"] in failed_statuses for item in providers.values()
        ),
        "providers": providers,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "deduplication": {"before": len(raw_documents), "after": len(docs)},
        "exhaustive": False,
    }
