"""Unit tests for ``src.core.user_provisioning`` JIT provisioning.

Coverage gap (daily audit #923, finding #2): ``ensure_user_and_org`` runs on
every first-time authenticated request (via ``dependencies.get_current_user``)
and contains an ``IntegrityError``-retry-then-refetch path for org/user
creation that had zero coverage. That path is precisely what protects two
simultaneous first requests for the same subject from 500-ing.

We drive the race deterministically with a mock ``AsyncSession`` whose
``flush()`` raises ``IntegrityError`` on demand (as a concurrent transaction
committing first would) and whose ``execute()`` then returns the row the
"other" request created. A real SQLite session can't reproduce this reliably —
it serializes writers, so the unique-violation window never opens.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.security import TokenData, _extract_supabase_token_data
from src.core.user_provisioning import ensure_user_and_org
from src.models.organization import Organization
from src.models.user import User, UserRole


def _result(value):
    """Mimic a SQLAlchemy Result: ``.scalars().first()`` -> value."""
    res = MagicMock()
    scalars = MagicMock()
    scalars.first.return_value = value
    res.scalars.return_value = scalars
    return res


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT ...", {}, Exception("duplicate key value"))


def _session(*, execute_results, flush_side_effects):
    """Build a mock AsyncSession.

    ``execute_results``    — values returned by successive ``execute()`` calls,
                             each wrapped so ``.scalars().first()`` yields it.
    ``flush_side_effects`` — per-call ``flush()`` behavior; an ``IntegrityError``
                             instance is raised, anything else is returned.
    """
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(v) for v in execute_results])
    db.flush = AsyncMock(side_effect=flush_side_effects)
    db.rollback = AsyncMock()
    db.add = MagicMock()
    return db


def _token(user_id="user-1", email="user@example.com", organization_id=None, **extra):
    return TokenData(
        user_id=user_id, email=email, organization_id=organization_id, **extra
    )


# ---------------------------------------------------------------------------
# Guard clauses / happy paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_none_without_user_id():
    db = _session(execute_results=[], flush_side_effects=[])
    assert await ensure_user_and_org(db, _token(user_id=None)) is None
    db.execute.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_existing_user_without_provisioning():
    existing = SimpleNamespace(id="user-1")
    db = _session(execute_results=[existing], flush_side_effects=[])

    result = await ensure_user_and_org(db, _token())

    assert result is existing
    db.add.assert_not_called()  # nothing created
    db.flush.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_creates_org_and_user_with_org_id():
    db = _session(
        execute_results=[None, None],  # user missing, org missing
        flush_side_effects=[None, None],  # org flush ok, user flush ok
    )

    user = await ensure_user_and_org(db, _token(organization_id="org-42"))

    assert isinstance(user, User)
    assert user.id == "user-1"
    assert user.organization_id == "org-42"
    db.rollback.assert_not_called()
    # Two objects added (org, then user), two flushes.
    assert db.add.call_count == 2
    assert db.flush.await_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_creates_per_user_org_without_org_id():
    db = _session(
        execute_results=[None, None],  # user missing, per-user org missing
        flush_side_effects=[None, None],
    )

    user = await ensure_user_and_org(db, _token(user_id="user-1", organization_id=None))

    assert isinstance(user, User)
    # First added object is the per-user fallback Organization, not a
    # shared "Default Organization".
    added_org = db.add.call_args_list[0].args[0]
    assert isinstance(added_org, Organization)
    assert added_org.name == "user-user-1 Organization"
    db.rollback.assert_not_called()


def _fake_db_with_org_table():
    """A stateful fake ``AsyncSession`` backed by a real in-memory dict of
    Organizations keyed by name, so two *sequential* ``ensure_user_and_org``
    calls see each other's already-flushed rows — the way two sequential
    real requests against the same DB would.

    ``_session``'s canned-list ``execute_results`` can't model this: it
    returns whatever's next in its list regardless of what was actually
    queried, so it can't distinguish "second org-less user's lookup hits
    the same shared name" from "hits a different per-user name" — which is
    exactly the distinction AU1 hinges on. This fake introspects the
    ``select()`` statement (entity + the literal being compared) instead of
    replaying a fixed sequence.
    """
    orgs_by_name: dict[str, Organization] = {}
    added: list = []

    db = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock(side_effect=added.append)

    async def _execute(stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is User:
            return _result(None)  # every user in this test is new
        # Organization lookup — always a single `.name == <literal>` filter
        # on the org-less path.
        name = stmt.whereclause.right.value
        return _result(orgs_by_name.get(name))

    async def _flush():
        obj = added[-1]
        if isinstance(obj, Organization) and getattr(obj, "id", None) is None:
            # Organization.name is UNIQUE — model the constraint, otherwise a
            # test can't tell "created its own org" from "silently overwrote
            # somebody else's row".
            if obj.name in orgs_by_name:
                raise _integrity_error()
            obj.id = uuid.uuid4()
            orgs_by_name[obj.name] = obj

    db.execute = AsyncMock(side_effect=_execute)
    db.flush = AsyncMock(side_effect=_flush)
    db.orgs_by_name = orgs_by_name
    return db


@pytest.mark.unit
@pytest.mark.asyncio
async def test_org_less_users_never_comingle_in_shared_org():
    """AU1 regression test: two DIFFERENT org-less users, provisioned
    sequentially against the SAME org table (as two real requests hitting
    the same DB would be), must land in DIFFERENT organizations. Pre-fix,
    both funnel into one shared "Default Organization" row and this fails."""
    db = _fake_db_with_org_table()

    user_a = await ensure_user_and_org(db, _token(user_id="user-aaaaaaaa"))
    user_b = await ensure_user_and_org(db, _token(user_id="user-bbbbbbbb"))

    assert isinstance(user_a, User)
    assert isinstance(user_b, User)
    assert user_a.organization_id is not None
    assert user_a.organization_id != user_b.organization_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_org_less_users_sharing_id_prefix_get_distinct_orgs():
    """Lock: the org-less fallback name is the org's IDENTITY (we select on
    it), so it must use the FULL user_id, not a truncated prefix. Two ids
    sharing their first 8 chars but otherwise different must NOT co-mingle."""
    db = _fake_db_with_org_table()

    user_a = await ensure_user_and_org(
        db, _token(user_id="019f69a1-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    )
    user_b = await ensure_user_and_org(
        db, _token(user_id="019f69a1-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    )

    assert user_a.organization_id is not None
    assert user_a.organization_id != user_b.organization_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_org_less_user_provisioned_twice_resolves_same_org():
    """Idempotency: re-provisioning (or a concurrent duplicate request for)
    the SAME org-less user_id must resolve back to the same organization,
    via the deterministic per-user org name + IntegrityError refetch guard."""
    existing_org = SimpleNamespace(id="org-existing", name="user-user-1 Organization")
    db = _session(
        execute_results=[None, existing_org],  # user missing, org already exists
        flush_side_effects=[None],  # user flush ok (org was reused, not flushed)
    )

    user = await ensure_user_and_org(db, _token(user_id="user-1", organization_id=None))

    assert isinstance(user, User)
    assert user.organization_id == "org-existing"
    db.add.assert_called_once()  # only the User was added; org was reused
    db.flush.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_app_metadata_organization_id_honored():
    """A token that DOES carry an org claim (Supabase app_metadata or CLI
    token) must place the user in THAT org, not a per-user fallback."""
    existing_org = SimpleNamespace(id="org-claimed")
    db = _session(
        execute_results=[None, existing_org],
        flush_side_effects=[None],  # user flush ok (org was reused, not flushed)
    )

    user = await ensure_user_and_org(
        db, _token(user_id="user-1", organization_id="org-claimed")
    )

    assert isinstance(user, User)
    assert user.organization_id == "org-claimed"
    db.add.assert_called_once()  # org already existed, only User added


# ---------------------------------------------------------------------------
# Race handling: IntegrityError -> rollback -> refetch
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_org_race_refetches_concurrently_created_org():
    """org_id branch: a concurrent request inserts the org between our SELECT
    and our flush → IntegrityError → rollback → refetch returns their row."""
    concurrent_org = SimpleNamespace(id="org-42")
    db = _session(
        execute_results=[None, None, concurrent_org],
        #                 ^user  ^org  ^org-refetch
        flush_side_effects=[_integrity_error(), None],
        #                    ^org flush races   ^user flush ok
    )

    user = await ensure_user_and_org(db, _token(organization_id="org-42"))

    assert isinstance(user, User)
    assert user.organization_id == "org-42"  # from the refetched org
    db.rollback.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_org_less_fallback_org_race_refetches_by_name():
    """No org_id: the per-user fallback org creation races and is refetched
    by its deterministic name, same guard pattern as the org_id branch."""
    concurrent_org = SimpleNamespace(id="org-default")
    db = _session(
        execute_results=[None, None, concurrent_org],
        flush_side_effects=[_integrity_error(), None],
    )

    user = await ensure_user_and_org(db, _token(organization_id=None))

    assert isinstance(user, User)
    assert user.organization_id == "org-default"
    db.rollback.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_race_refetches_concurrently_created_user():
    """The org resolves fine, but the user INSERT loses the race → refetch
    returns the user the concurrent request created."""
    concurrent_user = SimpleNamespace(id="user-1", organization_id="org-42")
    db = _session(
        execute_results=[None, None, concurrent_user],
        #                 ^user  ^org  ^user-refetch
        flush_side_effects=[None, _integrity_error()],
        #                    ^org  ^user flush races
    )

    result = await ensure_user_and_org(db, _token(organization_id="org-42"))

    assert result is concurrent_user
    db.rollback.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_email_conflict_warns_with_user_id_and_never_resolves_by_email(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GOO-405 hardening: the user INSERT conflicts, but no row with our own id
    exists. In practice another row already holds this verified address in
    ``users.email``.

    Provisioning must (a) return None, which the caller turns into a 401,
    (b) log a WARNING naming the user id but NOT the email, and (c) never
    look the user up by email. Adopting the conflicting row would turn email
    squatting into account takeover.
    """
    db = _session(
        execute_results=[None, None, None],
        #                 ^user  ^org  ^refetch-by-id finds nothing
        flush_side_effects=[None, _integrity_error()],
    )
    token = _token(
        user_id="victim-id", email="victim@example.com", organization_id="org-42"
    )

    with caplog.at_level("WARNING", logger="src.core.user_provisioning"):
        result = await ensure_user_and_org(db, token)

    assert result is None
    db.rollback.assert_awaited_once()
    # Exactly user lookup, org lookup, and ONE refetch, and that refetch is by id.
    assert db.execute.await_count == 3
    refetch_where = str(db.execute.await_args_list[2].args[0]).split("WHERE", 1)[1]
    assert "users.id" in refetch_where
    assert "users.email" not in refetch_where

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "victim-id" in message
    assert "victim@example.com" not in message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_id_race_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """The benign same-subject race (refetch by id succeeds) stays quiet."""
    concurrent_user = SimpleNamespace(id="user-1", organization_id="org-42")
    db = _session(
        execute_results=[None, None, concurrent_user],
        flush_side_effects=[None, _integrity_error()],
    )

    with caplog.at_level("WARNING", logger="src.core.user_provisioning"):
        result = await ensure_user_and_org(db, _token(organization_id="org-42"))

    assert result is concurrent_user
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_simultaneous_provisioning_converges_on_one_user():
    """Two first-time requests for the SAME subject provision concurrently.

    The winner creates the row; the loser hits the unique violation on its
    user flush and refetches the winner's row. Both return a user with the
    same id — no unhandled 500, no duplicate identity.
    """
    winner_user_id = "user-1"

    # Winner: everything succeeds, creates the canonical User.
    winner_db = _session(
        execute_results=[None, None],
        flush_side_effects=[None, None],
    )
    # Loser: user flush loses the race, refetch returns the winner's row.
    winners_row = SimpleNamespace(id=winner_user_id, organization_id="org-42")
    loser_db = _session(
        execute_results=[None, None, winners_row],
        flush_side_effects=[None, _integrity_error()],
    )

    token = _token(user_id=winner_user_id, organization_id="org-42")
    winner, loser = await asyncio.gather(
        ensure_user_and_org(winner_db, token),
        ensure_user_and_org(loser_db, token),
    )

    assert winner.id == winner_user_id
    assert loser.id == winner_user_id  # loser converged on the winner's row
    loser_db.rollback.assert_awaited_once()
    winner_db.rollback.assert_not_called()


