"""Pure archive deposit rules (GOO-318): derived status, approval validity,
read-back checks and redaction. No I/O.

A deposit operation is a linear chain of insert-only attempts, one per phase
try: ``prepared -> draft_created -> files_uploaded -> published ->
verified``. Status is computed from what the remote side reported, never
from what we asked for: ``published`` needs a succeeded publish attempt whose
remote response said ``submitted`` and named a record id; ``verified`` needs
a succeeded read-back. A last attempt with outcome ``unknown`` (timeout, 5xx,
crash) makes the operation ``ambiguous`` and the next step a reconcile by
remote id, never a blind retry.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

PHASES = ("prepared", "draft_created", "files_uploaded", "published", "verified")
OUTCOMES = ("succeeded", "failed", "unknown")
RECONCILE = "reconcile"
REPOSITORY = "zenodo_sandbox"
ACTION = "publish"

_SECRET_KEY = re.compile(r"authorization|access_token|token|secret|password", re.I)
_URL = re.compile(r"^https?://", re.I)


@dataclass(frozen=True)
class Attempt:
    id: str
    phase: str
    outcome: str
    retryable: bool = False
    remote_deposition_id: str | None = None
    remote_record_id: str | None = None
    doi: str | None = None
    submitted: bool = False
    reason: str | None = None
    files: Sequence[Mapping[str, Any]] = field(default_factory=tuple)


def _reached(chain: Sequence[Attempt]) -> str | None:
    """The highest phase the remote side confirmed."""
    reached = None
    for attempt in chain:
        if attempt.outcome != "succeeded":
            continue
        if attempt.phase == "published" and not (
            attempt.submitted and attempt.remote_record_id
        ):
            continue  # our own publish request proves nothing
        if reached is None or PHASES.index(attempt.phase) > PHASES.index(reached):
            reached = attempt.phase
    return reached


def operation_status(chain: Sequence[Attempt]) -> str:
    """A phase name, ``failed`` (terminal) or ``ambiguous``."""
    if not chain:
        raise ValueError("an operation starts with its prepared attempt")
    last = chain[-1]
    if last.outcome == "unknown":
        return "ambiguous"
    if last.outcome == "failed" and not last.retryable:
        return "failed"
    return _reached(chain) or "failed"


def next_phase(chain: Sequence[Attempt]) -> str | None:
    """``reconcile`` after an unknown outcome, the next phase, or None when
    verified or terminally failed."""
    status = operation_status(chain)
    if status == "ambiguous":
        return RECONCILE
    if status in ("failed", "verified"):
        return None
    return PHASES[PHASES.index(status) + 1]


def latest_remote(chain: Sequence[Attempt]) -> Attempt | None:
    """The newest attempt that names a remote deposition id."""
    for attempt in reversed(chain):
        if attempt.remote_deposition_id:
            return attempt
    return None


def valid_approval(
    approvals: Sequence[Mapping[str, Any]],
    package_sha256: str,
    account_ref: str,
    action: str = ACTION,
) -> Mapping[str, Any] | None:
    """The approval in force, or None. ``approvals`` are one release's rows,
    oldest first. Only the newest row for this exact account and action
    counts; a revocation, another account, another action or another package
    hash voids it."""
    scoped = [
        a
        for a in approvals
        if a["account_ref"] == account_ref
        and a["action"] == action
        and a["repository"] == REPOSITORY
    ]
    if not scoped:
        return None
    newest = scoped[-1]
    if newest["kind"] != "approved" or newest["package_sha256"] != package_sha256:
        return None
    return newest


def approval_valid(
    approvals: Sequence[Mapping[str, Any]],
    package_sha256: str,
    account_ref: str,
    action: str = ACTION,
) -> bool:
    return valid_approval(approvals, package_sha256, account_ref, action) is not None


def check_readback(
    release_files: Sequence[Mapping[str, Any]],
    remote: Mapping[str, Any],
    *,
    expected_doi: str | None,
    expected_record_id: str | None,
) -> list[str]:
    """Mismatch codes between what we prepared and what Zenodo reports.
    ``release_files``: the prepared attempt's ``{name, sha256, md5}``;
    ``remote``: the adapter's normalized record ``{record_id, doi, files:
    [{name, md5}]}``. Zenodo exposes md5, not sha256; both were computed
    from the same bytes at preparation, which is the bridge."""
    mismatch: list[str] = []
    expected = {str(f["name"]): str(f["md5"]) for f in release_files}
    actual = {str(f["name"]): str(f["md5"]) for f in remote.get("files") or []}
    for name in sorted(set(expected) - set(actual)):
        mismatch.append(f"missing:{name}")
    for name in sorted(set(actual) - set(expected)):
        mismatch.append(f"extra:{name}")
    for name in sorted(set(expected) & set(actual)):
        if expected[name] != actual[name]:
            mismatch.append(f"checksum:{name}")
    if not expected_doi or remote.get("doi") != expected_doi:
        mismatch.append("doi")
    if not expected_record_id or str(remote.get("record_id")) != expected_record_id:
        mismatch.append("record_id")
    return mismatch


def _strip_query(value: str) -> str:
    if not _URL.match(value):
        return value
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def redact(obj: Any) -> Any:
    """A copy safe to retain: secret-named keys dropped, query strings
    stripped from every URL (bucket and record links included)."""
    if isinstance(obj, Mapping):
        return {
            str(k): redact(v) for k, v in obj.items() if not _SECRET_KEY.search(str(k))
        }
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _strip_query(obj)
    return obj


def operation_marker(operation_id: UUID | str) -> str:
    """Stored in the draft's ``metadata.notes`` so a lost remote id can be
    found again before anything new is created."""
    return f"nous-operation:{operation_id}"
