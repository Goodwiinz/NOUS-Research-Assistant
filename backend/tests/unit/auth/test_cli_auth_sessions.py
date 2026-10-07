from __future__ import annotations


def test_inmemory_store_round_trips_pending_and_approved_sessions() -> None:
    from src.services.auth.cli_auth_sessions import InMemoryCLIAuthSessionStore

    store = InMemoryCLIAuthSessionStore()
    session = store.create_session()

    fetched = store.get_session(session.session_id, session.poll_token)
    assert fetched is not None
    assert fetched.status == "pending"
    assert fetched.session_id == session.session_id
    assert fetched.poll_token == session.poll_token
    assert fetched.verification_code == session.verification_code

    store.approve_session(
        session.session_id,
        verification_code=session.verification_code,
        user_id="user-1",
        credential_payload={"token": "tok", "organization_id": "org-1"},
    )

    approved = store.get_session(session.session_id, session.poll_token)
    assert approved is not None
    assert approved.status == "approved"
    assert approved.user_id == "user-1"
    assert approved.credential_payload["token"] == "tok"
    assert approved.credential_payload["organization_id"] == "org-1"


def test_inmemory_store_rejects_invalid_verification_code() -> None:
    from src.services.auth.cli_auth_sessions import InMemoryCLIAuthSessionStore

    store = InMemoryCLIAuthSessionStore()
    session = store.create_session()

    result = store.approve_session(
        session.session_id,
        verification_code="bad-code",
        user_id="user-1",
        credential_payload={"token": "tok", "organization_id": "org-1"},
    )

    assert result is None

    fetched = store.get_session(session.session_id, session.poll_token)
    assert fetched is not None
    assert fetched.status == "pending"
    assert fetched.user_id is None


def test_inmemory_store_enforces_one_way_lifecycle() -> None:
    from src.services.auth.cli_auth_sessions import InMemoryCLIAuthSessionStore

    store = InMemoryCLIAuthSessionStore()

    approved_session = store.create_session()
    approved = store.approve_session(
        approved_session.session_id,
        verification_code=approved_session.verification_code,
        user_id="user-1",
        credential_payload={"token": "tok", "organization_id": "org-1"},
    )
    assert approved is not None
    assert approved.status == "approved"

    assert store.deny_session(approved_session.session_id) is None
    assert store.expire_session(approved_session.session_id) is None

    denied_session = store.create_session()
    denied = store.deny_session(denied_session.session_id)
    assert denied is not None
    assert denied.status == "denied"

    assert (
        store.approve_session(
            denied_session.session_id,
            verification_code=denied_session.verification_code,
            user_id="user-2",
            credential_payload={"token": "tok-2", "organization_id": "org-2"},
        )
        is None
    )
    assert store.expire_session(denied_session.session_id) is None


def test_inmemory_store_rejects_invalid_poll_token() -> None:
    from src.services.auth.cli_auth_sessions import InMemoryCLIAuthSessionStore

    store = InMemoryCLIAuthSessionStore()
    session = store.create_session()

    assert store.get_session(session.session_id, "wrong-token") is None


def test_inmemory_store_can_deny_and_expire_sessions() -> None:
    from src.services.auth.cli_auth_sessions import InMemoryCLIAuthSessionStore

    store = InMemoryCLIAuthSessionStore()
    session = store.create_session()

    denied = store.deny_session(session.session_id)
    assert denied is not None
    assert denied.status == "denied"

    assert store.expire_session(session.session_id) is None


class _FakeRedis:
    """Just enough of redis.Redis for RedisCLIAuthSessionStore."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.data[key] = value

    def incr(self, key: str) -> int:
        value = int(self.data.get(key, "0")) + 1
        self.data[key] = str(value)
        return value

    def expire(self, _key: str, _ttl: int) -> bool:
        return True

    def delete(self, key: str) -> None:
        self.data.pop(key, None)


def test_verification_code_normalization_ignores_case_and_separators() -> None:
    from src.services.auth.cli_auth_sessions import normalize_verification_code

    assert normalize_verification_code(" abcd-1234 ") == "ABCD1234"
    assert normalize_verification_code("ABCD 1234") == "ABCD1234"
    assert normalize_verification_code("ab-cd") == "ABCD"


def test_inmemory_store_records_requester_metadata_and_clips_user_agent() -> None:
    from src.services.auth.cli_auth_sessions import InMemoryCLIAuthSessionStore

    store = InMemoryCLIAuthSessionStore()
    session = store.create_session(
        requester_ip="203.0.113.7", requester_user_agent="x" * 1000
    )

    fetched = store.get_session_for_approver(session.session_id)
    assert fetched is not None
    assert fetched.requester_ip == "203.0.113.7"
    assert fetched.requester_user_agent == "x" * 256


def test_inmemory_store_approves_code_typed_without_separator() -> None:
    from src.services.auth.cli_auth_sessions import InMemoryCLIAuthSessionStore

    store = InMemoryCLIAuthSessionStore()
    session = store.create_session()

    approved = store.approve_session(
        session.session_id,
        verification_code=session.verification_code.replace("-", "").lower(),
        user_id="user-1",
        credential_payload={"token": "tok"},
    )

    assert approved is not None
    assert approved.status == "approved"


def test_inmemory_get_session_for_approver_marks_expired_sessions() -> None:
    from dataclasses import replace
    from datetime import timedelta

    from src.services.auth.cli_auth_sessions import InMemoryCLIAuthSessionStore

    store = InMemoryCLIAuthSessionStore()
    session = store.create_session()
    store._sessions[session.session_id] = replace(
        session, expires_at=session.created_at - timedelta(seconds=1)
    )

    fetched = store.get_session_for_approver(session.session_id)
    assert fetched is not None
    assert fetched.status == "expired"
    assert store.get_session_for_approver("missing") is None


def test_redis_store_round_trips_requester_metadata() -> None:
    from src.services.auth.cli_auth_sessions import RedisCLIAuthSessionStore

    store = RedisCLIAuthSessionStore(_FakeRedis())  # type: ignore[arg-type]
    session = store.create_session(
        requester_ip="203.0.113.7", requester_user_agent="nous-cli/1.4.0"
    )

    fetched = store.get_session_for_approver(session.session_id)
    assert fetched is not None
    assert fetched.requester_ip == "203.0.113.7"
    assert fetched.requester_user_agent == "nous-cli/1.4.0"
    assert fetched.verification_code == session.verification_code


def test_redis_store_loads_sessions_written_before_requester_fields() -> None:
    # Rolling deploy: a pod on the old image wrote JSON without the new keys.
    import json

    from src.services.auth.cli_auth_sessions import RedisCLIAuthSessionStore

    fake = _FakeRedis()
    store = RedisCLIAuthSessionStore(fake)  # type: ignore[arg-type]
    session = store.create_session(requester_ip="203.0.113.7")
    key = f"cli_auth:session:{session.session_id}"
    legacy = json.loads(fake.data[key])
    legacy.pop("requester_ip")
    legacy.pop("requester_user_agent")
    fake.data[key] = json.dumps(legacy)

    fetched = store.get_session_for_approver(session.session_id)
    assert fetched is not None
    assert fetched.requester_ip is None
    assert fetched.requester_user_agent is None
