"""Pure statement, ORCID and venue-profile rules (GOO-316).

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-316 section):
``orcid_status`` treating any iD as authenticated makes ``-k orcid`` fail.
"""

import json
import unicodedata
from typing import Any
from uuid import uuid4

import pytest

from src.services.research import venue_rules as rules
from src.services.research_engine.contracts import canonical_json_sha256

U = str(uuid4())
ORCID_A = "0000-0002-1825-0097"
ORCID_B = "0000-0001-5109-3700"


def _author(key: str, order: int, **extra: Any) -> dict[str, Any]:
    return {
        "author_key": key,
        "order": order,
        "display_name": f"Author {key.upper()}",
        "affiliations": ["Fixture Institute"],
        "email": None,
        "corresponding": False,
        "user_id": None,
        "orcid": None,
        "credit_roles": ["methodology"],
        **extra,
    }


def _body(**overrides: Any) -> dict[str, Any]:
    body = {
        "authors": [
            _author("a", 1, corresponding=True, email="a@example.org"),
            _author("b", 2),
        ],
        "funding": {"text": "None", "grants": []},
        "conflicts": "None declared",
        "ethics": "Not required",
        "limitations": "Small sample",
        "data_availability": "On request",
        "code_availability": None,
        "licenses": {"text": "CC-BY-4.0", "data": None, "code": None},
    }
    return rules.validate_statement_body({**body, **overrides})


def _sealed(body: Any, schema: str = "x/1") -> bytes:
    return rules.dump_json(
        {"schema": schema, "body_sha256": canonical_json_sha256(body), "body": body}
    )


def test_unknown_credit_role_rejected() -> None:
    with pytest.raises(rules.StatementError) as error:
        _body(authors=[_author("a", 1, credit_roles=["vibes"])])
    assert error.value.field == "authors[0].credit_roles"
    with pytest.raises(rules.StatementError):
        _body(authors=[_author("a", 1, orcid="1234")])
    with pytest.raises(rules.StatementError):
        _body(authors=[_author("a", 1), _author("a", 2)])
    assert len(rules.CREDIT_ROLES_V1) == 14
    # Canonical order, independent of the request's order.
    body = _body(authors=[_author("a", 1, credit_roles=["software", "methodology"])])
    assert body["authors"][0]["credit_roles"] == ["methodology", "software"]


def test_explicit_null_reported_as_missing_with_fix_text() -> None:
    body = _body(limitations=None, ethics=None)
    assert body["limitations"] is None and body["ethics"] is None  # never filled
    items = rules.required_items(body)
    assert {i["field"] for i in items} == {"ethics", "limitations"}
    assert all(i["fix"] and i["rule"] == "required" for i in items)
    roles = _body(authors=[_author("a", 1, credit_roles=None, corresponding=True)])
    fields = rules.missing_fields(roles)
    assert "authors[0].credit_roles" in fields
    assert "authors[0].email" in fields
    item = next(
        i for i in rules.required_items(roles) if i["field"].endswith("credit_roles")
    )
    assert item["fix"] == "Select at least one CRediT role for author 1"
    assert rules.missing_fields(rules.validate_statement_body({})) == [
        "authors",
        "funding.text",
        "conflicts",
        "ethics",
        "limitations",
        "data_availability",
        "licenses.text",
    ]
    assert rules.missing_fields(_body()) == []
    assert rules.set_hash(_body()) != rules.set_hash(_body(limitations=None))


def test_orcid_name_match_is_not_authenticated() -> None:
    author = _author("b", 2, orcid=ORCID_B, display_name="Grace Hopper")
    receipt = {
        "id": str(uuid4()),
        "user_id": str(uuid4()),
        "orcid": ORCID_A,
        "name_claim": "Grace Hopper",
        "environment": "sandbox",
        "token_received_at": "2026-09-30T00:00:00+00:00",
    }
    assert rules.orcid_status(author, [receipt]) == ("unauthenticated", None)


def test_orcid_authenticated_requires_linked_user_and_matching_row() -> None:
    receipt = {
        "id": "r1",
        "user_id": U,
        "orcid": ORCID_A,
        "environment": "sandbox",
        "token_received_at": "2026-09-30T00:00:00+00:00",
    }
    linked = _author("a", 1, orcid=ORCID_A, user_id=U)
    status, shown = rules.orcid_status(linked, [receipt])
    assert status == "authenticated"
    assert shown == {
        "authentication_id": "r1",
        "environment": "sandbox",
        "token_received_at": "2026-09-30T00:00:00+00:00",
    }
    unlinked = _author("a", 1, orcid=ORCID_A)
    assert rules.orcid_status(unlinked, [receipt])[0] == "unauthenticated"
    other_id = _author("a", 1, orcid=ORCID_B, user_id=U)
    assert rules.orcid_status(other_id, [receipt])[0] == "unauthenticated"
    assert rules.orcid_status(linked, [])[0] == "unauthenticated"


def test_orcid_absent_is_unknown() -> None:
    assert rules.orcid_status(_author("c", 3, user_id=U), []) == ("unknown", None)


def _snapshot(title: str = "A study") -> dict[str, Any]:
    return {
        "draft": {"title": title},
        "references": [{"key": "doc1", "title": "Ref one"}],
    }


