"""GOO-316 statements service units: the ORCID receipt keeps no token, the
OAuth state is signed, expiring and bound to the session user, and the
anonymized build's byte scan refuses a leak redaction cannot reach.

Mutation verification (docs/engineering/testing.md), GOO-316 section of
``docs/testing/agent-orchestration-mutation-checks.md``: skipping
``scan_leaks`` in ``_anonymized`` fails ``-k leak``."""

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.services.research import statements_service as service
from src.shared.statements_schemas import OrcidAuthenticationResponse

# Built by concatenation so no token-shaped literal is committed.
ACCESS = "acc" + "ess-" + uuid4().hex
REFRESH = "ref" + "resh-" + uuid4().hex
ID_TOKEN = "eyJ" + uuid4().hex


class _Db:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def add(self, row: Any) -> None:
        self.rows.append(row)

    async def commit(self) -> None:
        return None

    async def refresh(self, row: Any) -> None:
        row.created_at = row.token_received_at


async def test_record_orcid_stores_no_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    db = _Db()
    token_response = {
        "orcid": "0000-0002-1825-0097",
        "name": "Ada Lovelace",
        "scope": "/authenticate",
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "id_token": ID_TOKEN,
        "token_type": "bearer",
        "expires_in": 631138518,
    }
    row = await service.record_orcid(
        db,  # type: ignore[arg-type]
        uuid4(),
        token_response,
        environment="sandbox",
        client_id="APP-FIXTURE",
        state_hash="a" * 64,
    )
    assert db.rows == [row]
    stored = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    assert (stored["orcid"], stored["name_claim"], stored["scope"]) == (
        "0000-0002-1825-0097",
        "Ada Lovelace",
        "/authenticate",
    )
    assert stored["id_token_sha256"] and len(stored["id_token_sha256"]) == 64
    response = OrcidAuthenticationResponse.model_validate(row).model_dump_json()
    for secret in (ACCESS, REFRESH, ID_TOKEN):
        assert all(secret not in str(value) for value in stored.values())
        assert secret not in response
        assert secret not in caplog.text
    with pytest.raises(ValueError):
        await service.record_orcid(
            db,  # type: ignore[arg-type]
            uuid4(),
            {"orcid": "not-an-id"},
            environment="sandbox",
            client_id="APP-FIXTURE",
            state_hash="a" * 64,
        )


def test_oauth_state_is_signed_expiring_and_user_bound() -> None:
    user, other = uuid4(), uuid4()
    state = service.sign_state(user, now=1000.0)
    assert service.verify_state(state, user, now=1001.0)
    assert not service.verify_state(state, other, now=1001.0)
    assert not service.verify_state(state, user, now=1000.0 + 601)
    payload, _, signature = state.partition(".")
    forged = f"{payload}.{'0' * len(signature)}"
    assert not service.verify_state(forged, user, now=1001.0)
    assert not service.verify_state("garbage", user)


async def test_anonymized_build_fails_on_a_leak_redaction_cannot_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A binary member is copied, never redacted: the post-condition byte
    scan is what refuses it (``anonymization_leak:<member>``)."""
    from fastapi import HTTPException

    from src.services.research import manuscript_release_service as mr
    from src.services.research_engine.audit_bundle import Part

    monkeypatch.setattr(
        mr.statements_service,
        "identities",
        AsyncMock(return_value=["Rosalind Featherstonehaugh"]),
    )
    text = b"## Methods\nThanks to Rosalind Featherstonehaugh.\n"
    binary = b"\xff\xfe\x00Rosalind Featherstonehaugh\x00\xff"
    meta: dict[str, Any] = {
        "project_id": str(uuid4()),
        "generated_at": "2026-10-01T00:00:00+00:00",
        "deployment_sha": None,
        "protocol_version_id": None,
        "stream_heads": {},
        "schema": "nous.manuscript-release.v1",
    }
    context: Any = SimpleNamespace()
    clean = await mr._anonymized(
        None,  # type: ignore[arg-type]
        context,
        [Part("manuscript.md", None, text, mr._sha(text))],
        {},
        meta,
    )
    assert b"Featherstonehaugh" not in b"".join(mr._members(clean).values())
    with pytest.raises(HTTPException) as refused:
        await mr._anonymized(
            None,  # type: ignore[arg-type]
            context,
            [Part("figures/fig-1.png", None, binary, mr._sha(binary))],
            {},
            meta,
        )
    assert refused.value.status_code == 422
    assert refused.value.detail == "anonymization_leak:figures/fig-1.png"