# ---------------------------------------------------------------------------
# Sign-up metadata (Supabase ``user_metadata``)
#
# The frontend sends {first_name, last_name, organization_name} through
# ``supabase.auth.signUp({options: {data}})``; GoTrue echoes it back in the
# JWT's ``user_metadata`` claim. Provisioning must use it for DISPLAY fields
# only — it is client-controlled, so it may never reach role / permissions /
# org membership / tenant scope.
# ---------------------------------------------------------------------------


def _supabase_payload(**user_metadata):
    return {
        "sub": "user-1",
        "email": "a.two@example.com",
        "app_metadata": {"role": "USER"},
        "user_metadata": user_metadata,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signup_metadata_names_and_org_name_persisted():
    """The names and organization the user typed at sign-up are what gets
    written — not a guess off the email local-part and a UUID-blob org name."""
    db = _session(
        execute_results=[None, None],  # user missing, per-user org missing
        flush_side_effects=[None, None],
    )

    user = await ensure_user_and_org(
        db,
        _token(
            email="a.two@example.com",
            signup_first_name="Abdelghafour",
            signup_last_name="Two",
            signup_organization_name="Acme Inc.",
        ),
    )

    assert isinstance(user, User)
    assert user.first_name == "Abdelghafour"
    assert user.last_name == "Two"
    added_org = db.add.call_args_list[0].args[0]
    assert isinstance(added_org, Organization)
    assert added_org.name == "Acme Inc."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blank_signup_metadata_keeps_derived_fallbacks():
    """Absent/blank metadata leaves today's behavior untouched: names derived
    from the email local-part, deterministic per-user org name."""
    db = _session(
        execute_results=[None, None],
        flush_side_effects=[None, None],
    )

    user = await ensure_user_and_org(db, _token(email="a.two@example.com"))

    assert user.first_name == "A"
    assert user.last_name == "Two"
    added_org = db.add.call_args_list[0].args[0]
    assert added_org.name == "user-user-1 Organization"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_org_claim_ignores_signup_organization_name():
    """A token carrying an org claim is authoritative: the org the user typed
    must not rename (or redirect them away from) the claimed tenant."""
    db = _session(
        execute_results=[None, None],  # user missing, claimed org missing
        flush_side_effects=[None, None],
    )

    user = await ensure_user_and_org(
        db,
        _token(
            organization_id="org-42",
            signup_organization_name="Acme Inc.",
        ),
    )

    added_org = db.add.call_args_list[0].args[0]
    assert added_org.name == "org-org-42"  # derived from the claimed org id
    assert added_org.name != "Acme Inc."
    assert user.organization_id == "org-42"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signup_org_name_never_joins_another_tenants_org():
    """SECURITY lock: typing an existing organization's name at sign-up must
    NOT place the user in that organization. They get their own org (under the
    deterministic fallback name) instead."""
    db = _fake_db_with_org_table()

    victim = await ensure_user_and_org(
        db, _token(user_id="victim", signup_organization_name="Acme Inc.")
    )
    attacker = await ensure_user_and_org(
        db, _token(user_id="attacker", signup_organization_name="Acme Inc.")
    )

    assert str(db.orgs_by_name["Acme Inc."].id) == victim.organization_id
    assert attacker.organization_id is not None
    assert attacker.organization_id != victim.organization_id
    assert str(db.orgs_by_name["user-attacker Organization"].id) == (
        attacker.organization_id
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signup_org_name_cannot_squat_reserved_fallback_name():
    """SECURITY lock: the per-user fallback name is a LOOKUP KEY, so a sign-up
    name shaped like one must be refused — otherwise the squatter's org is
    handed to the victim when the victim provisions later."""
    db = _fake_db_with_org_table()

    squatter = await ensure_user_and_org(
        db,
        _token(
            user_id="squatter",
            signup_organization_name="user-victim Organization",
        ),
    )
    victim = await ensure_user_and_org(db, _token(user_id="victim"))

    assert "user-victim Organization" not in [
        name
        for name, org in db.orgs_by_name.items()
        if org.id == squatter.organization_id
    ]
    assert victim.organization_id != squatter.organization_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hostile_signup_metadata_does_not_break_provisioning():
    """Oversized / wrong-typed / privilege-shaped metadata must not 401 the
    user, must not exceed the column widths, and must not set any
    authorization field."""
    token_data = _extract_supabase_token_data(
        _supabase_payload(
            first_name="A" * 500,
            last_name={"nested": "dict"},
            organization_name=["Acme", "Inc"],
            role="ADMIN",
            organization_id="org-victim",
            is_admin=True,
        )
    )

    assert token_data is not None
    assert len(token_data.signup_first_name) == 100  # users.first_name limit
    assert token_data.signup_last_name is None  # non-string dropped
    assert token_data.signup_organization_name is None  # non-string dropped
    assert token_data.role == "USER"  # from app_metadata, NOT user_metadata
    assert token_data.organization_id is None  # never from user_metadata

    db = _session(execute_results=[None, None], flush_side_effects=[None, None])
    user = await ensure_user_and_org(db, token_data)

    assert isinstance(user, User)
    assert user.first_name == "A" * 100
    assert user.last_name == "Two"  # derived fallback, dict was dropped
    assert user.role is UserRole.USER
    added_org = db.add.call_args_list[0].args[0]
    assert added_org.name == "user-user-1 Organization"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_dict_user_metadata_claim_is_ignored():
    """``user_metadata`` itself may be any JSON value; a string/list must not
    raise on extraction."""
    payload = _supabase_payload()
    payload["user_metadata"] = "not-a-dict"

    token_data = _extract_supabase_token_data(payload)

    assert token_data is not None
    assert token_data.signup_first_name is None
    assert token_data.signup_organization_name is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blank_and_whitespace_metadata_falls_back():
    token_data = _extract_supabase_token_data(
        _supabase_payload(first_name="   ", last_name="", organization_name="  ")
    )

    assert token_data.signup_first_name is None
    assert token_data.signup_last_name is None
    assert token_data.signup_organization_name is None

    db = _session(execute_results=[None, None], flush_side_effects=[None, None])
    user = await ensure_user_and_org(db, token_data)

    assert user.first_name == "A"  # derived from a.two@example.com
    assert user.last_name == "Two"
