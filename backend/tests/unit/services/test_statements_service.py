"""GOO-316 statements service units: the ORCID receipt keeps no token, and
the OAuth state is signed, expiring and bound to the session user."""

import logging
from typing import Any
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
