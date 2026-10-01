"""Pure archive deposit rules (GOO-318).

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-318 section):
``reached`` trusting a succeeded publish attempt without the remote
``submitted`` flag and record id fails ``-k unpublished``; ``redact``
returning its input fails ``-k redact``.
"""

from uuid import uuid4

import pytest

from src.services.research import deposit_rules as rules
from src.services.research.deposit_rules import Attempt

pytestmark = pytest.mark.unit

ACCOUNT = "zenodo_sandbox:nous-fixture"
SHA = "a" * 64


def _chain(*steps: tuple[str, str], **last: object) -> list[Attempt]:
    chain = [
        Attempt(id=str(i), phase=phase, outcome=outcome)
        for i, (phase, outcome) in enumerate(steps)
    ]
    if last:
        chain[-1] = Attempt(**{**chain[-1].__dict__, **last})  # type: ignore[arg-type]
    return chain


def test_unpublished_draft_never_reports_published() -> None:
    base = [("prepared", "succeeded"), ("draft_created", "succeeded")]
    base.append(("files_uploaded", "succeeded"))
    # Our publish call "succeeded" but the remote never said submitted.
    chain = _chain(*base, ("published", "succeeded"), remote_record_id="7")
    assert rules.operation_status(chain) == "files_uploaded"
    assert rules.next_phase(chain) == "published"
    no_record = _chain(*base, ("published", "succeeded"), submitted=True)
    assert rules.operation_status(no_record) == "files_uploaded"
    done = _chain(
        *base, ("published", "succeeded"), submitted=True, remote_record_id="7"
    )
    assert rules.operation_status(done) == "published"
    assert rules.next_phase(done) == "verified"


def test_partial_upload_status_is_draft_created() -> None:
    chain = _chain(
        ("prepared", "succeeded"),
        ("draft_created", "succeeded"),
        ("files_uploaded", "failed"),
        retryable=True,
        reason="approval_invalid",
    )
    assert rules.operation_status(chain) == "draft_created"
    assert rules.next_phase(chain) == "files_uploaded"
    terminal = _chain(
        ("prepared", "succeeded"),
        ("draft_created", "succeeded"),
        ("files_uploaded", "failed"),
    )
    assert rules.operation_status(terminal) == "failed"
    assert rules.next_phase(terminal) is None


def test_unknown_last_attempt_is_ambiguous_and_next_phase_is_reconcile() -> None:
    chain = _chain(("prepared", "succeeded"), ("draft_created", "unknown"))
    assert rules.operation_status(chain) == "ambiguous"
    assert rules.next_phase(chain) == rules.RECONCILE
    assert rules.latest_remote(chain) is None
    with_id = _chain(
        ("prepared", "succeeded"),
        ("draft_created", "succeeded"),
        ("files_uploaded", "unknown"),
    )
    with_id[1] = Attempt(**{**with_id[1].__dict__, "remote_deposition_id": "42"})
    assert rules.next_phase(with_id) == rules.RECONCILE
    assert rules.latest_remote(with_id) is with_id[1]
    verified = _chain(*[(p, "succeeded") for p in rules.PHASES])
    verified[3] = Attempt(
        **{**verified[3].__dict__, "submitted": True, "remote_record_id": "7"}
    )
    assert rules.operation_status(verified) == "verified"
    assert rules.next_phase(verified) is None


def _approval(kind: str = "approved", **overrides: str) -> dict[str, str]:
    return {
        "id": str(uuid4()),
        "kind": kind,
        "package_sha256": SHA,
        "account_ref": ACCOUNT,
        "action": "publish",
        "repository": "zenodo_sandbox",
        **overrides,
    }


def test_revocation_or_changed_hash_voids_approval() -> None:
    granted = _approval()
    assert rules.valid_approval([granted], SHA, ACCOUNT) is granted
    assert rules.approval_valid([granted], SHA, ACCOUNT)
    # A changed release package, account or action: no approval applies.
    assert not rules.approval_valid([granted], "b" * 64, ACCOUNT)
    assert not rules.approval_valid([granted], SHA, "zenodo_sandbox:other")
    assert not rules.approval_valid([granted], SHA, ACCOUNT, "newversion")
    # The newest row wins: a revocation voids, a re-approval restores.
    revoked = _approval("revoked")
    assert not rules.approval_valid([granted, revoked], SHA, ACCOUNT)
    again = _approval()
    assert rules.valid_approval([granted, revoked, again], SHA, ACCOUNT) is again
    # A revocation for another account leaves this one in force.
    other = _approval("revoked", account_ref="zenodo_sandbox:other")
    assert rules.approval_valid([granted, other], SHA, ACCOUNT)
    assert not rules.approval_valid([], SHA, ACCOUNT)


def test_readback_md5_mismatch_and_missing_file_fail() -> None:
    files = [
        {"name": "package.zip", "sha256": SHA, "md5": "1" * 32},
        {"name": "references.csl.json", "sha256": SHA, "md5": "2" * 32},
    ]
    remote = {
        "record_id": "7",
        "doi": "10.5072/zenodo.7",
        "files": [
            {"name": "package.zip", "md5": "1" * 32},
            {"name": "references.csl.json", "md5": "2" * 32},
        ],
    }
    ok = {"expected_doi": "10.5072/zenodo.7", "expected_record_id": "7"}
    assert rules.check_readback(files, remote, **ok) == []
    corrupted = {**remote, "files": [{"name": "package.zip", "md5": "9" * 32}]}
    assert rules.check_readback(files, corrupted, **ok) == [
        "missing:references.csl.json",
        "checksum:package.zip",
    ]
    extra = {**remote, "files": [*remote["files"], {"name": "x", "md5": "3" * 32}]}
    assert rules.check_readback(files, extra, **ok) == ["extra:x"]
    assert rules.check_readback(
        files, remote, expected_doi="10.5072/zenodo.8", expected_record_id="8"
    ) == ["doi", "record_id"]
    assert rules.check_readback(
        files, remote, expected_doi=None, expected_record_id=None
    ) == ["doi", "record_id"]


def test_redact_drops_tokens_and_link_query_strings() -> None:
    token = "zen" + "odo-" + uuid4().hex
    raw = {
        "Authorization": f"Bearer {token}",
        "headers": {"authorization": f"Bearer {token}", "Accept": "json"},
        "access_token": token,
        "refresh_token": token,
        "client_secret": token,
        "password": token,
        "links": {
            "bucket": f"https://sandbox.zenodo.org/api/files/abc?access_token={token}",
            "html": "https://sandbox.zenodo.org/deposit/7",
        },
        "files": [{"links": {"self": f"https://x.example/f?token={token}"}}],
        "metadata": {"title": "Kept", "notes": "nous-operation:1"},
        "id": 7,
    }
    clean = rules.redact(raw)
    assert token not in repr(clean)
    assert clean == {
        "headers": {"Accept": "json"},
        "links": {
            "bucket": "https://sandbox.zenodo.org/api/files/abc",
            "html": "https://sandbox.zenodo.org/deposit/7",
        },
        "files": [{"links": {"self": "https://x.example/f"}}],
        "metadata": {"title": "Kept", "notes": "nous-operation:1"},
        "id": 7,
    }
    operation = uuid4()
    assert rules.operation_marker(operation) == f"nous-operation:{operation}"
