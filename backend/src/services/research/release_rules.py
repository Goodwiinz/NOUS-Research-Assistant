"""Pure release rules (GOO-307): the promotion gate and the dependency walk.

No database, no FastAPI. A draft version is ``candidate`` until a person
promotes it; ``check_release`` decides whether that is allowed and lists
every blocker with its offsets. ``dependents`` walks the evidence graph so
an upstream change reaches only the releases built on it. A review's
``fully_verified`` flag and any model stance never authorize anything.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping, Protocol, Sequence
from uuid import UUID

POLICY_VERSION = 1
# A supported *statement of uncertainty* is still ``supporting``.
SUPPORTING_STANCES = frozenset({"supporting"})
BLOCKER_CODES = (
    "unclaimed_assertion",
    "unassessed",
    "model_only",
    "opposed",
    "unresolved",
    "legacy_only",
    "superseded_assessment",
    "stale_evidence",
    "unattributed_interpretation",
    "superseded_claim",
    "identity_mismatch",
    "retracted_source",
)
# GOO-311: a pooled estimate cites its synthesis result, which pins its inputs.
# GOO-312: a manuscript figure cites one exact run output, which pins its run.
ANCHORED_LINK_KINDS = frozenset(
    {"extraction", "source_span", "synthesis_result", "figure"}
)
Status = Literal["candidate", "verified", "stale"]
Node = tuple[str, str]

# GOO-292's sentence boundary: shared with ``CitationVerificationService``.
_BOUNDARY_RE = re.compile(r"(?<!\d)[.!?](?!\d)|\n")
_DOC_RE = re.compile(r"\[Doc\s+(\d+)\]")
_DETAIL = {
    "unclaimed_assertion": "No claim covers this sentence",
    "unassessed": "No adjudicator assessment",
    "model_only": "Only a model stance; no adjudicator assessment",
    "opposed": "The adjudicator found the evidence opposing",
    "unresolved": "The adjudicator left the claim unresolved",
    "legacy_only": "Only unanchored legacy citations; link a source span",
    "superseded_assessment": "The claim was re-versioned; its assessment is closed",
    "stale_evidence": "Cited evidence was withdrawn, superseded or changed",
    "unattributed_interpretation": "An interpretation needs a named author",
    "superseded_claim": "The claim was re-versioned since; re-bind it to this draft",
    "identity_mismatch": "The source identifier resolves to another work",
    "retracted_source": "The source is recorded as retracted",
}


def node(kind: str, value: Any) -> Node:
    return (kind, str(value))


def source_node(document_id: Any, source_hash: str, text_sha256: str | None) -> Node:
    """A source revision: the hashes a row was pinned to."""
    return ("source", f"{document_id}:{source_hash}:{text_sha256 or ''}")


def parse_source(value: str) -> tuple[str, str, str | None]:
    document_id, source_hash, text_sha256 = value.split(":")
    return document_id, source_hash, text_sha256 or None


def assertion_spans(content: str) -> list[tuple[int, int, str]]:
    """Non-heading prose assertions as ``(start, end, text)``, text stripped
    and ``content[start:end] == text``."""
    spans: list[tuple[int, int, str]] = []
    start = 0
    for boundary in [*_BOUNDARY_RE.finditer(content), None]:
        end = boundary.end() if boundary is not None else len(content)
        raw = content[start:end]
        text = raw.strip()
        if text and not text.startswith("#"):
            lead = len(raw) - len(raw.lstrip())
            spans.append((start + lead, start + lead + len(text), text))
        start = end
    return spans


@dataclass(frozen=True)
class LinkIn:
    id: UUID
    kind: str
    live: bool  # a ``linked`` tip
    observed: bool = False  # has a model stance snapshot


@dataclass(frozen=True)
class AssessmentIn:
    id: UUID
    stance: str
    link_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class ClaimIn:
    claim_version_id: UUID
    kind: str
    start: int
    end: int
    text: str
    attributed_to: str | None  # the interpretation's author label
    links: tuple[LinkIn, ...] = ()
    assessment: AssessmentIn | None = None
    is_tip: bool = True  # the claim was not re-versioned since


@dataclass(frozen=True)
class Blocker:
    code: str
    claim_version_id: UUID | None
    start: int
    end: int
    text: str
    detail: str


@dataclass(frozen=True)
class GateResult:
    blockers: tuple[Blocker, ...]
    dimensions: dict[str, Any]
    claim_version_ids: tuple[UUID, ...]
    assessment_ids: tuple[UUID, ...]
    interpretation_ids: tuple[UUID, ...]
    labels: tuple[tuple[int, str], ...] = field(default=())  # (offset, label)


class _Row(Protocol):
    stale_at: Any


def release_status(rows: Sequence[_Row], derived_stale: bool) -> Status:
    """No row: candidate. A live row: verified, unless its inputs changed
    since (derived on read). Only stale rows: stale."""
    if any(row.stale_at is None for row in rows):
        return "stale" if derived_stale else "verified"
    return "stale" if rows else "candidate"


def dependents(
    edges: Iterable[tuple[Node, Node]], changed: Iterable[Node]
) -> set[Node]:
    """Every node reachable from ``changed`` (breadth first)."""
    children: dict[Node, list[Node]] = defaultdict(list)
    for parent, child in edges:
        children[parent].append(child)
    reached: set[Node] = set()
    queue = deque(changed)
    while queue:
        for child in children.get(queue.popleft(), ()):
            if child not in reached:
                reached.add(child)
                queue.append(child)
    return reached


def _factual_code(claim: ClaimIn, stale_nodes: set[Node]) -> str | None:
    live = [link for link in claim.links if link.live]
    assessment = claim.assessment
    if assessment is None:
        if live and all(link.kind == "legacy_unanchored" for link in live):
            return "legacy_only"
        return "model_only" if any(link.observed for link in live) else "unassessed"
    if not claim.is_tip:
        return "superseded_assessment"
    if assessment.stance == "opposing":
        return "opposed"
    if assessment.stance not in SUPPORTING_STANCES:
        return "unresolved"
    by_id = {link.id: link for link in claim.links}
    cited = [by_id.get(link_id) for link_id in assessment.link_ids]
    if cited and all(c and c.kind == "legacy_unanchored" for c in cited):
        return "legacy_only"
    if not cited or any(
        c is None
        or not c.live
        or c.kind not in ANCHORED_LINK_KINDS
        or node("link", c.id) in stale_nodes
        for c in cited
    ):
        return "stale_evidence"
    return None


def _source_blockers(
    content: str, review: Mapping[str, Any]
) -> tuple[list[Blocker], dict[str, Any]]:
    """Identity and publication, per the persisted review verdicts."""
    blockers: list[Blocker] = []
    identity_reported, publication_reported = [], []
    for entry in review.get("verdicts") or []:
        if not isinstance(entry, Mapping):
            continue
        index = entry.get("doc_index")
        checks = entry.get("checks") or {}
        identity = (checks.get("identity") or {}).get("status") or entry.get("identity")
        publication = (checks.get("publication") or {}).get("observation_status")
        marker = next(
            (m for m in _DOC_RE.finditer(content) if str(m.group(1)) == str(index)),
            None,
        )
        start, end = (marker.start(), marker.end()) if marker else (0, 0)
        for code, hit in (
            ("identity_mismatch", identity == "mismatch"),
            ("retracted_source", publication == "retracted"),
        ):
            if hit:
                text = f"[Doc {index}]"
                blockers.append(Blocker(code, None, start, end, text, _DETAIL[code]))
        if identity in ("unresolved", "no_identifiers"):
            identity_reported.append({"doc_index": index, "status": identity})
        if publication in ("unknown", "corrected", None):
            publication_reported.append(
                {"doc_index": index, "status": publication or "unknown"}
            )
    codes = {b.code for b in blockers}
    return blockers, {
        "identity": {
            "status": "blocked" if "identity_mismatch" in codes else "passed",
            "reported": identity_reported,
        },
        # ponytail: no live retraction lookup exists; report it, never pass it.
        "publication": {
            "status": "blocked" if "retracted_source" in codes else "unavailable",
            "reported": publication_reported,
        },
    }


def check_release(
    content: str,
    claims: Sequence[ClaimIn],
    review: Mapping[str, Any],
    stale_nodes: set[Node],
) -> GateResult:
    """The release gate (policy v1). ``claims``: the claim versions bound to
    this exact content; ``review``: the draft's persisted citation review;
    ``stale_nodes``: graph nodes whose inputs changed (derived on read)."""
    blockers: list[Blocker] = []
    factual: list[UUID] = []
    assessments: list[UUID] = []
    interpretations: list[UUID] = []
    labels: list[tuple[int, str]] = []
    for start, end, text in assertion_spans(content):
        if not any(c.start < end and c.end > start for c in claims):
            code = "unclaimed_assertion"
            blockers.append(Blocker(code, None, start, end, text, _DETAIL[code]))
    for claim in claims:
        if claim.kind == "interpretation":
            interpretations.append(claim.claim_version_id)
            # A re-versioned interpretation is no longer the claim of record;
            # a release built on it would be derived-stale on creation.
            if not claim.is_tip:
                code_or_none: str | None = "superseded_claim"
            elif not claim.attributed_to:
                code_or_none = "unattributed_interpretation"
            else:
                code_or_none = None
            if claim.attributed_to:
                labels.append((claim.end, f"Interpretation — {claim.attributed_to}"))
        else:
            factual.append(claim.claim_version_id)
            if claim.assessment is not None:
                assessments.append(claim.assessment.id)
            code_or_none = _factual_code(claim, stale_nodes)
        if code_or_none is not None:
            blockers.append(
                Blocker(
                    code_or_none,
                    claim.claim_version_id,
                    claim.start,
                    claim.end,
                    claim.text,
                    _DETAIL[code_or_none],
                )
            )
    support_blocked = bool(blockers)
    source_blockers, dimensions = _source_blockers(content, review)
    blockers += source_blockers
    blockers.sort(key=lambda b: (b.start, b.end, b.code))
    return GateResult(
        blockers=tuple(blockers),
        dimensions={
            "policy_version": POLICY_VERSION,
            "support": {
                "status": "blocked" if support_blocked else "passed",
                "claims": len(claims),
            },
            **dimensions,
        },
        claim_version_ids=tuple(factual),
        assessment_ids=tuple(assessments),
        interpretation_ids=tuple(interpretations),
        labels=tuple(labels),
    )


def status_header(
    status: Status,
    gate: GateResult,
    *,
    release_id: str | None = None,
    content_hash: str | None = None,
    cause: str | None = None,
) -> str:
    if status == "verified":
        return f"> Status: VERIFIED release {release_id} · sha256 {(content_hash or '')[:12]}"
    unresolved = f"{len(gate.blockers)} unresolved item(s)."
    if status == "stale":
        return f"> Status: STALE — invalidated by {cause or 'an upstream change'}. {unresolved}"
    return f"> Status: CANDIDATE — not verified. {unresolved}"


def label_export(
    content: str,
    gate: GateResult,
    fmt: Literal["markdown", "latex"],
    header: str,
) -> str:
    """Wrap the stored content with the status header, inline labels after
    each blocking span or interpretation, and a trailing unresolved list.
    LaTeX gets plain labels (the caller escapes); its title line stays first."""
    bold = fmt == "markdown"
    marks: list[tuple[int, str]] = [
        (b.end, f"**[UNRESOLVED: {b.code}]**" if bold else f"[UNRESOLVED: {b.code}]")
        for b in gate.blockers
    ]
    marks += [
        (pos, f"*[{text}]*" if bold else f"[{text}]") for pos, text in gate.labels
    ]
    body = content
    for pos, label in sorted(marks, key=lambda m: m[0], reverse=True):
        body = f"{body[:pos]} {label}{body[pos:]}"
    if gate.blockers:
        items = "\n".join(
            f'- [{b.code}] "{b.text}" — {b.detail}' for b in gate.blockers
        )
        body = f"{body}\n\n## Unresolved items\n\n{items}\n"
    if fmt == "latex":
        title, _, rest = body.partition("\n")
        return f"{title}\n\n{header.removeprefix('> ')}\n\n{rest}"
    return f"{header}\n\n{body}"
