"""Pure report projection shared by workflow and persisted export paths."""

import csv
import io
import json
import re
from typing import Any
from urllib.parse import quote, urlparse

from src.services.research_engine.contracts import (
    CONTRACT_VERSION,
    canonical_stage_output_hash,
    normalize_evidence_level,
)


def _plain(value: Any) -> str:
    text = str(value or "")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", text)


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return quote(value, safe=":/?#@!$&'*+,;=%")


_MAX_CSV_CELL_CHARS = 8192

_PROVENANCE_BIBLIOGRAPHY_FIELDS = (
    "doi",
    "journal",
    "publication_type",
    "url",
)

_PUBLICATION_DATE_FIELDS = (
    "publication_date",
    "published",
    "publication_year",
    "year",
)

_READER_SAFE_IDENTIFIER_KINDS = (
    "doi",
    "pmid",
    "pmcid",
    "arxiv",
    "openalex",
    "semantic_scholar",
    "rag_store",
)


def _has_metadata_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return value is not None


def _flatten_metadata(value: Any) -> dict[str, Any]:
    """Flatten connector wrappers without exposing origin snapshots."""

    if not isinstance(value, dict):
        return {}
    nested = _flatten_metadata(value.get("metadata"))
    flattened = dict(nested)
    for key, candidate in value.items():
        if key in {"metadata", "provenance"}:
            continue
        if _has_metadata_value(candidate) or key not in flattened:
            flattened[key] = candidate
    return flattened


def _provenance_snapshots(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    snapshots = value.get("provenance")
    result = [item for item in snapshots or [] if isinstance(item, dict)]
    result.extend(_provenance_snapshots(value.get("metadata")))
    return result


def _reader_identifiers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        kind: identifier.strip()
        for kind in _READER_SAFE_IDENTIFIER_KINDS
        if isinstance((identifier := value.get(kind)), str) and identifier.strip()
    }


def _source_metadata(source: dict[str, Any]) -> dict[str, Any]:
    """Return canonical metadata enriched from retained provider snapshots."""

    value = source.get("metadata")
    if not isinstance(value, dict):
        return {}
    metadata = _flatten_metadata(value)
    merged_identifiers = _reader_identifiers(metadata.get("identifiers"))
    has_canonical_publication_date = any(
        _has_metadata_value(metadata.get(key)) or _has_metadata_value(source.get(key))
        for key in _PUBLICATION_DATE_FIELDS
    )
    # Direct canonical fields take precedence over retained provider snapshots,
    # including legacy records whose normalized identifier map is incomplete.
    for kind in _READER_SAFE_IDENTIFIER_KINDS:
        identifier = metadata.get(kind)
        if isinstance(identifier, str) and identifier.strip():
            merged_identifiers[kind] = identifier.strip()
        elif isinstance((identifier := source.get(kind)), str) and identifier.strip():
            merged_identifiers.setdefault(kind, identifier.strip())
    for snapshot in _provenance_snapshots(value):
        candidate = _flatten_metadata(snapshot)
        if not has_canonical_publication_date:
            publication_values = {
                key: candidate[key]
                for key in _PUBLICATION_DATE_FIELDS
                if _has_metadata_value(candidate.get(key))
            }
            if publication_values:
                metadata.update(publication_values)
                has_canonical_publication_date = True
        for key in _PROVENANCE_BIBLIOGRAPHY_FIELDS:
            if not _has_metadata_value(metadata.get(key)) and _has_metadata_value(
                candidate.get(key)
            ):
                metadata[key] = candidate[key]
        for kind, identifier in _reader_identifiers(
            candidate.get("identifiers")
        ).items():
            merged_identifiers.setdefault(kind, identifier)
        for kind in _READER_SAFE_IDENTIFIER_KINDS:
            identifier = candidate.get(kind)
            if isinstance(identifier, str) and identifier.strip():
                merged_identifiers.setdefault(kind, identifier.strip())
    if merged_identifiers:
        metadata["identifiers"] = merged_identifiers
    else:
        metadata.pop("identifiers", None)
    return metadata


