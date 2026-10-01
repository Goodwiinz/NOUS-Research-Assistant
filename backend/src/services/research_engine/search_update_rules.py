"""Pure scheduled search update rules (GOO-319). No I/O.

- **Fires.** A schedule is a 5-field cron in an IANA timezone, at most
  hourly (the minute field is one value). Fires are computed on the local
  wall clock and identified by the naive local minute, so the repeated
  01:30 on a fall-back day is one fire and a non-existent spring-forward
  01:30 is one fire, normalized forward by ``zoneinfo``. Missed fires are
  coalesced: only the latest one is returned, with how many were skipped.
- **Strategy hash.** The exact GOO-298 construction in ``step_executor``.
- **Delta classes.** Each work in baseline ∪ current results is ``new``,
  ``changed``, ``corrected_retracted``, ``unchanged`` or ``unknown`` with a
  reason. A work missing from today's results is never retracted or deleted:
  it is ``unknown`` (``provider_failed``, ``provider_capped`` or
  ``not_returned``). Only a Crossref ``update-to`` notice makes a work
  ``corrected_retracted``; ``unchanged`` needs a performed publication check.
  Workspace withdrawal is not an input here at all.
"""

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery.schedules import crontab

DELTA_CLASSES = ("new", "changed", "corrected_retracted", "unchanged", "unknown")
UNKNOWN_REASONS = (
    "provider_failed",
    "provider_capped",
    "not_returned",
    "no_doi_publication_check_not_performed",
    "merge_unresolved",
)
NOTICE_TYPES = (
    "retraction",
    "correction",
    "erratum",
    "expression_of_concern",
    "withdrawal",
    "removal",
)
# The provider record fields that make up a source version. Abstracts are
# left out: the GOO-300 export (the baseline) redacts non-CC0 abstracts, so
# they could never be compared.
VERSION_FIELDS = ("title", "authors", "venue", "year", "provider_updated")
# Which identifier kinds a provider can return a work by; a provider cap or
# failure only explains a missing work it could have returned.
PROVIDER_KINDS: dict[str, frozenset[str]] = {
    "crossref": frozenset({"doi"}),
    "pubmed": frozenset({"pmid", "pmcid"}),
    "openalex": frozenset({"openalex", "doi"}),
    "semantic_scholar": frozenset({"semantic_scholar", "doi", "arxiv_base"}),
    "arxiv": frozenset({"arxiv_base"}),
}
CAPPED = frozenset({"cap_reached", "more_available"})
LOCAL_FORMAT = "%Y-%m-%dT%H:%M"
# ponytail: fires older than this are neither run nor counted as missed.
MAX_LOOKBACK = timedelta(days=400)
_NEVER_HORIZON = timedelta(days=4 * 366 + 2)  # a Feb 29 fires within 4 years


# --- Fires -------------------------------------------------------------------


def parse_schedule(cron: str, timezone_name: str) -> tuple[crontab, ZoneInfo]:
    """Validated (crontab, ZoneInfo); ``ValueError`` (a 422) otherwise."""
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Unknown timezone") from error
    try:
        schedule = crontab.from_string(cron.strip())
    except (ValueError, TypeError, IndexError) as error:
        raise ValueError("Invalid cron expression") from error
    if len(schedule.minute) != 1:
        raise ValueError("Schedules run at most hourly")
    start = datetime(2026, 1, 1)
    if next(_candidates(schedule, start, start + _NEVER_HORIZON), None) is None:
        raise ValueError("This cron expression never fires")
    return schedule, tz


def _matches(schedule: crontab, day: date) -> bool:
    return (
        day.month in schedule.month_of_year
        and day.day in schedule.day_of_month
        and day.isoweekday() % 7 in schedule.day_of_week  # cron: Sunday = 0
    )


def _candidates(
    schedule: crontab, after: datetime, until: datetime
) -> Iterator[datetime]:
    """Naive local fire minutes in ``(after, until]``, in wall-clock order."""
    minute = next(iter(schedule.minute))
    day = after.date()
    while datetime.combine(day, time()) <= until:
        if _matches(schedule, day):
            for hour in sorted(schedule.hour):
                local = datetime.combine(day, time(hour, minute))
                if after < local <= until:
                    yield local
        day += timedelta(days=1)