def _members(source: str, body: Any = None) -> dict[str, bytes]:
    members = {"manuscript.source.md": source.encode()}
    if body is not None:
        members["statements.json"] = _sealed({"statements": body})
    return members


FULL = (
    "# Abstract\nx\n## Introduction\nx\n## Methods\nx\n## Results\n"
    "y [Doc 1]\n## Discussion\nz\n"
)


def test_profile_flags_missing_heading_and_unresolved_doc_key() -> None:
    source = FULL.replace("## Discussion\nz\n", "") + "See [Doc 2].\n"
    result = rules.check_profile(_snapshot(), _members(source, _body()), {}, [])
    assert result["status"] == "fail"
    fields = {i["field"]: i for i in result["items"]}
    assert set(fields) == {"heading:Discussion", "[Doc 2]"}
    assert all(i["fix"] for i in result["items"])
    assert result["rules"] == {
        "required": "pass",
        "formatting": "fail",
        "anonymization": "pass",
    }
    passing = rules.check_profile(_snapshot(), _members(FULL, _body()), {}, [])
    assert passing["status"] == "pass" and passing["items"] == []
    legacy = rules.check_profile(_snapshot(), _members(FULL), None, [])
    assert legacy["rules"]["anonymization"] == "unknown"
    assert {i["field"] for i in legacy["items"]} == {"statements", "package.anonymized"}


def test_anonymize_removes_acknowledgements_and_identity_fields() -> None:
    body = _body(
        authors=[
            _author(
                "a",
                1,
                display_name="Ada Lovelace",
                email="ada@example.org",
                orcid=ORCID_A,
                user_id=U,
                corresponding=True,
            )
        ],
        funding={"text": "Grant", "grants": [{"funder": "NSF", "award_id": "AW-778"}]},
    )
    source = (
        "# Results\nAda Lovelace found it.\n## Acknowledgements\nThanks to Zed "
        "Quartermaine.\n### Detail\nmore thanks\n## Discussion\nEnd\n"
    )
    members = {
        "manuscript.md": source.encode(),
        "statements.json": _sealed(
            {"statement_set_id": "s", "statements": body}, rules.STATEMENTS_PART_SCHEMA
        ),
        "checks.json": _sealed({"actor": U, "note": "by ZED QUARTERMAINE"}),
        "figure.png": b"\x89PNG\x00\xff",
    }
    identities = ["Zed Quartermaine", U]
    out = rules.anonymize(members, body, identities)
    text = out["manuscript.md"].decode()
    assert "Acknowledg" not in text and "more thanks" not in text
    assert "## Discussion\nEnd" in text
    statements = json.loads(out["statements.json"])
    author = statements["body"]["statements"]["authors"][0]
    assert {author[k] for k in ("display_name", "email", "orcid", "user_id")} == {
        rules.REDACTED
    }
    assert author["affiliations"] == [rules.REDACTED]
    assert author["credit_roles"] == ["methodology"]
    grant = statements["body"]["statements"]["funding"]["grants"][0]
    assert grant == {"funder": "NSF", "award_id": rules.REDACTED}
    assert statements["body_sha256"] == canonical_json_sha256(statements["body"])
    assert out["figure.png"] == members["figure.png"]
    names = [*identities, "Ada Lovelace", "ada@example.org", ORCID_A, "AW-778"]
    assert rules.scan_leaks(out, names) == []
    assert set(rules.scan_leaks(members, names)) == {
        "manuscript.md",
        "statements.json",
        "checks.json",
    }


def test_scan_leaks_catches_reviewer_name_case_and_nfc_variants() -> None:
    reviewer = "José Müller"
    decomposed = unicodedata.normalize("NFD", reviewer)
    assert decomposed != reviewer
    assert rules.scan_leaks({"a.md": decomposed.encode()}, [reviewer]) == ["a.md"]
    assert rules.scan_leaks({"b.md": b"JOS\xc3\x89 M\xc3\x9cLLER"}, [reviewer]) == [
        "b.md"
    ]
    escaped = json.dumps({"n": reviewer}).encode()  # ensure_ascii escapes
    assert b"Jos" in escaped and reviewer.encode() not in escaped
    assert rules.scan_leaks({"c.json": escaped}, [reviewer]) == ["c.json"]
    assert rules.scan_leaks({"d.md": b"no names"}, [reviewer]) == []
    # The redaction token itself is never a leak.
    assert rules.scan_leaks({"e.md": rules.REDACTED.encode()}, ["Ted Act"]) == []


def test_profile_result_names_profile_and_version() -> None:
    result = rules.check_profile(_snapshot(""), _members(FULL, _body()), {}, [])
    assert (result["profile_id"], result["profile_version"]) == (
        "generic-icmje-credit",
        1,
    )
    assert [i["field"] for i in result["items"]] == ["title"]
    leaked = rules.check_profile(
        _snapshot(),
        _members(FULL, _body()),
        {"m.md": b"Quixote Zarzuela"},
        ["quixote zarzuela"],
    )
    assert leaked["rules"]["anonymization"] == "fail"
    assert leaked["items"][0]["field"] == "anonymized:m.md"
    assert "quixote" not in json.dumps(leaked).lower()