def _source_doi(source: dict[str, Any]) -> str | None:
    metadata = _source_metadata(source)
    identifiers = metadata.get("identifiers")
    raw = identifiers.get("doi") if isinstance(identifiers, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        raw = metadata.get("doi") or source.get("doi")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return re.sub(
        r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", raw.strip(), flags=re.I
    )


def _coerce_year(candidate: Any) -> int | None:
    if isinstance(candidate, dict):  # Crossref {"date-parts": [[Y, M, D]]}
        candidate = candidate.get("date-parts")
    while isinstance(candidate, list) and candidate:  # Crossref [[Y, M, D]]
        candidate = candidate[0]
    if isinstance(candidate, int) and 1000 <= candidate <= 9999:
        return candidate
    if isinstance(candidate, str):
        match = re.search(r"(?<!\d)(\d{4})(?!\d)", candidate)
        if match:
            return int(match.group(1))
    return None


def publication_year(*sources: Any) -> int | None:
    """Canonical publication year from bibliographic metadata mappings.

    The single year derivation for exports, report rendering and identity
    matching. Each mapping is tried in order, and within it ``publication_year``
    → ``year`` → ``publication_date`` (arXiv ISO timestamp) → Crossref
    ``published`` date-parts. Ingest timestamps (``created_at``) are never a
    publication year, so they are deliberately not consulted.
    """
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("publication_year", "year", "publication_date", "published"):
            year = _coerce_year(source.get(key))
            if year is not None:
                return year
    return None


def _publication_year(source: dict[str, Any]) -> int | None:
    return publication_year(source, _source_metadata(source))


def _source_projection(source: dict[str, Any]) -> dict[str, Any]:
    metadata = _source_metadata(source)
    identifiers = metadata.get("identifiers")
    return {
        "source_id": source.get("source_id"),
        "external_id": source.get("external_id"),
        "title": source.get("title") or "Untitled source",
        "authors": source.get("authors") or [],
        "publication_year": _publication_year(source),
        "publication_date": metadata.get("publication_date")
        or metadata.get("published"),
        "doi": _source_doi(source),
        "journal": metadata.get("journal"),
        "publication_type": metadata.get("publication_type"),
        "identifiers": _reader_identifiers(identifiers),
        "connector_type": source.get("connector_type"),
        "evidence_level": normalize_evidence_level(
            source.get("evidence_level") or metadata.get("evidence_level")
        ),
        "url": _safe_url(source.get("url") or metadata.get("url")),
    }


def _claim_and_evidence_maps(
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_by_id = {
        source.get("source_id"): source
        for source in context.get("source_records") or []
        if isinstance(source, dict) and isinstance(source.get("source_id"), str)
    }
    evidence: list[dict[str, Any]] = []
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for extraction in context.get("extractions") or []:
        if not isinstance(extraction, dict):
            continue
        for item in extraction.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            evidence_id = item.get("evidence_id")
            if not isinstance(evidence_id, str) or evidence_id in evidence_by_id:
                continue
            projected = {
                "evidence_id": evidence_id,
                "source_id": extraction.get("source_id"),
                "part_id": item.get("part_id", extraction.get("part_id")),
                "pointer": item.get("pointer"),
                "quote": item.get("quote"),
                "page_reference": item.get("page_reference"),
                "evidence_level": normalize_evidence_level(
                    source_by_id.get(extraction.get("source_id"), {}).get(
                        "evidence_level"
                    )
                    or _source_metadata(
                        source_by_id.get(extraction.get("source_id"), {})
                    ).get("evidence_level")
                ),
            }
            evidence_by_id[evidence_id] = projected
            evidence.append(projected)

    verification_checks = {
        item.get("claim_id"): item
        for item in (context.get("verification") or {}).get("claims", [])
        if isinstance(item, dict) and isinstance(item.get("claim_id"), str)
    }
    claims: list[dict[str, Any]] = []
    synthesis = context.get("synthesis") or {}
    sections = synthesis.get("sections", []) if isinstance(synthesis, dict) else []
    for section in sections:
        if not isinstance(section, dict):
            continue
        for claim in section.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            references = [
                item
                for item in claim.get("evidence") or []
                if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
            ]
            check = verification_checks.get(claim.get("claim_id"), {})
            claims.append(
                {
                    "claim_id": claim.get("claim_id"),
                    "claim_text": claim.get("claim_text"),
                    "section": section.get("heading"),
                    "evidence_ids": [item["evidence_id"] for item in references],
                    "evidence_relations": references,
                    "verification_status": check.get("status", "unverified"),
                    "verification_reason": check.get("reason"),
                }
            )
    return claims, evidence


def _stage_audit(context: dict[str, Any], provenance: dict[str, Any]) -> list[dict]:
    stage_results = context.get("stage_results") or {}
    stage_hashes = provenance.get("stage_hashes") or {}
    if not isinstance(stage_results, dict):
        return []
    stages = []
    for raw_index, output in sorted(
        stage_results.items(), key=lambda item: int(item[0])
    ):
        if not isinstance(output, dict):
            continue
        index = str(raw_index)
        stages.append(
            {
                "step_index": int(index),
                "stage_type": output.get("stage_type"),
                "output_hash": stage_hashes.get(index)
                or canonical_stage_output_hash(output),
                "output": output,
            }
        )
    return stages


def build_report(context: dict[str, Any]) -> dict[str, Any]:
    """Project persisted typed stage outputs into a reader-facing report."""
    sources = []
    seen_source_ids: set[str] = set()
    for source in context.get("source_records", []):
        if not isinstance(source, dict):
            continue
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)
        sources.append(_source_projection(source))

    verification = context.get("verification")
    if not isinstance(verification, dict):
        verification = {
            "available": False,
            "passed": False,
            "semantic_status": "unverified",
            "reason": "Typed verification evidence is unavailable for this legacy run.",
        }
    else:
        verification = {"available": True, **verification}

    contract_version = (
        CONTRACT_VERSION
        if context.get("contract_version") == CONTRACT_VERSION
        else None
    )
    provenance = context.get("artifact_provenance") or {}
    if not isinstance(provenance, dict):
        provenance = {}
    no_evidence = bool(
        context.get("no_evidence")
        or provenance.get("final_status") == "no_evidence"
        or context.get("final_status") == "no_evidence"
    )
    final_status = (
        "no_evidence"
        if no_evidence
        else (
            "verified"
            if contract_version == CONTRACT_VERSION
            and verification.get("passed") is True
            and not verification.get("continued_after_failure")
            else "unverified"
        )
    )
    claims, evidence = _claim_and_evidence_maps(context)
    search_coverage = context.get("coverage") or {}
    coverage = (
        {**search_coverage, "exhaustive": False}
        if isinstance(search_coverage, dict)
        else {"exhaustive": False}
    )
    reviews = context.get("approved_review_overlays") or provenance.get(
        "review_history", []
    )
    report = {
        "artifact_version": 1,
        "contract_version": (contract_version),
        "run": {"id": provenance.get("run_id")},
        "blueprint": {
            "id": provenance.get("blueprint_id"),
            "version": provenance.get("blueprint_version"),
        },
        "template": {
            "source": provenance.get("template_source"),
            "contract_version": provenance.get("template_contract_version"),
        },
        "scope": context.get("scope_confirmation")
        or provenance.get("scope_confirmation")
        or {},
        "providers": context.get("provider_manifest")
        or provenance.get("provider_manifest")
        or [],
        "deduplication": context.get("deduplication")
        or (
            search_coverage.get("deduplication")
            if isinstance(search_coverage, dict)
            else {}
        )
        or {},
        "stages": _stage_audit(context, provenance),
        "reviews": reviews if isinstance(reviews, list) else [],
        "claims": claims,
        "evidence": evidence,
        "models": provenance.get("models") or provenance.get("model_ids") or [],
        "timestamps": {
            "started_at": provenance.get("started_at"),
            "completed_at": provenance.get("completed_at"),
            "generated_at": provenance.get("generated_at")
            or provenance.get("exported_at")
            or provenance.get("completed_at")
            or provenance.get("started_at"),
            "exported_at": provenance.get("exported_at"),
        },
        "limitations": provenance.get("limitations")
        or ["Bounded provider search; results are not exhaustive."],
        "final_status": final_status,
        "warning": (
            "UNVERIFIED: this artifact has not passed all verification checks."
            if final_status == "unverified"
            else (
                "NO EVIDENCE: no reader-facing research conclusion was produced."
                if final_status == "no_evidence"
                else None
            )
        ),
        "query": context.get("query"),
        "method": context.get("method") or context.get("methodology"),
        "sources": sources,
        "coverage": coverage,
        "search_coverage": coverage,
        "processing_coverage": context.get("processing_coverage") or {},
        "screening": context.get("screening") or [],
        "included_source_ids": context.get("included_source_ids") or [],
        "extractions": context.get("extractions") or [],
        "synthesis": context.get("synthesis") or {},
        "verification": verification,
        "continued_after_failure": bool(verification.get("continued_after_failure")),
        "stage_results": context.get("stage_results") or {},
    }
    if report["contract_version"] is None:
        report["evidence_status"] = "unavailable_legacy"
    else:
        report["evidence_status"] = "available"
    return report


def render_markdown(report: dict[str, Any]) -> str:
    """Render a complete escaped Markdown report without source-supplied HTML."""
    title = (
        "Daily Research Brief" if report.get("artifact_version") else "Research report"
    )
    lines = [f"# {title}", ""]
    verification = report.get("verification") or {}
    if report.get("final_status") == "unverified":
        lines.extend(
            [
                "> **UNVERIFIED.** This artifact has not passed all verification checks.",
                "",
            ]
        )
    if verification.get("passed") is not True:
        reason = (
            verification.get("reason")
            or "The result has not passed all verification gates."
        )
        lines.extend(
            [
                f"> **Verification failed or is incomplete.** {_plain(reason)} The report remains unverified.",
                "",
            ]
        )
    if report.get("continued_after_failure"):
        lines.extend(
            [
                "> **Verification failed.** The run continued after a failed quality check; this report remains unverified.",
                "",
            ]
        )
    if verification.get("passed") is True:
        lines.extend(["**Verification:** passed", ""])
    else:
        lines.extend(["**Verification:** failed or unverified", ""])
    failed_checks = [
        item
        for item in verification.get("claims", [])
        if isinstance(item, dict) and item.get("status") != "supported"
    ]
    if failed_checks:
        lines.extend(["## Failed verification checks", ""])
        for check in failed_checks:
            evidence_ids = ", ".join(
                f"`{_plain(item)}`"
                for item in check.get("evidence_ids", [])
                if isinstance(item, str)
            )
            suffix = f" Evidence: {evidence_ids}." if evidence_ids else ""
            lines.append(
                f"- `{_plain(check.get('claim_id'))}` ({_plain(check.get('status'))}): "
                f"{_plain(check.get('reason'))}.{suffix}"
            )
        lines.append("")
    if report.get("query"):
        lines.extend(["## Query and method", "", _plain(report["query"]), ""])
    if report.get("method"):
        lines.extend([_plain(report["method"]), ""])

    lines.extend(["## Source coverage", ""])
    coverage = report.get("search_coverage") or {}
    lines.append(f"- Exhaustive search: {_plain(coverage.get('exhaustive', False))}")
    lines.append(
        f"- Partial provider failures: {_plain(coverage.get('partial', False))}"
    )
    lines.append(f"- Sources returned: {len(report.get('sources') or [])}")
    lines.append("")

    screening = report.get("screening") or []
    if screening:
        lines.extend(["## Screening", ""])
        for decision in screening:
            included = "included" if decision.get("included") else "excluded"
            lines.append(
                f"- `{_plain(decision.get('source_id'))}`: {included} — {_plain(decision.get('reason'))}"
            )
        lines.append("")

    extractions = report.get("extractions") or []
    if extractions:
        lines.extend(["## Extracted evidence", ""])
        for extraction in extractions:
            lines.append(f"### Source `{_plain(extraction.get('source_id'))}`")
            data = extraction.get("data")
            if data is not None:
                lines.append("")
                lines.append("```json")
                # Fenced code is rendered as text, and leaving JSON untouched
                # keeps exported values valid and copyable.
                lines.append(json.dumps(data, ensure_ascii=False, indent=2))
                lines.append("```")
            for evidence in extraction.get("evidence", []):
                lines.append(
                    f"- `{_plain(evidence.get('pointer'))}`: “{_plain(evidence.get('quote'))}”"
                )
            lines.append("")

    synthesis = report.get("synthesis") or {}
    sections = synthesis.get("sections", []) if isinstance(synthesis, dict) else []
    if sections:
        lines.extend(["## Findings", ""])
        for section in sections:
            lines.extend([f"### {_plain(section.get('heading'))}", ""])
            for claim in section.get("claims", []):
                references = ", ".join(
                    f"`{_plain(item.get('evidence_id'))}` ({_plain(item.get('relation'))})"
                    for item in claim.get("evidence", [])
                )
                lines.append(f"- {_plain(claim.get('claim_text'))} [{references}]")
            lines.append("")

    if report.get("sources"):
        lines.extend(["## References", ""])
        for source in report["sources"]:
            title = _plain(source.get("title"))
            url = _safe_url(source.get("url"))
            label = f"[{title}]({url})" if url else title
            bibliography = "; ".join(
                value
                for value in (
                    ", ".join(_plain(author) for author in source.get("authors") or []),
                    _plain(source.get("publication_year")),
                    _plain(source.get("journal")),
                    (f"DOI: {_plain(source.get('doi'))}" if source.get("doi") else ""),
                )
                if value
            )
            details = f" — {bibliography}" if bibliography else ""
            lines.append(
                f"- `{_plain(source.get('source_id'))}` — {label}{details} "
                f"({_plain(source.get('evidence_level'))})"
            )
        lines.append("")

    lines.extend(["## Limitations", ""])
    limitations = report.get("limitations") or [
        "Bounded provider search; results are not exhaustive."
    ]
    for limitation in limitations:
        lines.append(f"- {_plain(limitation)}")
    lines.append("")

    lines.extend(["## Provenance", ""])
    run = report.get("run") or {}
    blueprint = report.get("blueprint") or {}
    template = report.get("template") or {}
    timestamps = report.get("timestamps") or {}
    lines.extend(
        [
            f"- Run: `{_plain(run.get('id'))}`",
            f"- Blueprint: `{_plain(blueprint.get('id'))}` version {_plain(blueprint.get('version'))}",
            f"- Template: `{_plain(template.get('source'))}` contract {_plain(template.get('contract_version'))}",
            f"- Generated: {_plain(timestamps.get('generated_at'))}",
            f"- Artifact status: {_plain(report.get('final_status'))}",
            "- Search exhaustive: false",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _csv_cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    elif value is None:
        text = ""
    else:
        text = str(value)
    dangerous = bool(text) and text[0] in "=+-@\t"
    limit = _MAX_CSV_CELL_CHARS - (1 if dangerous else 0)
    text = text[:limit]
    return f"'{text}" if dangerous else text


def _bibliography(source: dict[str, Any]) -> str:
    projected = _source_projection(source)
    title = str(projected["title"])
    authors = ", ".join(str(item) for item in projected["authors"])
    year = projected["publication_year"] or ""
    journal = projected["journal"] or ""
    doi = projected["doi"] or ""
    url = projected["url"] or ""
    details = "; ".join(
        item for item in (authors, str(year), str(journal), str(doi), str(url)) if item
    )
    return f"{title}. {details}".rstrip()


def render_csv(
    rows: list[dict[str, Any]],
    *,
    final_status: str,
    review_history: list[dict[str, Any]] | None = None,
    final_attestation: dict[str, Any] | None = None,
) -> str:
    """Render stable, bounded audit rows safe for spreadsheet applications."""
    fieldnames = [
        "record_type",
        "artifact_status",
        "warning",
        "source_id",
        "bibliography",
        "extracted",
        "evidence",
        "evidence_level",
        "decision",
        "reason",
        "review_id",
        "reviewer_id",
        "review_kind",
        "reviewed_at",
        "review_output_hash",
        "review_payload",
        "review_note",
        "report_hash",
        "verification_output_hash",
        "attestation_hash",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\r\n")
    writer.writeheader()
    warning = (
        "UNVERIFIED: verification checks did not all pass."
        if final_status == "unverified"
        else (
            "NO EVIDENCE: no research conclusion was produced."
            if final_status == "no_evidence"
            else ""
        )
    )
    records = rows or [{}]
    for row in records:
        source: dict[str, Any] = {}
        source_value = row.get("source")
        if isinstance(source_value, dict):
            source = source_value
        extraction: dict[str, Any] = {}
        extraction_value = row.get("extraction")
        if isinstance(extraction_value, dict):
            extraction = extraction_value
        writer.writerow(
            {
                "record_type": "source",
                "artifact_status": _csv_cell(final_status),
                "warning": _csv_cell(warning),
                "source_id": _csv_cell(
                    source.get("source_id") or extraction.get("source_id")
                ),
                "bibliography": _csv_cell(_bibliography(source)),
                "extracted": _csv_cell(extraction.get("data")),
                "evidence": _csv_cell(extraction.get("evidence")),
                "evidence_level": _csv_cell(
                    _source_projection(source)["evidence_level"]
                ),
                "decision": _csv_cell(row.get("decision")),
                "reason": _csv_cell(row.get("reason")),
            }
        )
    for review in review_history or []:
        writer.writerow(
            {
                "record_type": "review",
                "artifact_status": _csv_cell(final_status),
                "decision": _csv_cell(review.get("decision")),
                "review_id": _csv_cell(review.get("review_id")),
                "reviewer_id": _csv_cell(review.get("reviewer_id")),
                "review_kind": _csv_cell(review.get("review_kind")),
                "reviewed_at": _csv_cell(review.get("created_at")),
                "review_output_hash": _csv_cell(review.get("output_hash")),
                "review_payload": _csv_cell(review.get("decision_payload")),
                "review_note": _csv_cell(review.get("note")),
            }
        )
    if final_attestation is not None:
        writer.writerow(
            {
                "record_type": "final_attestation",
                "artifact_status": _csv_cell(final_status),
                "decision": _csv_cell(final_attestation.get("decision")),
                "review_id": _csv_cell(final_attestation.get("review_id")),
                "reviewer_id": _csv_cell(final_attestation.get("reviewer_id")),
                "review_kind": _csv_cell(final_attestation.get("review_kind")),
                "reviewed_at": _csv_cell(final_attestation.get("reviewed_at")),
                "review_output_hash": _csv_cell(final_attestation.get("output_hash")),
                "report_hash": _csv_cell(final_attestation.get("report_hash")),
                "verification_output_hash": _csv_cell(
                    final_attestation.get("verification_output_hash")
                ),
                "attestation_hash": _csv_cell(
                    final_attestation.get("attestation_hash")
                ),
            }
        )
    return output.getvalue()
