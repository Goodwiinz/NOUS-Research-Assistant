"""Pure, explainable report identity matching (GOO-299).

Identity is decided by identifiers only. Titles, authors and years never equate
two reports here; they are at most a read-only suggestion elsewhere.
"""

import re
from dataclasses import dataclass
from typing import Any, Mapping

from src.services.research_engine.discovery import extract_identifiers_from_mapping

# Priority order: the first kind that hits an existing report wins.
IDENTITY_KINDS = ("doi", "pmid", "pmcid", "arxiv_base", "openalex", "semantic_scholar")
# research_report_identifiers.value is String(512). An identifier that cannot be
# stored whole is dropped (never truncated), so the in-memory index key and the
# persisted value are always the same string.
MAX_IDENTIFIER_LENGTH = 512


def report_identifiers(raw: Mapping[str, Any]) -> dict[str, str]:
    """Normalized identifiers plus ``arxiv_base`` (arXiv id without version)."""
    ids = extract_identifiers_from_mapping(raw)
    if "arxiv" in ids:
        ids["arxiv_base"] = re.sub(r"v\d+$", "", ids["arxiv"])
    return {k: v for k, v in ids.items() if len(v) <= MAX_IDENTIFIER_LENGTH}


@dataclass(frozen=True)
class ReportAssignment:
    report_key: Any | None
    match_method: str
    evidence: dict[str, Any]


def assign_report(
    index: Mapping[tuple[str, str], Any], raw_ids: Mapping[str, Any]
) -> ReportAssignment:
    """Attach to the highest-priority identifier hit; never merge on conflict.

    Hits from lower-priority kinds that point at a different report are kept in
    ``evidence["conflicts"]`` so inconsistent identifiers stay inspectable.
    """
    ids = report_identifiers(raw_ids)
    hits = [
        (kind, ids[kind], index[(kind, ids[kind])])
        for kind in IDENTITY_KINDS
        if kind in ids and (kind, ids[kind]) in index
    ]
    if not hits:
        return ReportAssignment(None, "new", {"observed": ids, "conflicts": []})
    kind, value, key = hits[0]
    conflicts = [
        {"kind": k, "value": v, "report_key": r} for k, v, r in hits[1:] if r != key
    ]
    return ReportAssignment(
        key,
        kind,
        {
            "observed": ids,
            "matched": {"kind": kind, "value": value},
            "conflicts": conflicts,
        },
    )
