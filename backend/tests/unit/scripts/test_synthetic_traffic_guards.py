"""Guards that keep the synthetic-traffic generator off real data.

Each test here pins one audit finding that let the generator touch something
it should never touch: production (R5-L21), an arbitrary real organization
(R5-M28), or an ever-growing pile of undeleted documents (R5-M29). R5-L25
covers the half-bootstrapped account that used to stay broken forever.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from scripts.synthetic_traffic import (
    ALLOWED_ENVIRONMENTS,
    SYNTH_ORG_NAME,
    _cleanup_documents,
    _ensure_workspace,
    _get_or_create_synth_org,
    assert_safe_environment,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes — the real session is a live Postgres connection; these record the
# statements the code emits so we can assert on their SQL.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, value: Any = None, rowcount: int = 0) -> None:
        self._value = value
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        return self._value

    def scalars(self) -> "_FakeResult":
        return self

    def first(self) -> Any:
        return self._value

    def all(self) -> List[Any]:
        if self._value is None:
            return []
        return self._value if isinstance(self._value, list) else [self._value]


class _FakeDB:
    def __init__(self, *results: _FakeResult) -> None:
        self._results = list(results)
        self.statements: List[Any] = []
        self.added: List[Any] = []
        self.commits = 0

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        self.statements.append(stmt)
        return self._results.pop(0) if self._results else _FakeResult()

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass

    async def get(self, _model: Any, object_id: Any) -> Any:
        return type("SyntheticDocument", (), {"id": object_id, "is_deleted": False})()


def _sql(stmt: Any) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


class _Org:
    def __init__(self, name: str = SYNTH_ORG_NAME) -> None:
        self.id = "org-1"
        self.name = name


class _User:
    def __init__(self, organization_id: Optional[str] = "org-1") -> None:
        self.id = "user-1"
        self.organization_id = organization_id


class _Workspace:
    def __init__(
        self, workspace_id: str = "ws-1", organization_id: Optional[str] = "org-1"
    ) -> None:
        self.id = workspace_id
        self.owner_id = "user-1"
        self.organization_id = organization_id
        self.is_deleted = False


class _LogRecorder:
    """Captures structured log calls so a test can assert on the warning."""

    def __init__(self) -> None:
        self.calls: List[tuple[str, str, dict]] = []

    def _record(self, level: str) -> Any:
        def _emit(event: str, **fields: Any) -> None:
            self.calls.append((level, event, fields))

        return _emit

    def __getattr__(self, level: str) -> Any:
        return self._record(level)


# ---------------------------------------------------------------------------
# R5-L21 — refuse to run outside a dev-like environment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["production", "prod", "staging", "PRODUCTION"])
def test_refuses_to_run_in_production(env: str) -> None:
    """The finding: ENVIRONMENT was read only to tag traces, never to refuse."""
    with pytest.raises(SystemExit) as exc:
        assert_safe_environment(env)
    assert "refusing to run" in str(exc.value)


def test_refuses_when_environment_is_unset() -> None:
    """Fails closed — an unset var must not read as 'probably dev'."""
    with pytest.raises(SystemExit):
        assert_safe_environment("")
    with pytest.raises(SystemExit):
        assert_safe_environment("   ")


def test_reads_the_environment_variable_when_no_argument_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(SystemExit):
        assert_safe_environment()
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert assert_safe_environment() == "dev"


@pytest.mark.parametrize("env", sorted(ALLOWED_ENVIRONMENTS))
def test_allows_dev_like_environments(env: str) -> None:
    assert assert_safe_environment(env.upper()) == env


def test_the_cronjob_environment_value_is_allowed() -> None:
    """synthetic-traffic-cronjob.yaml defaults ENVIRONMENT to "dev"."""
    assert "dev" in ALLOWED_ENVIRONMENTS


# ---------------------------------------------------------------------------
# R5-M28 — target a dedicated org, never an arbitrary real one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_lookup_is_filtered_by_name_not_limit_1() -> None:
    """The finding: select(Organization).limit(1) picked an arbitrary REAL org."""
    db = _FakeDB(_FakeResult(_Org()))
    org = await _get_or_create_synth_org(db)

    assert org.name == SYNTH_ORG_NAME
    sql = _sql(db.statements[0])
    assert SYNTH_ORG_NAME in sql
    # \b so STORAGE_LIMIT_BYTES in the column list does not match.
    assert re.search(r"\bLIMIT\b", sql, re.I) is None


@pytest.mark.asyncio
async def test_org_is_created_when_missing_rather_than_borrowed() -> None:
    db = _FakeDB(_FakeResult(None))
    org = await _get_or_create_synth_org(db)

    assert db.added and db.added[0] is org
    assert org.name == SYNTH_ORG_NAME


# ---------------------------------------------------------------------------
# R5-L25 — a workspace-less synthetic account heals instead of staying broken
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_workspace_is_recreated() -> None:
    """Crash between the user commit and the workspace commit used to be fatal."""
    db = _FakeDB(_FakeResult(None))
    ws = await _ensure_workspace(db, _User(), "org-1")

    assert db.added and db.added[0] is ws
    assert ws.owner_id == "user-1"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_existing_workspace_is_reused() -> None:
    existing = _Workspace(organization_id="org-1")
    db = _FakeDB(_FakeResult(existing))

    assert await _ensure_workspace(db, _User(), "org-1") is existing
    assert db.added == []
    assert db.commits == 0


# ---------------------------------------------------------------------------
# R5-M28 follow-up — a workspace left behind in a REAL org never heals
# ---------------------------------------------------------------------------
#
# Mutation check (docs/engineering/testing.md): the guard is the effective-org
# filter in ``_ensure_workspace`` (backend/scripts/synthetic_traffic.py). Make
# it accept any owned workspace again and run
#   pytest -q tests/unit/scripts/test_synthetic_traffic_guards.py \
#       -k "real_org or null_org or preferred_over"
# — the first test must fail with the stale ``org-real`` workspace returned.


@pytest.mark.asyncio
async def test_workspace_left_in_a_real_org_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live bug: the user was re-pinned to the synthetic org, its only
    workspace was not, so ``accessible_research_workspace_ids`` returned []
    and ``create_project`` failed on every run with "No workspace found".
    """
    import scripts.synthetic_traffic as module

    recorder = _LogRecorder()
    monkeypatch.setattr(module, "log", recorder)
    stale = _Workspace(workspace_id="ws-stale", organization_id="org-real")
    db = _FakeDB(_FakeResult(stale))

    ws = await _ensure_workspace(db, _User(organization_id="org-1"), "org-1")

    assert ws is not stale
    assert db.added == [ws]
    assert ws.owner_id == "user-1"
    assert ws.organization_id == "org-1"
    assert db.commits == 1
    # The stale workspace belongs to a real tenant: never re-pinned, moved or
    # deleted here — that is a human decision. Only the lookup SELECT ran.
    assert stale.organization_id == "org-real"
    assert stale.is_deleted is False
    assert len(db.statements) == 1
    assert _sql(db.statements[0]).upper().startswith("SELECT")
    warnings = [c for c in recorder.calls if c[0] == "warning"]
    assert len(warnings) == 1
    _, event, fields = warnings[0]
    assert event == "synthetic_traffic.bootstrap"
    assert fields["workspace_id"] == "ws-stale"
    assert fields["workspace_org_id"] == "org-real"
    assert fields["org_id"] == "org-1"


