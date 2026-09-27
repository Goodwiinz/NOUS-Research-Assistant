"""Pure report projection shared by workflow and persisted export paths."""

import re
from typing import Any
from urllib.parse import quote, urlparse

from src.services.research_engine.contracts import (
    CONTRACT_VERSION,
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
        sources.append(
            {
                "source_id": source_id,
                "title": source.get("title") or "Untitled source",
                "connector_type": source.get("connector_type"),
                "evidence_level": normalize_evidence_level(
                    source.get("evidence_level")
                ),
                "url": _safe_url(source.get("url")),
            }
        )

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

    report = {
        "contract_version": (
            CONTRACT_VERSION
            if context.get("contract_version") == CONTRACT_VERSION
            else None
        ),
        "query": context.get("query"),
        "method": context.get("method") or context.get("methodology"),
        "sources": sources,
        "search_coverage": context.get("coverage") or {},
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
    lines = ["# Research report", ""]
    verification = report.get("verification") or {}
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
                import json

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
            lines.append(
                f"- `{_plain(source.get('source_id'))}` — {label} ({_plain(source.get('evidence_level'))})"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
