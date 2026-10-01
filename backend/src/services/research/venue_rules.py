"""Pure statement, identity and venue rules (GOO-316): no database, no FastAPI.

A statement set is one versioned document: authorship order and identity,
CRediT roles per author, and the funding, conflicts, ethics, limitations,
availability and licence statements. A literal ``null`` means *explicitly
missing*; nothing here ever fills one in. ORCID status is ``authenticated``
only with a retained OAuth receipt for the author's linked user (a name
match never upgrades it). The one venue profile, ``generic-icmje-credit/1``,
is built from public checklists (ICMJE Recommendations, NISO CRediT, IMRaD)
and is not modelled on any journal.

ponytail: one code-defined profile; a journal-specific profile is a new id or
version, never an edit to this one. No ROR ids, no automated CRediT
suggestion, no ORCID record writes.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from src.services.research_engine.contracts import canonical_json_sha256

STATEMENTS_SCHEMA = "nous.statements/1"
STATEMENTS_PART_SCHEMA = "nous.manuscript-statements/1"
CREDIT_VOCABULARY = "credit/1"
# NISO CRediT (ANSI/NISO Z39.104-2022), as slugs.
CREDIT_ROLES_V1: tuple[str, ...] = (
    "conceptualization",
    "data-curation",
    "formal-analysis",
    "funding-acquisition",
    "investigation",
    "methodology",
    "project-administration",
    "resources",
    "software",
    "supervision",
    "validation",
    "visualization",
    "writing-original-draft",
    "writing-review-editing",
)
PROFILE_ID = "generic-icmje-credit"
PROFILE_VERSION = 1
SPDX_TEXT_LICENSES = frozenset({"CC-BY-4.0", "CC-BY-SA-4.0", "CC0-1.0"})
REQUIRED_HEADINGS = ("Abstract", "Introduction", "Methods", "Results", "Discussion")
REQUIRED_STATEMENTS = ("conflicts", "ethics", "limitations", "data_availability")
TEXT_STATEMENTS = (*REQUIRED_STATEMENTS, "code_availability")
REDACTED = "[redacted]"
# ponytail: identities shorter than this are skipped (a two-letter string
# would redact ordinary words); full names, emails and ids are far longer.
MIN_IDENTITY = 3

_ORCID = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
_DOC = re.compile(r"\[Doc (\d+)\]")
_HEADING = re.compile(r"^(#{1,3})\s")
_ACK = re.compile(r"^#{1,3}\s*Acknowledg", re.IGNORECASE)
_TEXT_SUFFIXES = (".md", ".bib", ".svg", ".csv", ".txt", ".tsv", ".html")


class StatementError(ValueError):
    """A statement body the API answers with 422 (``field`` names it)."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(f"{field}: {detail}")
        self.field = field
        self.detail = detail


# --- Statement bodies ---


def _text(value: Any, field: str, limit: int = 20000) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise StatementError(field, "must be text or null")
    return value