def to_utc(local: datetime, tz: ZoneInfo) -> datetime:
    """``fold=0``: the first of a repeated time; a gap time moves forward."""
    return local.replace(tzinfo=tz).astimezone(timezone.utc)


def local_now(now_utc: datetime, tz: ZoneInfo) -> datetime:
    return now_utc.astimezone(tz).replace(tzinfo=None)


def due_fire(
    schedule: crontab,
    tz: ZoneInfo,
    last_local: datetime | None,
    created_local: datetime,
    now_utc: datetime,
) -> tuple[datetime, datetime, int] | None:
    """The latest fire ``<= now`` after both the last claimed fire and the
    schedule version's creation: ``(scheduled_local, scheduled_for,
    missed)``, ``missed`` being the earlier fires coalesced into it."""
    start = created_local if last_local is None else max(last_local, created_local)
    start = max(start, local_now(now_utc - MAX_LOOKBACK, tz))
    # +2h covers the repeated hour on a fall-back day.
    until = local_now(now_utc, tz) + timedelta(hours=2)
    due = [c for c in _candidates(schedule, start, until) if to_utc(c, tz) <= now_utc]
    if not due:
        return None
    return due[-1], to_utc(due[-1], tz), len(due) - 1


def next_fire(
    schedule: crontab, tz: ZoneInfo, after_local: datetime, now_utc: datetime
) -> datetime | None:
    """The first fire after ``after_local`` that is still in the future."""
    until = local_now(now_utc, tz) + _NEVER_HORIZON
    for candidate in _candidates(schedule, after_local, until):
        if to_utc(candidate, tz) > now_utc:
            return candidate
    return None


