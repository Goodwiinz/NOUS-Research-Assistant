"""Pure audit-bundle writer and verifier (GOO-308): parts fed as bytes, no DB.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-308 section):
``verify_bundle`` returning before its SHA256SUMS comparison makes
``-k tampered`` fail (a flipped byte in ``claims.json`` verifies).
"""

import hashlib
import io
import json
import zipfile
from typing import Any
from uuid import uuid4

import pytest

from src.services.research_engine import corpus_export
from src.services.research_engine.audit_bundle import (
    SCHEMA,
    BundleError,
    Part,
    verify_bundle,
    write_zip,
)
from src.services.research_engine.contracts import canonical_json_sha256

PROJECT = str(uuid4())
DRAFT = str(uuid4())
CONTENT = "## Results\nThe trial enrolled 412 participants [Doc 1].\n"
META: dict[str, Any] = {
    "project_id": PROJECT,
    "generated_at": "2026-09-30T00:00:00+00:00",
    "deployment_sha": "abc123",
    "protocol_version_id": None,
    "stream_heads": {f"research_claims:{PROJECT}": 4},
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _package(schema: str, body: Any) -> Part:
    package = {
        "schema": schema,
        "body_sha256": canonical_json_sha256(body),
        "body": body,
    }
    data = json.dumps(package, sort_keys=True, indent=2).encode()
    return Part(f"{schema}.json", schema, data, package["body_sha256"])


def _parts(claims_hash: str | None = None) -> list[Part]:
    corpus = corpus_export.seal(
        {
            "project": {"collection_id": PROJECT, "name": "p"},
            "searches": [],
            "imports": [],
            "identities": {"reports": [], "observations": [], "decisions": []},
        },
        "2026-09-30T00:00:00+00:00",
    )
    corpus_data = corpus_export.render(corpus, "json")[0]
    digest = _sha(CONTENT.encode())
    claims = _package(
        "claims",
        {
            "drafts": [
                {"id": DRAFT, "version": 1, "content_hash": claims_hash or digest}
            ]
        },
    )
    checks = _package(
        "checks", [{"draft_id": DRAFT, "draft_version": 1, "content_hash": digest}]
    )
    methods = _package("methods", [])
    labelled = f"> Status: CANDIDATE\n\n{CONTENT}".encode()
    source = CONTENT.encode()
    return [
        Part("corpus.json", corpus["schema"], corpus_data, corpus["body_sha256"]),
        Part("claims.json", claims.schema, claims.data, claims.body_sha256),
        Part(
            "drafts/release-checks.json", checks.schema, checks.data, checks.body_sha256
        ),
        Part(f"drafts/{DRAFT}-v1.md", None, labelled, _sha(labelled)),
        Part(f"drafts/{DRAFT}-v1.source.md", None, source, _sha(source)),
        Part("methods.json", methods.schema, methods.data, methods.body_sha256, True),
    ]


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _rezip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _resum(members: dict[str, bytes]) -> dict[str, bytes]:
    """Recompute SHA256SUMS (and the manifest's file sha) after an edit."""
    manifest = json.loads(members["manifest.json"])
    for part in manifest["parts"]:
        part["sha256"] = _sha(members[part["path"]])
        part["bytes"] = len(members[part["path"]])
    members["manifest.json"] = json.dumps(manifest).encode()
    members["SHA256SUMS"] = "".join(
        f"{_sha(d)}  {p}\n" for p, d in sorted(members.items()) if p != "SHA256SUMS"
    ).encode()
    return members


def test_manifest_lists_every_part_with_sha_bytes_and_body_hash() -> None:
    parts = _parts()
    data, manifest_sha = write_zip(parts, **META)
    members = _members(data)
    assert manifest_sha == _sha(members["manifest.json"])
    manifest = json.loads(members["manifest.json"])
    assert manifest["schema"] == SCHEMA
    assert {k: manifest[k] for k in META} == META
    assert [p["path"] for p in manifest["parts"]] == sorted(p.path for p in parts)
    for part in parts:
        [entry] = [e for e in manifest["parts"] if e["path"] == part.path]
        assert entry == {
            "path": part.path,
            "schema": part.schema,
            "sha256": _sha(part.data),
            "bytes": len(part.data),
            "body_sha256": part.body_sha256,
            "status": "empty" if part.empty else "ok",
        }
    assert verify_bundle(data)["manifest_sha256"] == manifest_sha


def test_sha256sums_is_sha256sum_compatible_and_covers_manifest() -> None:
    members = _members(write_zip(_parts(), **META)[0])
    lines = members["SHA256SUMS"].decode().splitlines()
    listed = {}
    for line in lines:
        digest, path = line.split("  ", 1)  # sha256sum's text-mode separator
        assert len(digest) == 64
        listed[path] = digest
    assert set(listed) == set(members) - {"SHA256SUMS"}
    assert "manifest.json" in listed
    assert all(_sha(members[p]) == d for p, d in listed.items())


def test_empty_part_listed_with_status_empty_never_omitted() -> None:
    members = _members(write_zip(_parts(), **META)[0])
    manifest = json.loads(members["manifest.json"])
    [methods] = [p for p in manifest["parts"] if p["path"] == "methods.json"]
    assert methods["status"] == "empty"
    assert "methods.json" in members


def test_verify_bundle_rejects_tampered_member() -> None:
    members = _members(write_zip(_parts(), **META)[0])
    claims = bytearray(members["claims.json"])
    claims[-2] ^= 0x01
    members["claims.json"] = bytes(claims)
    with pytest.raises(BundleError, match="claims.json does not match SHA256SUMS"):
        verify_bundle(_rezip(members))


def test_verify_bundle_rejects_body_hash_mismatch() -> None:
    members = _members(write_zip(_parts(), **META)[0])
    package = json.loads(members["claims.json"])
    package["body"]["drafts"][0]["version"] = 2  # edited body, stale body_sha256
    members["claims.json"] = json.dumps(package).encode()
    with pytest.raises(BundleError, match="claims.json body does not match"):
        verify_bundle(_rezip(_resum(members)))


def test_verify_bundle_rejects_draft_not_matching_claims_package_hash() -> None:
    data, _ = write_zip(_parts(claims_hash="0" * 64), **META)
    with pytest.raises(BundleError, match="does not match the claims package"):
        verify_bundle(data)


def test_same_parts_same_member_bytes() -> None:
    first = _members(write_zip(_parts(), **META)[0])
    second = _members(
        write_zip(_parts(), **{**META, "generated_at": "2026-10-01T00:00:00+00:00"})[0]
    )
    assert set(first) == set(second)
    changed = {p for p in first if first[p] != second[p]}
    assert changed == {"manifest.json", "SHA256SUMS"}  # generated_at only
    assert write_zip(_parts(), **META)[0] == write_zip(_parts(), **META)[0]