def _texts(value: Any, field: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise StatementError(field, "must be a list or null")
    return [str(_text(v, f"{field}[{i}]", 500)) for i, v in enumerate(value)]


def _author(raw: Any, index: int) -> dict[str, Any]:
    field = f"authors[{index}]"
    if not isinstance(raw, Mapping):
        raise StatementError(field, "must be an object")
    key = raw.get("author_key")
    if not isinstance(key, str) or not 1 <= len(key) <= 64:
        raise StatementError(f"{field}.author_key", "1-64 characters")
    order = raw.get("order")
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        raise StatementError(f"{field}.order", "must be a positive integer")
    user_id = raw.get("user_id")
    if user_id is not None:
        try:
            user_id = str(UUID(str(user_id)))
        except ValueError as error:
            raise StatementError(f"{field}.user_id", "must be a UUID") from error
    orcid = raw.get("orcid")
    if orcid is not None and (not isinstance(orcid, str) or not _ORCID.match(orcid)):
        raise StatementError(f"{field}.orcid", "must look like 0000-0000-0000-000X")
    roles = raw.get("credit_roles")
    if roles is not None:
        if not isinstance(roles, list) or any(r not in CREDIT_ROLES_V1 for r in roles):
            raise StatementError(f"{field}.credit_roles", "unknown CRediT role")
        roles = [r for r in CREDIT_ROLES_V1 if r in roles]
    corresponding = raw.get("corresponding", False)
    if not isinstance(corresponding, bool):
        raise StatementError(f"{field}.corresponding", "must be true or false")
    return {
        "author_key": key,
        "order": order,
        "display_name": _text(raw.get("display_name"), f"{field}.display_name", 255),
        "affiliations": _texts(raw.get("affiliations"), f"{field}.affiliations"),
        "email": _text(raw.get("email"), f"{field}.email", 255),
        "corresponding": corresponding,
        "user_id": user_id,
        "orcid": orcid,
        "credit_roles": roles,
    }


def validate_statement_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """The canonical body (authors in ``order``; absent keys become explicit
    ``null``), or ``StatementError``. Never fills a missing value."""
    raw_authors = body.get("authors")
    authors = None
    if raw_authors is not None:
        if not isinstance(raw_authors, list):
            raise StatementError("authors", "must be a list or null")
        authors = sorted(
            (_author(a, i) for i, a in enumerate(raw_authors)),
            key=lambda a: int(a["order"]),
        )
        for name in ("author_key", "order"):
            values = [a[name] for a in authors]
            if len(set(values)) != len(values):
                raise StatementError(f"authors.{name}", "must be unique")
    funding = body.get("funding")
    if funding is not None:
        if not isinstance(funding, Mapping):
            raise StatementError("funding", "must be an object or null")
        grants = funding.get("grants")
        if grants is not None and not isinstance(grants, list):
            raise StatementError("funding.grants", "must be a list or null")
        funding = {
            "text": _text(funding.get("text"), "funding.text"),
            "grants": (
                None
                if grants is None
                else [
                    {
                        "funder": _text(g.get("funder"), f"funding.grants[{i}]", 255),
                        "award_id": _text(
                            g.get("award_id"), f"funding.grants[{i}].award_id", 255
                        ),
                    }
                    for i, g in enumerate(grants)
                    if isinstance(g, Mapping)
                ]
            ),
        }
    licenses = body.get("licenses")
    if licenses is not None:
        if not isinstance(licenses, Mapping):
            raise StatementError("licenses", "must be an object or null")
        licenses = {
            name: _text(licenses.get(name), f"licenses.{name}", 64)
            for name in ("text", "data", "code")
        }
    return {
        "authors": authors,
        "funding": funding,
        **{name: _text(body.get(name), name) for name in TEXT_STATEMENTS},
        "licenses": licenses,
    }


def set_hash(body: Mapping[str, Any]) -> str:
    """Binds the schema and CRediT vocabulary with the body."""
    return canonical_json_sha256(
        {
            "schema": STATEMENTS_SCHEMA,
            "credit_vocabulary": CREDIT_VOCABULARY,
            "body": dict(body),
        }
    )


def _item(rule: str, field: str, detail: str, fix: str) -> dict[str, str]:
    return {"rule": rule, "field": field, "detail": detail, "fix": fix}


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def required_items(body: Mapping[str, Any]) -> list[dict[str, str]]:
    """The profile's required statement fields that are missing, each with
    fix text. ``missing_fields`` is their ``field`` values."""
    items = []
    authors = list(body.get("authors") or [])
    if not authors:
        items.append(
            _item("required", "authors", "No authors", "Add at least one author")
        )
    for i, author in enumerate(authors):
        if _blank(author.get("display_name")):
            items.append(
                _item(
                    "required",
                    f"authors[{i}].display_name",
                    "Author name is missing",
                    f"Enter the name of author {i + 1}",
                )
            )
        if not author.get("credit_roles"):
            items.append(
                _item(
                    "required",
                    f"authors[{i}].credit_roles",
                    "No CRediT role",
                    f"Select at least one CRediT role for author {i + 1}",
                )
            )
    corresponding = [i for i, a in enumerate(authors) if a.get("corresponding")]
    if authors and len(corresponding) != 1:
        items.append(
            _item(
                "required",
                "authors.corresponding",
                f"{len(corresponding)} corresponding authors",
                "Mark exactly one corresponding author",
            )
        )
    elif corresponding and _blank(authors[corresponding[0]].get("email")):
        i = corresponding[0]
        items.append(
            _item(
                "required",
                f"authors[{i}].email",
                "The corresponding author has no email",
                f"Add an email for author {i + 1}",
            )
        )
    if _blank((body.get("funding") or {}).get("text")):
        items.append(
            _item(
                "required",
                "funding.text",
                "Funding statement is missing",
                "State the funding sources, or that there was no funding",
            )
        )
    for name in REQUIRED_STATEMENTS:
        if _blank(body.get(name)):
            label = name.replace("_", " ")
            items.append(
                _item(
                    "required",
                    name,
                    f"The {label} statement is missing",
                    f"Write the {label} statement",
                )
            )
    licence = (body.get("licenses") or {}).get("text")
    if licence not in SPDX_TEXT_LICENSES:
        items.append(
            _item(
                "required",
                "licenses.text",
                (
                    "No accepted text licence"
                    if licence is None
                    else "Licence not accepted"
                ),
                "Choose CC-BY-4.0, CC-BY-SA-4.0 or CC0-1.0",
            )
        )
    return items


def missing_fields(body: Mapping[str, Any]) -> list[str]:
    return [item["field"] for item in required_items(body)]


# --- ORCID ---


def orcid_status(
    author: Mapping[str, Any], auths: Iterable[Mapping[str, Any]]
) -> tuple[str, dict[str, Any] | None]:
    """``authenticated`` (with the receipt) iff the author has an iD *and* a
    linked user *and* that user has an OAuth receipt for the same iD;
    otherwise ``unauthenticated`` with an iD and ``unknown`` without one.
    Names are never compared."""
    orcid, user_id = author.get("orcid"), author.get("user_id")
    if not orcid:
        return "unknown", None
    receipts = sorted(
        (
            a
            for a in auths
            if user_id is not None
            and str(a.get("user_id")) == str(user_id)
            and a.get("orcid") == orcid
        ),
        key=lambda a: str(a.get("token_received_at")),
    )
    if not receipts:
        return "unauthenticated", None
    latest = receipts[-1]
    return "authenticated", {
        "authentication_id": str(latest.get("id")),
        "environment": latest.get("environment"),
        "token_received_at": latest.get("token_received_at"),
    }


# --- Anonymization ---


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _fold(text: str) -> str:
    return _nfc(_nfc(text).casefold())


def _identities(identities: Iterable[str]) -> list[str]:
    """NFC, stripped, deduplicated, longest first (so a full name is replaced
    before any shorter identity inside it)."""
    seen = {_nfc(str(i)).strip() for i in identities if i}
    return sorted((i for i in seen if len(i) >= MIN_IDENTITY), key=len, reverse=True)


def _redact_text(text: str, identities: Sequence[str]) -> str:
    text = _nfc(text)
    for identity in identities:
        text = re.sub(re.escape(identity), REDACTED, text, flags=re.IGNORECASE)
    return text


def _redact_value(value: Any, identities: Sequence[str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, identities)
    if isinstance(value, list):
        return [_redact_value(v, identities) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v, identities) for k, v in value.items()}
    return value


def _drop_acknowledgements(text: str) -> str:
    """Remove each ``Acknowledg...`` section up to the next heading of the
    same or a higher level."""
    out: list[str] = []
    level = 0
    for line in text.splitlines(keepends=True):
        heading = _HEADING.match(line)
        if level and heading and len(heading.group(1)) <= level:
            level = 0
        if not level and _ACK.match(line):
            level = len(line) - len(line.lstrip("#"))
            continue
        if not level:
            out.append(line)
    return "".join(out)


def _redact_statements(body: Any) -> Any:
    """Designated identity fields of the packaged statement set."""
    if not isinstance(body, dict) or not isinstance(body.get("statements"), dict):
        return body
    statements = dict(body["statements"])
    statements["authors"] = [
        {
            **author,
            **{
                name: (
                    None
                    if author.get(name) is None
                    else (
                        [REDACTED for _ in author[name]]
                        if name == "affiliations"
                        else REDACTED
                    )
                )
                for name in (
                    "display_name",
                    "affiliations",
                    "email",
                    "orcid",
                    "user_id",
                )
            },
        }
        for author in statements.get("authors") or []
    ]
    funding = statements.get("funding")
    if isinstance(funding, dict) and funding.get("grants"):
        statements["funding"] = {
            **funding,
            "grants": [
                {**g, "award_id": None if g.get("award_id") is None else REDACTED}
                for g in funding["grants"]
            ],
        }
    return {**body, "statements": statements}


def dump_json(value: Any) -> bytes:
    """``audit_bundle._dump``'s byte format."""
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False).encode()