# --- Hashes ------------------------------------------------------------------


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def strategy_hash(strategy: Mapping[str, Any]) -> str:
    """``step_executor``'s ``strategy_version``: sha256 over the strategy
    without its own hash, ``json.dumps(sort_keys, compact)``."""
    body = {k: v for k, v in strategy.items() if k != "strategy_version"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def record_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    """The version fields a provider record actually carries; a missing
    field stays absent, it never defaults."""
    fields: dict[str, Any] = {}
    for name in VERSION_FIELDS:
        value = record.get(name)
        if isinstance(value, str):
            value = value.strip()
        if name == "authors" and isinstance(value, list):
            value = [str(a).strip() for a in value if str(a).strip()]
        if name == "year" and isinstance(value, int):
            value = str(value)
        if value not in (None, "", []):
            fields[name] = value
    return fields


def version_key(record: Mapping[str, Any]) -> str:
    return canonical_sha256(record_fields(record))


def snapshot_entry(
    identifiers: Mapping[str, str], record: Mapping[str, Any]
) -> dict[str, Any]:
    """One work in a corpus snapshot: identifiers plus its source version."""
    fields = record_fields(record)
    return {
        "identifiers": dict(sorted(identifiers.items())),
        "fields": fields,
        "version_key": canonical_sha256(fields),
    }


# --- Classification ----------------------------------------------------------


def _publication(
    doi: str | None,
    notices: Mapping[str, Sequence[Mapping[str, Any]]],
    failed: set[str],
) -> dict[str, Any]:
    if doi is None:
        return {"check": "not_performed", "reason": "no_doi"}
    if doi in failed:
        return {"check": "failed", "source": "crossref", "doi": doi}
    if doi not in notices:
        return {"check": "not_performed", "reason": "check_capped", "doi": doi}
    # Only correction/retraction types count; e.g. ``new_version`` is kept
    # as an other update, never as a correction.
    return {
        "check": "performed",
        "source": "crossref",
        "doi": doi,
        "notices": [dict(n) for n in notices[doi] if n["type"] in NOTICE_TYPES],
        "other_updates": [
            dict(n) for n in notices[doi] if n["type"] not in NOTICE_TYPES
        ],
    }


def _missing_reason(
    identifiers: Mapping[str, str], coverage: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Why a baseline work is absent from today's results: a failure or cap
    of a provider that could have returned it, else ``not_returned``."""
    providers = coverage.get("providers") or {}
    kinds = set(identifiers)
    covering = {
        name: receipt
        for name, receipt in providers.items()
        if not kinds or PROVIDER_KINDS.get(name, frozenset()) & kinds
    }
    evidence = {
        "providers": {
            name: {"status": r.get("status"), "completion": r.get("completion")}
            for name, r in sorted(covering.items())
        }
    }
    if any(r.get("status") != "ok" for r in covering.values()):
        return "provider_failed", evidence
    if any(r.get("completion") in CAPPED for r in covering.values()):
        return "provider_capped", evidence
    return "not_returned", evidence


def _differing(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Fields both sides carry with different values."""
    return {
        name: {"before": before[name], "after": after[name]}
        for name in VERSION_FIELDS
        if name in before and name in after and before[name] != after[name]
    }


def classify(
    baseline: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
    coverage: Mapping[str, Any],
    notices: Mapping[str, Sequence[Mapping[str, Any]]],
    notice_check_failed: set[str],
    *,
    merges: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """``{"counts": {class: n}, "items": [...]}`` sorted by report id.

    ``merges`` maps a baseline report that is no longer live to its live
    survivor, or to ``None`` when it cannot be resolved (a split). Identity
    is GOO-299's: nothing here ever equates two works by title.
    """
    merges = merges or {}
    resolved: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    for report_id in sorted(baseline):
        entry = dict(baseline[report_id])
        if report_id not in merges:
            resolved.setdefault(report_id, {**entry, "merged_from": []})
            continue
        target = merges[report_id]
        if target is None:
            items.append(
                {
                    "report_id": report_id,
                    "class": "unknown",
                    "reason": "merge_unresolved",
                    "evidence": {"baseline": entry},
                    "publication": {"check": "not_performed", "reason": "merge"},
                }
            )
            continue
        survivor = resolved.setdefault(
            target, {**dict(baseline.get(target) or entry), "merged_from": []}
        )
        survivor["merged_from"].append(report_id)
    for report_id in sorted(set(resolved) | set(current)):
        before, after = resolved.get(report_id), current.get(report_id)
        identifiers = dict((after or before or {}).get("identifiers") or {})
        publication = _publication(identifiers.get("doi"), notices, notice_check_failed)
        item: dict[str, Any] = {"report_id": report_id, "publication": publication}
        evidence: dict[str, Any] = {}
        if before is not None and before["merged_from"]:
            evidence["merged_from"] = before["merged_from"]
        if publication.get("notices"):
            item["class"] = "corrected_retracted"
            evidence["notices"] = publication["notices"]
            evidence["types"] = sorted({n["type"] for n in publication["notices"]})
        elif after is None:
            item["class"] = "unknown"
            item["reason"], providers = _missing_reason(identifiers, coverage)
            evidence.update(providers)
        elif before is None:
            item["class"] = "new"
            evidence["current_version_key"] = after["version_key"]
        else:
            fields = _differing(before["fields"], after["fields"])
            if before["version_key"] != after["version_key"] and fields:
                item["class"] = "changed"
                evidence.update(
                    baseline_version_key=before["version_key"],
                    current_version_key=after["version_key"],
                    fields=fields,
                )
            elif publication["check"] == "performed":
                item["class"] = "unchanged"
            else:
                item["class"] = "unknown"
                item["reason"] = {
                    "failed": "provider_failed",
                    "not_performed": (
                        "provider_capped"
                        if publication.get("reason") == "check_capped"
                        else "no_doi_publication_check_not_performed"
                    ),
                }[publication["check"]]
        item["evidence"] = evidence
        items.append(item)
    items.sort(key=lambda i: i["report_id"])
    counts = {name: 0 for name in DELTA_CLASSES}
    for item in items:
        counts[item["class"]] += 1
    return {"counts": counts, "items": items}


# --- Derived schedule status -------------------------------------------------

BLOCKING_REASONS = frozenset(
    {
        "owner_access_revoked",
        "owner_role_revoked",
        "project_archived",
        "protocol_changed",
    }
)


def schedule_status(enabled: bool, last_attempt: Mapping[str, Any] | None) -> str:
    """Never stored: ``disabled``, ``scheduled`` (never fired), ``running``,
    ``blocked`` (access or protocol), ``failed`` or ``ok``."""
    if not enabled:
        return "disabled"
    if last_attempt is None:
        return "scheduled"
    outcome = last_attempt["outcome"]
    if outcome == "started":
        return "running"
    if outcome == "failed":
        return "blocked" if last_attempt.get("reason") in BLOCKING_REASONS else "failed"
    return "ok"