@pytest.mark.asyncio
async def test_workspace_with_null_org_owned_by_synth_user_is_reused() -> None:
    """``coalesce(workspace.organization_id, owner.organization_id)`` is the
    canonical rule (project_access.py): a NULL-org workspace owned by the
    (already re-pinned) synthetic user resolves to the synthetic org.
    """
    existing = _Workspace(organization_id=None)
    db = _FakeDB(_FakeResult(existing))

    assert await _ensure_workspace(db, _User(), "org-1") is existing
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_synth_org_workspace_is_preferred_over_a_stale_one() -> None:
    """Once healed, the next fire finds both; it must keep using the good one."""
    stale = _Workspace(workspace_id="ws-stale", organization_id="org-real")
    good = _Workspace(workspace_id="ws-good", organization_id="org-1")
    db = _FakeDB(_FakeResult([stale, good]))

    assert await _ensure_workspace(db, _User(), "org-1") is good
    assert db.added == []
    assert db.commits == 0


# ---------------------------------------------------------------------------
# R5-M29 — documents are purged, so the content-hash dedup index frees up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_documents_use_full_file_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.documents.file_service import FileService

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=6)
    db = _FakeDB(_FakeResult(SYNTH_ORG_NAME), _FakeResult(["doc-1", "doc-2", "doc-3"]))
    deleted: list[str] = []

    async def fake_delete_file(_service: Any, document: Any, _user: Any) -> None:
        deleted.append(document.id)

    monkeypatch.setattr(FileService, "delete_file", fake_delete_file)

    assert await _cleanup_documents(db, _User(), cutoff) == 3

    sql = _sql(db.statements[1]).upper()
    assert sql.startswith("SELECT DOCUMENTS.ID")
    assert "IS_DELETED" in sql
    assert deleted == ["doc-1", "doc-2", "doc-3"]


@pytest.mark.asyncio
async def test_documents_are_never_purged_outside_the_synthetic_org() -> None:
    """A pre-R5-M28 synthetic user pinned to a REAL org must lose nothing."""
    db = _FakeDB(_FakeResult("Acme Corp"))

    assert await _cleanup_documents(db, _User(), datetime.now(tz=timezone.utc)) == 0
    assert len(db.statements) == 1  # the name lookup only; no UPDATE
    assert db.commits == 0


@pytest.mark.asyncio
async def test_user_without_an_org_purges_nothing() -> None:
    db = _FakeDB()

    assert (
        await _cleanup_documents(
            db, _User(organization_id=None), datetime.now(tz=timezone.utc)
        )
        == 0
    )
    assert db.statements == []