def anonymize(
    members: Mapping[str, bytes], body: Mapping[str, Any], identities: Iterable[str]
) -> dict[str, bytes]:
    """The anonymized variant of package parts (``manifest.json`` and
    ``SHA256SUMS`` are the writer's): statement identity fields replaced,
    acknowledgements dropped, every identity string (and the statement
    body's own identity values) redacted, JSON packages re-sealed. A binary
    member is copied; ``scan_leaks`` is the proof, never this redaction."""
    names = list(identities)
    for author in body.get("authors") or []:
        names += [author.get(k) or "" for k in ("display_name", "email", "orcid")]
        names += list(author.get("affiliations") or [])
        names.append(str(author.get("user_id") or ""))
    for grant in (body.get("funding") or {}).get("grants") or []:
        names.append(grant.get("award_id") or "")
    ids = _identities(names)
    out: dict[str, bytes] = {}
    for path, data in members.items():
        if path.endswith(".json"):
            package = json.loads(data)
            if isinstance(package, dict) and "body" in package:
                inner = package["body"]
                if path == "statements.json":
                    inner = _redact_statements(inner)
                package = _redact_value({**package, "body": inner}, ids)
                package["body_sha256"] = canonical_json_sha256(package["body"])
            else:
                package = _redact_value(package, ids)
            out[path] = dump_json(package)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            out[path] = data
            continue
        if path.endswith(".md"):
            text = _drop_acknowledgements(text)
        out[path] = _redact_text(text, ids).encode("utf-8")
    return out


