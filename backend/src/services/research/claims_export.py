"""Reconstructable claims evidence export (GOO-306): pure, no database.

The body lets a reader walk source -> observation -> claim -> passage
offline: drafts carry their content (the project's own text), documents only
their hashes (no ``content_text``, the GOO-300 restricted-content rule).
Every list is ordered by ``created_at, id`` so the canonical-JSON body hash
does not depend on query order. Rows are JSON-ready dicts.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from src.services.research import claim_rules
from src.services.research_engine.contracts import canonical_json_sha256

SCHEMA = "nous.academic.claims-export.v1"
Row = Mapping[str, Any]
_DOCUMENT_KEYS = ("id", "title", "source_hash", "current_text_sha256")


def _ordered(rows: Iterable[Row]) -> list[dict[str, Any]]:
    return sorted(
        (dict(row) for row in rows),
        key=lambda r: (str(r.get("created_at") or ""), str(r["id"])),
    )


def _live(links: Sequence[Row]) -> list[Row]:
    superseded = {link["supersedes_link_id"] for link in links}
    return [
        link
        for link in links
        if link["id"] not in superseded and link["status"] == "linked"
    ]


def _resolution(link: Row, version: Row, accepted: Mapping[str, Row]) -> dict[str, Any]:
    observation_id = span = None
    if link["kind"] == "source_span":
        span = [link["start_char"], link["end_char"]]
    elif link["kind"] == "extraction":
        value = accepted.get(link["accepted_value_id"]) or {}
        cited = value.get("observation_ids") or []
        observation_id = value.get("anchor_observation_id") or (
            cited[0] if cited else None
        )
        if value.get("anchor_start_char") is not None:
            span = [value["anchor_start_char"], value["anchor_end_char"]]
    return {
        "claim_version_id": version["id"],
        "passage": [version["draft_id"], version["start_char"], version["end_char"]],
        "link_id": link["id"],
        "kind": link["kind"],
        "observation_id": observation_id,
        "document_id": link["document_id"],
        "source_hash": link["source_hash"],
        "text_sha256": link["text_sha256"],
        "span": span,
        # GOO-311: a pooled estimate resolves to its synthesis result.
        "synthesis_result_id": link.get("synthesis_result_id"),
    }


def build_body(
    *,
    collection_id: str,
    draft_filter: str | None,
    stream_head: int,
    drafts: Sequence[Row],
    documents: Sequence[Row],
    accepted_values: Sequence[Row],
    extraction_observations: Sequence[Row],
    claims: Sequence[Row],
    claim_versions: Sequence[Row],
    links: Sequence[Row],
    stance_observations: Sequence[Row],
    assessments: Sequence[Row],
) -> dict[str, Any]:
    """``drafts`` rows are ``{id, version, content, created_at}``; the hash and
    whether every pinned version still matches it are computed here."""
    versions = _ordered(claim_versions)
    link_rows = _ordered(links)
    assessment_rows = _ordered(assessments)
    draft_rows = []
    for draft in _ordered(drafts):
        content_hash = claim_rules.content_hash(draft["content"])
        pinned = [v for v in versions if v["draft_id"] == draft["id"]]
        draft_rows.append(
            {
                "id": draft["id"],
                "version": draft["version"],
                "content": draft["content"],
                "content_hash": content_hash,
                "content_hash_matches": all(
                    v["draft_content_hash"] == content_hash for v in pinned
                ),
                "created_at": draft.get("created_at"),
            }
        )
    by_version = {v["id"]: v for v in versions}
    accepted = {a["id"]: a for a in accepted_values}
    superseded_versions = {v["supersedes_claim_version_id"] for v in versions}
    assessed_versions = {a["claim_version_id"] for a in assessment_rows}
    kinds = Counter(link["kind"] for link in link_rows)
    return {
        "collection_id": collection_id,
        "draft_filter": draft_filter,
        "stream_head": stream_head,
        "drafts": draft_rows,
        # Hashes only, whatever the caller passed: never ``content_text``.
        "documents": sorted(
            ({key: d[key] for key in _DOCUMENT_KEYS} for d in documents),
            key=lambda d: d["id"],
        ),
        "extraction": {
            "accepted_values": _ordered(accepted_values),
            "observations": _ordered(extraction_observations),
        },
        "claims": _ordered(claims),
        "claim_versions": versions,
        "links": link_rows,
        "stance_observations": _ordered(stance_observations),
        "assessments": assessment_rows,
        "counts": {
            "claims": len(claims),
            "claim_versions": len(versions),
            "links_by_kind": {kind: kinds[kind] for kind in claim_rules.LINK_KINDS},
            "withdrawn_links": sum(link["status"] == "withdrawn" for link in link_rows),
            "stance_observations": len(stance_observations),
            "assessments_by_stance": dict(
                sorted(Counter(a["stance"] for a in assessment_rows).items())
            ),
            "unassessed_tip_versions": sum(
                v["id"] not in superseded_versions and v["id"] not in assessed_versions
                for v in versions
            ),
        },
        "resolution": [
            _resolution(link, by_version[link["claim_version_id"]], accepted)
            for link in _live(link_rows)
        ],
    }


def package(body: Mapping[str, Any], exported_at: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "exported_at": exported_at,
        "body_sha256": canonical_json_sha256(body),
        "body": body,
    }