def scan_leaks(members: Mapping[str, bytes], identities: Iterable[str]) -> list[str]:
    """Members whose bytes contain any identity (case-insensitive, NFC); JSON
    members are also scanned decoded, so ``\\u`` escapes cannot hide one."""
    folded = [_fold(i) for i in _identities(identities)]
    token = _fold(REDACTED)
    leaks = []
    for path, data in sorted(members.items()):
        texts = [data.decode("utf-8", errors="ignore")]
        if path.endswith(".json"):
            try:
                texts.append(json.dumps(json.loads(data), ensure_ascii=False))
            except ValueError:
                pass
        haystack = "\n".join(_fold(t).replace(token, "\0") for t in texts)
        if any(identity in haystack for identity in folded):
            leaks.append(path)
    return leaks


# --- The venue profile ---


def _statements_body(members: Mapping[str, bytes]) -> Mapping[str, Any] | None:
    raw = members.get("statements.json")
    if raw is None:
        return None
    return dict(json.loads(raw)["body"]["statements"])


def check_profile(
    snapshot: Mapping[str, Any],
    members: Mapping[str, bytes],
    anonymized: Mapping[str, bytes] | None,
    identities: Iterable[str],
) -> dict[str, Any]:
    """``generic-icmje-credit/1`` against one exact package: required fields,
    formatting and anonymization, each failing rule an actionable item
    ``{rule, field, detail, fix}``. A package with no anonymized variant
    (built before GOO-316) has anonymization ``unknown`` and cannot pass."""
    items: list[dict[str, str]] = []
    if _blank((snapshot.get("draft") or {}).get("title")):
        items.append(_item("required", "title", "No title", "Give the draft a title"))
    body = _statements_body(members)
    if body is None:
        items.append(
            _item(
                "required",
                "statements",
                "No statement set is bound to this package",
                "Record and approve a statement set, then rebuild the candidate",
            )
        )
    else:
        items += required_items(body)
    required = "fail" if items else "pass"
    formatting: list[dict[str, str]] = []
    source = members.get("manuscript.source.md", b"").decode("utf-8", errors="ignore")
    for heading in REQUIRED_HEADINGS:
        if not re.search(rf"^#{{1,3}}\s*{heading}\b", source, re.I | re.M):
            formatting.append(
                _item(
                    "formatting",
                    f"heading:{heading}",
                    f"No '{heading}' heading",
                    f"Add a '{heading}' heading (#, ## or ###)",
                )
            )
    references = list(snapshot.get("references") or [])
    keys = {str(r.get("key")) for r in references}
    for number in sorted(set(_DOC.findall(source)), key=int):
        if f"doc{number}" not in keys:
            formatting.append(
                _item(
                    "formatting",
                    f"[Doc {number}]",
                    "The citation marker resolves to no saved reference",
                    f"Save the cited document as Doc {number} or remove the marker",
                )
            )
    for reference in references:
        if _blank(reference.get("title")):
            formatting.append(
                _item(
                    "formatting",
                    f"references.{reference.get('key')}.title",
                    "Reference has no title",
                    "Add a title to the reference record",
                )
            )
    items += formatting
    if anonymized is None:
        anonymization = "unknown"
        items.append(
            _item(
                "anonymization",
                "package.anonymized",
                "This package has no anonymized variant",
                "Rebuild the candidate to produce an anonymized variant",
            )
        )
    else:
        leaks = scan_leaks(anonymized, identities)
        anonymization = "fail" if leaks else "pass"
        items += [
            _item(
                "anonymization",
                f"anonymized:{path}",
                "An identity string remains in this member",
                "Remove the identifying text and rebuild the candidate",
            )
            for path in leaks
        ]
    return {
        "profile_id": PROFILE_ID,
        "profile_version": PROFILE_VERSION,
        "status": "fail" if items else "pass",
        "rules": {
            "required": required,
            "formatting": "fail" if formatting else "pass",
            "anonymization": anonymization,
        },
        "items": items,
    }
