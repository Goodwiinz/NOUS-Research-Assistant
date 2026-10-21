# Account-wide harness connection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development) to implement this plan task-by-task.

**Goal:** One browser approval connects a computer to every project in the user's organization workspaces, for Codex's NOUS tools and for Local Codex runs from any chat; `nous --rc` drops the project picker.

**Architecture:** A third grant binding kind, `account`, on approvals and grants. Codex's tools take a `project_id` selector checked against an account rule on every call; each Local Codex run still gets a project-bound grant minted under the account approval, and the bridge pins a run's tools to its chat's project. Gated by `ACCOUNT_CONNECTIONS_ENABLED`.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, PostgreSQL; TypeScript on Node 24 (`packages/harness-bridge`, `node:test`); Next.js + Vitest (`frontend`).

Design: [`2026-10-14-account-connection-design.md`](2026-10-14-account-connection-design.md).

**Base:** written against a local preview merge of `origin/develop` (`fdc9c00fc`), `feat/nous-rc` (#1983/#1986) and `feat/harness-grant-resume` (#1987). Line numbers refer to that preview; re-check them on the real base. Three PRs, in order: PR 1 backend, PR 2 runs and bridge, PR 3 website. Each section lists its own deviations from the shared contract and its guesses at the top.

---

## Shared contract


Design: `docs/plans/2026-10-14-account-connection-design.md` (approved; read it fully).

## Database (PR 1 creates; PR 2/3 consume)
- Migration revision id `ac01_account_bindings`, `down_revision` = the single head at the time of writing (check: `cd backend && <PY> ../scripts/ci/check_alembic.py`; on the preview it is `ic02_selected_skill_snapshot` unless #1987 or develop added one — verify).
- `integration_grant_requests.binding_kind` and `integration_grants.binding_kind`: `String(16)`, NOT NULL, server default for backfill only, values `'project' | 'workspace' | 'account'`. CHECKs `ck_integration_grant_requests_binding_kind` / `ck_integration_grants_binding_kind`:
  `(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL) OR (binding_kind = 'workspace' AND project_id IS NULL AND workspace_id IS NOT NULL) OR (binding_kind = 'account' AND project_id IS NULL AND workspace_id IS NULL)` — replacing the `*_one_binding` CHECKs.
- `bridge_workspace_bindings.project_id` becomes nullable (NULL = account folder).
- `integration_tool_actions.binding_kind` (`String(16)`, NOT NULL, backfilled) and `integration_tool_actions.target_workspace_id` (UUID, nullable, FK `workspaces.id`); the `some_binding` CHECK becomes kind-aware (`account` rows may have both `project_id` and `workspace_id` NULL; `create_folder` under account sets `target_workspace_id`).
- Downgrade: delete account requests/grants (and grants whose request is an account request), account-kind tool actions, folders with NULL `project_id`; restore the old CHECKs and NOT NULL.

## Python names (PR 1)
- `backend/src/core/config.py`: `ACCOUNT_CONNECTIONS_ENABLED: bool = False`. `create_request` with an account binding while it is off → `HTTPException(503, "Account connections are disabled")` raised from the router (services raise a dedicated exception, e.g. `AccountConnectionsDisabled`).
- `backend/src/services/integrations/context.py`:
  - `BindingKind = Literal["project", "workspace", "account"]`
  - `account_scope_condition(user_id: UUID, organization_id: UUID) -> ColumnElement[bool]` — Collections in the account (org workspaces owned or live member; workspace and Collection not deleted/archived). Built from `services/research_engine/project_access.accessible_research_workspace_ids_query` (or its predicate) plus the archive filters.
  - `ACCOUNT_SCOPES = frozenset({"harness:execute", "tools:read", "tools:write", "library:read", "library:write"})`; `check_scopes(scopes, *, binding_kind: BindingKind = "project")` — keep a compatibility path for existing `workspace_bound=` callers or update every caller in the same PR.
  - `authorized_scope_filter` / `authorized_scope` gain the account branch.
- Schemas (`backend/src/schemas/integration_context.py`):
  - `IntegrationContext.binding_kind: BindingKind` (derived/validated against project_id/workspace_id).
  - `GrantRequestCreate.account: bool = False` with a validator: exactly one of `project_id`, `workspace_id`, `account=True`; `thread_id` forbidden with `account`.
  - `GrantRequestDTO.binding_kind: BindingKind` and `GrantRequestDTO.account_summary: AccountSummaryDTO | None` where `AccountSummaryDTO = {workspace_count: int, project_count: int}` (set only for account requests; computed at read time).
  - `WorkspaceBindingCreate.project_id: UUID | None` (None = account folder; requires a live account consent for the device).
  - `ConnectionConsent.kind: Literal["project", "workspace", "account"]` (label "Whole account").
  - `DeviceListItemDTO.account_wide: bool` (device holds a live, unrevoked account consent) and `DeviceListItemDTO.project_ids: list[UUID]` (projects with a live project consent for this device) — PR 3's composer filter uses these.
- Tool selector: account grants behave like workspace grants for `project_id` selectors (`_PROJECT_SELECTOR`); `create_folder` takes a required `workspace_id` for account grants; `list_library` items gain `workspace_id` and `workspace_name`.

## Harness (PR 2)
- Start command envelope gains `projectId` (string UUID) — follow the existing casing in `backend/src/schemas/harness.py` `Envelope` and the bridge's `contracts.ts`.
- Bridge: `LocalState.account?: true` (exactly one of `projectId`, `workspaceId`, `account`); `workspaces[].projectId?: string`; CLI flag `--account`; `connect({ account: true, … })`; `nous --rc` always connects with `account: true` and scopes `harness:execute, tools:read, tools:write, library:read, library:write`; managed MCP child gets `--project <projectId>` from the run envelope and then accepts only that project (tool schemas from the server still carry the selector; the bridge fills/forces it).

## Website (PR 3)
- Uses the regenerated `frontend/src/types/generated/api.d.ts` from PR 1 (`binding_kind`, `account_summary`, `account_wide`, `project_ids`, nullable folder `project_id`, `ConnectionConsent.kind` `'account'`).

## Conventions for every plan section
- Paths are repo-relative; never write absolute personal paths (the repo is public). Use `<checkout>` for the repo root, `<PY>` for `<checkout>/backend/.venv/bin/python` (worktrees have no venv; the main checkout's venv works for any worktree), `<scratch>` for temp dirs.
- Backend tests: `<PY> -m pytest -c backend/pytest.ini -q <path>`; Postgres tests use the local recipe in `docs/testing/harness-live-proof.md` Gate 0 (initdb on port 55432, `ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch`), no Docker.
- Bridge tests: `export PATH=/opt/homebrew/opt/node@24/bin:$PATH` (or any Node 24) then `pnpm --filter @nous/harness-bridge exec node --experimental-sqlite --import tsx --test --test-timeout=60000 test/<file>`.
- Frontend: `pnpm --dir frontend exec vitest run <file>`, `pnpm --dir frontend type-check`.
- OpenAPI: `<PY> scripts/ci/generate_openapi.py` then `pnpm --dir frontend generate:api-types`; both regenerated files land in the same PR; label `api-breaking-approved` only if `oasdiff` reports an ERR-level break.
- Lint gate: ruff, black, isort on changed backend files; mypy `--disallow-untyped-defs` on added files.
- TDD per task: failing test → see it fail → minimal code → pass → commit (explicit `git add` paths). Security-critical tests (the account rule, approval matching, run authorization) must be mutation-checked: the plan says which line to break and which test must fail.

---

# PR 1 — Backend: data, rules, tools, actions, approvals, connections

## PR 1: Account binding: data, rules, tools, actions, approvals and connections

**Branch:** `feat/account-connection-backend`, cut from `origin/develop` **after** #1983, #1986 and #1987 have merged (design, "Order of work"). Every line range below is taken from the local preview merge (develop `fdc9c00fc` + `feat/nous-rc` + #1987, preview HEAD `1c052fc9f`). Re-check the ranges against the real merge base before you edit. If they moved, find the same function by name.

**Goal:** add a third binding kind, `account`, to consent requests, grants and tool actions; make account folders (`bridge_workspace_bindings.project_id IS NULL`) possible; enforce the account rule in grant validation, read tools and library actions; list account approvals under Connected devices; put the whole feature behind `ACCOUNT_CONNECTIONS_ENABLED`. Harness runs and the bridge come in PR 2, and the website in PR 3. In PR 1 the harness socket still refuses an account grant (Task 14 pins this).

**Conventions used below**

```sh
# from <checkout> (the repository root of the PR worktree)
export PYTEST="env ENVIRONMENT=testing PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q"
# Local PostgreSQL 14+, no Docker (docs/testing/harness-live-proof.md Gate 0):
#   PGDIR=<scratch>/pg; initdb -D "$PGDIR/data" -U postgres --auth=trust -E UTF8 --locale=C
#   pg_ctl -D "$PGDIR/data" -o "-p 55432 -c unix_socket_directories=''" -l "$PGDIR/pg.log" start
#   psql -h 127.0.0.1 -p 55432 -U postgres -c 'create database orch'
# (unix_socket_directories='' avoids the 103-byte socket-path limit when <scratch> is a long macOS temp path.)
export PG=postgresql://postgres@127.0.0.1:55432/orch
```

A PostgreSQL test run without `ORCHESTRATION_TEST_DATABASE_URL` is skipped, and a skip counts as **NOT RUN**, never as a pass. Every PostgreSQL command below sets the variable explicitly.

### Notes on the contract (read before Task 1)

1. **Alembic head.** On the preview the single head is `aw03_artifact_completion` (from #1973), not `ic02_selected_skill_snapshot`. `down_revision` is therefore `aw03_artifact_completion`. Re-check with `(cd backend && <PY> ../scripts/ci/check_alembic.py && <PY> -m alembic heads)` on the real base. If the head moved, change `down_revision` and `PARENT` in Task 1. This was a "verify" in the contract, not a deviation.
2. **`BindingKind` lives in the schema module.** It is defined in `backend/src/schemas/integration_context.py`, because schemas may not import services, and `services/integrations/context.py` re-exports it (`from src.schemas.integration_context import BindingKind`). So `context.BindingKind` exists as the contract says.
3. **Added names** that the contract does not list but PR 2 and PR 3 may use:
   - `account_workspace_condition(user_id, organization_id)`: the account rule on `Workspace`.
   - `authorized_account(db, user_id, organization_id)`: the user is active in a live organization.
   - `authorized_account_workspace(db, user_id, organization_id, workspace_id)`: one workspace in the account, returning `(id, name)`.
   - `account_summary(db, user_id, organization_id) -> AccountSummaryDTO`.
   - `AccountConnectionsDisabled`.
   - In the schemas module: `derive_binding_kind(project_id, workspace_id)` and `binding_matches_kind(kind, project_id, workspace_id)`.
4. **`account_wide` and `project_ids` count only consents that hold `harness:execute`.** These are the consents `bound_thread_ids` already uses: consumed, not revoked, on a live device. `project_ids` includes chat-bound project consents. This narrows the contract wording to what the composer filter needs, as the PR 3 planner asked.
5. **Registering an account folder** requires a live account consent that holds `harness:execute`. Folders exist only to run Codex.
6. **`list_library` gains `workspace_id` and `workspace_name` for account grants only.** Project and workspace grants keep their exact payload, so their byte-capped paging is unchanged.
7. **`ConnectionConsent` for an account consent** has `kind: "account"` and null ids and labels. The website renders the words "Whole account" from `kind`. No label field was added.
8. **`WorkspaceBindingCreate.project_id` is `UUID | None = None`.** A request that omits it, or sends `null`, registers an account folder. That is what PR 2 assumes (G6). The service still refuses an account folder unless the device holds a live account run consent, so a request that omits the field by mistake fails closed.
9. **The action CHECK is renamed.** `ck_integration_tool_actions_some_binding` becomes `ck_integration_tool_actions_binding_kind`, matching the two request and grant CHECKs.
10. **Consent precedence in `mint_integration_grant`** (requested by the PR 3 planner):
    - A project-kind mint considers the device's project consents for that project and its account consents covering that project.
    - If exactly one matching account consent exists, it wins, even when a project consent for the same project is still live.
    - If no account consent matches, the existing rule applies unchanged: exactly one project consent, otherwise refuse.
    - Two matching account consents are ambiguous and fail closed.
    - A second account request on the same device is allowed (PR 2, G5); the mint fails closed while two are live.
    - **`consent_kind`** (PR 2 always passes it): a new keyword `mint_integration_grant(..., consent_kind: BindingKind | None = None)`. When given, only consents of that kind are considered, and among them exactly one must match. When None, the precedence rule above applies.
    - Reason: `nous --rc` replaces a project pairing with an account pairing and leaves the project consent live until the user revokes it. The account lineage is the one the running bridge holds.
    - Today's `connect` full flow (`packages/harness-bridge/src/cli.ts`, about lines 420-427) registers a **new device** on every full pairing, so the clash arises only when a device is reused. The rule covers that case anyway.
11. **Turning `ACCOUNT_CONNECTIONS_ENABLED` off** blocks new account requests (503). It does not end existing account approvals; revoking does. This matches "gates creating account approvals".
12. **Guesses.**
    - `services/harness/*`, `api/harness.py`, `selected_context.py`, `handoffs.py` and `artifacts/*` are not changed in PR 1. An account grant can never hold their scopes, and the socket refuses a context without a project. These files were read in the preview, not run.
    - The `oasdiff` result of Task 15 is a guess. A nullable `WorkspaceBindingDTO.project_id` is likely to be reported as `response-property-became-nullable` (ERR).

### File map

| Path (repo-relative) | Change |
| --- | --- |
| `backend/alembic/versions/ac01_account_bindings.py` | new migration |
| `backend/src/models/integration_grant.py` (L1-95) | `binding_kind`, kind CHECKs, `derived_binding_kind` |
| `backend/src/models/tool_action.py` (L1-92) | `binding_kind`, `target_workspace_id`, kind-aware CHECK |
| `backend/src/models/bridge_device.py` (L25-40) | `WorkspaceBinding.project_id` nullable |
| `backend/src/core/config.py` (L103-108) | `ACCOUNT_CONNECTIONS_ENABLED` |
| `backend/src/schemas/integration_context.py` (L1-127) | `BindingKind`, account variants, DTO fields |
| `backend/src/schemas/integration_connections.py` (L10-25) | `kind` gains `"account"` |
| `backend/src/schemas/tool_actions.py` (L21-40) | `ActionActor.binding_kind` |
| `backend/src/services/integrations/context.py` (L1-1005) | account rule, scopes, validation, requests, grants, devices, folders |
| `backend/src/api/integrations/grants.py` (L32-40) | 503 mapping |
| `backend/src/services/integrations/read_tools.py` (L79-138, L326-465, L1429-1533) | account selector, `_target_project`, `list_library` |
| `backend/src/api/integrations/tools.py` (L26-33) | pass the binding kind to the catalog |
| `backend/src/api/integrations/actions.py` (L45-56) | `ActionActor.binding_kind` |
| `backend/src/services/agent/tool_actions.py` (L46-70, L342-435, L476-671, L723-831, L958-1096, L1271-1345) | account actions |
| `backend/src/services/integrations/connections.py` (L99-172) | account consents listed |
| `backend/openapi.json`, `frontend/src/types/generated/api.d.ts` | regenerated |
| `docs/engineering/harness-bridge.md` (insert before L116) | "Account connections" section |
| Tests | `backend/tests/unit/test_account_bindings_migration.py` (new), `backend/tests/integration/test_account_bindings_migration_postgres.py` (new), `backend/tests/unit/services/integrations/test_account_scope.py` (new), `backend/tests/unit/services/integrations/test_account_connections.py` (new), `backend/tests/unit/api/test_integration_account_requests.py` (new); edits to `test_context.py`, `test_read_tools.py`, `test_tool_actions.py`, `test_connections.py`, `backend/tests/unit/api/test_harness_workspace_guard.py`, `backend/tests/unit/architecture/test_integration_boundaries.py` |

---

### Task 1: Migration `ac01_account_bindings` (offline SQL test first)

**Files**
- Create: `backend/alembic/versions/ac01_account_bindings.py`
- Create: `backend/tests/unit/test_account_bindings_migration.py`. The pattern is `backend/tests/unit/test_workspace_grants_migration.py`, which renders `--sql` in a child process and never imports alembic in-process (see `backend/tests/unit/ci/test_migration_tests_collect_together.py`).

**Step 1: Write the failing test**

```python
# backend/tests/unit/test_account_bindings_migration.py
"""Offline checks of the ac01_account_bindings migration (account connections).

``alembic upgrade FROM:TO --sql`` renders the revision's DDL without a
database. This proves what the revision sends to PostgreSQL. It does not prove
that PostgreSQL accepts it: that is
``tests/integration/test_account_bindings_migration_postgres.py``. Offline mode
also skips the downgrade's guard against account-bound harness sessions (it
needs a live connection).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT = "aw03_artifact_completion"
REVISION = "ac01_account_bindings"
REQUESTS, GRANTS, ACTIONS = (
    "integration_grant_requests",
    "integration_grants",
    "integration_tool_actions",
)
BINDINGS = "bridge_workspace_bindings"
KIND_CHECK = (
    "(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL)"
    " OR (binding_kind = 'workspace' AND project_id IS NULL AND workspace_id IS NOT NULL)"
    " OR (binding_kind = 'account' AND project_id IS NULL AND workspace_id IS NULL)"
)
ACTION_KIND_CHECK = (
    "(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL"
    " AND target_workspace_id IS NULL)"
    " OR (binding_kind = 'workspace' AND workspace_id IS NOT NULL"
    " AND target_workspace_id IS NULL)"
    " OR (binding_kind = 'account' AND workspace_id IS NULL"
    " AND (project_id IS NULL) <> (target_workspace_id IS NULL))"
)


def _alembic(*alembic_args: str) -> str:
    """What ``python -m alembic ...`` prints, run in a child process (never
    import alembic here: see test_migration_tests_collect_together.py)."""
    env = {k: v for k, v in os.environ.items() if k != "SUPABASE_DB_URL"}
    env["DATABASE_URL"] = "postgresql://offline.invalid/nous"
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *alembic_args],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _render(*alembic_args: str) -> str:
    return re.sub(r"\s+", " ", _alembic(*alembic_args, "--sql"))


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _render("upgrade", f"{PARENT}:{REVISION}")


@pytest.fixture(scope="module")
def downgrade_sql() -> str:
    return _render("downgrade", f"{REVISION}:{PARENT}")


def _position(sql: str, statement: str) -> int:
    assert statement in sql, statement
    return sql.index(statement)


def test_revision_follows_the_head_and_fits_the_version_column() -> None:
    shown = _alembic("show", REVISION)
    parent = re.search(r"^Parent: (.+)$", shown, re.MULTILINE)
    assert parent is not None and parent.group(1).strip() == PARENT, shown
    assert len(REVISION) <= 32


def test_the_revision_chain_has_a_single_head() -> None:
    heads = [line for line in _alembic("heads").splitlines() if line.strip()]
    assert len(heads) == 1, heads


@pytest.mark.parametrize("table", [REQUESTS, GRANTS, ACTIONS])
def test_upgrade_backfills_the_kind_then_drops_the_default(
    upgrade_sql: str, table: str
) -> None:
    steps = [
        f"ALTER TABLE {table} ADD COLUMN binding_kind VARCHAR(16) "
        "DEFAULT 'project' NOT NULL;",
        f"UPDATE {table} SET binding_kind = 'workspace' WHERE workspace_id IS NOT NULL;",
        f"ALTER TABLE {table} ALTER COLUMN binding_kind DROP DEFAULT;",
    ]
    positions = [_position(upgrade_sql, step) for step in steps]
    assert positions == sorted(positions)


@pytest.mark.parametrize("table", [REQUESTS, GRANTS])
def test_upgrade_replaces_one_binding_with_one_check_per_kind(
    upgrade_sql: str, table: str
) -> None:
    dropped = _position(
        upgrade_sql, f"ALTER TABLE {table} DROP CONSTRAINT ck_{table}_one_binding;"
    )
    added = _position(
        upgrade_sql,
        f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_binding_kind "
        f"CHECK ({KIND_CHECK});",
    )
    backfilled = _position(
        upgrade_sql, f"ALTER TABLE {table} ALTER COLUMN binding_kind DROP DEFAULT;"
    )
    assert backfilled < dropped < added


def test_upgrade_gives_actions_a_target_workspace_and_a_kind_aware_check(
    upgrade_sql: str,
) -> None:
    column = _position(
        upgrade_sql, f"ALTER TABLE {ACTIONS} ADD COLUMN target_workspace_id UUID;"
    )
    foreign_key = _position(
        upgrade_sql,
        f"ALTER TABLE {ACTIONS} ADD FOREIGN KEY(target_workspace_id) "
        "REFERENCES workspaces (id);",
    )
    index = _position(
        upgrade_sql,
        f"CREATE INDEX ix_{ACTIONS}_target_workspace_id ON {ACTIONS} "
        "(target_workspace_id);",
    )
    dropped = _position(
        upgrade_sql, f"ALTER TABLE {ACTIONS} DROP CONSTRAINT ck_{ACTIONS}_some_binding;"
    )
    added = _position(
        upgrade_sql,
        f"ALTER TABLE {ACTIONS} ADD CONSTRAINT ck_{ACTIONS}_binding_kind "
        f"CHECK ({ACTION_KIND_CHECK});",
    )
    assert column < foreign_key < index < added and dropped < added


def test_upgrade_frees_the_folder_project_and_deletes_nothing(
    upgrade_sql: str,
) -> None:
    assert (
        f"ALTER TABLE {BINDINGS} ALTER COLUMN project_id DROP NOT NULL;" in upgrade_sql
    )
    assert "DELETE" not in upgrade_sql
    assert (
        f"UPDATE alembic_version SET version_num='{REVISION}' "
        f"WHERE alembic_version.version_num = '{PARENT}';"
    ) in upgrade_sql


def test_downgrade_deletes_account_rows_children_first(downgrade_sql: str) -> None:
    steps = [
        f"DELETE FROM {BINDINGS} WHERE project_id IS NULL;",
        f"ALTER TABLE {BINDINGS} ALTER COLUMN project_id SET NOT NULL;",
        f"ALTER TABLE {ACTIONS} DROP CONSTRAINT ck_{ACTIONS}_binding_kind;",
        f"DELETE FROM {ACTIONS} WHERE binding_kind = 'account';",
        f"ALTER TABLE {ACTIONS} DROP COLUMN target_workspace_id;",
        f"ALTER TABLE {ACTIONS} DROP COLUMN binding_kind;",
        f"ALTER TABLE {ACTIONS} ADD CONSTRAINT ck_{ACTIONS}_some_binding "
        "CHECK (project_id IS NOT NULL OR workspace_id IS NOT NULL);",
        f"ALTER TABLE {GRANTS} DROP CONSTRAINT ck_{GRANTS}_binding_kind;",
        f"DELETE FROM {GRANTS} WHERE binding_kind = 'account' OR request_id IN "
        f"(SELECT id FROM {REQUESTS} WHERE binding_kind = 'account');",
        f"ALTER TABLE {GRANTS} DROP COLUMN binding_kind;",
        f"ALTER TABLE {GRANTS} ADD CONSTRAINT ck_{GRANTS}_one_binding "
        "CHECK ((project_id IS NULL) <> (workspace_id IS NULL));",
        f"ALTER TABLE {REQUESTS} DROP CONSTRAINT ck_{REQUESTS}_binding_kind;",
        f"DELETE FROM {REQUESTS} WHERE binding_kind = 'account';",
        f"ALTER TABLE {REQUESTS} DROP COLUMN binding_kind;",
        f"ALTER TABLE {REQUESTS} ADD CONSTRAINT ck_{REQUESTS}_one_binding "
        "CHECK ((project_id IS NULL) <> (workspace_id IS NULL));",
    ]
    positions = [_position(downgrade_sql, step) for step in steps]
    assert positions == sorted(positions)
```

**Step 2: Run it (expect failure)**

```sh
$PYTEST backend/tests/unit/test_account_bindings_migration.py
```
Expected: every test fails. `alembic show ac01_account_bindings` exits non-zero ("Can't locate revision"), so `_alembic` asserts on the return code.

**Step 3: Implement**

```python
# backend/alembic/versions/ac01_account_bindings.py
"""Account-wide harness connections: a third binding kind.

A consent request and a grant now carry ``binding_kind``. ``project`` binds
one Collection and ``workspace`` binds one workspace, as before. ``account``
binds neither: it covers every live, unarchived Collection of the user's
organization workspaces that they own or belong to, evaluated per statement
(``services/integrations/context.account_scope_condition``). One CHECK per
kind replaces the old XOR, so "both NULL" is accepted only for an
``account`` row, never by accident.

An action row records its authorising grant's kind. ``target_workspace_id``
holds the workspace ``create_folder`` creates a folder in under an account
grant, because ``workspace_id`` on that table means "the grant's workspace".
A NULL ``bridge_workspace_bindings.project_id`` is an account folder.

Downgrade deletes account approvals, account grants and the project grants
minted under an account approval, account actions and account folders. It
refuses while a harness session cites any of them: those runs belong to their
users and are not deleted by a migration.

Revision ID: ac01_account_bindings
Revises: aw03_artifact_completion
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ac01_account_bindings"
down_revision = "aw03_artifact_completion"
branch_labels = None
depends_on = None

REQUESTS = "integration_grant_requests"
GRANTS = "integration_grants"
ACTIONS = "integration_tool_actions"
BINDINGS = "bridge_workspace_bindings"
TARGET_INDEX = "ix_integration_tool_actions_target_workspace_id"

KIND_CHECK = (
    "(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL)"
    " OR (binding_kind = 'workspace' AND project_id IS NULL AND workspace_id IS NOT NULL)"
    " OR (binding_kind = 'account' AND project_id IS NULL AND workspace_id IS NULL)"
)
ACTION_KIND_CHECK = (
    "(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL"
    " AND target_workspace_id IS NULL)"
    " OR (binding_kind = 'workspace' AND workspace_id IS NOT NULL"
    " AND target_workspace_id IS NULL)"
    " OR (binding_kind = 'account' AND workspace_id IS NULL"
    " AND (project_id IS NULL) <> (target_workspace_id IS NULL))"
)
ONE_BINDING = "(project_id IS NULL) <> (workspace_id IS NULL)"
SOME_BINDING = "project_id IS NOT NULL OR workspace_id IS NOT NULL"
DOOMED_GRANTS = (
    f"binding_kind = 'account' OR request_id IN "
    f"(SELECT id FROM {REQUESTS} WHERE binding_kind = 'account')"
)
ACCOUNT_RUNS = (
    f"SELECT 1 FROM harness_sessions WHERE grant_id IN "
    f"(SELECT id FROM {GRANTS} WHERE {DOOMED_GRANTS}) "
    f"OR (device_id, workspace_id) IN "
    f"(SELECT device_id, workspace_id FROM {BINDINGS} WHERE project_id IS NULL) "
    f"LIMIT 1"
)


def upgrade() -> None:
    for table in (REQUESTS, GRANTS, ACTIONS):
        # The default exists only to backfill: every writer names the kind.
        op.add_column(
            table,
            sa.Column(
                "binding_kind",
                sa.String(16),
                nullable=False,
                server_default="project",
            ),
        )
        op.execute(
            f"UPDATE {table} SET binding_kind = 'workspace' "
            f"WHERE workspace_id IS NOT NULL"
        )
        op.alter_column(table, "binding_kind", server_default=None)
    op.add_column(
        ACTIONS,
        sa.Column(
            "target_workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=True,
        ),
    )
    op.create_index(TARGET_INDEX, ACTIONS, ["target_workspace_id"])
    for table in (REQUESTS, GRANTS):
        op.drop_constraint(f"ck_{table}_one_binding", table, type_="check")
        op.create_check_constraint(f"ck_{table}_binding_kind", table, KIND_CHECK)
    op.drop_constraint(f"ck_{ACTIONS}_some_binding", ACTIONS, type_="check")
    op.create_check_constraint(
        f"ck_{ACTIONS}_binding_kind", ACTIONS, ACTION_KIND_CHECK
    )
    op.alter_column(BINDINGS, "project_id", nullable=True)


def downgrade() -> None:
    # op.get_context().as_sql, not alembic.context.is_offline_mode(): the
    # PostgreSQL test drives this module through a bare Operations object.
    if not op.get_context().as_sql:
        if op.get_bind().execute(sa.text(ACCOUNT_RUNS)).first() is not None:
            raise RuntimeError(
                "ac01_account_bindings: a harness session cites an account "
                "grant or an account folder. Remove those Local Codex runs by "
                "hand before downgrading; this migration does not delete them."
            )
    # Children before the parents they cite: folders, actions, grants (which
    # hold request_id), then the requests themselves.
    op.execute(f"DELETE FROM {BINDINGS} WHERE project_id IS NULL")
    op.alter_column(BINDINGS, "project_id", nullable=False)
    op.drop_constraint(f"ck_{ACTIONS}_binding_kind", ACTIONS, type_="check")
    op.execute(f"DELETE FROM {ACTIONS} WHERE binding_kind = 'account'")
    op.drop_index(TARGET_INDEX, table_name=ACTIONS)
    op.drop_column(ACTIONS, "target_workspace_id")
    op.drop_column(ACTIONS, "binding_kind")
    op.create_check_constraint(f"ck_{ACTIONS}_some_binding", ACTIONS, SOME_BINDING)
    op.drop_constraint(f"ck_{GRANTS}_binding_kind", GRANTS, type_="check")
    op.execute(f"DELETE FROM {GRANTS} WHERE {DOOMED_GRANTS}")
    op.drop_column(GRANTS, "binding_kind")
    op.create_check_constraint(f"ck_{GRANTS}_one_binding", GRANTS, ONE_BINDING)
    op.drop_constraint(f"ck_{REQUESTS}_binding_kind", REQUESTS, type_="check")
    op.execute(f"DELETE FROM {REQUESTS} WHERE binding_kind = 'account'")
    op.drop_column(REQUESTS, "binding_kind")
    op.create_check_constraint(f"ck_{REQUESTS}_one_binding", REQUESTS, ONE_BINDING)
```

The downgrade does not delete `integration_context_selections` or `integration_handoffs` rows. Neither can cite an account consent or grant: `context:read`, `handoff:*` and `artifacts:publish` are refused for the account kind, at request time and on every use (Task 6).

**Step 4: Run (expect pass)**

```sh
$PYTEST backend/tests/unit/test_account_bindings_migration.py backend/tests/unit/test_workspace_grants_migration.py backend/tests/unit/ci/test_migration_tests_collect_together.py
(cd backend && <PY> ../scripts/ci/check_alembic.py)
```
Expected: all pass, and `check_alembic.py` reports one head, `ac01_account_bindings`. The SQL strings were checked against alembic's offline renderer for `ADD COLUMN ... DEFAULT 'project' NOT NULL`, `DROP DEFAULT`, the FK and index lines, and `DROP NOT NULL`/`SET NOT NULL`.

**Step 5: Commit**

```sh
git add backend/alembic/versions/ac01_account_bindings.py backend/tests/unit/test_account_bindings_migration.py
git commit -m "feat(integrations): ac01_account_bindings migration for account connections"
```

---

### Task 2: Models carry the kind (SQLite metadata test)

**Files**
- Modify: `backend/src/models/integration_grant.py` (L1-95)
- Modify: `backend/src/models/tool_action.py` (L1-92)
- Modify: `backend/src/models/bridge_device.py` (L25-40)
- Modify: `backend/tests/unit/services/integrations/test_context.py` (L916-980: `_BINDING_ROWS`, `_BINDINGS`, `_REFUSED`, `test_database_enforces_the_binding_rule`)

**Step 1: Write the failing test.** Replace L947-980 of `test_context.py` (from `_BINDINGS: dict...` through the end of `test_database_enforces_the_binding_rule`) with the block below. `_BINDING_ROWS` (L916-944) stays as it is.

```python
# Each shape: (columns, is the row accepted?) per table. Without binding_kind
# the model derives project or workspace from the one column set, never
# account: a row naming neither is refused (NOT NULL), and one naming both is
# refused by the kind CHECK. An action's project_id is its target and its
# workspace_id the grant's binding, so a workspace action may name both.
_BINDINGS: dict[str, dict[str, Any]] = {
    "project": {"project_id": PROJECT},
    "workspace": {"workspace_id": WORKSPACE},
    "both": {"project_id": PROJECT, "workspace_id": WORKSPACE},
    "neither": {},
    "account": {"binding_kind": "account"},
    "account-with-project": {"binding_kind": "account", "project_id": PROJECT},
    "account-with-workspace": {"binding_kind": "account", "workspace_id": WORKSPACE},
    "project-kind-without-project": {"binding_kind": "project"},
    "account-action-with-target-workspace": {
        "binding_kind": "account",
        "target_workspace_id": WORKSPACE,
    },
    "account-action-with-project-and-target": {
        "binding_kind": "account",
        "project_id": PROJECT,
        "target_workspace_id": WORKSPACE,
    },
    "project-action-with-target-workspace": {
        "project_id": PROJECT,
        "target_workspace_id": WORKSPACE,
    },
}
_ACCEPTED: dict[str, set[str]] = {
    "integration_grant_requests": {"project", "workspace", "account"},
    "integration_grants": {"project", "workspace", "account"},
    "integration_tool_actions": {
        "project",
        "workspace",
        "both",
        "account-with-project",
        "account-action-with-target-workspace",
    },
}
_ACTION_ONLY = {
    "account-action-with-target-workspace",
    "account-action-with-project-and-target",
    "project-action-with-target-workspace",
}


@pytest.mark.parametrize("table_name", sorted(_BINDING_ROWS))
@pytest.mark.parametrize("binding", sorted(_BINDINGS))
async def test_database_enforces_the_binding_rule(
    db: AsyncSession, table_name: str, binding: str
) -> None:
    from src.models.base import Base

    if binding in _ACTION_ONLY and table_name != "integration_tool_actions":
        pytest.skip("target_workspace_id is an action column")
    row = insert(Base.metadata.tables[table_name]).values(
        **_BINDING_ROWS[table_name], **_BINDINGS[binding]
    )
    if binding in _ACCEPTED[table_name]:
        await db.execute(row)
        await db.commit()
        return
    # NOT NULL (no kind derivable) or the kind CHECK: both name binding_kind.
    with pytest.raises(IntegrityError, match="binding_kind"):
        await db.execute(row)


async def test_the_kind_is_derived_but_never_as_account(db: AsyncSession) -> None:
    from src.models.integration_grant import IntegrationGrantRequest

    device = uuid4()
    for values in ({"project_id": PROJECT}, {"workspace_id": WORKSPACE}):
        db.add(
            IntegrationGrantRequest(
                user_id=USER,
                organization_id=ORG,
                device_id=device,
                scopes=["tools:read"],
                status="pending",
                expires_at=SOON,
                **values,
            )
        )
    await db.commit()
    kinds = (
        await db.scalars(
            select(IntegrationGrantRequest.binding_kind).where(
                IntegrationGrantRequest.device_id == device
            )
        )
    ).all()
    assert sorted(kinds) == ["project", "workspace"]
```

`_BINDING_ROWS["integration_tool_actions"]` uses a fixed `invocation_id`. Each parametrized case gets a fresh database (function-scoped `db`), so the unique key never clashes.

**Step 2: Run it (expect failure)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_context.py -k "binding_rule or derived_but_never"
```
Expected: errors such as `TypeError: 'binding_kind' is an invalid keyword argument` or `CompileError: Unconsumed column names: binding_kind`, and a failing `account` case.

**Step 3: Implement**

`backend/src/models/integration_grant.py`: replace the whole file.

```python
"""Hashed short-lived grants and immutable browser consent requests."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import GUID, BaseModel

# One binding per kind (migration ac01_account_bindings). "account" binds
# neither column: what it covers is evaluated per statement
# (services/integrations/context.account_scope_condition).
BINDING_KIND_CHECK = (
    "(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL)"
    " OR (binding_kind = 'workspace' AND project_id IS NULL AND workspace_id IS NOT NULL)"
    " OR (binding_kind = 'account' AND project_id IS NULL AND workspace_id IS NULL)"
)


def derived_binding_kind(context: Any) -> str | None:
    """Default ``binding_kind`` for a writer that names only ``project_id`` or
    ``workspace_id`` (every writer that predates ac01).

    Never ``"account"``: an account row must say so, and a row naming neither
    column gets None and fails NOT NULL. Context-sensitive defaults do not work
    with a multi-VALUES ``insert(...).values([...])``; such a writer passes
    ``binding_kind`` itself.
    """
    params = context.get_current_parameters()
    if params.get("workspace_id") is not None:
        return "workspace"
    if params.get("project_id") is not None:
        return "project"
    return None


class IntegrationGrantRequest(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "integration_grant_requests"
    __table_args__ = (
        CheckConstraint(
            BINDING_KIND_CHECK, name="ck_integration_grant_requests_binding_kind"
        ),
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    # "project" | "workspace" | "account" (BindingKind).
    binding_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=derived_binding_kind
    )
    project_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=True
    )
    workspace_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("workspaces.id"), nullable=True, index=True
    )
    device_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("bridge_devices.id"), nullable=False, index=True
    )
    thread_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="CASCADE"), index=True
    )
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationGrant(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "integration_grants"
    __table_args__ = (
        CheckConstraint(BINDING_KIND_CHECK, name="ck_integration_grants_binding_kind"),
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id"), nullable=False
    )
    organization_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("organizations.id"), nullable=False
    )
    # The grant's own kind. A run's grant under an account approval is
    # "project" while its request_id names the "account" consent.
    binding_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=derived_binding_kind
    )
    project_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=True
    )
    workspace_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("workspaces.id"), nullable=True, index=True
    )
    device_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("bridge_devices.id"), index=True
    )
    request_id: Mapped[UUID | None] = mapped_column(
        GUID(),
        ForeignKey("integration_grant_requests.id", ondelete="SET NULL"),
        index=True,
    )
    thread_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="SET NULL")
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.job_id")
    )
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consented_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
```

`backend/src/models/tool_action.py`: add the import, the constant, the CHECK and the two columns. Replace L37-55 (class header through `__table_args__`) and insert the columns after `workspace_id` (L64-66).

```python
from src.models.integration_grant import derived_binding_kind

# The kind of the grant that stored the row. project: the Collection it
# targets is the grant's. workspace: workspace_id is the grant's workspace and
# project_id, when set, the Collection it targets. account: workspace_id is
# NULL and the row targets exactly one Collection (project_id) or, for
# create_folder, one workspace (target_workspace_id).
ACTION_BINDING_KIND_CHECK = (
    "(binding_kind = 'project' AND project_id IS NOT NULL AND workspace_id IS NULL"
    " AND target_workspace_id IS NULL)"
    " OR (binding_kind = 'workspace' AND workspace_id IS NOT NULL"
    " AND target_workspace_id IS NULL)"
    " OR (binding_kind = 'account' AND workspace_id IS NULL"
    " AND (project_id IS NULL) <> (target_workspace_id IS NULL))"
)


class IntegrationToolAction(BaseModel):
    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4, index=True)  # type: ignore[assignment]  # Typed override of legacy BaseModel.id.
    __tablename__ = "integration_tool_actions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            "invocation_id",
            name="uq_integration_tool_actions_invocation",
        ),
        CheckConstraint(
            ACTION_BINDING_KIND_CHECK,
            name="ck_integration_tool_actions_binding_kind",
        ),
    )
    # ... organization_id, user_id, project_id, workspace_id unchanged ...
    binding_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=derived_binding_kind
    )
    # Where an account grant's create_folder creates the folder.
    target_workspace_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("workspaces.id"), nullable=True, index=True
    )
```

`backend/src/models/bridge_device.py` L37-39:

```python
    # NULL: an account folder, usable from any chat whose project the device's
    # account approval covers (services/integrations/context.bind_workspace).
    project_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=True
    )
```

**Step 4: Run (expect pass, plus the regression suites that seed these tables)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_context.py backend/tests/unit/services/agent/test_tool_actions.py backend/tests/unit/services/integrations/test_connections.py backend/tests/unit/services/integrations/test_grant_resume.py backend/tests/unit/services/integrations/test_grant_thread_binding.py backend/tests/unit/services/harness
```
Expected: all pass. Existing seeds name `project_id` or `workspace_id` and get the derived kind. No existing test does a multi-VALUES insert into these three tables; the preview was grepped.

**Step 5: Commit**

```sh
git add backend/src/models/integration_grant.py backend/src/models/tool_action.py backend/src/models/bridge_device.py backend/tests/unit/services/integrations/test_context.py
git commit -m "feat(integrations): binding_kind on requests, grants and actions; nullable folder project"
```

---

### Task 3: Migration up and down on PostgreSQL, with rows

**Files**
- Create: `backend/tests/integration/test_account_bindings_migration_postgres.py`. The pattern is `backend/tests/integration/test_academic_wave_migrations.py`: it loads the revision module and runs it through `Operations(MigrationContext.configure(connection))` in a throwaway schema.

**Step 1: Write the failing test**

```python
# backend/tests/integration/test_account_bindings_migration_postgres.py
"""ac01_account_bindings on real PostgreSQL: backfill, CHECKs and downgrade.

Run with ORCHESTRATION_TEST_DATABASE_URL set (docs/testing/harness-live-proof.md
Gate 0). Without it every test skips, which is NOT RUN, never PASS.

The schema is built from the models (post-ac01), then taken to the pre-ac01
shape by running this revision's downgrade on empty tables. Pre-ac01 rows are
inserted with raw SQL because the models already name binding_kind.
"""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, create_engine, insert, text
from sqlalchemy.exc import IntegrityError

from src.models.base import Base
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.collection import Collection
from src.models.harness_session import HarnessSession
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.tool_action import IntegrationToolAction
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]
VERSIONS = Path(__file__).parents[2] / "alembic" / "versions"
USER, ORG, WORKSPACE, PROJECT, DEVICE = (uuid4() for _ in range(5))
SOON = datetime.now(timezone.utc) + timedelta(hours=1)
_MODELS: list[Any] = [
    Organization,
    User,
    Workspace,
    WorkspaceMember,
    Collection,
    BridgeDevice,
    WorkspaceBinding,
    IntegrationGrantRequest,
    IntegrationGrant,
    IntegrationToolAction,
    HarnessSession,
]


def _revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ac01_account_bindings", VERSIONS / "ac01_account_bindings.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(connection: Connection, direction: str) -> None:
    module = _revision()
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    getattr(module, direction)()


def _raw(connection: Connection, table: str, **values: Any) -> UUID:
    """Insert one row the way a pre-ac01 writer would (no binding_kind)."""
    row_id = values.pop("id", uuid4())
    now = datetime.now(timezone.utc)
    columns = {
        "id": row_id,
        "created_at": now,
        "updated_at": now,
        "is_deleted": False,
        **values,
    }
    for key, value in columns.items():
        if isinstance(value, (list, dict)):
            columns[key] = json.dumps(value)
        elif isinstance(value, UUID):
            columns[key] = str(value)
    names = ", ".join(columns)
    params = ", ".join(f":{name}" for name in columns)
    connection.execute(text(f"INSERT INTO {table} ({names}) VALUES ({params})"), columns)
    return row_id


@pytest.fixture
def pre_ac01() -> Iterator[Connection]:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured (NOT RUN)")
    schema = "ac01_" + uuid4().hex
    admin = create_engine(dsn)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(dsn, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE threads (id uuid PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE agent_runs (job_id varchar(36) PRIMARY KEY)"
            )
            Base.metadata.create_all(
                connection, tables=[model.__table__ for model in _MODELS]
            )
            _run(connection, "downgrade")  # empty tables: to the pre-ac01 shape
            connection.execute(
                insert(Organization).values(id=ORG, name="o", storage_limit_bytes=1)
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, organization_id, email, password_hash, "
                    "first_name, last_name, role, is_active, login_count, "
                    "created_at, updated_at, is_deleted) VALUES (:id, :org, "
                    "'m@example.test', 'unused', 'T', 'U', 'USER', true, 0, now(), "
                    "now(), false)"
                ),
                {"id": str(USER), "org": str(ORG)},
            )
            connection.execute(
                insert(Workspace).values(
                    id=WORKSPACE, name="w", owner_id=USER, organization_id=ORG
                )
            )
            connection.execute(
                insert(Collection).values(id=PROJECT, name="p", workspace_id=WORKSPACE)
            )
            connection.execute(
                insert(BridgeDevice).values(
                    id=DEVICE, user_id=USER, organization_id=ORG, label="laptop"
                )
            )
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def _request(connection: Connection, **binding: Any) -> UUID:
    return _raw(
        connection,
        "integration_grant_requests",
        user_id=USER,
        organization_id=ORG,
        device_id=DEVICE,
        scopes=["tools:read"],
        status="consumed",
        expires_at=SOON,
        **binding,
    )


def _grant(connection: Connection, request_id: UUID, **binding: Any) -> UUID:
    return _raw(
        connection,
        "integration_grants",
        user_id=USER,
        organization_id=ORG,
        device_id=DEVICE,
        request_id=request_id,
        scopes=["tools:read"],
        token_hash=uuid4().hex * 2,
        expires_at=SOON,
        consented_at=SOON,
        **binding,
    )


def _action(connection: Connection, **binding: Any) -> UUID:
    return _raw(
        connection,
        "integration_tool_actions",
        organization_id=ORG,
        user_id=USER,
        invocation_id=uuid4(),
        tool_name="rename_folder",
        arguments={},
        argument_hash="z" * 64,
        state="awaiting_approval",
        **binding,
    )


def _kinds(connection: Connection, table: str) -> dict[UUID, str]:
    rows = connection.execute(text(f"SELECT id, binding_kind FROM {table}")).all()
    return {UUID(str(row_id)): kind for row_id, kind in rows}


def _constraints(connection: Connection, table: str) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE t.relname = :table AND n.nspname = current_schema() "
            "AND c.contype = 'c'"
        ),
        {"table": table},
    ).all()
    return {name for (name,) in rows}


def _refused(connection: Connection, table: str, **values: Any) -> None:
    with pytest.raises(IntegrityError, match="binding_kind"):
        with connection.begin_nested():
            _raw(connection, table, **values)


def test_upgrade_backfills_kinds_and_enforces_one_check_per_kind(
    pre_ac01: Connection,
) -> None:
    connection = pre_ac01
    with connection.begin():
        project_request = _request(connection, project_id=PROJECT)
        workspace_request = _request(connection, workspace_id=WORKSPACE)
        project_grant = _grant(connection, project_request, project_id=PROJECT)
        workspace_grant = _grant(connection, workspace_request, workspace_id=WORKSPACE)
        project_action = _action(connection, project_id=PROJECT)
        workspace_action = _action(
            connection, project_id=PROJECT, workspace_id=WORKSPACE
        )
        _raw(
            connection,
            "bridge_workspace_bindings",
            device_id=DEVICE,
            workspace_id=uuid4(),
            project_id=PROJECT,
            label="repo",
        )
        _run(connection, "upgrade")
        assert _kinds(connection, "integration_grant_requests") == {
            project_request: "project",
            workspace_request: "workspace",
        }
        assert _kinds(connection, "integration_grants") == {
            project_grant: "project",
            workspace_grant: "workspace",
        }
        assert _kinds(connection, "integration_tool_actions") == {
            project_action: "project",
            workspace_action: "workspace",
        }
        for table in ("integration_grant_requests", "integration_grants"):
            assert _constraints(connection, table) >= {f"ck_{table}_binding_kind"}
            assert f"ck_{table}_one_binding" not in _constraints(connection, table)
        # Account rows are accepted, and only in their own shape.
        account = _request(connection, binding_kind="account")
        _grant(connection, account, binding_kind="account")
        _grant(connection, account, binding_kind="project", project_id=PROJECT)
        _action(connection, binding_kind="account", project_id=PROJECT)
        _action(connection, binding_kind="account", target_workspace_id=WORKSPACE)
        _raw(
            connection,
            "bridge_workspace_bindings",
            device_id=DEVICE,
            workspace_id=uuid4(),
            project_id=None,
            label="account folder",
        )
        _refused(
            connection,
            "integration_grant_requests",
            user_id=USER,
            organization_id=ORG,
            device_id=DEVICE,
            scopes=["tools:read"],
            status="pending",
            expires_at=SOON,
            binding_kind="account",
            project_id=PROJECT,
        )
        _refused(
            connection,
            "integration_grant_requests",
            user_id=USER,
            organization_id=ORG,
            device_id=DEVICE,
            scopes=["tools:read"],
            status="pending",
            expires_at=SOON,
            binding_kind="project",
        )
        for binding in (
            {"binding_kind": "account"},
            {
                "binding_kind": "account",
                "project_id": PROJECT,
                "target_workspace_id": WORKSPACE,
            },
            {
                "binding_kind": "project",
                "project_id": PROJECT,
                "target_workspace_id": WORKSPACE,
            },
        ):
            _refused(
                connection,
                "integration_tool_actions",
                organization_id=ORG,
                user_id=USER,
                invocation_id=uuid4(),
                tool_name="rename_folder",
                arguments="{}",
                argument_hash="z" * 64,
                state="awaiting_approval",
                **binding,
            )


def test_downgrade_removes_account_rows_restores_the_old_rules_and_round_trips(
    pre_ac01: Connection,
) -> None:
    connection = pre_ac01
    with connection.begin():
        _run(connection, "upgrade")
        project_request = _request(connection, project_id=PROJECT, binding_kind="project")
        project_grant = _grant(
            connection, project_request, project_id=PROJECT, binding_kind="project"
        )
        project_action = _action(connection, project_id=PROJECT, binding_kind="project")
        account = _request(connection, binding_kind="account")
        _grant(connection, account, binding_kind="account")
        _grant(connection, account, binding_kind="project", project_id=PROJECT)
        _action(connection, binding_kind="account", project_id=PROJECT)
        _raw(
            connection,
            "bridge_workspace_bindings",
            device_id=DEVICE,
            workspace_id=uuid4(),
            project_id=None,
            label="account folder",
        )
        _run(connection, "downgrade")
        ids = lambda table: {  # noqa: E731
            UUID(str(row_id))
            for (row_id,) in connection.execute(text(f"SELECT id FROM {table}")).all()
        }
        assert ids("integration_grant_requests") == {project_request}
        assert ids("integration_grants") == {project_grant}
        assert ids("integration_tool_actions") == {project_action}
        assert (
            connection.execute(
                text("SELECT count(*) FROM bridge_workspace_bindings")
            ).scalar_one()
            == 0
        )
        nullable = connection.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns WHERE "
                "table_schema = current_schema() AND "
                "table_name = 'bridge_workspace_bindings' AND column_name = 'project_id'"
            )
        ).scalar_one()
        assert nullable == "NO"
        assert "ck_integration_tool_actions_some_binding" in _constraints(
            connection, "integration_tool_actions"
        )
        for table in ("integration_grant_requests", "integration_grants"):
            assert f"ck_{table}_one_binding" in _constraints(connection, table)
        _run(connection, "upgrade")  # the round trip applies again
        assert _kinds(connection, "integration_grants") == {project_grant: "project"}


def test_downgrade_refuses_while_a_harness_session_uses_an_account_folder(
    pre_ac01: Connection,
) -> None:
    connection = pre_ac01
    with connection.begin():
        _run(connection, "upgrade")
    folder = uuid4()
    with connection.begin():
        account = _request(connection, binding_kind="account")
        grant = _grant(connection, account, binding_kind="account")
        _raw(
            connection,
            "bridge_workspace_bindings",
            device_id=DEVICE,
            workspace_id=folder,
            project_id=None,
            label="account folder",
        )
        run_id = str(uuid4())
        connection.execute(
            text("INSERT INTO agent_runs (job_id) VALUES (:job)"), {"job": run_id}
        )
        connection.execute(
            text(
                "INSERT INTO harness_sessions (id, run_id, grant_id, device_id, "
                "workspace_id) VALUES (:id, :run, :grant, :device, :folder)"
            ),
            {
                "id": str(uuid4()),
                "run": run_id,
                "grant": str(grant),
                "device": str(DEVICE),
                "folder": str(folder),
            },
        )
    with pytest.raises(RuntimeError, match="harness session"):
        with connection.begin():
            _run(connection, "downgrade")
    # Transactional DDL: nothing changed.
    assert "ck_integration_grants_binding_kind" in _constraints(
        connection, "integration_grants"
    )
```

**Step 2: Run it (expect failure)**

Run it before Task 1's migration exists, or with the migration file moved aside:

```sh
ORCHESTRATION_TEST_DATABASE_URL=$PG $PYTEST backend/tests/integration/test_account_bindings_migration_postgres.py
```
Expected: `FileNotFoundError` for `ac01_account_bindings.py`. If Tasks 1 and 2 are already committed, the tests pass. To prove they can fail, apply the mutations below.

Mutation checks (run once, revert each):
- In `upgrade()`, delete the `UPDATE {table} SET binding_kind = 'workspace' ...` line. `test_upgrade_backfills_kinds_and_enforces_one_check_per_kind` must fail: workspace rows violate the new CHECK and the upgrade raises.
- In `downgrade()`, change `DOOMED_GRANTS` to `"binding_kind = 'account'"`. `test_downgrade_removes_account_rows...` must fail with a foreign-key violation, because the project grant under the account request still cites it.
- Delete the `ACCOUNT_RUNS` guard. `test_downgrade_refuses_while_a_harness_session_uses_an_account_folder` must fail. It reports an FK violation instead of the RuntimeError.

**Step 3: Implement.** Nothing new: this task proves Task 1 on PostgreSQL.

**Step 4: Run (expect pass)**

```sh
ORCHESTRATION_TEST_DATABASE_URL=$PG $PYTEST backend/tests/integration/test_account_bindings_migration_postgres.py
# and the empty-database replay of the whole chain:
psql -h 127.0.0.1 -p 55432 -U postgres -c 'create database mig'
(cd backend && DATABASE_URL=postgresql://postgres@127.0.0.1:55432/mig ENVIRONMENT=testing \
  <PY> -m alembic upgrade head && \
  DATABASE_URL=postgresql://postgres@127.0.0.1:55432/mig ENVIRONMENT=testing <PY> -m alembic downgrade -1 && \
  DATABASE_URL=postgresql://postgres@127.0.0.1:55432/mig ENVIRONMENT=testing <PY> -m alembic upgrade head)
```
Expected: 3 passed (not skipped), and upgrade, `downgrade -1` and upgrade run clean. Unset `SUPABASE_DB_URL` first if it is set in your shell.

**Step 5: Commit**

```sh
git add backend/tests/integration/test_account_bindings_migration_postgres.py
git commit -m "test(integrations): ac01 upgrade, downgrade and guard on PostgreSQL"
```

---

### Task 4: Schemas: `BindingKind` and the account variants

**Files**
- Modify: `backend/src/schemas/integration_context.py` (L1-127, whole file shown below)
- Modify: `backend/src/schemas/integration_connections.py` (L10-14)
- Modify: `backend/src/schemas/tool_actions.py` (L1-40: import and `ActionActor`)
- Create: `backend/tests/unit/services/integrations/test_account_connections.py` (schema section. Later tasks append to this file.)

**Step 1: Write the failing test**

```python
# backend/tests/unit/services/integrations/test_account_connections.py
"""Account connections: schemas, grants, approvals, devices and folders.

The database tests run on SQLite always and on PostgreSQL when
ORCHESTRATION_TEST_DATABASE_URL is set (docs/testing/harness-live-proof.md Gate
0). Without the URL the PostgreSQL half is skipped, which is NOT RUN, never PASS.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.schemas.integration_connections import ConnectionConsent
from src.schemas.integration_context import GrantRequestCreate, IntegrationContext
from src.schemas.tool_actions import ActionActor

pytestmark = pytest.mark.unit
SCHEMA_IDS = {name: uuid4() for name in ("user", "org", "project", "workspace", "device")}


def _ctx(**values: object) -> IntegrationContext:
    return IntegrationContext(
        user_id=SCHEMA_IDS["user"],
        organization_id=SCHEMA_IDS["org"],
        grant_id=uuid4(),
        **values,
    )


def test_context_kind_is_derived_for_project_and_workspace_never_for_account() -> None:
    assert _ctx(project_id=SCHEMA_IDS["project"]).binding_kind == "project"
    assert _ctx(workspace_id=SCHEMA_IDS["workspace"]).binding_kind == "workspace"
    assert _ctx(binding_kind="account").binding_kind == "account"
    # Both NULL means "bound to nothing" unless the kind says account.
    with pytest.raises(ValidationError, match="exactly one of project_id or workspace_id"):
        _ctx()
    with pytest.raises(ValidationError):
        _ctx(project_id=SCHEMA_IDS["project"], workspace_id=SCHEMA_IDS["workspace"])
    for wrong in (
        {"binding_kind": "account", "project_id": SCHEMA_IDS["project"]},
        {"binding_kind": "account", "workspace_id": SCHEMA_IDS["workspace"]},
        {"binding_kind": "project", "workspace_id": SCHEMA_IDS["workspace"]},
        {"binding_kind": "workspace", "project_id": SCHEMA_IDS["project"]},
    ):
        with pytest.raises(ValidationError, match="binding does not match"):
            _ctx(**wrong)


def test_grant_request_names_exactly_one_binding_and_no_chat_for_an_account() -> None:
    device, scopes = SCHEMA_IDS["device"], {"tools:read"}
    account = GrantRequestCreate(account=True, device_id=device, scopes=scopes)
    assert account.binding_kind == "account"
    assert GrantRequestCreate(
        project_id=SCHEMA_IDS["project"], device_id=device, scopes=scopes
    ).binding_kind == "project"
    assert GrantRequestCreate(
        workspace_id=SCHEMA_IDS["workspace"], device_id=device, scopes=scopes
    ).binding_kind == "workspace"
    with pytest.raises(ValidationError, match="exactly one of project_id or workspace_id"):
        GrantRequestCreate(device_id=device, scopes=scopes)
    for extra in ({"project_id": SCHEMA_IDS["project"]}, {"workspace_id": SCHEMA_IDS["workspace"]}):
        with pytest.raises(ValidationError, match="account excludes"):
            GrantRequestCreate(account=True, device_id=device, scopes=scopes, **extra)
    with pytest.raises(ValidationError, match="cannot be bound to a chat"):
        GrantRequestCreate(account=True, device_id=device, scopes=scopes, thread_id=uuid4())


def test_a_connection_consent_may_be_the_whole_account() -> None:
    consent = ConnectionConsent(
        request_id=uuid4(),
        kind="account",
        project_id=None,
        project_label=None,
        workspace_id=None,
        workspace_label=None,
        thread_id=None,
        thread_label=None,
        scopes=["harness:execute", "tools:read"],
        status="consumed",
    )
    assert consent.kind == "account"


def test_action_actor_may_name_its_kind() -> None:
    actor = ActionActor(
        user_id=SCHEMA_IDS["user"],
        organization_id=SCHEMA_IDS["org"],
        project_id=None,
        binding_kind="account",
    )
    assert actor.binding_kind == "account"
    legacy = ActionActor(
        user_id=SCHEMA_IDS["user"], organization_id=SCHEMA_IDS["org"], project_id=uuid4()
    )
    assert legacy.binding_kind is None  # derived where it is used (tool_actions)
```

**Step 2: Run it (expect failure)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_account_connections.py
```
Expected: `ValidationError: binding_kind Extra inputs are not permitted` or `unexpected keyword`. The `account` field, `account_wide` and `"account"` do not exist yet.

**Step 3: Implement**

`backend/src/schemas/integration_context.py`: replace L1-75 (module docstring through `GrantRequestCreate`) and add `AccountSummaryDTO` after `GrantRequestStatus` (L77). The classes from `GrantRequestDTO` down stay as they are in this task.

```python
"""Shared project-, workspace- or account-scoped integration identity and pairing contracts."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Scopes some route enforces, plus library:write (consented from Plan 07 slice
# 1, enforced by the auto-run actions in slice 3). context:read is enforced by
# GET /integrations/context (#1784) and, like harness:execute, artifacts:publish
# and the handoff scopes, is refused for a workspace grant (see
# services/integrations/context.py). A scope is added in the PR that checks it.
STANDARD_SCOPES = frozenset(
    {
        "harness:execute",
        "tools:read",
        "tools:write",
        "context:read",
        "artifacts:publish",
        "handoff:read",
        "handoff:write",
        # Plan 07: workspace library. read = list folders; write = reversible
        # folder/document changes run without per-action approval.
        "library:read",
        "library:write",
    }
)

# project: one Collection. workspace: every live Collection of one workspace.
# account: every live, unarchived Collection of the user's organization
# workspaces that they own or belong to, evaluated per statement
# (services/integrations/context.account_scope_condition).
BindingKind = Literal["project", "workspace", "account"]


def derive_binding_kind(
    project_id: object, workspace_id: object
) -> Literal["project", "workspace"] | None:
    """The kind a binding without a stated kind has. Never "account": both
    NULL without the kind means "bound to nothing" and is denied."""
    if project_id is not None and workspace_id is None:
        return "project"
    if workspace_id is not None and project_id is None:
        return "workspace"
    return None


def binding_matches_kind(kind: str, project_id: object, workspace_id: object) -> bool:
    """Whether the two binding columns have the shape ``kind`` requires."""
    if kind == "project":
        return project_id is not None and workspace_id is None
    if kind == "workspace":
        return workspace_id is not None and project_id is None
    if kind == "account":
        return project_id is None and workspace_id is None
    return False


class IntegrationContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: UUID
    organization_id: UUID
    # project_id: one Collection. workspace_id: every live Collection the user
    # can see in that workspace. Neither: an account context (binding_kind).
    project_id: UUID | None = None
    workspace_id: UUID | None = None
    # Derived from project_id / workspace_id when not given; an account
    # context must name it.
    binding_kind: BindingKind = "project"
    thread_id: UUID | None = None
    run_id: UUID | None = None
    grant_id: UUID
    # The consumed consent request the grant was exchanged from; None for
    # trusted internal issuance. Renewed grants share it.
    consent_id: UUID | None = None
    # The scopes of the grant this context was resolved from, so a service can
    # authorize each tool against them. Empty for a context built without
    # resolving a grant, which therefore passes no scope check.
    scopes: frozenset[str] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _derive_kind(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("binding_kind") is None:
            kind = derive_binding_kind(data.get("project_id"), data.get("workspace_id"))
            if kind is None:
                raise ValueError("exactly one of project_id or workspace_id")
            data = {**data, "binding_kind": kind}
        return data

    @model_validator(mode="after")
    def _one_binding(self) -> "IntegrationContext":
        if not binding_matches_kind(
            self.binding_kind, self.project_id, self.workspace_id
        ):
            raise ValueError("binding does not match binding_kind")
        return self


class IssuedGrant(BaseModel):
    token: str
    grant_id: UUID


class GrantRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID | None = None
    workspace_id: UUID | None = None
    # The whole account (ACCOUNT_CONNECTIONS_ENABLED): no project, workspace
    # or chat.
    account: bool = False
    device_id: UUID
    scopes: set[str]
    thread_id: UUID | None = None

    @model_validator(mode="after")
    def _one_binding(self) -> "GrantRequestCreate":
        if self.account:
            if self.project_id is not None or self.workspace_id is not None:
                raise ValueError("account excludes project_id and workspace_id")
            if self.thread_id is not None:
                raise ValueError("an account connection cannot be bound to a chat")
        elif (self.project_id is None) == (self.workspace_id is None):
            raise ValueError("exactly one of project_id or workspace_id")
        return self

    @property
    def binding_kind(self) -> BindingKind:
        if self.account:
            return "account"
        return "project" if self.project_id is not None else "workspace"


GrantRequestStatus = Literal["pending", "approved", "denied", "expired", "consumed"]


class AccountSummaryDTO(BaseModel):
    """What an account approval covers at the moment it is shown."""

    workspace_count: int
    project_count: int


# GrantRequestDTO, GrantDecision, DeviceCreate, DeviceDTO, DeviceListItemDTO,
# WorkspaceBindingCreate and WorkspaceBindingDTO are unchanged in this task.
# GrantRequestDTO gains its fields in Task 7 (with request_dto, which builds
# it), DeviceListItemDTO and WorkspaceBindingCreate in Task 10 (with
# list_devices and bind_workspace), so every commit stays green.
```

`backend/src/schemas/integration_connections.py` L10-14:

```python
class ConnectionConsent(BaseModel):
    request_id: UUID
    # One project, one workspace and every live project in it (Plan 07), or
    # the whole account (ids and labels null; the page says "Whole account").
    kind: Literal["project", "workspace", "account"]
```

`backend/src/schemas/tool_actions.py`: add `from src.schemas.integration_context import BindingKind` and this field after `workspace_id` in `ActionActor`:

```python
    # Set by the integrations router from the resolved grant. None: derived
    # from project_id / workspace_id (services/agent/tool_actions._actor_kind),
    # never "account".
    binding_kind: BindingKind | None = None
```

**Step 4: Run (expect pass; the existing context constructors still work)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_account_connections.py backend/tests/unit/services/integrations backend/tests/unit/api/test_integration_tools.py backend/tests/unit/api/test_harness_workspace_guard.py backend/tests/unit/services/artifacts
```
Expected: all pass. Every existing `IntegrationContext(...)` call names exactly one of `project_id` or `workspace_id`, so the derived kind keeps them valid.

**Step 5: Commit**

```sh
git add backend/src/schemas/integration_context.py backend/src/schemas/integration_connections.py backend/src/schemas/tool_actions.py backend/tests/unit/services/integrations/test_account_connections.py
git commit -m "feat(integrations): account binding kind in integration schemas"
```

---

### Task 5: The account rule (SECURITY-CRITICAL)

**Files**
- Modify: `backend/src/services/integrations/context.py`:
  - imports L11 and L26-36 (add `func`, `AccountSummaryDTO`, `BindingKind`)
  - insert the new block after `_workspace_organization_admits` (after L185, before `authorized_project` at L188)
- Create: `backend/tests/unit/services/integrations/test_account_scope.py`
- Modify: `backend/tests/unit/architecture/test_integration_boundaries.py` (L42-48 imports, L64-77 `ACCESS_GETTERS`)

**Step 1: Write the failing test.** This module was prototyped against the preview models and passed on SQLite and on PostgreSQL 14. The five mutations listed in Step 2 were each seen to fail it.

```python
# backend/tests/unit/services/integrations/test_account_scope.py
"""The account rule: which workspaces and projects one account approval covers.

SECURITY-CRITICAL. Runs on SQLite always and on PostgreSQL when
ORCHESTRATION_TEST_DATABASE_URL is set (docs/testing/harness-live-proof.md
Gate 0). Without the URL the PostgreSQL half is skipped, which is NOT RUN,
never PASS.

The rule is /projects' organization rule (project_access
.accessible_research_workspace_ids_query) plus "not archived", with the user
and organization live. It is stricter than authorized_project: an explicit
member of another organization's workspace keeps project-pairing access, but
that workspace is never in the account.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.collection import Collection
from src.models.organization import Organization
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.services.integrations.context import (
    IntegrationAccessDenied,
    account_scope_condition,
    account_summary,
    account_workspace_condition,
    authorized_account,
    authorized_account_workspace,
)
from src.services.research_engine.project_access import (
    accessible_research_workspace_ids_query,
)

pytestmark = pytest.mark.unit

ORG, OTHER_ORG = uuid4(), uuid4()
USER, COLLEAGUE, OUTSIDER = uuid4(), uuid4(), uuid4()
# name: (owner, organization_id, is_archived, is_deleted, is_public, USER's
# membership: None, "live" or "lapsed"). A None organization is a legacy row,
# which belongs to its owner's organization.
WORKSPACES: dict[str, tuple[UUID, UUID | None, bool, bool, bool, str | None]] = {
    "owned": (USER, ORG, False, False, False, None),
    "member": (COLLEAGUE, ORG, False, False, False, "live"),
    "legacy-owned": (USER, None, False, False, False, None),
    "legacy-colleague-member": (COLLEAGUE, None, False, False, False, "live"),
    "lapsed-member": (COLLEAGUE, ORG, False, False, False, "lapsed"),
    "not-member": (COLLEAGUE, ORG, False, False, False, None),
    "public-not-member": (COLLEAGUE, ORG, False, False, True, None),
    "other-org-member": (OUTSIDER, OTHER_ORG, False, False, False, "live"),
    "legacy-other-org-member": (OUTSIDER, None, False, False, False, "live"),
    "archived": (USER, ORG, True, False, False, None),
    "deleted": (USER, ORG, False, True, False, None),
}
WORKSPACE = {name: uuid4() for name in WORKSPACES}
PROJECT = {name: uuid4() for name in WORKSPACES}
# Extra Collections in "owned": the rule must drop the first two by themselves.
ARCHIVED_PROJECT, DELETED_PROJECT, PAUSED_PROJECT = uuid4(), uuid4(), uuid4()
IN_ACCOUNT = {"owned", "member", "legacy-owned", "legacy-colleague-member"}


async def _seed(db: AsyncSession) -> None:
    for org in (ORG, OTHER_ORG):
        await db.execute(
            insert(Organization).values(id=org, name=str(org), storage_limit_bytes=1)
        )
    for user, org in ((USER, ORG), (COLLEAGUE, ORG), (OUTSIDER, OTHER_ORG)):
        await db.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, "
                "first_name, last_name, role, is_active, login_count, created_at, "
                "updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'T', "
                "'U', 'USER', :active, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                ":deleted)"
            ),
            {
                "id": str(user),
                "org": str(org),
                "email": f"{user}@example.test",
                "active": True,
                "deleted": False,
            },
        )
    for name, (owner, org, archived, deleted, public, membership) in WORKSPACES.items():
        await db.execute(
            insert(Workspace).values(
                id=WORKSPACE[name],
                name=name,
                owner_id=owner,
                organization_id=org,
                is_archived=archived,
                is_deleted=deleted,
                is_public=public,
            )
        )
        await db.execute(
            insert(Collection).values(
                id=PROJECT[name], name=name, workspace_id=WORKSPACE[name]
            )
        )
        if membership is not None:
            await db.execute(
                insert(WorkspaceMember).values(
                    id=uuid4(),
                    workspace_id=WORKSPACE[name],
                    user_id=USER,
                    role=WorkspaceRole.EDITOR,
                    is_deleted=membership == "lapsed",
                )
            )
    await db.execute(
        insert(Collection).values(
            [
                dict(
                    id=ARCHIVED_PROJECT,
                    name="archived project",
                    workspace_id=WORKSPACE["owned"],
                    research_status="archived",
                    is_deleted=False,
                ),
                dict(
                    id=DELETED_PROJECT,
                    name="deleted project",
                    workspace_id=WORKSPACE["owned"],
                    research_status="active",
                    is_deleted=True,
                ),
                dict(
                    id=PAUSED_PROJECT,
                    name="paused project",
                    workspace_id=WORKSPACE["owned"],
                    research_status="paused",
                    is_deleted=False,
                ),
            ]
        )
    )
    await db.commit()


_MODELS: list[Any] = [Organization, User, Workspace, WorkspaceMember, Collection]


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request: pytest.FixtureRequest) -> AsyncIterator[AsyncSession]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            for model in _MODELS:
                await conn.run_sync(model.__table__.create)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await _seed(session)
                yield session
        finally:
            await engine.dispose()
        return
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured (NOT RUN)")
    dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "account_scope_" + uuid4().hex
    admin = create_async_engine(dsn)
    async with admin.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        dsn, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all, tables=[m.__table__ for m in _MODELS]
            )
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _seed(session)
            yield session
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def _account_projects(db: AsyncSession) -> set[UUID]:
    rows = await db.scalars(
        select(Collection.id).where(account_scope_condition(USER, ORG))
    )
    return {UUID(str(value)) for value in rows.all()}


async def _account_workspaces(db: AsyncSession) -> set[UUID]:
    rows = await db.scalars(
        select(Workspace.id).where(account_workspace_condition(USER, ORG))
    )
    return {UUID(str(value)) for value in rows.all()}


@pytest.mark.parametrize("name", sorted(WORKSPACES))
async def test_each_workspace_is_in_the_account_only_for_its_reason(
    db: AsyncSession, name: str
) -> None:
    inside = name in IN_ACCOUNT
    assert (WORKSPACE[name] in await _account_workspaces(db)) is inside
    assert (PROJECT[name] in await _account_projects(db)) is inside


async def test_the_account_is_exactly_its_live_unarchived_projects(
    db: AsyncSession,
) -> None:
    assert await _account_projects(db) == {PROJECT[n] for n in IN_ACCOUNT} | {
        PAUSED_PROJECT
    }
    assert await _account_workspaces(db) == {WORKSPACE[n] for n in IN_ACCOUNT}


async def test_the_account_agrees_with_the_projects_rule(db: AsyncSession) -> None:
    # /projects (and so nous --rc) lists accessible_research_workspace_ids:
    # the same organization rule, legacy NULL-org rows included. The account
    # adds only "not archived".
    research = {
        UUID(str(value))
        for value in (
            await db.scalars(accessible_research_workspace_ids_query(USER))
        ).all()
    }
    assert await _account_workspaces(db) == research - {WORKSPACE["archived"]}


@pytest.mark.parametrize(
    "lapse",
    ["user-inactive", "user-deleted", "user-left", "org-inactive", "org-deleted"],
)
async def test_a_lapsed_identity_empties_the_account(
    db: AsyncSession, lapse: str
) -> None:
    statement = {
        "user-inactive": update(User).where(User.id == USER).values(is_active=False),
        "user-deleted": update(User).where(User.id == USER).values(is_deleted=True),
        "user-left": update(User)
        .where(User.id == USER)
        .values(organization_id=OTHER_ORG),
        "org-inactive": update(Organization)
        .where(Organization.id == ORG)
        .values(is_active=False),
        "org-deleted": update(Organization)
        .where(Organization.id == ORG)
        .values(is_deleted=True),
    }[lapse]
    await db.execute(statement)
    await db.commit()
    assert await _account_projects(db) == set()
    assert await _account_workspaces(db) == set()
    with pytest.raises(IntegrationAccessDenied):
        await authorized_account(db, USER, ORG)


async def test_the_account_is_evaluated_on_every_statement(db: AsyncSession) -> None:
    await authorized_account(db, USER, ORG)
    fresh = uuid4()
    await db.execute(
        insert(Collection).values(id=fresh, name="new", workspace_id=WORKSPACE["member"])
    )
    await db.execute(
        update(Workspace)
        .where(Workspace.id == WORKSPACE["owned"])
        .values(is_archived=True)
    )
    await db.execute(
        delete(WorkspaceMember).where(
            WorkspaceMember.workspace_id == WORKSPACE["legacy-colleague-member"]
        )
    )
    await db.commit()
    assert await _account_projects(db) == {
        PROJECT["member"],
        PROJECT["legacy-owned"],
        fresh,
    }


async def test_authorized_account_workspace_names_only_account_workspaces(
    db: AsyncSession,
) -> None:
    row = await authorized_account_workspace(db, USER, ORG, WORKSPACE["member"])
    assert (row.id, row.name) == (WORKSPACE["member"], "member")
    for name in sorted(set(WORKSPACES) - IN_ACCOUNT):
        with pytest.raises(IntegrationAccessDenied):
            await authorized_account_workspace(db, USER, ORG, WORKSPACE[name])
    with pytest.raises(IntegrationAccessDenied):
        await authorized_account_workspace(db, USER, ORG, uuid4())


async def test_account_summary_counts_what_the_rule_covers_today(
    db: AsyncSession,
) -> None:
    summary = await account_summary(db, USER, ORG)
    assert (summary.workspace_count, summary.project_count) == (4, 5)
    await db.execute(
        insert(Workspace).values(
            id=uuid4(), name="empty", owner_id=USER, organization_id=ORG
        )
    )
    await db.commit()
    summary = await account_summary(db, USER, ORG)
    assert (summary.workspace_count, summary.project_count) == (5, 5)
```

Add these two entries to `ACCESS_GETTERS` in `test_integration_boundaries.py`, and import `authorized_account` and `authorized_account_workspace` beside `authorized_workspace` at L42-48:

```python
    (authorized_account, {"user_id": POSITIONAL, "organization_id": POSITIONAL}),
    (
        authorized_account_workspace,
        {"user_id": POSITIONAL, "organization_id": POSITIONAL},
    ),
```

**Step 2: Run it (expect failure)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_account_scope.py backend/tests/unit/architecture/test_integration_boundaries.py
ORCHESTRATION_TEST_DATABASE_URL=$PG $PYTEST backend/tests/unit/services/integrations/test_account_scope.py -k postgres
```
Expected: `ImportError: cannot import name 'account_scope_condition'` for both.

**Mutation checks** (after Step 3; break one line, run `-k sqlite`, then revert; repeat with `-k postgres` where noted):

| Break (in `context.py`) | Must fail |
| --- | --- |
| delete `workspace_in_org(organization_id),` in `account_workspace_condition` | `test_each_workspace_is_in_the_account_only_for_its_reason[other-org-member]` and `[legacy-other-org-member]`, `test_the_account_is_exactly_its_live_unarchived_projects`, `test_the_account_agrees_with_the_projects_rule` |
| delete `Workspace.is_archived.is_(False),` | `test_each_workspace_is_in_the_account_only_for_its_reason[archived]`, `test_the_account_agrees_with_the_projects_rule` |
| delete `membership.is_deleted.is_(False),` | `test_each_workspace_is_in_the_account_only_for_its_reason[lapsed-member]` |
| delete `Collection.research_status != "archived",` in `account_scope_condition` | `test_the_account_is_exactly_its_live_unarchived_projects` |
| delete `_account_identity_live(user_id, organization_id),` in `account_workspace_condition` | `test_a_lapsed_identity_empties_the_account[*]` |
| replace `or_(Workspace.owner_id == user_id, exists()...)` with `true()` | `[not-member]`, `[public-not-member]` |

**Step 3: Implement.** In `context.py` add `func` to the `from sqlalchemy import ...` line (L11). Extend the schema import (L26-36) with `AccountSummaryDTO` and `BindingKind`. `BindingKind` is re-exported for PR 2 and PR 3 as `context.BindingKind`. Then insert after L185:

```python
# --- The account rule (account connections) ---------------------------------
#
# An account approval covers the Collections of the workspaces in the user's
# organization that they own or are a live member of, neither workspace nor
# Collection archived or deleted, while the user is active in that live
# organization. It is the organization rule of /projects
# (research_engine.project_access.accessible_research_workspace_ids_query),
# written with workspace_in_org (rule (c) of test_integration_boundaries.py)
# plus "not archived". It is stricter than authorized_project, which keeps an
# explicit member of another organization's workspace: such a workspace is
# reachable through a project pairing, never through the account. Always a
# SQL condition, never an id list: a statement must not bind one value per
# Collection (see authorized_scope_filter).


def _account_identity_live(
    user_id: UUID, organization_id: UUID
) -> ColumnElement[bool]:
    """The user is active in ``organization_id``, and that organization is
    live. Aliased so an enclosing statement that joins users or
    organizations cannot correlate these subqueries onto its own rows."""
    actor = aliased(User)
    organization = aliased(Organization)
    return and_(
        exists().where(
            actor.id == user_id,
            actor.organization_id == organization_id,
            actor.is_active.is_(True),
            actor.is_deleted.is_(False),
        ),
        exists().where(
            organization.id == organization_id,
            organization.is_deleted.is_(False),
            organization.is_active.is_(True),
        ),
    )


def account_workspace_condition(
    user_id: UUID, organization_id: UUID
) -> ColumnElement[bool]:
    """``Workspace`` is in the account of ``user_id`` in ``organization_id``."""
    membership = aliased(WorkspaceMember)
    return and_(
        Workspace.is_deleted.is_(False),
        Workspace.is_archived.is_(False),
        or_(
            Workspace.owner_id == user_id,
            exists().where(
                membership.workspace_id == Workspace.id,
                membership.user_id == user_id,
                membership.is_deleted.is_(False),
            ),
        ),
        workspace_in_org(organization_id),
        _account_identity_live(user_id, organization_id),
    )


def account_scope_condition(
    user_id: UUID, organization_id: UUID
) -> ColumnElement[bool]:
    """``Collection`` is in the account: live, not archived, in an account
    workspace. The workspace subquery never correlates (``correlate(None)``),
    so a statement that also joins ``Workspace`` (``_library_folders``) cannot
    turn it into a test of its own row."""
    workspaces = (
        select(Workspace.id)
        .where(account_workspace_condition(user_id, organization_id))
        .correlate(None)
    )
    return and_(
        Collection.is_deleted.is_(False),
        Collection.research_status != "archived",
        Collection.workspace_id.in_(workspaces),
    )


async def authorized_account(
    db: AsyncSession, user_id: UUID, organization_id: UUID
) -> None:
    """The account binding's own check: the user is active in the live
    organization. What the account covers is account_scope_condition."""
    found = await db.scalar(
        select(User.id).where(
            User.id == user_id, _account_identity_live(user_id, organization_id)
        )
    )
    if found is None:
        raise IntegrationAccessDenied()


async def authorized_account_workspace(
    db: AsyncSession, user_id: UUID, organization_id: UUID, workspace_id: UUID
) -> Row[Any]:
    """``(id, name)`` of one workspace in the account, else a denial."""
    row = (
        await db.execute(
            select(Workspace.id, Workspace.name).where(
                Workspace.id == workspace_id,
                account_workspace_condition(user_id, organization_id),
            )
        )
    ).first()
    if row is None:
        raise IntegrationAccessDenied()
    return row


async def account_summary(
    db: AsyncSession, user_id: UUID, organization_id: UUID
) -> AccountSummaryDTO:
    """How many workspaces and projects the account covers right now."""
    workspaces = await db.scalar(
        select(func.count())
        .select_from(Workspace)
        .where(account_workspace_condition(user_id, organization_id))
    )
    projects = await db.scalar(
        select(func.count())
        .select_from(Collection)
        .where(account_scope_condition(user_id, organization_id))
    )
    return AccountSummaryDTO(
        workspace_count=int(workspaces or 0), project_count=int(projects or 0)
    )
```

`authorized_account` and `authorized_account_workspace` are defined below `IntegrationAccessDenied` (L41), so the raise resolves.

**Step 4: Run (expect pass, both dialects)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_account_scope.py backend/tests/unit/architecture/test_integration_boundaries.py
ORCHESTRATION_TEST_DATABASE_URL=$PG $PYTEST backend/tests/unit/services/integrations/test_account_scope.py
```
Expected: the SQLite half passes (21 tests) plus the architecture suite. With the URL, 42 passed and 0 skipped. Then run the mutation table above.

**Step 5: Commit**

```sh
git add backend/src/services/integrations/context.py backend/tests/unit/services/integrations/test_account_scope.py backend/tests/unit/architecture/test_integration_boundaries.py
git commit -m "feat(integrations): the account rule as a per-statement SQL condition"
```

---

### Task 6: Scopes, binding validation and scope filter by kind

**Files**
- Modify: `backend/src/services/integrations/context.py`:
  - `_PROJECT_ONLY_SCOPES` neighbourhood and `check_scopes` (L81-111)
  - `authorized_scope_filter` (L268-291)
  - `validate_binding` (L322-341)
  - import `derive_binding_kind` from the schemas module
- Modify: `backend/tests/unit/services/integrations/test_account_connections.py` (append the database fixture and tests)

**Step 1: Write the failing test.** Append to `test_account_connections.py`. Add these imports at the top: `os`, `AsyncIterator`, `SimpleNamespace`, `Any`, `UUID`; `insert`, `select`, `text`, `update` from sqlalchemy; `AsyncSession`, `async_sessionmaker`, `create_async_engine`; models `Base`, `BridgeDevice`, `WorkspaceBinding`, `Collection`, `IntegrationGrant`, `IntegrationGrantRequest`, `Organization`, `User`, `Workspace`, `WorkspaceMember`, `WorkspaceRole`; and `from src.services.integrations import context as service`.

```python
# --- database: one account, a device, and what lies outside the account -----

ORG, OTHER_ORG = uuid4(), uuid4()
USER, COLLEAGUE, OUTSIDER = uuid4(), uuid4(), uuid4()
OWNED, MEMBER_WS, FOREIGN_WS = uuid4(), uuid4(), uuid4()
# P_FOREIGN: USER is a live member of another organization's workspace.
# authorized_project admits it; the account does not.
P_OWNED, P_OWNED_2, P_MEMBER, P_FOREIGN = (uuid4() for _ in range(4))
DEVICE, OTHER_DEVICE = uuid4(), uuid4()
OWNER = SimpleNamespace(id=USER, organization_id=ORG)
ACCOUNT_RUN = frozenset(
    {"harness:execute", "tools:read", "tools:write", "library:read", "library:write"}
)
_TABLES: list[Any] = [
    Organization,
    User,
    Workspace,
    WorkspaceMember,
    Collection,
    BridgeDevice,
    WorkspaceBinding,
    IntegrationGrantRequest,
    IntegrationGrant,
]


async def _seed_account(db: AsyncSession) -> None:
    for org in (ORG, OTHER_ORG):
        await db.execute(
            insert(Organization).values(id=org, name=str(org), storage_limit_bytes=1)
        )
    for user, org in ((USER, ORG), (COLLEAGUE, ORG), (OUTSIDER, OTHER_ORG)):
        await db.execute(
            text(
                "INSERT INTO users (id, organization_id, email, password_hash, "
                "first_name, last_name, role, is_active, login_count, created_at, "
                "updated_at, is_deleted) VALUES (:id, :org, :email, 'unused', 'T', "
                "'U', 'USER', :active, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                ":deleted)"
            ),
            {
                "id": str(user),
                "org": str(org),
                "email": f"{user}@example.test",
                "active": True,
                "deleted": False,
            },
        )
    for workspace, owner, org, name in (
        (OWNED, USER, ORG, "Owned"),
        (MEMBER_WS, COLLEAGUE, ORG, "Shared"),
        (FOREIGN_WS, OUTSIDER, OTHER_ORG, "Foreign"),
    ):
        await db.execute(
            insert(Workspace).values(
                id=workspace, name=name, owner_id=owner, organization_id=org
            )
        )
    for workspace in (MEMBER_WS, FOREIGN_WS):
        await db.execute(
            insert(WorkspaceMember).values(
                id=uuid4(),
                workspace_id=workspace,
                user_id=USER,
                role=WorkspaceRole.EDITOR,
            )
        )
    for project, workspace, name in (
        (P_OWNED, OWNED, "Thesis"),
        (P_OWNED_2, OWNED, "Grant"),
        (P_MEMBER, MEMBER_WS, "Shared notes"),
        (P_FOREIGN, FOREIGN_WS, "Foreign"),
    ):
        await db.execute(
            insert(Collection).values(id=project, name=name, workspace_id=workspace)
        )
    for device, label in ((DEVICE, "Laptop"), (OTHER_DEVICE, "Desktop")):
        await db.execute(
            insert(BridgeDevice).values(
                id=device, user_id=USER, organization_id=ORG, label=label
            )
        )
    await db.commit()


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request: pytest.FixtureRequest) -> AsyncIterator[AsyncSession]:
    if request.param == "sqlite":
        dsn, schema = "sqlite+aiosqlite:///:memory:", None
    else:
        raw = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
        if not raw:
            pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured (NOT RUN)")
        dsn = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
        schema = "account_connections_" + uuid4().hex
    admin = create_async_engine(dsn) if schema else None
    if admin is not None:
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = (
        create_async_engine(dsn, connect_args={"server_settings": {"search_path": schema}})
        if schema
        else create_async_engine(dsn)
    )
    try:
        async with engine.begin() as conn:
            # Nullable chat and run associations keep their real FK targets.
            await conn.exec_driver_sql("CREATE TABLE threads (id uuid PRIMARY KEY)")
            await conn.exec_driver_sql(
                "CREATE TABLE agent_runs (job_id varchar(36) PRIMARY KEY)"
            )
            await conn.run_sync(
                Base.metadata.create_all, tables=[m.__table__ for m in _TABLES]
            )
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _seed_account(session)
            yield session
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as conn:
                await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            await admin.dispose()


def _account_ctx(**values: Any) -> IntegrationContext:
    return IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        binding_kind="account",
        grant_id=uuid4(),
        **values,
    )


# --- scopes by kind ------------------------------------------------------------


def test_an_account_may_hold_only_the_account_scopes() -> None:
    service.check_scopes(ACCOUNT_RUN, binding_kind="account")
    service.check_scopes({"tools:read", "harness:execute"}, binding_kind="account")
    for project_only in ("artifacts:publish", "context:read", "handoff:read", "handoff:write"):
        with pytest.raises(service.IntegrationAccessDenied):
            service.check_scopes({"tools:read", project_only}, binding_kind="account")
    # The scope implications still hold for an account.
    with pytest.raises(service.IntegrationAccessDenied):
        service.check_scopes({"tools:read", "library:write"}, binding_kind="account")


def test_project_and_workspace_scope_rules_are_unchanged() -> None:
    # Regression for the signature change: the old keyword still works.
    service.check_scopes({"tools:read", "artifacts:publish", "context:read"})
    service.check_scopes({"tools:read", "library:read"}, workspace_bound=True)
    with pytest.raises(service.IntegrationAccessDenied):
        service.check_scopes({"tools:read", "context:read"}, workspace_bound=True)
    with pytest.raises(service.IntegrationAccessDenied):
        service.check_scopes({"harness:execute"}, binding_kind="workspace")


# --- validate_binding: the account branch --------------------------------------


async def test_an_account_binding_needs_a_device_and_no_chat_or_run(
    db: AsyncSession,
) -> None:
    await service.validate_binding(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=None,
        device_id=DEVICE,
        binding_kind="account",
    )
    for extra in (
        {"device_id": None},
        {"device_id": DEVICE, "thread_id": uuid4()},
        {"device_id": DEVICE, "run_id": uuid4()},
        {"device_id": DEVICE, "project_id": P_OWNED},
        {"device_id": DEVICE, "workspace_id": OWNED},
        {"device_id": uuid4()},  # not the user's device
    ):
        values: dict[str, Any] = {"project_id": None, **extra}
        with pytest.raises(service.IntegrationAccessDenied):
            await service.validate_binding(
                db, user_id=USER, organization_id=ORG, binding_kind="account", **values
            )


async def test_both_null_without_the_kind_is_still_bound_to_nothing(
    db: AsyncSession,
) -> None:
    with pytest.raises(service.IntegrationAccessDenied):
        await service.validate_binding(
            db, user_id=USER, organization_id=ORG, project_id=None, device_id=DEVICE
        )


async def test_an_account_binding_ends_with_its_user(db: AsyncSession) -> None:
    await db.execute(update(User).where(User.id == USER).values(is_active=False))
    await db.commit()
    with pytest.raises(service.IntegrationAccessDenied):
        await service.validate_binding(
            db,
            user_id=USER,
            organization_id=ORG,
            project_id=None,
            device_id=DEVICE,
            binding_kind="account",
        )


# --- authorized_scope / authorized_scope_filter ----------------------------------


async def test_an_account_scope_is_the_account_rule_not_authorized_project(
    db: AsyncSession,
) -> None:
    assert await service.authorized_scope(db, _account_ctx()) == {
        P_OWNED,
        P_OWNED_2,
        P_MEMBER,
    }
    # P_FOREIGN stays reachable by a project pairing, never by the account.
    await service.authorized_project(db, USER, ORG, P_FOREIGN)


async def test_an_account_scope_is_rechecked_on_every_call(db: AsyncSession) -> None:
    await db.execute(
        update(Organization).where(Organization.id == ORG).values(is_active=False)
    )
    await db.commit()
    with pytest.raises(service.IntegrationAccessDenied):
        await service.authorized_scope_filter(db, _account_ctx())
```

**Step 2: Run it (expect failure)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_account_connections.py
```
Expected: `TypeError: check_scopes() got an unexpected keyword argument 'binding_kind'` and `validate_binding() got an unexpected keyword argument 'binding_kind'`.

Mutation check (SECURITY-CRITICAL, scopes): delete the `if kind == "account" and not held <= ACCOUNT_SCOPES:` branch. `test_an_account_may_hold_only_the_account_scopes` must fail.

**Step 3: Implement.** In `context.py`, add `derive_binding_kind` to the schema import. Replace `check_scopes` (L102-111):

```python
# An account approval spans many projects. Everything bound to one project or
# one chat (artifact publication, selected memories, chat handoffs) stays with
# project pairings; Local Codex runs get a project grant per run (PR 2).
ACCOUNT_SCOPES = frozenset(
    {"harness:execute", "tools:read", "tools:write", "library:read", "library:write"}
)


def check_scopes(
    scopes: Iterable[str],
    *,
    binding_kind: BindingKind = "project",
    workspace_bound: bool = False,
) -> None:
    """Refuse a scope set no grant of ``binding_kind`` may hold.

    ``workspace_bound=True`` is the older spelling of
    ``binding_kind="workspace"`` and is kept for its callers.
    """
    kind: BindingKind = "workspace" if workspace_bound else binding_kind
    held = set(scopes)
    if not held or not held <= STANDARD_SCOPES:
        raise IntegrationAccessDenied()
    if any(
        not needed <= held for scope, needed in _SCOPE_REQUIRES.items() if scope in held
    ):
        raise IntegrationAccessDenied()
    if kind == "workspace" and held & _PROJECT_ONLY_SCOPES:
        raise IntegrationAccessDenied()
    if kind == "account" and not held <= ACCOUNT_SCOPES:
        raise IntegrationAccessDenied()
```

Replace the head of `authorized_scope_filter` (L281, before `if context.project_id is not None:`) with:

```python
    if context.binding_kind == "account":
        await authorized_account(db, context.user_id, context.organization_id)
        return account_scope_condition(context.user_id, context.organization_id)
```
The rest of the function is unchanged, and so its docstring's last sentence is now true only for project and workspace scopes. Add: "An account condition excludes deleted and archived rows itself."

Replace the signature and first branch of `validate_binding` (L322-341):

```python
async def validate_binding(
    db: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
    project_id: UUID | None,
    workspace_id: UUID | None = None,
    thread_id: UUID | None = None,
    run_id: UUID | str | None = None,
    device_id: UUID | None = None,
    binding_kind: BindingKind | None = None,
) -> None:
    # Without a stated kind the columns decide, and both NULL is no binding:
    # only binding_kind="account" makes it the account.
    kind = (
        binding_kind
        if binding_kind is not None
        else derive_binding_kind(project_id, workspace_id)
    )
    if kind == "project" and project_id is not None and workspace_id is None:
        await authorized_project(db, user_id, organization_id, project_id)
    elif kind == "workspace" and workspace_id is not None and project_id is None:
        await authorized_workspace(db, user_id, organization_id, workspace_id)
        # A workspace has no single project for a chat or a run to belong to.
        if thread_id is not None or run_id is not None:
            raise IntegrationAccessDenied()
    elif kind == "account" and project_id is None and workspace_id is None:
        await authorized_account(db, user_id, organization_id)
        # An account approval belongs to one device and to no chat or run;
        # each run gets a project grant of its own (PR 2).
        if device_id is None or thread_id is not None or run_id is not None:
            raise IntegrationAccessDenied()
    else:
        raise IntegrationAccessDenied()
    # ... the device, thread and run checks below are unchanged ...
```

**Step 4: Run (expect pass; regression suites unchanged)**

```sh
$PYTEST backend/tests/unit/services/integrations backend/tests/unit/services/harness backend/tests/unit/services/artifacts backend/tests/unit/architecture/test_integration_boundaries.py
ORCHESTRATION_TEST_DATABASE_URL=$PG $PYTEST backend/tests/unit/services/integrations/test_account_connections.py
```
Expected: all pass, and the PostgreSQL half is not skipped.

**Step 5: Commit**

```sh
git add backend/src/services/integrations/context.py backend/tests/unit/services/integrations/test_account_connections.py
git commit -m "feat(integrations): account branch in check_scopes, validate_binding and the scope filter"
```

---

### Task 7: The flag, account requests, the approval DTO and exchange

**Files**
- Modify: `backend/src/core/config.py` (L106-108, after `NOUS_MCP_ENABLED`)
- Modify: `backend/src/schemas/integration_context.py` (`GrantRequestDTO`, L80-93)
- Modify: `backend/src/services/integrations/context.py`:
  - add `AccountConnectionsDisabled` after `IntegrationConflict` (L60-61)
  - `_new_grant` (L386-414)
  - `mint_integration_grant`'s `_new_grant` call (L482-492)
  - `create_request` (L607-634), `owned_request` (L652-661), `request_dto` (L664-703), `exchange_request` (L749-758)
  - `_replace_grant`'s `_new_grant` call (L880-890)
  - import `settings`
- Modify: `backend/src/api/integrations/grants.py` (`call`, L32-40)
- Create: `backend/tests/unit/api/test_integration_account_requests.py`
- Modify: `backend/tests/unit/services/integrations/test_account_connections.py` (append)

**Step 1: Write the failing tests.** Append to `test_account_connections.py` and add the imports `GrantRequestCreate` (already imported), `IssuedGrant`, and `from src.core.config import settings`:

```python
@pytest.fixture(autouse=True)
def account_connections_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ACCOUNT_CONNECTIONS_ENABLED", True)


async def _approve(
    db: AsyncSession,
    *,
    device: UUID = DEVICE,
    scopes: frozenset[str] = ACCOUNT_RUN,
    **binding: Any,
) -> tuple[UUID, IssuedGrant]:
    """Request, approve and exchange one consent; account unless a project or
    workspace is given."""
    data = GrantRequestCreate(
        device_id=device,
        scopes=set(scopes),
        **(binding or {"account": True}),
    )
    request = await service.create_request(db, OWNER, data)
    await service.decide_request(db, OWNER, request.id, True)
    return request.id, await service.exchange_request(db, OWNER, request.id)


async def test_an_account_request_shows_what_it_covers(db: AsyncSession) -> None:
    dto = await service.create_request(
        db,
        OWNER,
        GrantRequestCreate(account=True, device_id=DEVICE, scopes=set(ACCOUNT_RUN)),
    )
    assert dto.binding_kind == "account"
    assert (dto.project_id, dto.workspace_id, dto.thread_id) == (None, None, None)
    assert (dto.project_label, dto.workspace_label) == (None, None)
    assert dto.device_label == "Laptop"
    assert dto.account_summary is not None
    # OWNED and MEMBER_WS; FOREIGN_WS is another organization's.
    assert (dto.account_summary.workspace_count, dto.account_summary.project_count) == (
        2,
        3,
    )


async def test_a_project_request_is_unchanged_and_has_no_summary(
    db: AsyncSession,
) -> None:
    dto = await service.create_request(
        db,
        OWNER,
        GrantRequestCreate(project_id=P_OWNED, device_id=DEVICE, scopes={"tools:read"}),
    )
    assert (dto.binding_kind, dto.project_label, dto.account_summary) == (
        "project",
        "Thesis",
        None,
    )


async def test_account_requests_are_refused_while_the_flag_is_off(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ACCOUNT_CONNECTIONS_ENABLED", False)
    with pytest.raises(service.AccountConnectionsDisabled):
        await service.create_request(
            db,
            OWNER,
            GrantRequestCreate(account=True, device_id=DEVICE, scopes={"tools:read"}),
        )
    assert await db.scalar(select(IntegrationGrantRequest.id)) is None
    # Project pairings do not depend on the flag.
    await service.create_request(
        db,
        OWNER,
        GrantRequestCreate(project_id=P_OWNED, device_id=DEVICE, scopes={"tools:read"}),
    )


async def test_an_account_request_refuses_project_only_scopes(db: AsyncSession) -> None:
    with pytest.raises(service.IntegrationAccessDenied):
        await service.create_request(
            db,
            OWNER,
            GrantRequestCreate(
                account=True,
                device_id=DEVICE,
                scopes={"tools:read", "artifacts:publish"},
            ),
        )


async def test_exchange_issues_an_account_kind_pairing_grant(db: AsyncSession) -> None:
    consent, issued = await _approve(db)
    grant = await db.get(IntegrationGrant, issued.grant_id)
    assert grant is not None
    assert (grant.binding_kind, grant.project_id, grant.workspace_id) == (
        "account",
        None,
        None,
    )
    assert (grant.request_id, grant.device_id) == (consent, DEVICE)
```

Router test, new file:

```python
# backend/tests/unit/api/test_integration_account_requests.py
"""Transport for account approvals: the rollout flag answers 503."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.core.security import TokenData, get_current_user_token
from src.services.integrations.context import AccountConnectionsDisabled

pytestmark = pytest.mark.unit
USER, ORG, DEVICE = uuid4(), uuid4(), uuid4()


def test_a_disabled_account_request_is_503_with_a_stable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.integrations import grants
    from src.api.integrations.grants import router

    async def create_request(_db: Any, _user: Any, data: Any) -> Any:
        assert data.account is True
        raise AccountConnectionsDisabled()

    monkeypatch.setattr(grants.service, "create_request", create_request)

    async def fake_db() -> AsyncIterator[Any]:
        yield None

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/integrations")
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_current_user_token] = lambda: TokenData(
        user_id=str(USER), organization_id=str(ORG), is_cli=True
    )
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=USER, organization_id=ORG
    )
    response = TestClient(app).post(
        "/api/v1/integrations/grant-requests",
        json={"account": True, "device_id": str(DEVICE), "scopes": ["tools:read"]},
        headers={"Authorization": "Bearer cli-jwt"},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Account connections are disabled"}
```

**Step 2: Run them (expect failure)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_account_connections.py backend/tests/unit/api/test_integration_account_requests.py
```
Expected: `ImportError: cannot import name 'AccountConnectionsDisabled'`, and `AttributeError: ... has no attribute 'ACCOUNT_CONNECTIONS_ENABLED'`.

**Step 3: Implement**

`backend/src/core/config.py`, after L108:

```python
    # Rollout gate for account-wide harness connections: new account approval
    # requests answer 503 while false. Existing account approvals keep
    # working; revoke them under Connected devices.
    ACCOUNT_CONNECTIONS_ENABLED: bool = False
```

`context.py`: add `from src.core.config import settings`, then after `IntegrationConflict`:

```python
class AccountConnectionsDisabled(Exception):
    """ACCOUNT_CONNECTIONS_ENABLED is off: no new account approval request."""
```

`_new_grant` gains a required keyword. It goes first among the binding keywords, so every caller states it:

```python
def _new_grant(
    *,
    user_id: UUID,
    organization_id: UUID,
    binding_kind: BindingKind,
    project_id: UUID | None = None,
    workspace_id: UUID | None = None,
    scopes: Iterable[str],
    thread_id: UUID | None = None,
    run_id: UUID | str | None = None,
    device_id: UUID | None = None,
    request_id: UUID | None = None,
) -> tuple[IntegrationGrant, IssuedGrant]:
    token = "nous_ig_" + token_urlsafe(32)
    grant = IntegrationGrant(
        id=uuid4(),
        user_id=user_id,
        organization_id=organization_id,
        binding_kind=binding_kind,
        project_id=project_id,
        # ... the remaining fields are unchanged ...
    )
```

Callers. Task 9 replaces the mint call wholesale; until then use `binding_kind=derive_binding_kind(project_id, workspace_id) or "project"`, because validate_binding has already refused the None case. `_replace_grant` passes `binding_kind=cast(BindingKind, grant.binding_kind)`. `exchange_request` passes `binding_kind=cast(BindingKind, request.binding_kind)`.

`backend/src/schemas/integration_context.py`: replace `GrantRequestDTO` (L80-93):

```python
class GrantRequestDTO(BaseModel):
    id: UUID
    status: GrantRequestStatus
    expires_at: datetime
    approval_url: str
    binding_kind: BindingKind
    project_id: UUID | None
    workspace_id: UUID | None = None
    device_id: UUID
    scopes: set[str]
    project_label: str | None
    workspace_label: str | None = None
    device_label: str
    thread_id: UUID | None = None
    thread_label: str | None = None
    # Account requests only, computed each time the request is read.
    account_summary: AccountSummaryDTO | None = None
```

`create_request` (replace L607-634):

```python
async def create_request(
    db: AsyncSession, user: Any, data: GrantRequestCreate
) -> GrantRequestDTO:
    kind = data.binding_kind
    if kind == "account" and not settings.ACCOUNT_CONNECTIONS_ENABLED:
        raise AccountConnectionsDisabled()
    check_scopes(data.scopes, binding_kind=kind)
    await validate_binding(
        db,
        user_id=user.id,
        organization_id=user.organization_id,
        project_id=data.project_id,
        workspace_id=data.workspace_id,
        device_id=data.device_id,
        thread_id=data.thread_id,
        binding_kind=kind,
    )
    request = IntegrationGrantRequest(
        id=uuid4(),
        user_id=user.id,
        organization_id=user.organization_id,
        binding_kind=kind,
        project_id=data.project_id,
        workspace_id=data.workspace_id,
        device_id=data.device_id,
        thread_id=data.thread_id,
        scopes=sorted(data.scopes),
        status="pending",
        expires_at=now() + timedelta(minutes=10),
    )
    db.add(request)
    await db.commit()
    return await request_dto(db, user, request.id)
```

In `owned_request`, add `binding_kind=cast(BindingKind, request.binding_kind),` to its `validate_binding` call.

`request_dto` (replace L664-703):

```python
async def request_dto(db: AsyncSession, user: Any, request_id: UUID) -> GrantRequestDTO:
    request = await owned_request(db, user, request_id)
    project_label = workspace_label = None
    summary: AccountSummaryDTO | None = None
    if request.binding_kind == "account":
        summary = await account_summary(db, user.id, user.organization_id)
    elif request.project_id is not None:
        project = await authorized_project(
            db, user.id, user.organization_id, request.project_id
        )
        project_label = project.name
    elif request.workspace_id is not None:
        workspace = await authorized_workspace(
            db, user.id, user.organization_id, request.workspace_id
        )
        workspace_label = workspace.name
    device = await owned_device(db, request.device_id, user.id, user.organization_id)
    status = request.status
    if status in {"pending", "approved"} and not live(request.expires_at):
        status = "expired"
    thread_label = None
    if request.thread_id is not None:
        title = await db.scalar(
            select(Thread.title).where(
                Thread.id == request.thread_id, Thread.is_deleted.is_(False)
            )
        )
        thread_label = title or "Untitled chat"
    return GrantRequestDTO(
        id=request.id,
        status=cast(GrantRequestStatus, status),
        expires_at=request.expires_at,
        approval_url=f"/integrations/approve?request_id={request.id}",
        binding_kind=cast(BindingKind, request.binding_kind),
        project_id=request.project_id,
        workspace_id=request.workspace_id,
        device_id=request.device_id,
        scopes=set(request.scopes),
        project_label=project_label,
        workspace_label=workspace_label,
        device_label=device.label,
        thread_id=request.thread_id,
        thread_label=thread_label,
        account_summary=summary,
    )
```

`backend/src/api/integrations/grants.py` `call` (L32-40): add one branch before the generic denial. The router stays transport-only.

```python
async def call(operation: Awaitable[T]) -> T:
    try:
        return await operation
    except service.AccountConnectionsDisabled as error:
        raise HTTPException(503, "Account connections are disabled") from error
    except service.IntegrationAccessDenied as error:
        raise HTTPException(403, "Integration access denied") from error
    except service.IntegrationConflict as error:
        raise HTTPException(
            409, "Integration request is no longer available"
        ) from error
```

**Step 4: Run (expect pass)**

```sh
$PYTEST backend/tests/unit/services/integrations backend/tests/unit/api/test_integration_account_requests.py backend/tests/unit/api/test_integration_grant_resume.py backend/tests/unit/architecture/test_integration_boundaries.py
ORCHESTRATION_TEST_DATABASE_URL=$PG $PYTEST backend/tests/unit/services/integrations/test_account_connections.py backend/tests/unit/services/integrations/test_grant_resume_postgres.py
```
Expected: all pass, and the PostgreSQL tests are not skipped.

**Step 5: Commit**

```sh
git add backend/src/core/config.py backend/src/schemas/integration_context.py backend/src/services/integrations/context.py backend/src/api/integrations/grants.py backend/tests/unit/services/integrations/test_account_connections.py backend/tests/unit/api/test_integration_account_requests.py
git commit -m "feat(integrations): account approval requests behind ACCOUNT_CONNECTIONS_ENABLED"
```

---

### Task 8: Grant validation across kinds (SECURITY-CRITICAL)

The two rules:
- An account-kind grant needs an account consent of the same device.
- A project-kind grant whose consent is an account consent is valid only while its project is in the account.

`renew_grant`, `_replace_grant` (Task 7) and `resume_grant` carry the kind.

**Files**
- Modify: `backend/src/services/integrations/context.py`:
  - add `_consent_admits` before `mint_integration_grant` (before L417)
  - `_validate_grant` (L498-538)
  - `resolve_integration_context` (L562-572)
  - `resume_grant` (L817-843)
- Modify: `backend/tests/unit/services/integrations/test_account_connections.py` (append)

**Step 1: Write the failing tests.** Add the imports `from datetime import datetime, timedelta, timezone`, `from hashlib import sha256`, `from secrets import token_urlsafe` and `delete` (sqlalchemy).

```python
SOON = datetime.now(timezone.utc) + timedelta(hours=1)


async def _store_grant(
    db: AsyncSession,
    *,
    request_id: UUID,
    binding_kind: str,
    project_id: UUID | None = None,
    device: UUID = DEVICE,
    scopes: frozenset[str] = ACCOUNT_RUN,
) -> str:
    """A live grant row with a known token, as a mint or exchange stores it."""
    token = "nous_ig_" + token_urlsafe(32)
    db.add(
        IntegrationGrant(
            id=uuid4(),
            user_id=USER,
            organization_id=ORG,
            binding_kind=binding_kind,
            project_id=project_id,
            device_id=device,
            request_id=request_id,
            scopes=sorted(scopes),
            token_hash=sha256(token.encode()).hexdigest(),
            expires_at=SOON,
            consented_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return token


async def _revoke(db: AsyncSession, consent: UUID) -> None:
    await db.execute(
        update(IntegrationGrantRequest)
        .where(IntegrationGrantRequest.id == consent)
        .values(consent_revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()


async def test_an_account_grant_resolves_to_an_account_context(
    db: AsyncSession,
) -> None:
    consent, issued = await _approve(db)
    ctx = await service.resolve_integration_context(
        db, issued.token, required_scope="tools:read"
    )
    assert (ctx.binding_kind, ctx.project_id, ctx.workspace_id) == ("account", None, None)
    assert ctx.consent_id == consent and ctx.scopes == ACCOUNT_RUN
    await _revoke(db, consent)
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resolve_integration_context(
            db, issued.token, required_scope="tools:read"
        )


# SECURITY-CRITICAL: approval/grant kind matching.
@pytest.mark.parametrize("case", ["project-consent", "other-device-account-consent"])
async def test_an_account_grant_needs_an_account_consent_of_its_device(
    db: AsyncSession, case: str
) -> None:
    if case == "project-consent":
        consent, _ = await _approve(
            db, project_id=P_OWNED, scopes=frozenset({"harness:execute", "tools:read"})
        )
    else:
        consent, _ = await _approve(db, device=OTHER_DEVICE)
    # tools:read alone, so only the consent's kind (or device) can refuse it.
    token = await _store_grant(
        db,
        request_id=consent,
        binding_kind="account",
        scopes=frozenset({"tools:read"}),
    )
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resolve_integration_context(db, token, required_scope="tools:read")


# SECURITY-CRITICAL: a project grant under an account consent.
@pytest.mark.parametrize(
    "leaves", ["archived-workspace", "archived-project", "membership-ended"]
)
async def test_a_project_grant_under_an_account_consent_ends_when_its_project_leaves(
    db: AsyncSession, leaves: str
) -> None:
    consent, _ = await _approve(db)
    token = await _store_grant(
        db,
        request_id=consent,
        binding_kind="project",
        project_id=P_MEMBER,
        scopes=frozenset({"harness:execute"}),
    )
    ctx = await service.resolve_integration_context(
        db, token, required_scope="harness:execute"
    )
    assert (ctx.binding_kind, ctx.project_id, ctx.consent_id) == (
        "project",
        P_MEMBER,
        consent,
    )
    change = {
        "archived-workspace": update(Workspace)
        .where(Workspace.id == MEMBER_WS)
        .values(is_archived=True),
        "archived-project": update(Collection)
        .where(Collection.id == P_MEMBER)
        .values(research_status="archived"),
        "membership-ended": update(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == MEMBER_WS)
        .values(is_deleted=True),
    }[leaves]
    await db.execute(change)
    await db.commit()
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resolve_integration_context(
            db, token, required_scope="harness:execute"
        )


async def test_an_account_consent_never_covers_another_organizations_project(
    db: AsyncSession,
) -> None:
    consent, _ = await _approve(db)
    await service.authorized_project(db, USER, ORG, P_FOREIGN)  # a pairing could
    token = await _store_grant(
        db,
        request_id=consent,
        binding_kind="project",
        project_id=P_FOREIGN,
        scopes=frozenset({"harness:execute"}),
    )
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resolve_integration_context(
            db, token, required_scope="harness:execute"
        )


async def test_a_project_grant_still_needs_its_own_project_consent(
    db: AsyncSession,
) -> None:
    consent, _ = await _approve(
        db, project_id=P_OWNED, scopes=frozenset({"harness:execute", "tools:read"})
    )
    token = await _store_grant(
        db,
        request_id=consent,
        binding_kind="project",
        project_id=P_OWNED_2,
        scopes=frozenset({"tools:read"}),
    )
    with pytest.raises(service.IntegrationAccessDenied):
        await service.resolve_integration_context(db, token, required_scope="tools:read")


async def test_renewal_and_resume_keep_the_account_kind(db: AsyncSession) -> None:
    consent, issued = await _approve(db)
    renewed = await service.renew_grant(db, OWNER, issued.grant_id)
    stored = await db.get(IntegrationGrant, renewed.grant_id, populate_existing=True)
    assert stored is not None and stored.binding_kind == "account"
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.id == renewed.grant_id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    await db.commit()
    resumed = await service.resume_grant(db, OWNER, renewed.grant_id, renewed.token)
    ctx = await service.resolve_integration_context(
        db, resumed.token, required_scope="tools:read"
    )
    assert (ctx.binding_kind, ctx.consent_id) == ("account", consent)
```

**Step 2: Run (expect failure)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_account_connections.py -k "grant or renewal or consent"
```
Expected: `test_an_account_grant_resolves_to_an_account_context` fails with `IntegrationAccessDenied`. The consent lookup still compares `project_id`/`workspace_id`, and `validate_binding` gets no kind, so an account grant reads as "bound to nothing". The project-under-account tests fail the same way at the first resolve.

**Mutation checks** (after Step 3):

| Break | Must fail |
| --- | --- |
| In `_consent_admits`'s project branch, replace `exists().where(Collection.id == project_id, account_scope_condition(...))` with `true()` | `test_a_project_grant_under_an_account_consent_ends_when_its_project_leaves[archived-workspace]` and `[archived-project]` (`authorized_project` ignores archiving), `test_an_account_consent_never_covers_another_organizations_project`. `[membership-ended]` still passes because `authorized_project` refuses it too. |
| `_consent_admits`: make the account branch `return true()` | `test_an_account_grant_needs_an_account_consent_of_its_device[project-consent]` |
| In `_validate_grant`, delete `IntegrationGrantRequest.device_id == grant.device_id,` | `test_an_account_grant_needs_an_account_consent_of_its_device[other-device-account-consent]` |
| In `_consent_admits`'s project branch, drop `IntegrationGrantRequest.project_id == project_id` | `test_a_project_grant_still_needs_its_own_project_consent` |

**Step 3: Implement.** The code needs only `false`, which is already imported.

```python
def _consent_admits(
    kind: str,
    project_id: UUID | None,
    workspace_id: UUID | None,
    user_id: UUID,
    organization_id: UUID,
) -> ColumnElement[bool]:
    """The consent requests that may stand behind a grant with this binding.

    account: an account consent. workspace: a consent of that workspace.
    project: a consent of that project, or an account consent while the
    project is in the account. That is evaluated in the same statement, so a
    project that leaves the account ends every grant under the account consent
    at its next use.
    """
    if kind == "account":
        return IntegrationGrantRequest.binding_kind == "account"
    if kind == "workspace" and workspace_id is not None:
        return and_(
            IntegrationGrantRequest.binding_kind == "workspace",
            IntegrationGrantRequest.workspace_id == workspace_id,
        )
    if kind == "project" and project_id is not None:
        return or_(
            and_(
                IntegrationGrantRequest.binding_kind == "project",
                IntegrationGrantRequest.project_id == project_id,
            ),
            and_(
                IntegrationGrantRequest.binding_kind == "account",
                exists().where(
                    Collection.id == project_id,
                    account_scope_condition(user_id, organization_id),
                ),
            ),
        )
    return false()
```

`_validate_grant` (replace L498-538):

```python
async def _validate_grant(
    db: AsyncSession, grant: IntegrationGrant | None
) -> IntegrationGrant:
    if (
        grant is None
        or grant.is_deleted
        or grant.revoked_at is not None
        or not live(grant.expires_at)
    ):
        raise IntegrationAccessDenied()
    kind = cast(BindingKind, grant.binding_kind)
    check_scopes(grant.scopes, binding_kind=kind)
    # An account grant exists only as one device's pairing, from its consent.
    if kind == "account" and grant.request_id is None:
        raise IntegrationAccessDenied()
    await validate_binding(
        db,
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
        workspace_id=grant.workspace_id,
        thread_id=grant.thread_id,
        run_id=grant.run_id,
        device_id=grant.device_id,
        binding_kind=kind,
    )
    if grant.device_id is not None:
        consent = await db.scalar(
            select(IntegrationGrantRequest)
            .where(
                IntegrationGrantRequest.id == grant.request_id,
                IntegrationGrantRequest.status == "consumed",
                IntegrationGrantRequest.user_id == grant.user_id,
                IntegrationGrantRequest.organization_id == grant.organization_id,
                _consent_admits(
                    kind,
                    grant.project_id,
                    grant.workspace_id,
                    grant.user_id,
                    grant.organization_id,
                ),
                IntegrationGrantRequest.device_id == grant.device_id,
                IntegrationGrantRequest.consent_revoked_at.is_(None),
                IntegrationGrantRequest.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
        if consent is None or not set(grant.scopes) <= set(consent.scopes):
            raise IntegrationAccessDenied()

    return grant
```

`resolve_integration_context`: add `binding_kind=cast(BindingKind, grant.binding_kind),` to the returned `IntegrationContext`.

`resume_grant`:
- replace `check_scopes(grant.scopes, workspace_bound=grant.workspace_id is not None)` with `kind = cast(BindingKind, grant.binding_kind)` and `check_scopes(grant.scopes, binding_kind=kind)`;
- add `binding_kind=kind,` to its `validate_binding` call;
- in its consent query replace the two lines `IntegrationGrantRequest.project_id == grant.project_id,` and `IntegrationGrantRequest.workspace_id == grant.workspace_id,` with:

```python
            _consent_admits(
                kind,
                grant.project_id,
                grant.workspace_id,
                grant.user_id,
                grant.organization_id,
            ),
```
The `thread_id == grant.thread_id` line stays. A run grant under an account consent carries a chat, and its token is never stored ("intentionally discarded" in `harness_streaming.create_chat_context`), so it cannot be resumed anyway.

**Step 4: Run (expect pass; #1987's suites unchanged)**

```sh
$PYTEST backend/tests/unit/services/integrations backend/tests/unit/api/test_integration_grant_resume.py backend/tests/unit/services/harness
ORCHESTRATION_TEST_DATABASE_URL=$PG $PYTEST backend/tests/unit/services/integrations/test_account_connections.py backend/tests/unit/services/integrations/test_grant_resume_postgres.py
```
Expected: all pass, PostgreSQL not skipped. Then apply the mutation table.

**Step 5: Commit**

```sh
git add backend/src/services/integrations/context.py backend/tests/unit/services/integrations/test_account_connections.py
git commit -m "feat(integrations): grant validation, renewal and resume across binding kinds"
```

---

### Task 9: A run's project grant under an account approval, and consent precedence (SECURITY-CRITICAL)

PR 2 calls `mint_integration_grant` unchanged from `create_chat_context`. This task makes it find the account consent.

**Files**
- Modify: `backend/src/services/integrations/context.py` (`mint_integration_grant`, L417-495; add `_single_lineage` above it)
- Modify: `backend/tests/unit/services/integrations/test_account_connections.py` (append)

**Step 1: Write the failing tests**

```python
RUN = frozenset({"harness:execute"})


async def _mint(db: AsyncSession, project: UUID) -> Any:
    issued = await service.mint_integration_grant(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=project,
        scopes=RUN,
        device_id=DEVICE,
    )
    return await db.get(IntegrationGrant, issued.grant_id, populate_existing=True)


async def test_a_run_grant_is_minted_under_the_account_consent(db: AsyncSession) -> None:
    consent, _ = await _approve(db)
    grant = await _mint(db, P_MEMBER)
    assert (grant.binding_kind, grant.project_id, grant.request_id) == (
        "project",
        P_MEMBER,
        consent,
    )


async def test_no_run_grant_outside_the_account(db: AsyncSession) -> None:
    await _approve(db)
    with pytest.raises(service.IntegrationAccessDenied):
        await _mint(db, P_FOREIGN)


# SECURITY-CRITICAL: consent precedence (the PR 3 planner's question).
async def test_the_account_consent_wins_over_a_live_project_consent(
    db: AsyncSession,
) -> None:
    await _approve(db, project_id=P_OWNED, scopes=frozenset({"harness:execute", "tools:read"}))
    account, _ = await _approve(db)
    grant = await _mint(db, P_OWNED)
    assert grant.request_id == account


async def test_two_account_consents_are_ambiguous(db: AsyncSession) -> None:
    await _approve(db)
    await _approve(db)
    with pytest.raises(service.IntegrationAccessDenied):
        await _mint(db, P_OWNED)


async def test_project_consents_alone_keep_the_old_rule(db: AsyncSession) -> None:
    scopes = frozenset({"harness:execute", "tools:read"})
    first, _ = await _approve(db, project_id=P_OWNED, scopes=scopes)
    assert (await _mint(db, P_OWNED)).request_id == first
    await _approve(db, project_id=P_OWNED, scopes=scopes)
    with pytest.raises(service.IntegrationAccessDenied):
        await _mint(db, P_OWNED)


async def test_an_account_consent_without_the_run_scope_mints_nothing(
    db: AsyncSession,
) -> None:
    await _approve(db, scopes=frozenset({"tools:read"}))
    with pytest.raises(service.IntegrationAccessDenied):
        await _mint(db, P_OWNED)


@pytest.mark.parametrize(
    ("consent_kind", "expected"), [("account", "account"), ("project", "project")]
)
async def test_consent_kind_picks_the_lineage_the_caller_names(
    db: AsyncSession, consent_kind: str, expected: str
) -> None:
    # Two live consents on one device only arise through the API (a full
    # bridge pairing registers a new device), so the API makes them here.
    project, _ = await _approve(
        db, project_id=P_OWNED, scopes=frozenset({"harness:execute", "tools:read"})
    )
    account, _ = await _approve(db)
    issued = await service.mint_integration_grant(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=P_OWNED,
        scopes=RUN,
        device_id=DEVICE,
        consent_kind=consent_kind,  # type: ignore[arg-type]
    )
    grant = await db.get(IntegrationGrant, issued.grant_id, populate_existing=True)
    assert grant.request_id == {"account": account, "project": project}[expected]


async def test_consent_kind_account_still_fails_closed_on_two_account_consents(
    db: AsyncSession,
) -> None:
    await _approve(db)
    await _approve(db)  # a second account request on one device is allowed
    with pytest.raises(service.IntegrationAccessDenied):
        await service.mint_integration_grant(
            db,
            user_id=USER,
            organization_id=ORG,
            project_id=P_OWNED,
            scopes=RUN,
            device_id=DEVICE,
            consent_kind="account",
        )


async def test_the_mint_never_issues_an_account_grant(db: AsyncSession) -> None:
    await _approve(db)
    with pytest.raises(service.IntegrationAccessDenied):
        await service.mint_integration_grant(
            db, user_id=USER, organization_id=ORG, scopes=RUN, device_id=DEVICE
        )
```

**Step 2: Run (expect failure)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_account_connections.py -k "mint or run_grant or consent_wins or ambiguous or old_rule or run_scope or consent_kind"
```
Expected: the `consent_kind` tests fail with `TypeError: unexpected keyword argument 'consent_kind'`. `test_a_run_grant_is_minted_under_the_account_consent` and `test_the_account_consent_wins_over_a_live_project_consent` fail with `IntegrationAccessDenied`. The mint's consent filter compares `project_id`, so an account consent never matches. The regression tests already pass.

**Mutation checks** (after Step 3):

| Break (in `_single_lineage`) | Must fail |
| --- | --- |
| delete the `if accounts:` block | `test_the_account_consent_wins_over_a_live_project_consent` (two matches, refused) |
| replace the body with `return matching[0] if matching else None` | `test_two_account_consents_are_ambiguous`, `test_project_consents_alone_keep_the_old_rule`, `test_consent_kind_account_still_fails_closed_on_two_account_consents` |
| in `mint_integration_grant`, drop the `consent_kind` filter | `test_consent_kind_picks_the_lineage_the_caller_names[project]` (the account consent wins instead) |

**Step 3: Implement**

```python
def _single_lineage(
    matching: list[IntegrationGrantRequest],
) -> IntegrationGrantRequest | None:
    """The one consent a device's grant may be minted under, else None.

    An account consent wins over project consents. ``nous --rc`` replaces a
    project pairing with an account pairing and leaves the project consent
    live until the user revokes it; the account lineage is the one the running
    bridge holds and renews. Two account consents, or no account consent and
    more than one project consent, are ambiguous: fail closed. Picking the
    first row would be nondeterministic and could mint authority the paired
    bridge cannot renew.
    """
    accounts = [consent for consent in matching if consent.binding_kind == "account"]
    if accounts:
        return accounts[0] if len(accounts) == 1 else None
    return matching[0] if len(matching) == 1 else None


async def mint_integration_grant(
    db: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
    project_id: UUID | None = None,
    workspace_id: UUID | None = None,
    scopes: frozenset[str],
    thread_id: UUID | None = None,
    run_id: UUID | None = None,
    device_id: UUID | None = None,
    consent_kind: BindingKind | None = None,
) -> IssuedGrant:
    """Trusted internal API. External issuance must use the consent exchange.

    ``consent_kind`` (PR 2's run path always passes it) restricts the device's
    consents to that kind. Among them exactly one must match. Without it, an
    account consent wins over project consents (_single_lineage).

    Mints a project or workspace grant, never an account grant: that exists
    only as a device's pairing (exchange_request). With a device, the grant's
    consent is the device's one live consent that admits the binding
    (_consent_admits, _single_lineage). For a project that is a consent of the
    project, or an account consent while the project is in the account.
    """
    kind = derive_binding_kind(project_id, workspace_id)
    if kind is None:
        raise IntegrationAccessDenied()
    check_scopes(scopes, binding_kind=kind)
    await validate_binding(
        db,
        user_id=user_id,
        organization_id=organization_id,
        project_id=project_id,
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        device_id=device_id,
        binding_kind=kind,
    )
    request_id = None
    if device_id is not None:
        consents = (
            await db.scalars(
                select(IntegrationGrantRequest)
                .where(
                    IntegrationGrantRequest.user_id == user_id,
                    IntegrationGrantRequest.organization_id == organization_id,
                    _consent_admits(
                        kind, project_id, workspace_id, user_id, organization_id
                    ),
                    IntegrationGrantRequest.device_id == device_id,
                    IntegrationGrantRequest.status == "consumed",
                    IntegrationGrantRequest.consent_revoked_at.is_(None),
                    IntegrationGrantRequest.is_deleted.is_(False),
                    *(
                        ()
                        if consent_kind is None
                        else (IntegrationGrantRequest.binding_kind == consent_kind,)
                    ),
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        # A chat-bound consent authorizes only its chat; a project-wide or
        # account consent (thread_id NULL) authorizes any chat it covers.
        matching_consents = [
            consent
            for consent in consents
            if scopes <= set(consent.scopes) and consent.thread_id in (None, thread_id)
        ]
        chosen = _single_lineage(list(matching_consents))
        if chosen is None:
            # Name the one cause the owner can fix from the chat: a consent
            # with enough scopes exists but is bound to another chat. No
            # consent, too few scopes or an ambiguous lineage stay opaque.
            if not matching_consents and any(
                scopes <= set(consent.scopes)
                and consent.thread_id not in (None, thread_id)
                for consent in consents
            ):
                raise DeviceBoundToAnotherChat()
            raise IntegrationAccessDenied()
        request_id = chosen.id
    grant, issued = _new_grant(
        user_id=user_id,
        organization_id=organization_id,
        binding_kind=kind,
        project_id=project_id,
        workspace_id=workspace_id,
        scopes=scopes,
        thread_id=thread_id,
        run_id=run_id,
        device_id=device_id,
        request_id=request_id,
    )
    db.add(grant)
    await db.commit()
    return issued
```

**Step 4: Run (expect pass; the chat-binding suite proves `DeviceBoundToAnotherChat` is unchanged)**

```sh
$PYTEST backend/tests/unit/services/integrations backend/tests/unit/api/agent/test_harness_streaming.py backend/tests/unit/services/harness
ORCHESTRATION_TEST_DATABASE_URL=$PG $PYTEST backend/tests/unit/services/integrations/test_account_connections.py
```
Expected: all pass. Then apply the mutation table.

**Step 5: Commit**

```sh
git add backend/src/services/integrations/context.py backend/tests/unit/services/integrations/test_account_connections.py
git commit -m "feat(integrations): mint run grants under an account approval; account consent wins"
```

---

### Task 10: Devices and folders: `list_devices`, `bind_workspace`, `list_workspaces`

**Files**
- Modify: `backend/src/schemas/integration_context.py` (`DeviceListItemDTO` L111-118, `WorkspaceBindingCreate` L121-123)
- Modify: `backend/src/services/integrations/context.py`:
  - `list_devices` (L910-955)
  - `bind_workspace` (L958-981)
  - `list_workspaces` (L984-1005)
  - add `_holds_account_run_consent` above `list_devices`
- Modify: `backend/tests/unit/services/integrations/test_account_connections.py` (append)

**Step 1: Write the failing tests.** Add imports `WorkspaceBindingCreate` and `DeviceListItemDTO` (schemas).

```python
def test_an_omitted_folder_project_is_an_account_folder() -> None:
    assert WorkspaceBindingCreate(label="repo", workspace_id=uuid4()).project_id is None
    assert (
        WorkspaceBindingCreate(label="repo", workspace_id=uuid4(), project_id=None).project_id
        is None
    )


async def _consent_row(
    db: AsyncSession,
    *,
    device: UUID,
    binding: dict[str, Any],
    scopes: list[str],
    revoked: bool = False,
    thread: UUID | None = None,
) -> UUID:
    """A consumed consent written directly, for shapes create_request refuses
    to make here (a chat-bound one needs a real chat)."""
    consent = uuid4()
    if thread is not None:
        await db.execute(text("INSERT INTO threads (id) VALUES (:id)"), {"id": str(thread)})
    await db.execute(
        insert(IntegrationGrantRequest).values(
            id=consent,
            user_id=USER,
            organization_id=ORG,
            device_id=device,
            scopes=scopes,
            status="consumed",
            expires_at=SOON,
            thread_id=thread,
            consent_revoked_at=datetime.now(timezone.utc) if revoked else None,
            **binding,
        )
    )
    await db.commit()
    return consent


async def test_list_devices_reports_account_and_project_run_consents(
    db: AsyncSession,
) -> None:
    run = ["harness:execute", "tools:read"]
    await _consent_row(db, device=DEVICE, binding={"binding_kind": "account"}, scopes=run)
    await _consent_row(db, device=OTHER_DEVICE, binding={"project_id": P_OWNED}, scopes=run)
    chat = uuid4()
    await _consent_row(
        db, device=OTHER_DEVICE, binding={"project_id": P_OWNED_2}, scopes=run, thread=chat
    )
    # Ignored: no harness:execute, and a revoked account consent.
    await _consent_row(
        db, device=OTHER_DEVICE, binding={"project_id": P_MEMBER}, scopes=["tools:read"]
    )
    await _consent_row(
        db,
        device=OTHER_DEVICE,
        binding={"binding_kind": "account"},
        scopes=run,
        revoked=True,
    )
    listed = {device.id: device for device in await service.list_devices(db, OWNER)}
    assert (listed[DEVICE].account_wide, listed[DEVICE].project_ids) == (True, [])
    assert listed[DEVICE].bound_thread_ids == []
    assert listed[OTHER_DEVICE].account_wide is False
    assert listed[OTHER_DEVICE].project_ids == sorted([P_OWNED, P_OWNED_2], key=str)
    # A project-wide run consent beside the chat-bound one: not restricted.
    assert listed[OTHER_DEVICE].bound_thread_ids == []


# SECURITY-CRITICAL: an account folder needs a live account approval.
async def test_an_account_folder_needs_a_live_account_run_consent(
    db: AsyncSession,
) -> None:
    folder = WorkspaceBindingCreate(label="repo", workspace_id=uuid4(), project_id=None)
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(db, OWNER, DEVICE, folder)
    await _approve(db, scopes=frozenset({"tools:read"}))  # cannot run Codex
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(db, OWNER, DEVICE, folder)
    await _approve(db, device=OTHER_DEVICE)  # another computer's approval
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(db, OWNER, DEVICE, folder)
    consent, _ = await _approve(db)
    bound = await service.bind_workspace(db, OWNER, DEVICE, folder)
    assert bound.project_id is None
    assert (await service.bind_workspace(db, OWNER, DEVICE, folder)).id == bound.id
    await _revoke(db, consent)
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(
            db,
            OWNER,
            DEVICE,
            WorkspaceBindingCreate(label="other", workspace_id=uuid4(), project_id=None),
        )


async def test_list_workspaces_keeps_account_folders_while_the_approval_lives(
    db: AsyncSession,
) -> None:
    consent, _ = await _approve(db)
    account_folder = await service.bind_workspace(
        db,
        OWNER,
        DEVICE,
        WorkspaceBindingCreate(label="any", workspace_id=uuid4(), project_id=None),
    )
    project_folder = await service.bind_workspace(
        db,
        OWNER,
        DEVICE,
        WorkspaceBindingCreate(label="thesis", workspace_id=uuid4(), project_id=P_OWNED),
    )
    listed = {binding.id for binding in await service.list_workspaces(db, OWNER, DEVICE)}
    assert listed == {account_folder.id, project_folder.id}
    await _revoke(db, consent)
    listed = {binding.id for binding in await service.list_workspaces(db, OWNER, DEVICE)}
    assert listed == {project_folder.id}


async def test_a_folder_keeps_its_kind_and_a_project_folder_its_project(
    db: AsyncSession,
) -> None:
    await _approve(db)
    workspace_id = uuid4()
    await service.bind_workspace(
        db,
        OWNER,
        DEVICE,
        WorkspaceBindingCreate(label="repo", workspace_id=workspace_id, project_id=None),
    )
    with pytest.raises(service.IntegrationConflict):
        await service.bind_workspace(
            db,
            OWNER,
            DEVICE,
            WorkspaceBindingCreate(
                label="repo", workspace_id=workspace_id, project_id=P_OWNED
            ),
        )
    with pytest.raises(service.IntegrationAccessDenied):
        await service.bind_workspace(
            db,
            OWNER,
            DEVICE,
            WorkspaceBindingCreate(label="x", workspace_id=uuid4(), project_id=uuid4()),
        )
```

**Step 2: Run (expect failure)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_account_connections.py -k "folder or list_devices or list_workspaces"
```
Expected: `ValidationError` (`project_id` cannot be None) and `AttributeError: ... has no attribute 'account_wide'`.

**Mutation checks** (after Step 3):

| Break | Must fail |
| --- | --- |
| In `bind_workspace`, delete the `if not await _holds_account_run_consent(...)` refusal | `test_an_account_folder_needs_a_live_account_run_consent` |
| In `_holds_account_run_consent`, drop `IntegrationGrantRequest.device_id == device_id` | `test_an_account_folder_needs_a_live_account_run_consent` (the other device's approval) |
| In `list_workspaces`, append account folders without checking `account` | `test_list_workspaces_keeps_account_folders_while_the_approval_lives` |

**Step 3: Implement**

Schemas: replace `DeviceListItemDTO` and `WorkspaceBindingCreate`:

```python
class DeviceListItemDTO(DeviceDTO):
    """A paired computer as the chat composer lists it.

    All three lists come from the device's live run consents: consumed, not
    revoked, holding harness:execute. The mint stays the authority."""

    # Chats this computer can run Codex in when every live run consent it
    # holds is bound to a chat (Plan 06 slice 2). Empty when one of them is
    # project-wide or account-wide, or when it holds none.
    bound_thread_ids: list[UUID]
    # It holds a live account run consent: any project in the account.
    account_wide: bool
    # Projects of its live project run consents, chat-bound ones included.
    project_ids: list[UUID]


class WorkspaceBindingCreate(DeviceCreate):
    workspace_id: UUID
    # None or omitted: an account folder (needs a live account run consent
    # on the device; bind_workspace refuses it otherwise).
    project_id: UUID | None = None
```

`context.py`:

```python
async def _holds_account_run_consent(
    db: AsyncSession, user_id: UUID, organization_id: UUID, device_id: UUID
) -> bool:
    """The device holds a live account approval that can run Codex: consumed,
    not revoked, with harness:execute, while the user is active in the live
    organization."""
    try:
        await authorized_account(db, user_id, organization_id)
    except IntegrationAccessDenied:
        return False
    held = (
        await db.scalars(
            select(IntegrationGrantRequest.scopes).where(
                IntegrationGrantRequest.user_id == user_id,
                IntegrationGrantRequest.organization_id == organization_id,
                IntegrationGrantRequest.device_id == device_id,
                IntegrationGrantRequest.binding_kind == "account",
                IntegrationGrantRequest.status == "consumed",
                IntegrationGrantRequest.consent_revoked_at.is_(None),
                IntegrationGrantRequest.is_deleted.is_(False),
            )
        )
    ).all()
    return any("harness:execute" in (scopes or []) for scopes in held)
```

`list_devices`: keep the device and consent queries (L911-936). Replace the aggregation and return (L937-955):

```python
    run_chats: dict[UUID, list[UUID | None]] = {d.id: [] for d in devices}
    account_wide: dict[UUID, bool] = {d.id: False for d in devices}
    project_ids: dict[UUID, set[UUID]] = {d.id: set() for d in devices}
    for consent in consents:
        if "harness:execute" not in (consent.scopes or []):
            continue
        run_chats[consent.device_id].append(consent.thread_id)
        if consent.binding_kind == "account":
            account_wide[consent.device_id] = True
        elif consent.binding_kind == "project" and consent.project_id is not None:
            project_ids[consent.device_id].add(consent.project_id)
    return [
        DeviceListItemDTO(
            id=device.id,
            label=device.label,
            bound_thread_ids=(
                []
                if None in run_chats[device.id]
                else sorted(
                    {chat for chat in run_chats[device.id] if chat is not None},
                    key=str,
                )
            ),
            account_wide=account_wide[device.id],
            project_ids=sorted(project_ids[device.id], key=str),
        )
        for device in devices
    ]
```

`bind_workspace`: replace L961-962 (`owned_device` and `authorized_project`). The duplicate-binding block below is unchanged; it already refuses a different `project_id`, `None` included.

```python
    await owned_device(db, device_id, user.id, user.organization_id)
    if data.project_id is None:
        # An account folder belongs to no project: only a live account
        # approval of this device that can run Codex may register one.
        if not await _holds_account_run_consent(
            db, user.id, user.organization_id, device_id
        ):
            raise IntegrationAccessDenied()
    else:
        await authorized_project(db, user.id, user.organization_id, data.project_id)
```

`list_workspaces`: replace the loop (L996-1005):

```python
    account = await _holds_account_run_consent(
        db, user.id, user.organization_id, device_id
    )
    authorized = []
    for binding in bindings:
        if binding.project_id is None:
            # An account folder: listed while the account approval lives.
            if account:
                authorized.append(binding)
            continue
        try:
            await authorized_project(
                db, user.id, user.organization_id, binding.project_id
            )
        except IntegrationAccessDenied:
            continue
        authorized.append(binding)
    return authorized
```

**Step 4: Run (expect pass)**

```sh
$PYTEST backend/tests/unit/services/integrations backend/tests/unit/api/agent/test_harness_streaming.py backend/tests/unit/services/harness
ORCHESTRATION_TEST_DATABASE_URL=$PG $PYTEST backend/tests/unit/services/integrations/test_account_connections.py
```
Expected: all pass. Then apply the mutation table.

**Step 5: Commit**

```sh
git add backend/src/schemas/integration_context.py backend/src/services/integrations/context.py backend/tests/unit/services/integrations/test_account_connections.py
git commit -m "feat(integrations): account folders and account-wide devices"
```

---

### Task 11: Read tools: the account selector and `list_library` across the account

**Files**
- Modify: `backend/src/services/integrations/read_tools.py`:
  - module docstring (L1-11)
  - `_PROJECT_SELECTOR` (L132-138)
  - `_registry_schema` (L326-361)
  - `_local_schema` (L364-372)
  - `list_read_tools` (L375-398)
  - `_validate_arguments` (L425-465)
  - `_target_project` (L1429-1455)
  - `_library_folders` (L1458-1497)
  - `_list_library` (L1500-1533)
- Modify: `backend/src/api/integrations/tools.py` (`list_tools`, L26-33)
- Modify: `backend/tests/unit/services/integrations/test_read_tools.py` (append; add `WorkspaceRole` to the `src.models.workspace` import)

**Step 1: Write the failing tests** (append to `test_read_tools.py`)

```python
# --- account grants: the selector across the whole account ------------------

OUTSIDE_ORG, OUTSIDER_USER, OUTSIDE_WORKSPACE, OUTSIDE_PROJECT = (
    uuid4() for _ in range(4)
)


def _account_context(scopes: Iterable[str] = TOOLS_READ) -> IntegrationContext:
    return IntegrationContext(
        user_id=USER,
        organization_id=ORG,
        binding_kind="account",
        grant_id=uuid4(),
        scopes=frozenset(scopes),
    )


async def _outside_the_account(db: AsyncSession) -> None:
    """A live project USER may open as an explicit member of another
    organization's workspace: authorized_project admits it, the account rule
    does not."""
    await db.execute(
        insert(Organization).values(
            id=OUTSIDE_ORG, name="Outside", storage_limit_bytes=1000000
        )
    )
    await db.execute(
        text(
            "INSERT INTO users (id, organization_id, email, password_hash, first_name, last_name, role, is_active, login_count, created_at, updated_at, is_deleted) VALUES (:id, :org, 'outsider@example.test', 'unused', 'Test', 'User', 'USER', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"
        ),
        {"id": str(OUTSIDER_USER), "org": str(OUTSIDE_ORG)},
    )
    await db.execute(
        insert(Workspace).values(
            id=OUTSIDE_WORKSPACE,
            name="Outside",
            owner_id=OUTSIDER_USER,
            organization_id=OUTSIDE_ORG,
        )
    )
    await db.execute(
        insert(WorkspaceMember).values(
            id=uuid4(),
            workspace_id=OUTSIDE_WORKSPACE,
            user_id=USER,
            role=WorkspaceRole.EDITOR,
        )
    )
    await db.execute(
        insert(Collection).values(
            id=OUTSIDE_PROJECT, name="Outside", workspace_id=OUTSIDE_WORKSPACE
        )
    )
    await db.commit()


def test_an_account_catalog_has_the_selector_where_a_workspace_catalog_has_it() -> None:
    account = {t.name: t for t in list_read_tools(LIBRARY_READ, account_bound=True)}
    workspace = {t.name: t for t in list_read_tools(LIBRARY_READ, workspace_bound=True)}
    assert set(account) == set(workspace)
    for name, tool in workspace.items():
        selects = "project_id" in tool.input_schema["properties"]
        assert ("project_id" in account[name].input_schema["properties"]) is selects
    selector = account["search_documents"].input_schema["properties"]["project_id"]
    assert selector["format"] == "uuid" and "account" in selector["description"]
    # The workspace wording is unchanged, byte for byte.
    assert workspace["search_documents"].input_schema["properties"]["project_id"] == {
        "type": "string",
        "format": "uuid",
        "description": "Required for workspace grants: which project to act on.",
    }


async def test_an_account_tool_without_a_selector_asks_for_one(
    db: AsyncSession, adapters: dict[str, AsyncMock]
) -> None:
    result = await invoke_read(
        db, _account_context(), _invocation("list_project_documents")
    )
    assert result.content == [{"error": "project_id_required"}]
    adapters["_tool_list_project_documents"].assert_not_awaited()


@pytest.mark.parametrize(
    ("selected", "expected"),
    [(PROJECT, DOC_IN_PROJECT), (FOREIGN_PROJECT, DOC_FOREIGN)],
    ids=["first-workspace", "second-workspace"],
)
async def test_an_account_selector_reaches_projects_across_workspaces(
    db: AsyncSession, selected: UUID, expected: UUID
) -> None:
    listed = await invoke_read(
        db,
        _account_context(),
        _invocation("list_project_documents", project_id=str(selected)),
    )
    assert listed.is_error is False
    assert listed.source_refs == [{"document_id": str(expected)}]


async def test_an_account_selector_outside_the_account_is_denied(
    db: AsyncSession, adapters: dict[str, AsyncMock]
) -> None:
    await _outside_the_account(db)
    for project in (OUTSIDE_PROJECT, uuid4()):
        with pytest.raises(IntegrationAccessDenied):
            await invoke_read(
                db,
                _account_context(),
                _invocation("list_project_documents", project_id=str(project)),
            )
    with pytest.raises(read_tools.ToolArgumentError):
        await invoke_read(
            db,
            _account_context(),
            _invocation("list_project_documents", project_id="Project"),
        )
    for adapter in adapters.values():
        adapter.assert_not_awaited()


async def test_list_library_of_an_account_names_each_folders_workspace(
    db: AsyncSession,
) -> None:
    await _outside_the_account(db)
    result = await invoke_read(
        db, _account_context(LIBRARY_READ), _invocation("list_library")
    )
    assert result.is_error is False
    folders = result.content[0]["folders"]
    assert [(f["name"], f["workspace_name"]) for f in folders] == [
        ("Foreign", "Other workspace"),
        ("None", "Other workspace"),
        ("Other", "Workspace"),
        ("Project", "Workspace"),
    ]
    assert {f["workspace_id"] for f in folders} == {str(WORKSPACE), str(OTHER_WORKSPACE)}


@pytest.mark.parametrize(
    "make_context", [_project_context, _workspace_context], ids=["project", "workspace"]
)
async def test_list_library_of_other_grants_keeps_its_exact_shape(
    db: AsyncSession, make_context: Any
) -> None:
    result = await invoke_read(db, make_context(LIBRARY_READ), _invocation("list_library"))
    for folder in result.content[0]["folders"]:
        assert set(folder) == {"id", "name", "description", "document_count"}


async def test_list_library_of_an_account_binds_no_value_per_collection(
    db: AsyncSession, executed_sql: list[tuple[str, Any]]
) -> None:
    async def most_bound() -> int:
        executed_sql.clear()
        result = await invoke_read(
            db, _account_context(LIBRARY_READ), _invocation("list_library")
        )
        assert result.is_error is False
        return max(len(list(_bound_values(p))) for _sql, p in executed_sql)

    before = await most_bound()
    await db.execute(
        insert(Collection).values(
            [
                dict(id=uuid4(), name=f"Folder {n:03}", workspace_id=WORKSPACE)
                for n in range(60)
            ]
        )
    )
    await db.commit()
    assert await most_bound() == before
```

**Step 2: Run (expect failure)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_read_tools.py -k "account or other_grants_keeps"
```
Expected: `TypeError: list_read_tools() got an unexpected keyword argument 'account_bound'`. The selector tests get `ToolArgumentError("unknown arguments")`, because no selector is advertised for an account grant.

Mutation check (SECURITY): in `_target_project`'s account branch, delete `scope,` from the lookup. `test_an_account_selector_outside_the_account_is_denied` must fail: `OUTSIDE_PROJECT` passes `_verify_project_ownership`? No, `resolve_project` refuses another organization, so the failure is an error result rather than the expected denial. Either way the test fails, because it expects `IntegrationAccessDenied`.

**Step 3: Implement**

Module docstring L4-6: replace "The one exception is the ``project_id`` selector of a workspace grant, which picks one of the workspace's live projects" with "The one exception is the ``project_id`` selector of a workspace or account grant, which picks one of the grant's live projects".

After `_PROJECT_SELECTOR` (L134-138):

```python
# An account grant spans every project of the account; list_library names them.
_ACCOUNT_PROJECT_SELECTOR: dict[str, Any] = {
    "type": "string",
    "format": "uuid",
    "description": (
        "Required for account connections: which project to act on "
        "(list_library names them)."
    ),
}


def _selector(workspace_bound: bool, account_bound: bool) -> dict[str, Any] | None:
    if account_bound:
        return dict(_ACCOUNT_PROJECT_SELECTOR)
    if workspace_bound:
        return dict(_PROJECT_SELECTOR)
    return None
```

`_registry_schema`: the signature gains `account_bound: bool = False`, and the selector block (L353-355) becomes:

```python
    selector = _selector(workspace_bound, account_bound)
    if selector is not None and name not in _PROJECTLESS_TOOLS:
        # Optional in the schema: omitting it is a structured error, not a 422.
        properties["project_id"] = selector
```

`_local_schema`: same signature change, with body:

```python
    description, schema, _scope = LOCAL_TOOLS[name]
    schema = deepcopy(schema)
    selector = _selector(workspace_bound, account_bound)
    if selector is not None and name not in _PROJECTLESS_TOOLS:
        schema["properties"]["project_id"] = selector
    return description, schema
```

`list_read_tools(scopes=None, *, workspace_bound=False, account_bound=False)` passes both flags to `_local_schema` and `_registry_schema`. Add to its docstring: "An account grant sees the same selector, worded for the account."

`_validate_arguments` (L434-445): replace `workspace_bound = context.workspace_id is not None` with:

```python
    workspace_bound = context.binding_kind == "workspace"
    account_bound = context.binding_kind == "account"
```
Pass `account_bound=account_bound` to both `_local_schema(...)` and `_registry_schema(...)` calls in that function. Update the comment at L447-450 to "Only a workspace or account grant advertises (and so accepts) the project_id selector."

`_target_project` (replace L1429-1455):

```python
def _selector_uuid(raw: Any) -> UUID:
    if not isinstance(raw, str):
        raise ToolArgumentError("project_id must be a UUID")
    try:
        return UUID(raw)
    except ValueError as error:
        raise ToolArgumentError("project_id must be a UUID") from error


async def _target_project(
    db: AsyncSession, context: IntegrationContext, arguments: dict[str, Any]
) -> UUID | None:
    """The Collection a call acts on.

    Project grants: always the bound Collection (a client project_id was
    already refused as an unknown argument). Workspace grants: ``project_id``
    is a *selector* and must be one of the workspace's live Collections as
    ``authorized_scope`` computes them on this call. Account grants: the
    selector must satisfy the account rule in the statement that looks it up
    (``authorized_scope_filter``; the account may hold more Collections than
    one statement may bind). Returns None when a workspace or account grant
    omits the selector. The selector is a UUID, never a name: the agent
    helpers resolve any other string as a project NAME.
    """
    if context.project_id is not None:
        return context.project_id
    if context.binding_kind == "account":
        scope = await authorized_scope_filter(db, context)
        raw = arguments.get("project_id")
        if raw is None:
            return None
        chosen = _selector_uuid(raw)
        found = await db.scalar(
            select(Collection.id).where(
                Collection.id == chosen, scope, Collection.is_deleted.is_(False)
            )
        )
        if found is None:
            raise IntegrationAccessDenied()
        return chosen
    allowed = await authorized_scope(db, context)
    raw = arguments.get("project_id")
    if raw is None:
        return None
    chosen = _selector_uuid(raw)
    if chosen not in allowed:
        raise IntegrationAccessDenied()
    return chosen
```
The workspace path behaves exactly as before: the same messages, the same order, and the access check before the missing-selector answer.

`_library_folders` gains a keyword `with_workspace: bool = False`. The selected columns become:

```python
    columns: list[Any] = [
        Collection.id,
        Collection.name,
        Collection.description,
        live_documents,
    ]
    if with_workspace:
        columns += [Workspace.id, Workspace.name]
    rows = await db.execute(
        select(*columns)
        .join(Workspace, Workspace.id == Collection.workspace_id)
        .where(scope, Collection.is_deleted.is_(False), Workspace.is_deleted.is_(False))
        .order_by(Collection.name, Collection.id)
        .offset(offset)
        .limit(limit)
    )
    return rows.all()
```

`_list_library`: the docstring adds "an account grant every project of the account, each with its workspace id and name". The body becomes:

```python
    limit = _clamp(arguments.get("limit", MAX_RESULTS), 1, MAX_RESULTS, MAX_RESULTS)
    offset = _clamp(arguments.get("offset", 0), 0, MAX_OFFSET, 0)
    scope = await authorized_scope_filter(db, context)
    account = context.binding_kind == "account"
    found = await _library_folders(
        db, scope, context.organization_id, offset, limit + 1, with_workspace=account
    )
    folders = []
    for row in found[:limit]:
        folder = {
            "id": str(row[0]),
            "name": row[1],
            "description": (row[2] or "")[:MAX_FOLDER_DESCRIPTION_CHARS],
            "document_count": int(row[3]),
        }
        if account:
            # Projects of several workspaces may share a name.
            folder["workspace_id"] = str(row[4])
            folder["workspace_name"] = row[5]
        folders.append(folder)
    # ... the byte-cap loop and the return are unchanged ...
```

`backend/src/api/integrations/tools.py` `list_tools`:

```python
    return list_read_tools(
        context.scopes,
        workspace_bound=context.binding_kind == "workspace",
        account_bound=context.binding_kind == "account",
    )
```

**Step 4: Run (expect pass; the whole read-tool suite proves project and workspace behaviour unchanged)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_read_tools.py backend/tests/unit/api/test_integration_tools.py backend/tests/unit/architecture/test_integration_boundaries.py
```
Expected: all pass. Then apply the mutation check.

**Step 5: Commit**

```sh
git add backend/src/services/integrations/read_tools.py backend/src/api/integrations/tools.py backend/tests/unit/services/integrations/test_read_tools.py
git commit -m "feat(integrations): account project selector and account-wide list_library"
```

---

### Task 12: Library actions under an account grant (SECURITY-CRITICAL re-checks)

**Files**
- Modify: `backend/src/services/agent/tool_actions.py`:
  - imports (L46-70)
  - account validators after `_validate_arxiv_ingest` (after L409)
  - `validate_arguments` (L426-435)
  - `request_action` (L476-548)
  - `_resolve_target` (L551-608)
  - `_scope_condition` (L611-623)
  - `_same_target` (L654-671)
  - `get_action_for_review` (L723-795)
  - `_selected_folder_names` (L809-831)
  - `_binding_matches` and `_consent_binding` (L958-985)
  - `_target_reason` (L1001-1049)
  - `_authority_intact` (L1066)
  - `_create_folder` (L1271-1295)
  - `_update_document_metadata` (L1336-1342)
- Modify: `backend/src/api/integrations/actions.py` (`_actor`, L45-56)
- Modify: `backend/tests/unit/services/agent/test_tool_actions.py` (append after the library-action tests)

**Step 1: Write the failing tests** (append to `test_tool_actions.py`)

```python
# --- account grants: library actions across the account ---------------------

OUTSIDE_WORKSPACE, OUTSIDE = uuid4(), uuid4()


async def _account_actor(
    db: AsyncSession,
    scopes: frozenset[str] = LIBRARY,
    *,
    consent_scopes: frozenset[str] | None = None,
) -> ActionActor:
    """The actor of a live account grant and its account consent on DEVICE."""
    grant_id, consent_id = uuid4(), uuid4()
    await db.execute(
        insert(IntegrationGrantRequest).values(
            id=consent_id,
            user_id=USER,
            organization_id=ORG,
            binding_kind="account",
            project_id=None,
            workspace_id=None,
            device_id=DEVICE,
            scopes=sorted(scopes if consent_scopes is None else consent_scopes),
            status="consumed",
            expires_at=SOON,
            approved_at=datetime.now(timezone.utc),
        )
    )
    await db.execute(
        insert(IntegrationGrant).values(
            **_grant_values(
                grant_id,
                project_id=None,
                binding_kind="account",
                request_id=consent_id,
                scopes=sorted(scopes),
            )
        )
    )
    await db.commit()
    return ActionActor(
        user_id=USER,
        organization_id=ORG,
        project_id=None,
        workspace_id=None,
        binding_kind="account",
        grant_id=grant_id,
        consent_id=consent_id,
        scopes=scopes,
    )


async def _outside_the_account(db: AsyncSession) -> None:
    """A Collection USER may change as an editor of another organization's
    workspace: never part of USER's account."""
    await db.execute(
        insert(Workspace).values(
            id=OUTSIDE_WORKSPACE, name="Outside", owner_id=OTHER_USER, organization_id=ORG2
        )
    )
    await db.execute(
        insert(WorkspaceMember).values(
            id=uuid4(),
            workspace_id=OUTSIDE_WORKSPACE,
            user_id=USER,
            role=WorkspaceRole.EDITOR,
        )
    )
    await db.execute(
        insert(Collection).values(id=OUTSIDE, name="Outside", workspace_id=OUTSIDE_WORKSPACE)
    )
    await db.commit()


async def test_an_account_grant_moves_papers_between_workspaces_without_asking(
    db: AsyncSession, library: None
) -> None:
    actor = await _account_actor(db)
    status = await request_action(
        db,
        actor,
        _call(
            "move_papers_between_folders",
            document_ids=[str(PAPER)],
            from_project_id=P,
            to_project_id=str(ELSEWHERE),
        ),
    )
    assert status.state == "succeeded"
    row = await _row(db, status.invocation_id)
    assert (row.binding_kind, row.project_id, row.workspace_id) == (
        "account",
        PROJECT,
        None,
    )
    assert row.target_workspace_id is None
    assert await _linked(db, ELSEWHERE, PAPER) and not await _linked(db, PROJECT, PAPER)


OUTSIDE_THE_ACCOUNT: dict[str, tuple[str, dict[str, Any]]] = {
    "rename-outside": ("rename_folder", {"project_id": str(OUTSIDE), "name": "X"}),
    "save-outside": (
        "save_papers_to_folder",
        {"document_ids": [str(PAPER2)], "project_id": str(OUTSIDE)},
    ),
    "move-out": (
        "move_papers_between_folders",
        {"document_ids": [str(PAPER)], "from_project_id": P, "to_project_id": str(OUTSIDE)},
    ),
    "rename-deleted": ("rename_folder", {"project_id": str(RETIRED), "name": "X"}),
    "note-outside": ("create_project_note", {**NOTE_ARGS, "project_id": str(OUTSIDE)}),
    "folder-outside": (
        "create_folder",
        {"name": "Reading", "workspace_id": str(OUTSIDE_WORKSPACE)},
    ),
    "folder-unknown-workspace": (
        "create_folder",
        {"name": "Reading", "workspace_id": str(uuid4())},
    ),
}


@pytest.mark.parametrize(
    ("name", "arguments"), OUTSIDE_THE_ACCOUNT.values(), ids=list(OUTSIDE_THE_ACCOUNT)
)
async def test_an_account_grant_cannot_reach_outside_the_account(
    db: AsyncSession, library: None, name: str, arguments: dict[str, Any]
) -> None:
    await _outside_the_account(db)
    with pytest.raises(IntegrationAccessDenied):
        await request_action(db, await _account_actor(db), _call(name, **arguments))
    assert await _row_count(db) == 0
    assert (await _collection(db, OUTSIDE)).name == "Outside"


async def test_an_account_folder_names_its_workspace(
    db: AsyncSession, library: None
) -> None:
    actor = await _account_actor(db)
    with pytest.raises(ToolActionArgumentError, match="workspace_id is required"):
        await request_action(db, actor, _call("create_folder", name="Reading"))
    status = await request_action(
        db,
        actor,
        _call("create_folder", name="Reading", workspace_id=str(OTHER_WORKSPACE)),
    )
    assert status.state == "succeeded"
    row = await _row(db, status.invocation_id)
    assert (row.project_id, row.workspace_id, row.target_workspace_id) == (
        None,
        None,
        OTHER_WORKSPACE,
    )
    created = await db.scalar(select(Collection).where(Collection.name == "Reading"))
    assert created is not None and created.workspace_id == OTHER_WORKSPACE
    assert _receipt(status) == {
        "ok": True,
        "tool_name": "create_folder",
        "project_id": str(created.id),
        "workspace_id": str(OTHER_WORKSPACE),
        "name": "Reading",
    }


async def test_project_and_workspace_grants_still_refuse_a_workspace_argument(
    db: AsyncSession, library: None
) -> None:
    for actor in (await _library_actor(db), _actor(scopes=LIBRARY)):
        with pytest.raises(
            ToolActionArgumentError, match="identity arguments are not accepted"
        ):
            await request_action(
                db,
                actor,
                _call("create_folder", name="Reading", workspace_id=str(WORKSPACE)),
            )
    assert await _row_count(db) == 0


async def test_an_account_note_names_its_project_and_waits_for_a_decision(
    db: AsyncSession, library: None
) -> None:
    actor = await _account_actor(db)
    with pytest.raises(ToolActionArgumentError, match="project_id is required"):
        await request_action(db, actor, _call("create_project_note", **NOTE_ARGS))
    status = await request_action(
        db, actor, _call("create_project_note", **NOTE_ARGS, project_id=P)
    )
    assert status.state == "awaiting_approval"
    await _approve(db, status.invocation_id)
    done = await execute_action(db, status.invocation_id)
    assert done is not None and done.state == "succeeded"
    note = await db.scalar(select(ProjectNote))
    assert note is not None and note.project_id == PROJECT


async def test_auto_run_under_an_account_follows_library_write(
    db: AsyncSession, library: None
) -> None:
    actor = await _account_actor(db)
    renamed = await request_action(
        db, actor, _call("rename_folder", project_id=str(SECOND), name="Renamed")
    )
    assert renamed.state == "succeeded"
    for invocation in (
        _call("delete_folder", project_id=str(SECOND)),
        _call("ingest_arxiv_papers", paper_ids=["2401.00001"], project_id=P),
    ):
        assert (await request_action(db, actor, invocation)).state == "awaiting_approval"
    with pytest.raises(ToolActionArgumentError, match="project_id is required"):
        await request_action(db, actor, _call("ingest_arxiv_papers", paper_ids=["2401.00002"]))


async def test_an_account_action_replays_and_another_binding_conflicts(
    db: AsyncSession, library: None
) -> None:
    actor = await _account_actor(db, WRITE)
    invocation = _call("rename_folder", project_id=str(SECOND), name="Renamed")
    first = await request_action(db, actor, invocation)
    assert (await request_action(db, actor, invocation)).invocation_id == first.invocation_id
    with pytest.raises(ActionConflict):
        await request_action(db, _actor(scopes=WRITE), invocation)


# SECURITY-CRITICAL: the account is re-checked before the effect.
@pytest.mark.parametrize("action", ["rename", "create-folder"])
async def test_an_account_action_rechecks_the_account_when_it_runs(
    db: AsyncSession, library: None, action: str
) -> None:
    actor = await _account_actor(db, WRITE)  # tools:write: every action waits
    invocation = (
        _call("rename_folder", project_id=str(ELSEWHERE), name="Renamed")
        if action == "rename"
        else _call("create_folder", name="Reading", workspace_id=str(OTHER_WORKSPACE))
    )
    status = await request_action(db, actor, invocation)
    assert status.state == "awaiting_approval"
    await _approve(db, status.invocation_id)
    await db.execute(
        update(Workspace).where(Workspace.id == OTHER_WORKSPACE).values(is_archived=True)
    )
    await db.commit()
    failed = await execute_action(db, status.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert await _last_error(db, status.invocation_id) == "grant no longer authorizes this action"
    assert (await _collection(db, ELSEWHERE)).name == "Elsewhere"
    assert await db.scalar(select(Collection).where(Collection.name == "Reading")) is None


# SECURITY-CRITICAL: approval and grant kinds must match at execution.
async def test_an_account_action_needs_an_account_grant(
    db: AsyncSession, library: None
) -> None:
    actor = await _account_actor(db, WRITE)
    status = await request_action(
        db, actor, _call("rename_folder", project_id=P, name="Renamed")
    )
    # The project grant and consent of PROJECT try to authorise the account row.
    await db.execute(
        update(IntegrationToolAction)
        .where(IntegrationToolAction.invocation_id == status.invocation_id)
        .values(grant_id=GRANT, consent_id=CONSENT)
    )
    await db.commit()
    await _approve(db, status.invocation_id)
    failed = await execute_action(db, status.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert await _last_error(db, status.invocation_id) == "grant no longer authorizes this action"
    assert await _project_name(db) == "Project"


# SECURITY-CRITICAL: a project grant under an account consent.
async def test_a_project_grant_under_an_account_consent_acts_while_its_project_is_in_the_account(
    db: AsyncSession, library: None
) -> None:
    account = await _account_actor(db, WRITE)
    run_grant = uuid4()
    await db.execute(
        insert(IntegrationGrant).values(
            **_grant_values(
                run_grant,
                project_id=PROJECT,
                request_id=account.consent_id,
                scopes=sorted(WRITE),
            )
        )
    )
    await db.commit()
    actor = _actor(grant_id=run_grant, consent_id=account.consent_id)
    status = await request_action(db, actor, _rename_project())
    await _approve(db, status.invocation_id)
    done = await execute_action(db, status.invocation_id)
    assert done is not None and done.state == "succeeded"
    # Archived: authorized_project still admits PROJECT, the account does not.
    await db.execute(
        update(Workspace).where(Workspace.id == WORKSPACE).values(is_archived=True)
    )
    await db.commit()
    later = await request_action(db, actor, _rename_project())
    await _approve(db, later.invocation_id)
    failed = await execute_action(db, later.invocation_id)
    assert failed is not None and failed.state == "failed"
    assert await _last_error(db, later.invocation_id) == "consent revoked"


async def test_the_review_of_an_account_folder_names_its_workspace(
    db: AsyncSession, library: None
) -> None:
    actor = await _account_actor(db, WRITE)
    status = await request_action(
        db,
        actor,
        _call("create_folder", name="Reading", workspace_id=str(OTHER_WORKSPACE)),
    )
    review = await get_action_for_review(db, await _user(db), status.invocation_id)
    assert (review.workspace_id, review.workspace_label) == (OTHER_WORKSPACE, "Other")
    assert review.summary == "Create folder “Reading” in workspace “Other”"
    assert review.project_available is True
```

**Step 2: Run (expect failure)**

```sh
$PYTEST backend/tests/unit/services/agent/test_tool_actions.py -k "account"
```
Expected: `IntegrationAccessDenied` on every account request (`_resolve_target` treats an actor with neither column as bound to nothing), and `identity arguments are not accepted` for the account `create_folder`.

**Mutation checks** (after Step 3):

| Break (in `tool_actions.py`) | Must fail |
| --- | --- |
| `_binding_matches`: the account branch returns `True` | `test_an_account_action_needs_an_account_grant`. It then fails with "consent revoked", not the expected `GRANT_DENIED`. |
| `_account_target_reason`: `return None` as its first line | `test_an_account_action_rechecks_the_account_when_it_runs[rename]` and `[create-folder]` |
| `_resolve_target`: delete the `authorized_account_workspace(...)` call | `test_an_account_grant_cannot_reach_outside_the_account[folder-outside]` and `[folder-unknown-workspace]` |
| `_consent_binding`: drop the account `or_` branch of the project case | `test_a_project_grant_under_an_account_consent_acts_while_its_project_is_in_the_account` (its first run fails) |
| `_consent_binding`: replace the `exists().where(... account_scope_condition ...)` with `true()` | the same test (its archived run then succeeds) |

**Step 3: Implement.** Imports: add `and_, exists` to the sqlalchemy import. Change the context import to `IntegrationAccessDenied, account_scope_condition, authorized_account_workspace, authorized_scope_filter, live`. Add `from src.schemas.integration_context import BindingKind, IntegrationContext, binding_matches_kind, derive_binding_kind`.

Helpers (add after `_now`/`_clock`):

```python
def _actor_kind(actor: ActionActor) -> BindingKind | None:
    """The actor's binding kind: as stated, else derived from its columns
    (never "account", which only the router states from the grant)."""
    if actor.binding_kind is not None:
        return actor.binding_kind
    return derive_binding_kind(actor.project_id, actor.workspace_id)


def _row_kind(row: IntegrationToolAction) -> str:
    """The kind of the grant that stored ``row``. A row read back always
    carries it; one built in memory and never flushed has only its columns."""
    if row.binding_kind is not None:
        return str(row.binding_kind)
    return "workspace" if row.workspace_id is not None else "project"
```

Account validators (after `_validate_arxiv_ingest`):

```python
def _validate_account_note(arguments: dict[str, Any]) -> dict[str, Any]:
    """An account grant names the project its note lands in; the rest is a
    note, identity still refused."""
    validated = _validate_note_arguments(
        {key: value for key, value in arguments.items() if key != "project_id"}
    )
    return {**validated, "project_id": _uuid(arguments.get("project_id"), "project_id")}


def _validate_account_create_folder(arguments: dict[str, Any]) -> dict[str, Any]:
    """An account grant names the workspace the folder is created in. Here,
    and only here, ``workspace_id`` is a selector rather than identity; the
    target check holds it to the account."""
    validated = _validate_create_folder(
        {key: value for key, value in arguments.items() if key != "workspace_id"}
    )
    return {
        **validated,
        "workspace_id": _uuid(arguments.get("workspace_id"), "workspace_id"),
    }


_ACCOUNT_VALIDATORS: Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]] = (
    MappingProxyType(
        {
            "create_project_note": _validate_account_note,
            "create_folder": _validate_account_create_folder,
        }
    )
)
```

`validate_arguments`:

```python
def validate_arguments(
    tool_name: str, arguments: dict[str, Any], *, binding_kind: str = "project"
) -> dict[str, Any]:
    """The canonical arguments of one action: what is stored and hashed.

    Identity is refused on every action. Selectors are parsed into canonical
    UUID strings, never resolved here. An account grant's note names its
    project and its create_folder its workspace. Raises
    ``ToolActionArgumentError`` with a fixed reason, a public 422 detail that
    never echoes the caller's values.
    """
    if tool_name not in ALLOWED_ACTIONS:
        raise ToolActionArgumentError("tool is not available as an action")
    if binding_kind == "account" and tool_name in _ACCOUNT_VALIDATORS:
        return _ACCOUNT_VALIDATORS[tool_name](arguments)
    return _VALIDATORS[tool_name](arguments)
```

`request_action`: three changes.
1. First line: `kind = _actor_kind(actor)` and `arguments = validate_arguments(invocation.tool_name, invocation.arguments, binding_kind=kind or "project")`.
2. After `target = await _resolve_target(...)`:

```python
        if kind is None:
            raise IntegrationAccessDenied()  # _resolve_target already refused it
        target_workspace = (
            UUID(arguments["workspace_id"])
            if kind == "account" and invocation.tool_name == "create_folder"
            else None
        )
```
3. In the `IntegrationToolAction(...)` constructor add `binding_kind=kind,` and `target_workspace_id=target_workspace,`. Its comment becomes "The Collection the action aims at (None: a workspace itself), and the binding of the grant that asked for it."

`_resolve_target` (replace L551-608):

```python
async def _resolve_target(
    db: AsyncSession, actor: ActionActor, tool_name: str, arguments: dict[str, Any]
) -> UUID | None:
    """The Collection a new action aims at, checked against the grant's live scope.

    A project grant reaches its own Collection only: a selector naming any
    other is refused, and it cannot create a folder beside it. A workspace
    grant's selectors must name live Collections of its workspace that the
    user can still reach; ``create_folder`` aims at the workspace itself
    (None). An account grant's selectors must name Collections the account
    rule covers; its ``create_folder`` aims at the workspace it names, which
    must be in the account (None is returned; request_action stores it as
    target_workspace_id). ``update_document_metadata`` aims at a folder of the
    scope holding the document (the first by name), and ``ingest_arxiv_papers``
    must name its folder unless the grant is a project's. Out of scope raises
    ``IntegrationAccessDenied``, opaque, so a foreign Collection reads like a
    missing one.
    """
    kind = _actor_kind(actor)
    if kind is None or not binding_matches_kind(
        kind, actor.project_id, actor.workspace_id
    ):
        raise IntegrationAccessDenied()  # one binding, of the kind it states
    selected = {UUID(arguments[key]) for key in SELECTOR_KEYS if key in arguments}
    target: UUID | None = None
    if kind == "project":
        assert actor.project_id is not None
        if tool_name == "create_folder" or selected - {actor.project_id}:
            raise IntegrationAccessDenied()
        target = actor.project_id
    elif kind == "account" and tool_name == "create_folder":
        await authorized_account_workspace(
            db, actor.user_id, actor.organization_id, UUID(arguments["workspace_id"])
        )
        return None
    elif kind == "workspace" and tool_name == "create_project_note":
        # A note lands in one project, and a workspace grant cannot name it.
        raise ToolActionArgumentError(
            "workspace connections cannot request this action"
        )
    elif tool_name == "move_papers_between_folders":
        target = UUID(arguments["from_project_id"])
    elif "project_id" in arguments:
        target = UUID(arguments["project_id"])
    elif tool_name == "ingest_arxiv_papers":
        # A workspace or an account holds many folders: name one.
        raise ToolActionArgumentError("project_id is required")
    scope = await _scope_condition(db, actor)
    if tool_name == "update_document_metadata":
        folders = await _folders_holding(
            db, UUID(arguments["document_id"]), actor.organization_id, scope
        )
        if not folders:
            raise IntegrationAccessDenied()
        return folders[0]
    wanted = selected | ({target} if target is not None else set())
    if wanted:
        reachable = set(
            (
                await db.scalars(
                    select(Collection.id).where(
                        Collection.id.in_(wanted),
                        scope,
                        Collection.is_deleted.is_(False),
                    )
                )
            ).all()
        )
        if not wanted <= reachable:
            raise IntegrationAccessDenied()
    return target
```

The `assert` narrows a value already checked by `binding_matches_kind`. If the repo's lint forbids `assert` in `src`, use `cast(UUID, actor.project_id)` instead.

`_scope_condition`: add `binding_kind=_actor_kind(actor),` to the `IntegrationContext(...)` it builds. For a project or workspace actor the value is the derived one; for an account actor it is "account".

`_same_target`: insert before the `if row.workspace_id is not None:` branch:

```python
    if _row_kind(row) == "account":
        # The consent scoping in _scoped already pins the approval; the actor
        # must also be the account's, never a project or workspace actor.
        same_binding = _actor_kind(actor) == "account"
    elif row.workspace_id is not None:
```

`get_action_for_review`: three changes.
1. The join becomes `Workspace.id == func.coalesce(IntegrationToolAction.workspace_id, IntegrationToolAction.target_workspace_id, Collection.workspace_id)`.
2. Before building the DTO add `shown_workspace = row.workspace_id if row.workspace_id is not None else row.target_workspace_id`.
3. Use `workspace_id=shown_workspace` and `workspace_label=None if shown_workspace is None else str(workspace_label)`.

Project and workspace rows have `target_workspace_id` NULL, so the coalesce returns what it returned before.

`_selected_folder_names`: replace the opening guard:

```python
    if _row_kind(row) == "account":
        within: ColumnElement[bool] = account_scope_condition(
            row.user_id, row.organization_id
        )
    elif row.workspace_id is None:
        return {}
    else:
        within = Collection.workspace_id == row.workspace_id
```
In its query use `within` in place of `Collection.workspace_id == row.workspace_id`. Add to its docstring: "an account row names only Collections its user's account covers now".

`_binding_matches` and `_consent_binding` (replace L958-985):

```python
def _binding_matches(grant: IntegrationGrant, row: IntegrationToolAction) -> bool:
    """Whether a grant's binding is the one the row needs.

    An account row needs an account grant. A row with a workspace_id was
    authorised by a workspace grant of that workspace, which carries no
    project: the row's own project_id, if any, is the Collection it aims at,
    and ``_target_reason`` checks that against the workspace. Any other row
    needs a project grant on exactly its project.
    """
    if _row_kind(row) == "account":
        return grant.binding_kind == "account"
    if row.workspace_id is not None:
        return (
            grant.binding_kind == "workspace"
            and grant.project_id is None
            and grant.workspace_id == row.workspace_id
        )
    return (
        grant.binding_kind == "project"
        and grant.project_id == row.project_id
        and grant.workspace_id is None
    )


def _consent_binding(row: IntegrationToolAction) -> tuple[ColumnElement[bool], ...]:
    """The consent rows that may stand behind the row's grant: an account
    consent for an account row; a consent of the row's workspace for a
    workspace row; for a project row a consent of exactly its project, or an
    account consent while the project is in the account (a run's grant under
    an account approval)."""
    if _row_kind(row) == "account":
        return (IntegrationGrantRequest.binding_kind == "account",)
    if row.workspace_id is not None:
        return (
            IntegrationGrantRequest.project_id.is_(None),
            IntegrationGrantRequest.workspace_id == row.workspace_id,
        )
    return (
        or_(
            and_(
                IntegrationGrantRequest.project_id == row.project_id,
                IntegrationGrantRequest.workspace_id.is_(None),
            ),
            and_(
                IntegrationGrantRequest.binding_kind == "account",
                exists().where(
                    Collection.id == row.project_id,
                    account_scope_condition(row.user_id, row.organization_id),
                ),
            ),
        ),
    )
```
In `_authority_intact` change `or not _binding_matches(grant.project_id, grant.workspace_id, row)` to `or not _binding_matches(grant, row)`.

`_target_reason`: insert at the top, after `targets = _targets(row)`:

```python
    if _row_kind(row) == "account":
        return await _account_target_reason(db, row, grant, targets)
```
and add below it:

```python
async def _account_target_reason(
    db: AsyncSession,
    row: IntegrationToolAction,
    grant: IntegrationGrant,
    targets: set[UUID],
) -> str | None:
    """An account row may touch only what the account covers when it runs:
    every Collection it names, or for create_folder the workspace it names.
    A project or workspace that left the account since the request refuses
    the effect."""
    if row.target_workspace_id is not None:
        if targets:
            return GRANT_DENIED
        try:
            await authorized_account_workspace(
                db, row.user_id, row.organization_id, row.target_workspace_id
            )
        except IntegrationAccessDenied:
            return GRANT_DENIED
        return None
    if not targets:
        return GRANT_DENIED  # names nothing: never authorisable
    context = IntegrationContext(
        user_id=row.user_id,
        organization_id=row.organization_id,
        binding_kind="account",
        grant_id=grant.id,
    )
    try:
        scope = await authorized_scope_filter(db, context)
    except IntegrationAccessDenied:
        return GRANT_DENIED
    reachable = set(
        (
            await db.scalars(
                select(Collection.id).where(Collection.id.in_(targets), scope)
            )
        ).all()
    )
    return None if reachable == targets else GRANT_DENIED
```

`_create_folder` (replace L1274-1294):

```python
    workspace_id = (
        row.target_workspace_id if _row_kind(row) == "account" else row.workspace_id
    )
    if workspace_id is None:
        # Only a workspace or account grant may create one; its row names
        # the workspace.
        raise ToolActionError("action has no target workspace")
    created = await collection_service.create_collection(
        db,
        CollectionCreate(
            workspace_id=workspace_id,
            name=row.arguments["name"],
            description=row.arguments.get("description"),
            color=None,
            icon=None,
        ),
        row.user_id,
    )
    if created is None:
        return _refused(row, TARGET_NOT_FOUND)
    return _receipt(row, created.id, workspace_id=str(workspace_id), name=str(created.name))
```

`_update_document_metadata` (replace the `scope = (...)` at L1337-1341):

```python
    if _row_kind(row) == "account":
        scope: ColumnElement[bool] = account_scope_condition(
            row.user_id, row.organization_id
        )
    elif row.workspace_id is not None:
        scope = Collection.workspace_id == row.workspace_id
    else:
        scope = Collection.id == row.project_id
```

`backend/src/api/integrations/actions.py` `_actor`: add `binding_kind=context.binding_kind,`.

`_LIBRARY_EFFECTS` and the other effects need no change. They pass `workspace_id=row.workspace_id`, which is None for an account row, so the collection services use their standalone lookup (the user's own access). `_account_target_reason` has already held the row to the account.

**Step 4: Run (expect pass; the full action suite proves project and workspace rows unchanged)**

```sh
$PYTEST backend/tests/unit/services/agent/test_tool_actions.py backend/tests/unit/api/test_integration_actions.py backend/tests/unit/services/integrations
```
Expected: all pass. Then apply the mutation table.

**Step 5: Commit**

```sh
git add backend/src/services/agent/tool_actions.py backend/src/api/integrations/actions.py backend/tests/unit/services/agent/test_tool_actions.py
git commit -m "feat(integrations): library actions under an account grant, re-checked before the effect"
```

---

### Task 13: Connected devices list and revoke an account approval

**Files**
- Modify: `backend/src/services/integrations/connections.py` (`list_connections` loop, L137-163)
- Modify: `backend/tests/unit/services/integrations/test_connections.py` (append)

**Step 1: Write the failing test**

```python
async def test_an_account_approval_is_listed_and_revoked_with_every_grant_under_it(
    db: AsyncSession,
) -> None:
    account = uuid4()
    await db.execute(
        insert(IntegrationGrantRequest).values(
            id=account,
            user_id=USER,
            organization_id=ORG,
            binding_kind="account",
            device_id=DESKTOP,
            scopes=["harness:execute", "tools:read"],
            status="consumed",
            expires_at=SOON,
        )
    )
    tokens = {
        name: "nous_ig_" + name.ljust(43, "x") for name in ("pairing", "run")
    }
    for name, kind, project, scopes in (
        ("pairing", "account", None, ["harness:execute", "tools:read"]),
        ("run", "project", PROJECT, ["harness:execute"]),
    ):
        await db.execute(
            insert(IntegrationGrant).values(
                id=uuid4(),
                user_id=USER,
                organization_id=ORG,
                binding_kind=kind,
                project_id=project,
                device_id=DESKTOP,
                request_id=account,
                scopes=scopes,
                token_hash=sha256(tokens[name].encode()).hexdigest(),
                expires_at=SOON,
                consented_at=datetime.now(timezone.utc),
            )
        )
    await db.commit()
    desktop = next(
        d for d in await list_connections(db, await _user(db)) if d.device_id == DESKTOP
    )
    listed = {c.request_id: c for c in desktop.consents}
    assert listed[account] == ConnectionConsent(
        request_id=account,
        kind="account",
        project_id=None,
        project_label=None,
        workspace_id=None,
        workspace_label=None,
        thread_id=None,
        thread_label=None,
        scopes=["harness:execute", "tools:read"],
        status="consumed",
        approved_at=None,
    )
    for name, scope in (("pairing", "tools:read"), ("run", "harness:execute")):
        await resolve_integration_context(db, tokens[name], required_scope=scope)
    await revoke_consent(db, await _user(db), account)
    for name, scope in (("pairing", "tools:read"), ("run", "harness:execute")):
        with pytest.raises(IntegrationAccessDenied):
            await resolve_integration_context(db, tokens[name], required_scope=scope)
```

**Step 2: Run (expect failure)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_connections.py -k account
```
Expected: `KeyError: <account id>`. `_binding_label` returns None for a consent with neither column, and the loop drops it silently.

Mutation check: restore the `continue` on `label is None` for account consents (delete the account branch). The test must fail with `KeyError`.

**Step 3: Implement.** At the top of the `for consent in consents:` loop (L137):

```python
        if consent.binding_kind == "account":
            # The whole account: there is no project or workspace name to
            # look up, and it is never dropped, so it can always be revoked
            # here. The page words it "Whole account".
            by_device[consent.device_id].append(
                ConnectionConsent(
                    request_id=consent.id,
                    kind="account",
                    project_id=None,
                    project_label=None,
                    workspace_id=None,
                    workspace_label=None,
                    thread_id=None,
                    thread_label=None,
                    scopes=sorted(consent.scopes or []),
                    status=str(consent.status),
                    approved_at=consent.approved_at,
                )
            )
            continue
```
Update `_binding_label`'s docstring: "A consent binds one project or one workspace, or the whole account, which `list_connections` handles before calling this." `revoke_consent` needs no change: it revokes every grant whose `request_id` is the consent, so the run grants minted under the account approval are revoked too.

**Step 4: Run (expect pass)**

```sh
$PYTEST backend/tests/unit/services/integrations/test_connections.py backend/tests/unit/api/test_integration_connections.py
```

**Step 5: Commit**

```sh
git add backend/src/services/integrations/connections.py backend/tests/unit/services/integrations/test_connections.py
git commit -m "feat(integrations): list account approvals under Connected devices"
```

---

### Task 14: Pin that the harness socket refuses an account grant until PR 2

**Files**
- Modify: `backend/tests/unit/api/test_harness_workspace_guard.py` (append)

PR 1 changes nothing in `api/harness.py`. `_grant_context` (L103-113) already refuses a context with no `project_id`. This test records that, so an account pairing cannot open the socket before PR 2 builds the per-run checks. PR 2 replaces this test with its own acceptance tests.

**Step 1: Write the test.** It passes as soon as it is written, because it pins existing behaviour. The mutation below proves it can fail.

```python
async def test_connect_refuses_an_account_grant_until_account_runs_exist(
    transport: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        transport,
        "resolve_integration_context",
        AsyncMock(
            return_value=IntegrationContext(
                user_id=USER,
                organization_id=ORG,
                binding_kind="account",
                grant_id=GRANT,
                scopes=frozenset({"harness:execute"}),
            )
        ),
    )
    socket = _socket()
    await transport.connect(socket)
    socket.accept.assert_not_awaited()
    socket.close.assert_awaited_once_with(
        code=4403, reason="Bridge authorization denied"
    )
```

**Step 2: Mutation check.** In `backend/src/api/harness.py` L111, drop `context.project_id is None or`. This test fails (the socket accepts), and so does the existing workspace guard. Revert.

**Step 3: Run**

```sh
$PYTEST backend/tests/unit/api/test_harness_workspace_guard.py
```

**Step 4: Commit**

```sh
git add backend/tests/unit/api/test_harness_workspace_guard.py
git commit -m "test(harness): an account grant cannot open the bridge socket before PR 2"
```

---

### Task 15: Regenerate the OpenAPI snapshot and the frontend types

**Files**
- Modify (generated): `backend/openapi.json`, `frontend/src/types/generated/api.d.ts`

**Steps**

1. Regenerate:
   ```sh
   <PY> scripts/ci/generate_openapi.py
   pnpm --dir frontend generate:api-types
   ```
2. Check the diff names only the intended changes:
   ```sh
   git diff --stat backend/openapi.json frontend/src/types/generated/api.d.ts
   git diff backend/openapi.json | grep -E '^[+-]' | grep -E 'account|binding_kind|account_wide|project_ids|AccountSummaryDTO|"project_id"|"kind"'
   ```
   The expected changes are:
   - `GrantRequestCreate.account` (boolean, default false).
   - `GrantRequestDTO.binding_kind` (required enum) and `account_summary` (`AccountSummaryDTO | null`).
   - The new `AccountSummaryDTO`.
   - `DeviceListItemDTO.account_wide` and `project_ids` (required).
   - `WorkspaceBindingCreate`/`WorkspaceBindingDTO.project_id` becomes `uuid | null` with default `null` (no longer required).
   - The `ConnectionConsent.kind` enum gains `"account"`.

   Nothing else should change.
3. Check drift and the frontend types:
   ```sh
   <PY> scripts/ci/generate_openapi.py --check
   pnpm --dir frontend type-check
   pnpm --dir frontend exec vitest run src/components/integrations/__tests__/ConnectedDevices.test.tsx src/components/integrations/__tests__/IntegrationApprovalPage.test.tsx src/hooks/chat/__tests__/useHarnessConnection.test.tsx src/lib/integrations/__tests__/scopeLabels.test.ts
   ```
   Expected: no drift, type-check clean, and the suites pass. No preview frontend file reads `HarnessWorkspace.project_id` (grepped). If type-check fails on a fixture that builds one of these generated types literally, add the new fields to the fixture. That is the only frontend edit PR 1 may make.
4. Run the breaking-change report the way CI does:
   ```sh
   git show origin/develop:backend/openapi.json > <scratch>/openapi.base.json
   oasdiff breaking <scratch>/openapi.base.json backend/openapi.json
   ```
   Expect `WorkspaceBindingDTO.project_id` becoming nullable to be reported as an ERR (`response-property-became-nullable`), and the new `ConnectionConsent.kind` value probably as a warning. Add the `api-breaking-approved` label to the PR only if an ERR is reported. If `oasdiff` is not installed locally, record it as **NOT RUN** and let CI decide.
5. Commit:
   ```sh
   git add backend/openapi.json frontend/src/types/generated/api.d.ts
   git commit -m "chore(api): regenerate OpenAPI snapshot and frontend types for account connections"
   ```

---

### Task 16: Document the backend contract

**Files**
- Modify: `docs/engineering/harness-bridge.md`: insert a section `### Account connections` immediately before `## Consent, disclosure, and isolation limits` (preview L116), after the "Library actions" section.

**Content to insert**

```markdown
### Account connections

Design: [`docs/plans/2026-10-14-account-connection-design.md`](../plans/2026-10-14-account-connection-design.md). PR 1 holds the backend data, rules, tools, actions, approvals and Connected devices. Local Codex runs and the bridge (`--account`, `nous --rc`) and the website follow in their own PRs; until the runs PR, the harness socket refuses an account grant (`backend/tests/unit/api/test_harness_workspace_guard.py`).

- **Binding kind.** Consent requests, grants and tool actions carry `binding_kind`: `project`, `workspace` or `account` (migration `ac01_account_bindings`, which backfills existing rows from the column they set). One CHECK per kind replaces the old XOR: `account` sets neither `project_id` nor `workspace_id`, and a row that sets neither without saying `account` is refused. In code, `derive_binding_kind` never yields `account`. An action row records its grant's kind, and an account `create_folder` stores its workspace in `target_workspace_id`. `bridge_workspace_bindings.project_id` is nullable: NULL is an account folder.
- **The account rule.** `account_scope_condition` (`backend/src/services/integrations/context.py`) selects the Collections of the workspaces in the user's organization that they own or are a live member of, with neither workspace nor Collection archived or deleted, while the user is active in that live organization. It is the organization rule of `/projects` (`accessible_research_workspace_ids_query`), written with `workspace_in_org` so legacy NULL-organization workspaces follow their owner, plus "not archived". It is evaluated in each statement as a SQL condition, never as an id list, so new and removed projects apply at once and no statement binds one value per Collection. It is stricter than `authorized_project`: an explicit member of another organization's workspace keeps project-pairing access, but that workspace is never in the account. `test_account_scope.py` pins every case on SQLite and PostgreSQL and states the mutation checks.
- **Scopes.** An account approval may hold only `harness:execute`, `tools:read`, `tools:write`, `library:read` and `library:write` (`ACCOUNT_SCOPES`, enforced by `check_scopes` at request and on every use). It can never be chat-bound. Publication, selected memories and chat handoffs stay with project pairings.
- **Requests.** `POST /integrations/grant-requests` with `account: true` (and no project, workspace or chat) answers 503 `Account connections are disabled` while `ACCOUNT_CONNECTIONS_ENABLED` is false (the default). Turning the flag off stops new account approvals only; revoke existing ones under Connected devices. `GrantRequestDTO` always carries `binding_kind`; an account request also carries `account_summary` (the workspace and project counts at the time it is read).
- **Grants.** Exchanging an account approval issues an account-kind pairing grant to its device. An account grant is valid only with an account consent of the same device. A project grant whose consent is an account consent (the grant `mint_integration_grant` issues for a run) is valid only while its project satisfies the account rule. Both are checked on every use, in the consent lookup of `_validate_grant`. Renewal and resume keep the kind. When a device holds both an account consent covering a project and a project consent for it, a mint for that project takes the account consent; two account consents, or several project consents without an account consent, fail closed (`_single_lineage`). A caller that passes `consent_kind` (the run path does) considers only consents of that kind, and exactly one must match.
- **Folders and devices.** Registering an account folder (`project_id` null or omitted) needs a live account consent of that device holding `harness:execute`. `GET /integrations/devices/{id}/workspaces` lists account folders only while that approval lives. `GET /integrations/devices` adds `account_wide` and `project_ids`, from the same live `harness:execute` consents as `bound_thread_ids`.
- **Read tools.** An account grant gets the `project_id` selector on every tool that acts on one project, worded for the account. The selector must satisfy the account rule in the statement that looks it up (403 otherwise). Omitting it is `project_id_required`. `list_library` lists every folder in the account, each with `workspace_id` and `workspace_name`; project and workspace grants keep their exact payload.
- **Actions.** Selectors must be in the account. `create_folder` takes `workspace_id`, which must be an account workspace (`workspace_id` stays identity, and is refused, for project and workspace grants). `create_project_note` takes `project_id`. An account row runs only under an account grant and consent, and `_account_target_reason` re-checks every Collection or the target workspace against the account immediately before the effect. Auto-run versus approval follows `library:write` exactly as for workspace grants.
- **Connected devices.** An account approval is listed with `kind: "account"` and null ids and labels, and is never dropped. Revoking it revokes every grant issued under it, including the run grants.
- **Evidence.** SQLite: `test_account_connections.py`, `test_read_tools.py`, `test_tool_actions.py`, `test_connections.py`. PostgreSQL (when `ORCHESTRATION_TEST_DATABASE_URL` is set; otherwise **NOT RUN**): the PostgreSQL halves of `test_account_scope.py` and `test_account_connections.py`, and `backend/tests/integration/test_account_bindings_migration_postgres.py` (upgrade backfill, the CHECKs with rows, the downgrade's deletions and its refusal while a harness session cites an account grant or folder). `backend/tests/unit/test_account_bindings_migration.py` renders the revision offline. A deployed upgrade and any live use on dev are **NOT RUN**.
```

**Steps**

```sh
git diff --check -- docs/
make docs-lint
```
Resolve the one relative link by hand: `docs/engineering/../plans/2026-10-14-account-connection-design.md` must exist on the branch. The design doc lands with these plans. If it is not on `develop` yet, add it in this PR, or point the link at its PR.

```sh
git add docs/engineering/harness-bridge.md
git commit -m "docs(harness): account connections backend contract"
```

---

### Task 17: PR 1 verification and PR

**1. Targeted tests (SQLite, always)**

```sh
$PYTEST \
  backend/tests/unit/test_account_bindings_migration.py \
  backend/tests/unit/test_workspace_grants_migration.py \
  backend/tests/unit/ci/test_migration_tests_collect_together.py \
  backend/tests/unit/services/integrations \
  backend/tests/unit/services/agent/test_tool_actions.py \
  backend/tests/unit/services/harness \
  backend/tests/unit/services/artifacts \
  backend/tests/unit/api/test_integration_account_requests.py \
  backend/tests/unit/api/test_integration_tools.py \
  backend/tests/unit/api/test_integration_actions.py \
  backend/tests/unit/api/test_integration_connections.py \
  backend/tests/unit/api/test_integration_grant_resume.py \
  backend/tests/unit/api/test_harness_workspace_guard.py \
  backend/tests/unit/api/agent/test_harness_streaming.py \
  backend/tests/unit/architecture \
  backend/tests/unit/api
```
`backend/tests/unit/architecture` must pass whole. In particular:
- `test_integration_boundaries.py`: no transaction call in `api/integrations/*`, identity required on `authorized_account` and `authorized_account_workspace`, and no direct `Workspace.organization_id` read outside the rule's helpers;
- `test_workspace_boundaries.py`, `test_agent_api_boundaries.py` and `test_maintenance_contracts.py`.

**2. PostgreSQL (local recipe; record the PostgreSQL version)**

```sh
ORCHESTRATION_TEST_DATABASE_URL=$PG $PYTEST \
  backend/tests/unit/services/integrations/test_account_scope.py \
  backend/tests/unit/services/integrations/test_account_connections.py \
  backend/tests/unit/services/integrations/test_grant_resume_postgres.py \
  backend/tests/integration/test_account_bindings_migration_postgres.py \
  backend/tests/integration/test_harness_dispatch_postgres.py \
  backend/tests/integration/test_same_project_continuation.py
```
Expected: no skips. Then the empty-database replay from Task 3 Step 4.

**3. Mutation checks.** Run and record every mutation table in Tasks 3, 5, 6, 8, 9, 10, 11, 12, 13 and 14 (each must fail, then pass again after the revert). The security-critical ones are:
- the account rule (Task 5);
- approval and grant kind matching (Task 8 rows 2 and 3; Task 12 rows 1 and 4);
- a project grant under an account consent (Task 8 row 1; Task 12 row 5);
- consent precedence (Task 9);
- `bind_workspace` without an account consent (Task 10 rows 1 and 2);
- the execution-time re-check (Task 12 rows 2 and 3).

**4. Lint gate** (pinned versions: ruff 0.15.15, black 26.5.1, isort 5.13.2, mypy 1.7.1)

```sh
CHANGED=$(git diff --name-only origin/develop...HEAD -- 'backend/*.py')
ADDED=$(git diff --name-only --diff-filter=A origin/develop...HEAD -- 'backend/*.py')
<PY> -m ruff check $CHANGED && <PY> -m black --check $CHANGED && <PY> -m isort --check-only $CHANGED
(cd backend && <PY> -m mypy --disallow-untyped-defs $(echo "$ADDED" | sed 's#^backend/##'))
<PY> -m ruff check backend/src
```

**5. Contract checks**

```sh
<PY> scripts/ci/generate_openapi.py --check
(cd backend && <PY> ../scripts/ci/check_alembic.py)   # one head: ac01_account_bindings
pnpm --dir frontend type-check
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**6. Personal-data check before pushing (the repo is public)**

```sh
git diff origin/develop...HEAD | grep -nE '@gmail|/Users/|goodwiinz@' || echo clean
```

**7. PR** (`gh pr create --base develop`, title `feat(integrations): account binding kind — data, rules, tools, actions and approvals (account connections PR 1/3)`). Body outline:

- **Summary.**
  - A third binding kind, `account`: migration `ac01_account_bindings` with the backfill and one CHECK per kind; account folders.
  - The account rule as a per-statement SQL condition.
  - Grant validation, renewal, resume and mint across kinds, including the consent precedence rule.
  - Read-tool selector and account-wide `list_library`.
  - Library actions with execution-time re-checks.
  - Connected devices.
  - `ACCOUNT_CONNECTIONS_ENABLED` (default false, 503).
  - Regenerated OpenAPI and frontend types.
  - The docs section.
  - Link to the design. Name PR 2 (runs and bridge) and PR 3 (website) as follow-ups. The harness socket still refuses account grants.
- **Contract notes.**
  - The alembic head was `aw03_artifact_completion`.
  - The added helper names.
  - `account_wide` and `project_ids` from `harness:execute` consents only.
  - The precedence rule.
  - `list_library` workspace fields for account grants only.
- **Verification.** List the commands from steps 1-5 with their results: the pass counts, the PostgreSQL version, and that no PostgreSQL test was skipped. List each mutation check with the line broken and the test that failed. Include the `oasdiff` output.
- **NOT RUN.**
  - Live dev deployment and the migration on the deployed database.
  - Turning the flag on in `values-aws.yaml` (after merge, separately).
  - Any account pairing from a real bridge (needs PR 2).
  - The website pages (PR 3).
  - Concurrent PostgreSQL action claims for account rows.
  - `oasdiff`, if it was not available locally.
- **Labels.** `api-breaking-approved` only if `oasdiff` reported an ERR (expected for the nullable `WorkspaceBindingDTO.project_id`).
- Link a Linear ticket with `Fixes GOO-nnn` if one exists for this work.
- End the body with the attribution line the session's system reminder gives.

---

# PR 2 — Local Codex runs and the bridge

## PR 2 — Local Codex runs from any chat under an account approval, plus the bridge

> **For Claude:** REQUIRED SUB-SKILL: use superpowers:executing-plans (or superpowers:subagent-driven-development) to implement this section task by task. TDD per task: failing test, see it fail, minimal code, see it pass, commit with explicit paths.

**Goal.** A computer paired once with the whole NOUS account (`nous-harness connect --account`, or `nous --rc`, which becomes account-only) can run Local Codex from any chat in any project of the account. Each run still gets a project-kind grant for the chat's project, minted under the account approval; once the bridge's account pairing grant replaces it on the session (the long-run swap), every check takes the project from the run and re-checks it against the account rule. The run's NOUS tools are pinned to the chat's project.

**Architecture.** Backend: a small `services/harness/account.py` (account rule helpers built on PR 1's `account_scope_condition`); `create_chat_context` accepts account folders and mints the run grant with an explicit `consent_kind="account"`; `authorize_external_submission` / `delivery._authorize` gain an account branch (the single place a run's project is checked against the account); leasing selects runs by "project in the account and session on this device"; the start command carries `projectId` only under an account pairing. Bridge: `LocalState.account`, `connect({ account: true })`, account folders, `projectId` threaded from the start command through the journal into `sessionOptionsFor`, a `--project` pin in the MCP child (`src/mcp/pin.ts`), and `nous --rc` rewritten without the project picker.

**Tech stack.** FastAPI / SQLAlchemy async (SQLite unit tests, PostgreSQL integration tests), Node 24 + `node:test` + tsx for `packages/harness-bridge`.

**Placeholders.** `<checkout>` = the repo root of the PR 2 worktree; `<PY>` = `<checkout-of-main>/backend/.venv/bin/python` (worktrees have no venv); `<scratch>` = a temp dir. Line ranges below are from the preview merge of `develop` + `feat/nous-rc` + #1987 (`1c052fc9f`); PR 1 shifts lines in `services/integrations/context.py`, so re-locate by function name there.

### Guesses and contract deviations (read first)

**Deviations from `contract.md`, each needed by this PR:**

1. **`mint_integration_grant(..., consent_kind: Literal["project", "account"] | None = None)`** (new keyword, PR 2). When given, it decides which approval the run grant is minted under, ahead of whatever default precedence PR 1 defines (callers that omit it keep PR 1's behaviour). `create_chat_context` always passes it, from the folder's kind: an account folder mints under the device's account approval, a project folder under the project approval. This makes "project approval and account approval both live on one device" deterministic (Task 6 test 5). If PR 1 already added an equivalent keyword, use PR 1's name and skip Task 1 step 3.
2. **`services/harness/account.py`** (new module, PR 2): `account_projects`, `project_in_account`, `live_account_consent`, `consent_kind`, `run_project`, `ProjectOutsideAccount(IntegrationAccessDenied)`, `PROJECT_UNREACHABLE`.
3. **`projectId` lives on `BridgeCommand`, not on the shared `Envelope`.** `Envelope` is also the base of `BridgeEvent` (`extra="forbid"`), so putting it there would force every event to carry it. It is set only on `start` commands leased under an account pairing and is **omitted from the wire** otherwise (`BridgeCommand.wire()`): today's bridges reject any unknown top-level key (`parseCommand`'s `exact()`), so sending `"projectId": null` to a project pairing would disconnect every existing bridge.
4. **`--account` always requests `library:read` too** (as `--workspace` does, WG-3): account tools take a `project_id`, and only `list_library` (library:read) names the choices. Contract text said "always adds `harness:execute`, `tools:read`"; `library:read` is in `ACCOUNT_SCOPES`, so this is a clarification, not a new scope.
5. **Pinned runs refuse three actions locally**: `create_folder`, `move_papers_between_folders` (a project connection cannot take them either) and `update_document_metadata` (it names a document, not a project, so the bridge cannot hold it to the chat's project). A pinned session also hides `list_library`. Account-wide reach stays with a Codex the user starts (`mcp install`). The pin is a guard on the model, not a security boundary: the MCP child holds the account credential.
6. **"The chat says the project is no longer reachable"** is delivered for a run that has not started yet (`dispatch_pending` fails it with `This project is no longer reachable from this computer.`). A run already executing when its project leaves the account is refused at its next command (no new output, its events are rejected with `run_access_denied`, and the bridge prints its existing refusal notice), exactly as today when a chat moves project. A chat-visible message mid-run needs a server-owned notice event and is left as a follow-up (PR body lists it).
7. **`create_chat_context` refusals are `IntegrationAccessDenied`** (not `ValueError`) for every device/folder/project refusal, and they sit in one helper `_run_consent_kind(...)` so PR 3 can split it into `_authorize_chat_run` and wrap it in its own `DeviceNotConnectedToProject`. PR 2 does **not** define that exception or touch `streaming.py`'s error mapping. The two policy checks (bridge disabled, thread without a project, user without an organization) stay `ValueError` as today.
8. **`nous --rc` checks the stored sign-in with `GET /auth/me`** instead of `GET /projects?limit=50` (both use `get_current_user`; the picker that needed the list is gone). `listProjects`, `createProject`, `pickProject`, `menuChoice`, `plain`, `RcProject*`, `PROJECT_PAGE` and `RcOptions.prompt` are deleted. `terminalPrompt` stays: it is still the Ctrl-C/Ctrl-D cancel path before the bridge starts (and `rc-terminal.test.ts` covers it).
9. **422/503 detection.** On the account consent request (`POST /integrations/grant-requests` with `account: true`), a **422** (a server before PR 1: `GrantRequestCreate` is `extra="forbid"`) or a **503 whose `detail` is exactly `Account connections are disabled`** (PR 1's flag off) becomes `this NOUS server doesn't support account connections yet`. Any other 503 passes through as `NOUS request failed (503) /integrations/grant-requests`. The device row `POST /integrations/devices` created just before stays, as it does today after a refused approval.

**Guesses about PR 1 (verified in Task 0; each has a fallback step):**

- G1. `account_scope_condition(user_id, organization_id)` is a `ColumnElement[bool]` over `Collection` (usable as `select(Collection.id).where(...)`), excludes archived workspaces (`Workspace.is_archived`), and does not change `authorized_project` (which ignores `is_archived`). Tests use "archive the workspace" as "the project leaves the account while chat access stays".
- G2. `IntegrationContext(..., binding_kind="account")` with both `project_id` and `workspace_id` `None` validates; a context built with only `project_id` still derives `binding_kind="project"`.
- G3. `IntegrationGrant.binding_kind` and `IntegrationGrantRequest.binding_kind` exist; `resolve_integration_context` returns `binding_kind` and `consent_id`.
- G4. `_validate_grant` accepts a project-kind grant whose `request_id` is a live account approval for the same device (design §2). Task 2 step 4 holds the fallback code if PR 1 left it out. PR 2 re-checks account membership for such grants itself (`authorize_external_submission`), so it does not rely on PR 1 for that.
- G5. `create_request` accepts `GrantRequestCreate(account=True, device_id=..., scopes=...)` when `settings.ACCOUNT_CONNECTIONS_ENABLED` is on, and allows a second account request for a device that already holds one (only Task 5's takeover test needs that; fallback: insert the second request/grant rows directly).
- G6. `WorkspaceBindingCreate.project_id` may be omitted (None = account folder) on `POST /integrations/devices/{id}/workspaces`.
- G7. `renew_grant` and #1987's `resume_grant` carry `binding_kind`, so an account pairing renews and resumes like a project one.

**Pairing switch note (answers the coordinator's risk).** `connect`'s full flow always registers a **new device** (`POST /integrations/devices`) before requesting consent, so when `nous --rc` replaces a project pairing with an account pairing, the old project approval belongs to the old device id and can never match the new device in `mint_integration_grant`. Two live approvals on one device are therefore not reachable through the bridge, only through the API; Task 6 test 5 still proves they resolve deterministically. `nous --rc` cannot revoke the replaced approval (the bridge deleted that grant's credential), so it keeps the printed revoke hint.

---

### Task 0: Branch and PR 1 preconditions

**Files:** none changed.

**Step 1: Worktree from a fresh `origin/develop` (after #1983, #1986, #1987 and PR 1 merged).**

```sh
git -C <checkout-of-main> fetch origin
git -C <checkout-of-main> worktree add .worktrees/account-runs -b feat/account-runs origin/develop
cd <checkout-of-main>/.worktrees/account-runs   # this is <checkout> from here on
git push -u origin feat/account-runs
```

**Step 2: Confirm PR 1's names exist.**

```sh
grep -n "def account_scope_condition\|^BindingKind\|^ACCOUNT_SCOPES" backend/src/services/integrations/context.py
grep -n "binding_kind" backend/src/models/integration_grant.py backend/src/schemas/integration_context.py | head
grep -n "ACCOUNT_CONNECTIONS_ENABLED" backend/src/core/config.py
grep -n "project_id: UUID | None" backend/src/schemas/integration_context.py   # WorkspaceBindingCreate
```

Expected: each grep prints at least one line. If any prints nothing, stop: PR 1 has not landed.

**Step 3: Baseline the suites this PR touches (all must pass before any change).**

```sh
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q --no-cov \
  backend/tests/unit/services/harness backend/tests/unit/api/test_harness_workspace_guard.py \
  backend/tests/unit/api/agent/test_harness_streaming.py backend/tests/unit/services/integrations
export PATH=/opt/homebrew/opt/node@24/bin:$PATH   # or any Node 24
pnpm install --frozen-lockfile
pnpm --filter @nous/harness-bridge test
```

Expected: all green; record the bridge totals (`# tests N`, `# pass N`) for Task 13.

---

### Task 1: Account helpers and minting a run grant under the account approval

**Files:**
- Create: `backend/src/services/harness/account.py`
- Modify: `backend/src/services/integrations/context.py` — `mint_integration_grant` (preview lines 417–495; consent query 441–481)
- Create: `backend/tests/unit/services/harness/test_account_runs.py` (fixtures + Task 1 tests; later tasks append)

**Step 1: Write the test module header, fixtures and the Task 1 tests.**

```python
"""Local Codex runs under an account approval (account-connection design §3).

SQLite unit evidence. The PostgreSQL twin is
backend/tests/integration/test_harness_account_postgres.py.

Mutation verification: Task 7 of the PR 2 plan lists, for each guard, the line
to break and the test here that must fail; record the date and result below
when it is run.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.agent.harness_streaming import (
    context_for_accepted_run,
    create_chat_context,
)
from src.core.config import settings
from src.models.agent_run import AgentRun
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.harness_session import (
    HarnessCommand,
    HarnessNativeRequest,
    HarnessReceipt,
    HarnessSession,
)
from src.models.integration_grant import IntegrationGrant
from src.models.thread import Thread
from src.models.workspace import Workspace
from src.schemas.harness import BridgeEvent
from src.schemas.integration_context import GrantRequestCreate, IntegrationContext
from src.services.agent.agent_submission_service import accept_submission
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage
from src.services.harness.account import live_account_consent
from src.services.integrations.context import (
    IntegrationAccessDenied,
    create_request,
    decide_request,
    exchange_request,
    mint_integration_grant,
    resolve_integration_context,
)
from tests.unit.services.harness.test_runs import (  # noqa: F401
    CONVERSATION,
    DEVICE,
    LOCAL,
    ORG,
    PROJECT,
    THREAD,
    USER,
    WORKSPACE,
    context,
    db,
)

pytestmark = pytest.mark.unit
OWNER = SimpleNamespace(id=USER, organization_id=ORG)
(
    ACCOUNT_DEVICE,
    WORKSPACE_B,
    PROJECT_B,
    CONVERSATION_B,
    THREAD_B,
    FOLDER_A,
    FOLDER_B,
) = (uuid4() for _ in range(7))
PAIRING_SCOPES = {"harness:execute", "tools:read"}


@pytest.fixture(autouse=True)
async def account_world(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second workspace and project in the account, and an account device."""
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
    monkeypatch.setattr(settings, "ACCOUNT_CONNECTIONS_ENABLED", True)
    connection = await db.connection()
    models: tuple[Any, ...] = (HarnessCommand, HarnessReceipt, HarnessNativeRequest)
    for model in models:
        await connection.run_sync(model.__table__.create)
    # One flush per parent level: these models declare no relationships.
    for row in (
        Workspace(id=WORKSPACE_B, name="w2", owner_id=USER, organization_id=ORG),
        Collection(id=PROJECT_B, workspace_id=WORKSPACE_B, name="p2"),
        Conversation(
            id=CONVERSATION_B, workspace_id=WORKSPACE_B, title="c2", created_by_id=USER
        ),
        Thread(
            id=THREAD_B,
            conversation_id=CONVERSATION_B,
            source_project_id=PROJECT_B,
            title="t2",
            created_by_id=USER,
            message_count=0,
        ),
        BridgeDevice(
            id=ACCOUNT_DEVICE, user_id=USER, organization_id=ORG, label="desktop"
        ),
    ):
        db.add(row)
        await db.flush()
    await db.commit()


async def _paired(db: AsyncSession, data: GrantRequestCreate) -> IntegrationContext:
    request = await create_request(db, OWNER, data)
    await decide_request(db, OWNER, request.id, True)
    issued = await exchange_request(db, OWNER, request.id)
    return await resolve_integration_context(
        db, issued.token, required_scope="harness:execute"
    )


async def pair_account(
    db: AsyncSession, device: UUID = ACCOUNT_DEVICE
) -> IntegrationContext:
    """The bridge's account pairing grant for ``device``."""
    return await _paired(
        db, GrantRequestCreate(account=True, device_id=device, scopes=PAIRING_SCOPES)
    )


async def pair_project(
    db: AsyncSession, project_id: UUID, device: UUID
) -> IntegrationContext:
    """A project pairing grant (today's kind) for ``device``."""
    return await _paired(
        db,
        GrantRequestCreate(
            project_id=project_id, device_id=device, scopes={"harness:execute"}
        ),
    )


@pytest.fixture
async def account(db: AsyncSession) -> IntegrationContext:
    """The account pairing on ACCOUNT_DEVICE, with two account folders."""
    paired = await pair_account(db)
    db.add_all(
        [
            WorkspaceBinding(
                device_id=ACCOUNT_DEVICE, workspace_id=FOLDER_A, project_id=None, label="a"
            ),
            WorkspaceBinding(
                device_id=ACCOUNT_DEVICE, workspace_id=FOLDER_B, project_id=None, label="b"
            ),
        ]
    )
    await db.commit()
    return paired


async def start(
    db: AsyncSession, thread_id: UUID, folder: UUID, device: UUID = ACCOUNT_DEVICE
) -> str:
    """One chat turn that runs Local Codex in ``folder``; returns the run id."""
    thread = await db.get(Thread, thread_id)
    run_context = await create_chat_context(
        db, current_user=OWNER, thread=thread, device_id=device, workspace_id=folder
    )
    accepted = await accept_submission(
        db,
        current_user=OWNER,
        request=AgentExecuteRequest(
            messages=[
                AgentMessage(role="user", content="hello", client_message_id=uuid4())
            ],
            execution_provider="codex",
            device_id=device,
            workspace_id=folder,
            thread_id=str(thread_id),
        ),
        thread=thread,
        integration_context=run_context,
    )
    return str(accepted.run_id)


def event_for(command: Any, seq: int = 1, body: Any = None) -> BridgeEvent:
    values = command.model_dump(exclude={"expiresAt", "body", "projectId"})
    values.update(
        sourceId=f"source-{command.runId}",
        sourceSeq=seq,
        body=body
        or {
            "kind": "event",
            "eventType": "assistant.delta",
            "payload": {"text": "answer"},
        },
    )
    return BridgeEvent.model_validate(values)


async def leave_account(db: AsyncSession, workspace_id: UUID = WORKSPACE_B) -> None:
    """Archive a workspace: its projects leave the account; chat access stays."""
    await db.execute(
        update(Workspace).where(Workspace.id == workspace_id).values(is_archived=True)
    )
    await db.commit()


async def test_mint_under_the_account_approval_issues_a_project_grant(
    db: AsyncSession, account: IntegrationContext
) -> None:
    consent = await live_account_consent(
        db, user_id=USER, organization_id=ORG, device_id=ACCOUNT_DEVICE
    )
    assert consent is not None and consent.id == account.consent_id
    issued = await mint_integration_grant(
        db,
        user_id=USER,
        organization_id=ORG,
        project_id=PROJECT_B,
        scopes=frozenset({"harness:execute"}),
        thread_id=THREAD_B,
        device_id=ACCOUNT_DEVICE,
        consent_kind="account",
    )
    grant = await db.get(IntegrationGrant, issued.grant_id)
    assert grant is not None
    assert (grant.binding_kind, grant.project_id, grant.thread_id) == (
        "project",
        PROJECT_B,
        THREAD_B,
    )
    assert grant.request_id == consent.id


# Mutation MA2 (Task 7): delete the `in_account is None` raise in
# mint_integration_grant; this test must fail.
async def test_mint_under_the_account_approval_refuses_a_project_outside_the_account(
    db: AsyncSession, account: IntegrationContext
) -> None:
    await leave_account(db)
    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(
            db,
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT_B,
            scopes=frozenset({"harness:execute"}),
            thread_id=THREAD_B,
            device_id=ACCOUNT_DEVICE,
            consent_kind="account",
        )


async def test_mint_under_the_account_approval_needs_one_on_the_device(
    db: AsyncSession, context: IntegrationContext
) -> None:
    # DEVICE holds only a project approval (the `context` fixture).
    with pytest.raises(IntegrationAccessDenied):
        await mint_integration_grant(
            db,
            user_id=USER,
            organization_id=ORG,
            project_id=PROJECT,
            scopes=frozenset({"harness:execute"}),
            thread_id=THREAD,
            device_id=DEVICE,
            consent_kind="account",
        )
```

**Step 2: Run, expect failure.**

```sh
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q --no-cov \
  backend/tests/unit/services/harness/test_account_runs.py
```

Expected: collection error `ModuleNotFoundError: No module named 'src.services.harness.account'`.

**Step 3: Implement `services/harness/account.py`.**

```python
"""Account-approval checks for Local Codex runs (account-connection design §3).

An account approval lets any chat in the account run Codex on its device. Each
run still gets a project-kind grant for its chat's project, minted under the
account approval. Once the bridge's account pairing grant replaces that grant
on the session (the long-run swap in ``delivery.lease_commands``), the run's
own project is used, and only while the account rule admits it.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.collection import Collection
from src.models.integration_grant import IntegrationGrantRequest
from src.schemas.integration_context import IntegrationContext
from src.services.integrations.context import (
    IntegrationAccessDenied,
    account_scope_condition,
)

PROJECT_UNREACHABLE = "This project is no longer reachable from this computer."


class ProjectOutsideAccount(IntegrationAccessDenied):
    """The run's project is not, or is no longer, in the device's account."""


def account_projects(user_id: UUID, organization_id: UUID) -> Select[tuple[UUID]]:
    """The account's Collection ids as a subquery, evaluated on every call."""
    return select(Collection.id).where(
        account_scope_condition(user_id, organization_id)
    )


async def project_in_account(
    db: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
    project_id: UUID | None,
) -> bool:
    if project_id is None:
        return False
    found = await db.scalar(
        account_projects(user_id, organization_id).where(Collection.id == project_id)
    )
    return found is not None


async def live_account_consent(
    db: AsyncSession, *, user_id: UUID, organization_id: UUID, device_id: UUID
) -> IntegrationGrantRequest | None:
    """The device's one exchanged, unrevoked account approval, else None.

    Two would be ambiguous; mint_integration_grant fails closed on them too.
    """
    rows = (
        await db.scalars(
            select(IntegrationGrantRequest)
            .where(
                IntegrationGrantRequest.user_id == user_id,
                IntegrationGrantRequest.organization_id == organization_id,
                IntegrationGrantRequest.device_id == device_id,
                IntegrationGrantRequest.binding_kind == "account",
                IntegrationGrantRequest.status == "consumed",
                IntegrationGrantRequest.consent_revoked_at.is_(None),
                IntegrationGrantRequest.is_deleted.is_(False),
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    return rows[0] if len(rows) == 1 else None


async def consent_kind(db: AsyncSession, request_id: UUID | None) -> str | None:
    """The binding kind of the approval a grant was issued under."""
    if request_id is None:
        return None
    kind = await db.scalar(
        select(IntegrationGrantRequest.binding_kind).where(
            IntegrationGrantRequest.id == request_id
        )
    )
    return None if kind is None else str(kind)


def run_project(context: IntegrationContext, run: Any) -> UUID | None:
    """The project ``context`` acts on for ``run``.

    A project or run grant names it. An account pairing grant names none, so
    the run's own project is used; authorize_external_submission checks it
    against the account rule on every call. A workspace grant never runs Codex.
    """
    if context.binding_kind == "account":
        project: UUID | None = run.project_id
        return project
    if context.binding_kind == "project":
        return context.project_id
    raise IntegrationAccessDenied()
```

**Step 4: Add `consent_kind` to `mint_integration_grant`** (`backend/src/services/integrations/context.py`). Add the keyword, the account check before the consent query, and replace the consent query's two project/workspace predicates with the kind-dependent list. Keep PR 1's default precedence for `consent_kind=None` (shown here as PR 1's existing predicates; if PR 1 already rewrote them, keep its version in the `else` arm):

```python
async def mint_integration_grant(
    db: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
    project_id: UUID | None = None,
    workspace_id: UUID | None = None,
    scopes: frozenset[str],
    thread_id: UUID | None = None,
    run_id: UUID | None = None,
    device_id: UUID | None = None,
    consent_kind: Literal["project", "account"] | None = None,
) -> IssuedGrant:
    """Trusted internal API. External issuance must use the consent exchange.

    ``consent_kind`` names the approval a device-bound grant is minted under.
    ``"account"`` mints a project-kind grant (a Local Codex run's) under the
    device's account approval, only while the project is in the account;
    ``"project"`` only under a project approval for this project. None keeps
    the default precedence.
    """
    # ... existing check_scopes(...) and validate_binding(...) unchanged ...
    if consent_kind == "account":
        if device_id is None or project_id is None or workspace_id is not None:
            raise IntegrationAccessDenied()
        in_account = await db.scalar(
            select(Collection.id).where(
                Collection.id == project_id,
                account_scope_condition(user_id, organization_id),
            )
        )
        if in_account is None:
            raise IntegrationAccessDenied()
    request_id = None
    if device_id is not None:
        consents = (
            await db.scalars(
                select(IntegrationGrantRequest)
                .where(
                    IntegrationGrantRequest.user_id == user_id,
                    IntegrationGrantRequest.organization_id == organization_id,
                    IntegrationGrantRequest.device_id == device_id,
                    IntegrationGrantRequest.status == "consumed",
                    IntegrationGrantRequest.consent_revoked_at.is_(None),
                    IntegrationGrantRequest.is_deleted.is_(False),
                    *(
                        [IntegrationGrantRequest.binding_kind == "account"]
                        if consent_kind == "account"
                        else [
                            IntegrationGrantRequest.project_id == project_id,
                            IntegrationGrantRequest.workspace_id == workspace_id,
                        ]
                    ),
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        # ... the matching_consents / DeviceBoundToAnotherChat / len != 1 logic unchanged ...
```

`Literal` comes from `typing` (PR 1 imports it for `BindingKind`); `Collection` is already imported.

**Step 5: Run, expect pass.** Same command as step 2. Expected: `3 passed`.

**Step 6: Commit.**

```sh
git add backend/src/services/harness/account.py backend/src/services/integrations/context.py \
  backend/tests/unit/services/harness/test_account_runs.py
git commit -m "feat(harness): mint a run grant under the device's account approval"
```

---

### Task 2: `create_chat_context` accepts account folders

**Files:**
- Modify: `backend/src/api/agent/harness_streaming.py:33-84` (`create_chat_context`)
- Possibly modify: `backend/src/services/integrations/context.py` `_validate_grant` (preview 498–538), only per step 4
- Test: append to `backend/tests/unit/services/harness/test_account_runs.py`

**Step 1: Append the tests.**

```python
async def test_an_account_folder_serves_chats_in_every_project_of_the_account(
    db: AsyncSession, account: IntegrationContext
) -> None:
    for thread_id, project_id in ((THREAD, PROJECT), (THREAD_B, PROJECT_B)):
        run_context = await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, thread_id),
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
        )
        assert run_context.binding_kind == "project"
        assert run_context.project_id == project_id
        grant = await db.get(IntegrationGrant, run_context.grant_id)
        assert grant is not None and grant.request_id == account.consent_id
        assert grant.thread_id == thread_id and grant.device_id == ACCOUNT_DEVICE


# Mutation MA1 (Task 7): delete `or not await project_in_account(...)` in
# harness_streaming._run_consent_kind; this test must fail.
async def test_an_account_folder_refuses_a_project_outside_the_account(
    db: AsyncSession, account: IntegrationContext
) -> None:
    await leave_account(db)
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD_B),
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
        )


async def test_an_account_folder_needs_a_live_account_approval_on_its_device(
    db: AsyncSession, context: IntegrationContext, account: IntegrationContext
) -> None:
    # DEVICE holds a project approval only; give it an account folder anyway.
    loose = uuid4()
    db.add(WorkspaceBinding(device_id=DEVICE, workspace_id=loose, project_id=None, label="x"))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD),
            device_id=DEVICE,
            workspace_id=loose,
        )
    # A revoked account approval no longer serves its folders.
    from src.models.integration_grant import IntegrationGrantRequest

    await db.execute(
        update(IntegrationGrantRequest)
        .where(IntegrationGrantRequest.id == account.consent_id)
        .values(consent_revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD),
            device_id=ACCOUNT_DEVICE,
            workspace_id=FOLDER_A,
        )


async def test_a_project_folder_still_serves_only_its_own_project(
    db: AsyncSession, context: IntegrationContext
) -> None:
    # LOCAL on DEVICE is tied to PROJECT; THREAD_B is in PROJECT_B.
    with pytest.raises(IntegrationAccessDenied):
        await create_chat_context(
            db,
            current_user=OWNER,
            thread=await db.get(Thread, THREAD_B),
            device_id=DEVICE,
            workspace_id=LOCAL,
        )
    ok = await create_chat_context(
        db,
        current_user=OWNER,
        thread=await db.get(Thread, THREAD),
        device_id=DEVICE,
        workspace_id=LOCAL,
    )
    assert ok.project_id == PROJECT
```

**Step 2: Run, expect failure.**

```sh
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q --no-cov \
  backend/tests/unit/services/harness/test_account_runs.py -k "folder"
```

Expected: `test_an_account_folder_serves_chats...` fails with `ValueError: The selected workspace is not bound to this chat project`; `test_a_project_folder_still_serves_only_its_own_project` fails with the same `ValueError` (not `IntegrationAccessDenied`).

**Step 3: Implement.** Replace `create_chat_context` (lines 33–84) with:

```python
async def _run_consent_kind(
    db: Any,
    *,
    current_user: Any,
    thread: Any,
    device_id: UUID,
    workspace_id: UUID,
) -> Literal["project", "account"]:
    """Which approval a chat's Codex run on this folder is minted under.

    A folder tied to the chat's project uses that project's approval. An
    account folder (no project) serves any chat whose project the device's
    account approval covers on this call. Every refusal is an opaque
    IntegrationAccessDenied.
    """
    binding = await db.scalar(
        select(WorkspaceBinding).where(
            WorkspaceBinding.device_id == device_id,
            WorkspaceBinding.workspace_id == workspace_id,
            WorkspaceBinding.is_deleted.is_(False),
        )
    )
    if binding is None:
        raise IntegrationAccessDenied()
    if binding.project_id is None:
        consent = await live_account_consent(
            db,
            user_id=current_user.id,
            organization_id=current_user.organization_id,
            device_id=device_id,
        )
        if consent is None or not await project_in_account(
            db,
            user_id=current_user.id,
            organization_id=current_user.organization_id,
            project_id=thread.source_project_id,
        ):
            raise IntegrationAccessDenied()
        return "account"
    if binding.project_id != thread.source_project_id:
        raise IntegrationAccessDenied()
    return "project"


async def create_chat_context(
    db: Any,
    *,
    current_user: Any,
    thread: Any,
    device_id: UUID,
    workspace_id: UUID,
) -> IntegrationContext:
    """Mint a short-lived run-authority from prior browser-approved consent.

    The returned token is intentionally discarded. It is never returned to the
    browser; only its database identity is attached to the accepted run.
    """
    from src.core.config import settings

    if not settings.HARNESS_BRIDGE_ENABLED:
        raise ValueError("Local harness execution is disabled by server policy")
    if thread is None or thread.source_project_id is None:
        raise ValueError("Codex runs require a project-bound NOUS chat thread")
    if current_user.organization_id is None:
        raise ValueError("Codex runs require an organization-backed account")
    kind = await _run_consent_kind(
        db,
        current_user=current_user,
        thread=thread,
        device_id=device_id,
        workspace_id=workspace_id,
    )
    issued = await mint_integration_grant(
        db,
        user_id=current_user.id,
        organization_id=current_user.organization_id,
        project_id=thread.source_project_id,
        scopes=frozenset({"harness:execute"}),
        thread_id=thread.id,
        device_id=device_id,
        consent_kind=kind,
    )
    # ... the existing re-resolve and IntegrationContext(...) return, unchanged ...
```

Imports: `from typing import Any, AsyncIterator, Literal`; `from src.services.harness.account import live_account_consent, project_in_account`; `from src.services.integrations.context import IntegrationAccessDenied, mint_integration_grant`.

**Step 4: Run; if the first test now fails inside `accept_submission`/`_validate_grant` (not in this task's code), PR 1 left out G4.** Then, in `_validate_grant`, replace the consent lookup's `project_id == grant.project_id, workspace_id == grant.workspace_id` predicates with a kind-aware pair and re-check the account for a project grant under an account approval:

```python
                or_(
                    and_(
                        IntegrationGrantRequest.project_id == grant.project_id,
                        IntegrationGrantRequest.workspace_id == grant.workspace_id,
                    ),
                    and_(
                        IntegrationGrantRequest.binding_kind == "account",
                        literal(grant.binding_kind in ("project", "account")),
                    ),
                ),
```

followed, after `consent` is loaded, by:

```python
        if (
            consent is not None
            and consent.binding_kind == "account"
            and grant.binding_kind == "project"
            and await db.scalar(
                select(Collection.id).where(
                    Collection.id == grant.project_id,
                    account_scope_condition(grant.user_id, grant.organization_id),
                )
            )
            is None
        ):
            raise IntegrationAccessDenied()
```

(`literal` from `sqlalchemy`.) Skip this step when PR 1 already does it.

**Step 5: Run, expect pass.** Same command as step 2 without `-k`. Expected: `7 passed`.

**Step 6: Commit.**

```sh
git add backend/src/api/agent/harness_streaming.py backend/tests/unit/services/harness/test_account_runs.py
# plus backend/src/services/integrations/context.py only if step 4 applied
git commit -m "feat(harness): start Codex in an account folder from any chat in the account"
```

---

### Task 3: Run authorization under an account approval (the single account check)

**Files:**
- Modify: `backend/src/services/harness/runs.py:41-85` (`authorize_external_submission`)
- Modify: `backend/src/services/harness/delivery.py:49-57` (`grant_context`), `:60-89` (`_authorize`)
- Test: append to `test_account_runs.py`

**Step 1: Append the tests.**

```python
async def _run_and_session(db: AsyncSession, run_id: str) -> tuple[Any, HarnessSession]:
    run = await db.get(AgentRun, run_id, populate_existing=True)
    session = await db.scalar(
        select(HarnessSession).where(HarnessSession.run_id == run_id)
    )
    assert run is not None and session is not None
    return run, session


async def test_an_account_folder_run_is_bound_to_its_chats_project(
    db: AsyncSession, account: IntegrationContext
) -> None:
    run_id = await start(db, THREAD_B, FOLDER_A)
    run, session = await _run_and_session(db, run_id)
    assert run.project_id == PROJECT_B and run.execution_provider == "codex"
    grant = await db.get(IntegrationGrant, session.grant_id)
    assert grant is not None and grant.binding_kind == "project"
    assert grant.request_id == account.consent_id


async def test_the_account_pairing_grant_authorizes_each_run_by_its_project(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import _authorize

    for thread_id, folder in ((THREAD, FOLDER_A), (THREAD_B, FOLDER_B)):
        run, session = await _run_and_session(db, await start(db, thread_id, folder))
        granted = await _authorize(db, account, run, session)
        assert granted.id == account.grant_id


# Mutation MA3 (Task 7): delete the `if under_account and not await
# project_in_account(...)` raise in authorize_external_submission; this test
# and test_a_run_whose_project_leaves_the_account_is_refused_at_the_next_command
# must fail.
async def test_an_account_grant_refuses_a_run_whose_project_left_the_account(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.account import ProjectOutsideAccount
    from src.services.harness.delivery import _authorize

    run, session = await _run_and_session(db, await start(db, THREAD_B, FOLDER_A))
    await leave_account(db)
    with pytest.raises(ProjectOutsideAccount):
        await _authorize(db, account, run, session)


async def test_a_project_grant_cannot_authorize_another_projects_account_run(
    db: AsyncSession, context: IntegrationContext, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import _authorize

    run, session = await _run_and_session(db, await start(db, THREAD_B, FOLDER_A))
    with pytest.raises(IntegrationAccessDenied):
        await _authorize(db, context, run, session)


# Mutation MA4 (Task 7): add `WorkspaceBinding.project_id.is_(None)` to the
# folder predicates unconditionally (drop `if under_account`); this test must fail.
async def test_a_project_approval_never_runs_in_an_account_folder(
    db: AsyncSession, context: IntegrationContext
) -> None:
    from tests.unit.services.harness.test_runs import accept, request

    loose = uuid4()
    db.add(WorkspaceBinding(device_id=DEVICE, workspace_id=loose, project_id=None, label="x"))
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await accept(db, context, request().model_copy(update={"workspace_id": loose}))
```

**Step 2: Run, expect failure.**

```sh
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q --no-cov \
  backend/tests/unit/services/harness/test_account_runs.py -k "authorize or bound or approval_never"
```

Expected: `test_an_account_folder_run_is_bound...` fails in `accept_submission` with `IntegrationAccessDenied` (the folder query requires `project_id == context.project_id`); the two `_authorize(account, ...)` tests fail with `IntegrationAccessDenied` (`run.project_id != context.project_id`, which is `None`).

**Step 3: Implement `authorize_external_submission`** (`runs.py:41-85`):

```python
async def authorize_external_submission(
    db: AsyncSession,
    *,
    context: IntegrationContext,
    device_id: UUID,
    workspace_id: UUID,
    thread_id: UUID,
    project_id: UUID | None = None,
) -> None:
    """Recheck authority even for idempotent acceptance replays; no writes.

    ``project_id`` is the run's project. A project or run grant must name the
    same one (callers may leave it out at acceptance). An account pairing
    grant names none: the caller passes the run's project. Under an account
    approval (its pairing grant, or a run grant minted from it) the project
    must be in the account on this call, and an account folder serves it.
    """
    grant = await db.scalar(
        select(IntegrationGrant)
        .where(IntegrationGrant.id == context.grant_id)
        .execution_options(populate_existing=True)
    )
    grant = await _validate_grant(db, grant)
    if (
        grant.user_id != context.user_id
        or grant.organization_id != context.organization_id
        or grant.binding_kind != context.binding_kind
        or grant.project_id != context.project_id
        or grant.thread_id != context.thread_id
        or grant.run_id != (str(context.run_id) if context.run_id else None)
        or grant.device_id != device_id
        or "harness:execute" not in grant.scopes
    ):
        raise IntegrationAccessDenied()
    if grant.binding_kind == "account":
        target = project_id
    elif grant.binding_kind == "project" and project_id in (None, grant.project_id):
        target = grant.project_id
    else:
        raise IntegrationAccessDenied()
    if context.thread_id is not None and context.thread_id != thread_id:
        raise IntegrationAccessDenied()
    under_account = await consent_kind(db, grant.request_id) == "account"
    if grant.binding_kind == "account" and not under_account:
        raise IntegrationAccessDenied()
    if under_account and not await project_in_account(
        db,
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=target,
    ):
        raise ProjectOutsideAccount()
    await validate_binding(
        db,
        user_id=context.user_id,
        organization_id=context.organization_id,
        project_id=target,
        thread_id=thread_id,
        device_id=device_id,
    )
    folders = [WorkspaceBinding.project_id == target]
    if under_account:
        folders.append(WorkspaceBinding.project_id.is_(None))
    binding = await db.scalar(
        select(WorkspaceBinding).where(
            WorkspaceBinding.device_id == device_id,
            WorkspaceBinding.workspace_id == workspace_id,
            or_(*folders),
            WorkspaceBinding.is_deleted.is_(False),
        )
    )
    if binding is None:
        raise IntegrationAccessDenied()
```

Imports in `runs.py`: `from sqlalchemy import or_, select`; `from src.services.harness.account import ProjectOutsideAccount, consent_kind, project_in_account`.

**Step 4: Implement `grant_context` and `_authorize`** (`delivery.py:49-89`):

```python
def grant_context(grant: IntegrationGrant) -> IntegrationContext:
    return IntegrationContext(
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
        binding_kind=grant.binding_kind,
        thread_id=grant.thread_id,
        run_id=UUID(grant.run_id) if grant.run_id else None,
        grant_id=grant.id,
    )


async def _authorize(
    db: AsyncSession, context: IntegrationContext, run: Any, session: HarnessSession
) -> IntegrationGrant:
    if (
        run.execution_provider != "codex"
        or run.user_id != context.user_id
        or run.organization_id != context.organization_id
        or (context.run_id and str(context.run_id) != run.job_id)
    ):
        raise IntegrationAccessDenied()
    # An account pairing grant (the bridge's, after lease_commands swapped it
    # in) names no project: the run's own is used, and
    # authorize_external_submission checks it against the account.
    project_id = run_project(context, run)
    if project_id is None or run.project_id != project_id:
        raise IntegrationAccessDenied()
    await authorize_external_submission(
        db,
        context=context,
        device_id=session.device_id,
        workspace_id=session.workspace_id,
        thread_id=run.thread_id,
        project_id=project_id,
    )
    # ... the existing grant / original / request_id lineage check unchanged ...
```

Import: `from src.services.harness.account import run_project`.

**Step 5: Run, expect pass.** The step 2 command, then the whole harness unit suite (regressions):

```sh
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q --no-cov \
  backend/tests/unit/services/harness backend/tests/unit/services/integrations
```

Expected: all pass; `test_account_runs.py` reports `12 passed`.

**Step 6: Commit.**

```sh
git add backend/src/services/harness/runs.py backend/src/services/harness/delivery.py \
  backend/tests/unit/services/harness/test_account_runs.py
git commit -m "feat(harness): authorize account runs by their own project, rechecked against the account"
```

---

### Task 4: Leasing for an account pairing, `projectId` on the start command, the bridge socket

**Files:**
- Modify: `backend/src/services/harness/delivery.py:235-365` (`lease_commands`; filter 245–269, `BridgeCommand(...)` 350–360), `:664-710` (`lease_runs`; filter 674–697)
- Modify: `backend/src/schemas/harness.py:73-81` (`BridgeCommand`)
- Modify: `backend/src/api/harness.py:103-113` (`_grant_context`), `:168-174` (frame)
- Modify: `backend/tests/unit/services/harness/test_delivery.py:96-109` (`event()` helper: exclude `projectId`)
- Test: append to `test_account_runs.py`; append to `backend/tests/unit/api/test_harness_workspace_guard.py`

**Step 1: Append the tests.**

To `test_account_runs.py`:

```python
async def test_one_account_bridge_leases_runs_in_two_projects(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import dispatch_pending, lease_commands, lease_runs

    run_a = await start(db, THREAD, FOLDER_A)
    run_b = await start(db, THREAD_B, FOLDER_B)
    assert await dispatch_pending(db) == 2
    commands = await lease_commands(db, account, ACCOUNT_DEVICE)
    assert {(str(c.runId), c.projectId) for c in commands} == {
        (run_a, PROJECT),
        (run_b, PROJECT_B),
    }
    assert set(await lease_runs(db, account, ACCOUNT_DEVICE)) == {run_a, run_b}
    # The long-run swap: both sessions now hold the bridge's pairing grant.
    sessions = (await db.scalars(select(HarnessSession))).all()
    assert {s.grant_id for s in sessions} == {account.grant_id}


# Mutation MA10 (Task 7): replace `context.binding_kind == "account"` in the
# BridgeCommand(projectId=...) expression with `True`; this test must fail.
async def test_a_start_command_names_its_project_only_under_an_account_pairing(
    db: AsyncSession, context: IntegrationContext, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import dispatch_pending, lease_commands
    from tests.unit.services.harness.test_runs import accept

    await accept(db, context)  # a project pairing's run on DEVICE / LOCAL
    await start(db, THREAD_B, FOLDER_A)
    assert await dispatch_pending(db) == 2
    (project_command,) = await lease_commands(db, context, DEVICE)
    assert project_command.projectId is None
    assert "projectId" not in project_command.wire()  # old bridges refuse the key
    (account_command,) = await lease_commands(db, account, ACCOUNT_DEVICE)
    assert account_command.wire()["projectId"] == str(PROJECT_B)


async def test_a_run_whose_project_leaves_the_account_is_refused_at_the_next_command(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import (
        dispatch_pending,
        ingest_bridge_event,
        lease_commands,
        lease_runs,
    )

    run_a = await start(db, THREAD, FOLDER_A)
    run_b = await start(db, THREAD_B, FOLDER_B)
    assert await dispatch_pending(db) == 2
    commands = {str(c.runId): c for c in await lease_commands(db, account, ACCOUNT_DEVICE)}
    await leave_account(db)
    await db.execute(update(HarnessCommand).values(lease_until=None))
    await db.commit()
    assert [str(c.runId) for c in await lease_commands(db, account, ACCOUNT_DEVICE)] == [run_a]
    assert set(await lease_runs(db, account, ACCOUNT_DEVICE)) == {run_a}
    with pytest.raises(IntegrationAccessDenied):
        await ingest_bridge_event(db, account, event_for(commands[run_b]))
    assert await ingest_bridge_event(db, account, event_for(commands[run_a])) >= 1


async def test_a_project_pairing_still_cannot_lease_another_projects_run(
    db: AsyncSession, context: IntegrationContext
) -> None:
    """Regression: a project pairing reaches its own project's runs only.

    Three independent guards hold it (the lease query's project filter,
    run_project in _authorize and the thread binding in validate_binding), so
    no single-line mutation makes it pass; see Task 7.
    """
    from src.services.harness.delivery import (
        dispatch_pending,
        ingest_bridge_event,
        lease_commands,
        lease_runs,
    )

    # DEVICE also holds a project approval for PROJECT_B and a folder tied to it.
    other_folder = uuid4()
    db.add(
        WorkspaceBinding(
            device_id=DEVICE, workspace_id=other_folder, project_id=PROJECT_B, label="b"
        )
    )
    await db.commit()
    pairing_b = await pair_project(db, PROJECT_B, DEVICE)
    run_b = await start(db, THREAD_B, other_folder, device=DEVICE)
    assert await dispatch_pending(db) == 1
    assert await lease_commands(db, context, DEVICE) == []
    assert await lease_runs(db, context, DEVICE) == {}
    (command,) = await lease_commands(db, pairing_b, DEVICE)
    assert str(command.runId) == run_b and command.projectId is None
    with pytest.raises(IntegrationAccessDenied):
        await ingest_bridge_event(db, context, event_for(command))
```

To `backend/tests/unit/api/test_harness_workspace_guard.py`:

```python
async def test_grant_context_accepts_an_account_grant_and_still_refuses_a_workspace_one(
    transport: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services.integrations.context import IntegrationAccessDenied

    account = IntegrationContext(
        user_id=USER, organization_id=ORG, binding_kind="account", grant_id=GRANT
    )
    monkeypatch.setattr(
        transport, "resolve_integration_context", AsyncMock(return_value=account)
    )
    assert await transport._grant_context(object(), "opaque", {"sub": str(USER)}) is account
    with pytest.raises(IntegrationAccessDenied):
        await transport._grant_context(object(), "opaque", {"sub": str(uuid4())})
    monkeypatch.setattr(
        transport,
        "resolve_integration_context",
        AsyncMock(return_value=_context(workspace=True)),
    )
    with pytest.raises(IntegrationAccessDenied):
        await transport._grant_context(object(), "opaque", {"sub": str(USER)})
```

**Step 2: Run, expect failure.**

```sh
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q --no-cov \
  backend/tests/unit/services/harness/test_account_runs.py backend/tests/unit/api/test_harness_workspace_guard.py \
  -k "lease or start_command or still_cannot or grant_context"
```

Expected: the two-project test fails with `0 != 2` commands-set mismatch (the lease query filters `AgentRun.project_id == None`); `projectId` tests fail with `AttributeError: 'BridgeCommand' object has no attribute 'projectId'`; the socket test fails with `IntegrationAccessDenied` (`context.project_id is None`).

**Step 3: Implement the schema** (`schemas/harness.py:73-81`):

```python
class BridgeCommand(Envelope):
    expiresAt: datetime
    body: Annotated[Start | Interrupt | Respond, Field(discriminator="kind")]
    # The run's project, on a start command leased under an account pairing,
    # so the bridge pins the run's NOUS tools to it. Never on an event.
    projectId: UUID | None = None

    @model_validator(mode="after")
    def utc_expiry(self) -> "BridgeCommand":
        if self.expiresAt.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError("expiresAt must be UTC")
        if self.projectId is not None and not isinstance(self.body, Start):
            raise ValueError("projectId names a start command's project only")
        return self

    def wire(self) -> dict[str, Any]:
        """The frame sent to the bridge. Bridges before account connections
        refuse any unknown key (parseCommand), so an absent project is left
        out rather than sent as null."""
        value = self.model_dump(mode="json")
        if value.get("projectId") is None:
            value.pop("projectId", None)
        return value
```

**Step 4: Implement the lease scope** (`delivery.py`). Add above `lease_commands`:

```python
def _lease_scope(context: IntegrationContext) -> list[Any]:
    """Which of the device's locked runs this grant may lease (the caller
    filters the device). A project grant leases its own project's runs; an
    account pairing grant leases runs in any project the account rule admits
    on this call. Each run is authorized again in _authorize."""
    if context.binding_kind == "account":
        project = AgentRun.project_id.in_(
            account_projects(context.user_id, context.organization_id)
        )
    else:
        project = AgentRun.project_id == context.project_id
    return [
        AgentRun.user_id == context.user_id,
        AgentRun.organization_id == context.organization_id,
        project,
        *([] if context.run_id is None else [AgentRun.job_id == str(context.run_id)]),
        *(
            []
            if context.thread_id is None
            else [AgentRun.thread_id == context.thread_id]
        ),
    ]
```

In `lease_commands` replace the `.where(...)` body (lines 249–265) with:

```python
                .where(
                    HarnessSession.device_id == device_id,
                    HarnessSession.workspace_locked.is_(True),
                    *_lease_scope(context),
                )
```

and the same in `lease_runs` (lines 678–694). In `lease_commands`' `BridgeCommand(...)` (lines 351–359) add:

```python
                        projectId=(
                            run.project_id
                            if context.binding_kind == "account"
                            and command.kind == "start"
                            else None
                        ),
```

Import: `from src.services.harness.account import account_projects, run_project`.

**Step 5: Implement the socket** (`api/harness.py`). `_grant_context` (103–113):

```python
    context = await resolve_integration_context(
        db, token, required_scope="harness:execute"
    )
    # A run needs one Collection: a project grant names it; an account
    # pairing grant takes each run's own (delivery._authorize). A workspace
    # grant never runs Codex.
    if context.binding_kind == "workspace" or str(context.user_id) != str(
        identity.get("sub")
    ):
        raise IntegrationAccessDenied()
    return context
```

and in the poll branch replace `command.model_dump(mode="json")` (line 171) with `command.wire()`.

**Step 6: Update the existing helper** `backend/tests/unit/services/harness/test_delivery.py:97`:

```python
    values = command.model_dump(exclude={"expiresAt", "body", "projectId"})
```

**Step 7: Run, expect pass.** Step 2's command without `-k`, then the full harness + api guard suites:

```sh
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q --no-cov \
  backend/tests/unit/services/harness backend/tests/unit/api/test_harness_workspace_guard.py
```

Expected: all pass (`test_account_runs.py` `16 passed`).

**Step 8: Commit.**

```sh
git add backend/src/schemas/harness.py backend/src/services/harness/delivery.py backend/src/api/harness.py \
  backend/tests/unit/services/harness/test_account_runs.py backend/tests/unit/services/harness/test_delivery.py \
  backend/tests/unit/api/test_harness_workspace_guard.py
git commit -m "feat(harness): one account bridge leases runs across the account; start names its project"
```

---

### Task 5: The long-run swap everywhere the session's grant is read

**Files:**
- Modify: `backend/src/services/harness/delivery.py:109-232` (`dispatch_pending`: `terminalize` 114–142, denial 191–199)
- Modify: `backend/src/services/harness/approvals.py:398-507` (`resolve_native_request`, checks 414–421), `:610-645` (`native_request_context`)
- Modify: `backend/src/api/agent/harness_streaming.py:87-113` (`context_for_accepted_run`)
- `backend/src/services/harness/prompt_context.py` needs no change (it uses `grant_context` + `_authorize`); covered by a test here.
- Test: append to `test_account_runs.py`

**Step 1: Append the tests.**

```python
def native_request_event(command: Any, seq: int) -> BridgeEvent:
    return event_for(
        command,
        seq,
        {
            "kind": "request",
            "sessionId": "native-session",
            "turnId": "native-turn",
            "itemId": "item-1",
            "requestId": 7,
            "approvalId": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "native-session",
                "turnId": "native-turn",
                "itemId": "item-1",
                "approvalId": "approval-1",
                "command": "pytest -q",
                "cwd": "/workspace",
            },
        },
    )


def running_event(command: Any, seq: int = 1) -> BridgeEvent:
    return event_for(
        command,
        seq,
        {
            "kind": "observation",
            "state": "running",
            "sessionId": "native-session",
            "turnId": "native-turn",
        },
    )


async def expire_run_grants(db: AsyncSession) -> None:
    """Run grants are chat-bound and last 15 minutes; pairing grants are not."""
    await db.execute(
        update(IntegrationGrant)
        .where(IntegrationGrant.thread_id.is_not(None))
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db.commit()


async def test_dispatch_after_the_bridge_polled_still_starts_the_run(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import dispatch_pending, lease_commands

    run_id = await start(db, THREAD_B, FOLDER_A)
    # The bridge polls between acceptance and dispatch: the swap comes first.
    assert await lease_commands(db, account, ACCOUNT_DEVICE) == []
    _, session = await _run_and_session(db, run_id)
    assert session.grant_id == account.grant_id
    assert await dispatch_pending(db) == 1
    run, _ = await _run_and_session(db, run_id)
    assert run.status == "queued"


# Mutation MA5 (Task 7): delete `session.grant_id = grant.id` in
# lease_commands; this test must fail.
async def test_a_long_run_outlives_its_run_grant_and_resolves_an_approval_card(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.approvals import (
        NativeDecision,
        ingest_native_request,
        native_request_context,
        resolve_native_request,
    )
    from src.services.harness.delivery import (
        dispatch_pending,
        ingest_bridge_event,
        lease_commands,
    )
    from src.services.integrations.context import renew_grant

    await start(db, THREAD_B, FOLDER_A)
    assert await dispatch_pending(db) == 1
    (first,) = await lease_commands(db, account, ACCOUNT_DEVICE)
    await expire_run_grants(db)
    issued = await renew_grant(db, OWNER, account.grant_id)
    renewed = await resolve_integration_context(
        db, issued.token, required_scope="harness:execute"
    )
    # A never-acknowledged start is re-leased: its prompt is rebuilt and
    # checked under the session's (pairing) grant, not the expired run grant.
    await db.execute(update(HarnessCommand).values(lease_until=None))
    await db.commit()
    (again,) = await lease_commands(db, renewed, ACCOUNT_DEVICE)
    assert again.commandId == first.commandId and again.projectId == PROJECT_B
    await ingest_bridge_event(db, renewed, running_event(again, 1))
    request_id, _ = await ingest_native_request(db, renewed, native_request_event(again, 2))
    decision_context = await native_request_context(db, request_id, USER)
    assert decision_context.binding_kind == "account"
    assert decision_context.grant_id == renewed.grant_id
    await resolve_native_request(db, decision_context, request_id, NativeDecision.allow())
    responses = [
        c for c in await lease_commands(db, renewed, ACCOUNT_DEVICE) if c.body.kind == "respond"
    ]
    assert len(responses) == 1 and responses[0].projectId is None


# Mutations MA7 / MA8 (Task 7): delete `request.project_id != run.project_id`
# in native_request_context (first assertion fails) or in
# resolve_native_request (second assertion fails).
async def test_a_native_request_recorded_for_another_project_is_refused(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.approvals import (
        NativeDecision,
        ingest_native_request,
        native_request_context,
        resolve_native_request,
    )
    from src.services.harness.delivery import (
        dispatch_pending,
        ingest_bridge_event,
        lease_commands,
    )

    await start(db, THREAD_B, FOLDER_A)
    assert await dispatch_pending(db) == 1
    (command,) = await lease_commands(db, account, ACCOUNT_DEVICE)
    await ingest_bridge_event(db, account, running_event(command, 1))
    request_id, _ = await ingest_native_request(db, account, native_request_event(command, 2))
    decision_context = await native_request_context(db, request_id, USER)
    await db.execute(
        update(HarnessNativeRequest)
        .where(HarnessNativeRequest.id == request_id)
        .values(project_id=PROJECT)
    )
    await db.commit()
    with pytest.raises(IntegrationAccessDenied):
        await native_request_context(db, request_id, USER)
    with pytest.raises(IntegrationAccessDenied):
        await resolve_native_request(db, decision_context, request_id, NativeDecision.allow())


# Mutation MA6 (Task 7): delete `original.request_id != grant.request_id` from
# _authorize's lineage check; this test must fail.
async def test_a_second_account_approval_on_the_device_cannot_take_over_a_run(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import (
        dispatch_pending,
        ingest_bridge_event,
        lease_commands,
    )

    await start(db, THREAD_B, FOLDER_A)
    assert await dispatch_pending(db) == 1
    (command,) = await lease_commands(db, account, ACCOUNT_DEVICE)
    other = await pair_account(db)  # a second consent lineage on the same device
    assert other.consent_id != account.consent_id
    assert await lease_commands(db, other, ACCOUNT_DEVICE) == []
    with pytest.raises(IntegrationAccessDenied):
        await ingest_bridge_event(db, other, event_for(command))


async def test_a_run_that_cannot_start_says_its_project_left_the_account(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.agent.run_event_store import read_events
    from src.services.harness.account import PROJECT_UNREACHABLE
    from src.services.harness.delivery import dispatch_pending

    run_id = await start(db, THREAD_B, FOLDER_A)
    await leave_account(db)
    assert await dispatch_pending(db) == 0
    run, session = await _run_and_session(db, run_id)
    assert run.status == "failed" and not session.workspace_locked
    events = await read_events(db, run_id, organization_id=ORG, user_id=USER)
    assert events[-1].payload["message"] == PROJECT_UNREACHABLE


async def test_stream_resume_after_the_swap_reads_as_the_owner(
    db: AsyncSession, account: IntegrationContext
) -> None:
    from src.services.harness.delivery import lease_commands

    run_id = await start(db, THREAD_B, FOLDER_A)
    await lease_commands(db, account, ACCOUNT_DEVICE)  # swap
    stream = await context_for_accepted_run(db, run_id=UUID(run_id), current_user=OWNER)
    assert (stream.user_id, stream.organization_id) == (USER, ORG)
    assert stream.grant_id == account.grant_id
```

**Step 2: Run, expect failure.**

```sh
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q --no-cov \
  backend/tests/unit/services/harness/test_account_runs.py \
  -k "dispatch or long_run or native_request or take_over or cannot_start or stream_resume"
```

Expected: `native_request_context` raises `IntegrationAccessDenied` (`grant.project_id != request.project_id`), `context_for_accepted_run` raises `ValidationError` (a context with neither project nor workspace and no account kind), `test_a_run_that_cannot_start...` fails on the message (`Local Codex could not be started.`). The dispatch-after-poll, long-run and takeover tests pass already after Task 4 (they guard against regressions; keep them).

**Step 3: `dispatch_pending`** (`delivery.py`). Give `terminalize` a message and name a project that left the account:

```python
_DISPATCH_FAILED = "Local Codex could not be started."


async def _start_failure(db: AsyncSession, run: Any, session: HarnessSession) -> str:
    """Why a run cannot start: name a project that left the device's account."""
    grant = await db.get(IntegrationGrant, session.grant_id, populate_existing=True)
    if (
        grant is not None
        and await consent_kind(db, grant.request_id) == "account"
        and not await project_in_account(
            db,
            user_id=run.user_id,
            organization_id=run.organization_id,
            project_id=run.project_id,
        )
    ):
        return PROJECT_UNREACHABLE
    return _DISPATCH_FAILED
```

In `terminalize` add `message: str = _DISPATCH_FAILED` after `cancelled` and drop the local `message = "Local Codex could not be started."`. Change the authorization `except` (lines 197–199) to:

```python
            except IntegrationAccessDenied:
                await terminalize(
                    outbox,
                    run,
                    cancelled=False,
                    message=await _start_failure(db, run, session),
                )
                continue
```

Imports: `from src.services.harness.account import PROJECT_UNREACHABLE, account_projects, consent_kind, project_in_account, run_project`.

**Step 4: Approvals** (`approvals.py`). `native_request_context` (610–645), replace the grant check and the return:

```python
    grant = await db.get(IntegrationGrant, session.grant_id, populate_existing=True)
    if (
        grant is None
        or grant.user_id != actor_id
        or grant.organization_id != request.organization_id
    ):
        raise IntegrationAccessDenied()
    if grant.binding_kind == "account":
        # The bridge's account pairing grant after the long-run swap names no
        # project: the request must name its run's own project.
        if request.project_id is None or request.project_id != run.project_id:
            raise IntegrationAccessDenied()
    elif grant.project_id != request.project_id:
        raise IntegrationAccessDenied()
    return grant_context(grant)
```

`resolve_native_request` (414–421), replace the first identity check, and add the run check right after `run`/`session` load (after line 436):

```python
        if (
            request.actor_id != context.user_id
            or request.organization_id != context.organization_id
            or (
                context.project_id is not None
                if context.binding_kind == "account"
                else request.project_id != context.project_id
            )
            or request.run_id
            != (str(context.run_id) if context.run_id else request.run_id)
        ):
            raise IntegrationAccessDenied()
        # ... consumed/expired check, run and session loads unchanged ...
        if context.binding_kind == "account" and request.project_id != run.project_id:
            raise IntegrationAccessDenied()
```

Import: `from src.services.harness.delivery import TERMINAL, _authorize, grant_context, now`.

**Step 5: Stream authority** (`harness_streaming.py:105-113`):

```python
    _run, _session, grant = result
    if grant.binding_kind == "account":
        # After lease_commands' long-run swap the session holds the bridge's
        # account pairing grant. The stream reads only the owner's own events
        # (by user and organization), which that grant's context carries.
        from src.services.harness.delivery import grant_context

        return grant_context(grant)
    return IntegrationContext(
        # ... unchanged ...
    )
```

**Step 6: Run, expect pass.** Step 2's command without `-k`; then all harness, approvals, streaming and integrations unit suites:

```sh
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q --no-cov \
  backend/tests/unit/services/harness backend/tests/unit/api/agent/test_harness_streaming.py \
  backend/tests/unit/api/test_harness_workspace_guard.py backend/tests/unit/services/integrations \
  backend/tests/unit/api/test_agent_streaming_error_categories.py
```

Expected: all pass (`test_account_runs.py` `22 passed`).

**Step 7: Commit.**

```sh
git add backend/src/services/harness/delivery.py backend/src/services/harness/approvals.py \
  backend/src/api/agent/harness_streaming.py backend/tests/unit/services/harness/test_account_runs.py
git commit -m "feat(harness): keep account runs authorized across the long-run grant swap"
```

---

### Task 6: PostgreSQL evidence

**Files:**
- Create: `backend/tests/integration/test_harness_account_postgres.py`

**Step 1: Start the local PostgreSQL (Gate 0 of `docs/testing/harness-live-proof.md`).**

```sh
PGDIR=$(mktemp -d)   # under <scratch> if preferred
initdb -D "$PGDIR/data" -U postgres --auth=trust -E UTF8 --locale=C
pg_ctl -D "$PGDIR/data" -o "-p 55432 -k $PGDIR" -l "$PGDIR/pg.log" start
psql -h 127.0.0.1 -p 55432 -U postgres -c 'create database orch'
```

**Step 2: Write the test file.**

```python
r"""PostgreSQL evidence for Local Codex runs under an account approval.

Account-connection design §3; the SQLite twin, with the mutation notes, is
backend/tests/unit/services/harness/test_account_runs.py. Here the account
rule, the lease query's ``IN (subquery)`` and the long-run swap run on real
PostgreSQL.

Focused command, from the repository root (``PY`` and the throwaway
PostgreSQL come from Gate 0 of ``docs/testing/harness-live-proof.md``)::

    ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch \
    ENVIRONMENT=testing PYTHONPATH=backend "$PY" -m pytest -c backend/pytest.ini -q \
    backend/tests/integration/test_harness_account_postgres.py
"""

import os
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.models.agent_outbox import AgentOutbox
from src.models.agent_run import AgentRun
from src.models.agent_run_event import AgentRunEvent
from src.models.bridge_device import BridgeDevice, WorkspaceBinding
from src.models.chat_message import ChatMessage
from src.models.collection import Collection
from src.models.conversation import Conversation
from src.models.harness_session import (
    HarnessCommand,
    HarnessNativeRequest,
    HarnessReceipt,
    HarnessSession,
)
from src.models.integration_grant import IntegrationGrant, IntegrationGrantRequest
from src.models.organization import Organization
from src.models.thread import Thread
from src.models.user import User, UserRole
from src.models.workspace import Workspace, WorkspaceMember
from src.services.harness.approvals import (
    NativeDecision,
    ingest_native_request,
    native_request_context,
    resolve_native_request,
)
from src.services.harness.delivery import (
    dispatch_pending,
    ingest_bridge_event,
    lease_commands,
    lease_runs,
)
from src.services.integrations.context import (
    IntegrationAccessDenied,
    renew_grant,
    resolve_integration_context,
)
from tests.unit.services.harness.test_account_runs import (
    ACCOUNT_DEVICE,
    CONVERSATION_B,
    FOLDER_A,
    FOLDER_B,
    OWNER,
    PROJECT_B,
    THREAD_B,
    WORKSPACE_B,
    event_for,
    expire_run_grants,
    leave_account,
    native_request_event,
    pair_account,
    pair_project,
    running_event,
    start,
)
from tests.unit.services.harness.test_runs import (
    CONVERSATION,
    DEVICE,
    LOCAL,
    ORG,
    PROJECT,
    THREAD,
    USER,
    WORKSPACE,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


@pytest.fixture
def mock_external_services() -> None:
    """This database-only fixture uses no vector, mail, or model clients."""


@pytest.fixture
async def pg(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    dsn = os.getenv("ORCHESTRATION_TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("ORCHESTRATION_TEST_DATABASE_URL is not configured")
    dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    schema = "harness_account_" + uuid4().hex
    admin = create_async_engine(dsn)
    async with admin.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        dsn, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE agent_runtime_snapshots (id UUID PRIMARY KEY)"
            )
            models: list[Any] = [
                Organization,
                User,
                Workspace,
                WorkspaceMember,
                Collection,
                Conversation,
                Thread,
                ChatMessage,
                AgentRun,
                AgentRunEvent,
                AgentOutbox,
                BridgeDevice,
                WorkspaceBinding,
                IntegrationGrantRequest,
                IntegrationGrant,
                HarnessSession,
                HarnessCommand,
                HarnessReceipt,
                HarnessNativeRequest,
            ]
            for model in models:
                await conn.run_sync(model.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            levels: list[list[Any]] = [
                [Organization(id=ORG, name="org", storage_limit_bytes=1000)],
                [
                    User(
                        id=USER,
                        organization_id=ORG,
                        email="harness@example.test",
                        password_hash="unused",
                        first_name="Test",
                        last_name="User",
                        role=UserRole.USER,
                    )
                ],
                [
                    Workspace(id=WORKSPACE, name="w", owner_id=USER, organization_id=ORG),
                    Workspace(id=WORKSPACE_B, name="w2", owner_id=USER, organization_id=ORG),
                ],
                [
                    Collection(id=PROJECT, workspace_id=WORKSPACE, name="p"),
                    Collection(id=PROJECT_B, workspace_id=WORKSPACE_B, name="p2"),
                    Conversation(
                        id=CONVERSATION, workspace_id=WORKSPACE, title="c", created_by_id=USER
                    ),
                    Conversation(
                        id=CONVERSATION_B,
                        workspace_id=WORKSPACE_B,
                        title="c2",
                        created_by_id=USER,
                    ),
                    BridgeDevice(id=DEVICE, user_id=USER, organization_id=ORG, label="laptop"),
                    BridgeDevice(
                        id=ACCOUNT_DEVICE, user_id=USER, organization_id=ORG, label="desktop"
                    ),
                ],
                [
                    Thread(
                        id=THREAD,
                        conversation_id=CONVERSATION,
                        source_project_id=PROJECT,
                        title="t",
                        created_by_id=USER,
                        message_count=0,
                    ),
                    Thread(
                        id=THREAD_B,
                        conversation_id=CONVERSATION_B,
                        source_project_id=PROJECT_B,
                        title="t2",
                        created_by_id=USER,
                        message_count=0,
                    ),
                    WorkspaceBinding(
                        device_id=DEVICE, workspace_id=LOCAL, project_id=PROJECT, label="repo"
                    ),
                    WorkspaceBinding(
                        device_id=ACCOUNT_DEVICE, workspace_id=FOLDER_A, project_id=None, label="a"
                    ),
                    WorkspaceBinding(
                        device_id=ACCOUNT_DEVICE, workspace_id=FOLDER_B, project_id=None, label="b"
                    ),
                ],
            ]
            for level in levels:
                db.add_all(level)
                await db.flush()
            await db.commit()
        monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
        monkeypatch.setattr(settings, "ACCOUNT_CONNECTIONS_ENABLED", True)
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def test_one_account_bridge_leases_runs_in_two_projects(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    async with pg() as db:
        account = await pair_account(db)
        run_a = await start(db, THREAD, FOLDER_A)
        run_b = await start(db, THREAD_B, FOLDER_B)
        assert await dispatch_pending(db) == 2
    async with pg() as bridge:
        commands = await lease_commands(bridge, account, ACCOUNT_DEVICE)
        assert {(str(c.runId), c.projectId) for c in commands} == {
            (run_a, PROJECT),
            (run_b, PROJECT_B),
        }
        assert set(await lease_runs(bridge, account, ACCOUNT_DEVICE)) == {run_a, run_b}
        for command in commands:
            assert await ingest_bridge_event(bridge, account, event_for(command)) >= 1


async def test_a_project_that_leaves_the_account_is_refused_at_the_next_command(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    async with pg() as db:
        account = await pair_account(db)
        run_a = await start(db, THREAD, FOLDER_A)
        run_b = await start(db, THREAD_B, FOLDER_B)
        assert await dispatch_pending(db) == 2
        commands = {str(c.runId): c for c in await lease_commands(db, account, ACCOUNT_DEVICE)}
        await leave_account(db)
        await db.execute(update(HarnessCommand).values(lease_until=None))
        await db.commit()
    async with pg() as bridge:
        assert [str(c.runId) for c in await lease_commands(bridge, account, ACCOUNT_DEVICE)] == [
            run_a
        ]
        assert set(await lease_runs(bridge, account, ACCOUNT_DEVICE)) == {run_a}
        with pytest.raises(IntegrationAccessDenied):
            await ingest_bridge_event(bridge, account, event_for(commands[run_b]))


async def test_a_long_run_outlives_its_run_grant_and_resolves_an_approval_card(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    async with pg() as db:
        account = await pair_account(db)
        await start(db, THREAD_B, FOLDER_A)
        assert await dispatch_pending(db) == 1
        (first,) = await lease_commands(db, account, ACCOUNT_DEVICE)
        await expire_run_grants(db)
        issued = await renew_grant(db, OWNER, account.grant_id)
        renewed = await resolve_integration_context(
            db, issued.token, required_scope="harness:execute"
        )
        await db.execute(update(HarnessCommand).values(lease_until=None))
        await db.commit()
    async with pg() as bridge:
        (again,) = await lease_commands(bridge, renewed, ACCOUNT_DEVICE)
        assert again.commandId == first.commandId
        await ingest_bridge_event(bridge, renewed, running_event(again, 1))
        request_id, _ = await ingest_native_request(
            bridge, renewed, native_request_event(again, 2)
        )
    async with pg() as browser:
        decision = await native_request_context(browser, request_id, USER)
        await resolve_native_request(browser, decision, request_id, NativeDecision.deny())
    async with pg() as bridge:
        kinds = [c.body.kind for c in await lease_commands(bridge, renewed, ACCOUNT_DEVICE)]
        assert kinds == ["respond"]


async def test_a_project_pairing_still_cannot_lease_another_projects_run(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    async with pg() as db:
        pairing_a = await pair_project(db, PROJECT, DEVICE)
        folder_b = uuid4()
        db.add(
            WorkspaceBinding(device_id=DEVICE, workspace_id=folder_b, project_id=PROJECT_B, label="b")
        )
        await db.commit()
        pairing_b = await pair_project(db, PROJECT_B, DEVICE)
        run_b = await start(db, THREAD_B, folder_b, device=DEVICE)
        assert await dispatch_pending(db) == 1
    async with pg() as bridge:
        assert await lease_commands(bridge, pairing_a, DEVICE) == []
        assert await lease_runs(bridge, pairing_a, DEVICE) == {}
        (command,) = await lease_commands(bridge, pairing_b, DEVICE)
        assert str(command.runId) == run_b
        with pytest.raises(IntegrationAccessDenied):
            await ingest_bridge_event(bridge, pairing_a, event_for(command))


async def test_project_and_account_approvals_on_one_device_each_authorize_their_own_runs(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    """Both approvals live on one device: each folder kind mints under its own
    approval (consent_kind), and each run is leased only by that lineage."""
    async with pg() as db:
        account = await pair_account(db)
        project = await pair_project(db, PROJECT, ACCOUNT_DEVICE)
        tied = uuid4()
        db.add(
            WorkspaceBinding(device_id=ACCOUNT_DEVICE, workspace_id=tied, project_id=PROJECT, label="tied")
        )
        await db.commit()
        via_account = await start(db, THREAD_B, FOLDER_A)
        via_project = await start(db, THREAD, tied)
        grants = {
            s.run_id: (await db.get(IntegrationGrant, s.grant_id)).request_id  # type: ignore[union-attr]
            for s in (await db.scalars(select(HarnessSession))).all()
        }
        assert grants == {via_account: account.consent_id, via_project: project.consent_id}
        assert await dispatch_pending(db) == 2
    async with pg() as bridge:
        assert [str(c.runId) for c in await lease_commands(bridge, account, ACCOUNT_DEVICE)] == [
            via_account
        ]
        assert [str(c.runId) for c in await lease_commands(bridge, project, ACCOUNT_DEVICE)] == [
            via_project
        ]
```

(The fixture is not shared with the SQLite module; the helpers imported from it are plain functions.)

**Step 3: Run, expect pass** (it exercises Tasks 1–5 on PostgreSQL; a failure here that SQLite missed is a real defect: fix the code, not the test):

```sh
ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch ENVIRONMENT=testing \
  PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q \
  backend/tests/integration/test_harness_account_postgres.py \
  backend/tests/integration/test_harness_dispatch_postgres.py \
  backend/tests/integration/test_harness_cancel_delivery_postgres.py
```

Expected: `5 passed` for the new file, and the two existing files still pass (`2 passed`, `2 passed`). Without the variable every test reports `skipped`: that is NOT RUN, never PASS.

**Step 4: Commit.**

```sh
git add backend/tests/integration/test_harness_account_postgres.py
git commit -m "test(harness): PostgreSQL evidence for account runs, the swap and approval lineage"
```

---

### Task 7: Mutation-verify the security tests

**Files:**
- Modify: `backend/tests/unit/services/harness/test_account_runs.py` (module docstring: record date and outcome)

**Step 1: For each row, apply the mutation, run the named test, see it FAIL, restore** (`git diff --stat` must be clean of `backend/src` after each restore):

```sh
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q --no-cov -p no:cacheprovider \
  backend/tests/unit/services/harness/test_account_runs.py -k <test name>
```

| ID | Guard (file) | Mutation | Test that must FAIL |
| --- | --- | --- | --- |
| MA1 | `api/agent/harness_streaming.py` `_run_consent_kind` | delete `or not await project_in_account(...)` | `test_an_account_folder_refuses_a_project_outside_the_account` |
| MA2 | `services/integrations/context.py` `mint_integration_grant` | delete the `if in_account is None: raise` | `test_mint_under_the_account_approval_refuses_a_project_outside_the_account` |
| MA3 | `services/harness/runs.py` `authorize_external_submission` | delete the `if under_account and not await project_in_account(...): raise ProjectOutsideAccount()` | `test_an_account_grant_refuses_a_run_whose_project_left_the_account` and `test_a_run_whose_project_leaves_the_account_is_refused_at_the_next_command` (the ingest assertion; the lease query's SQL filter still hides the run from the lease, which is why the ingest assertion is the detector) |
| MA4 | same function, folder rule | make `folders.append(WorkspaceBinding.project_id.is_(None))` unconditional | `test_a_project_approval_never_runs_in_an_account_folder` |
| MA5 | `services/harness/delivery.py` `lease_commands` | delete `session.grant_id = grant.id` | `test_a_long_run_outlives_its_run_grant_and_resolves_an_approval_card` (the re-leased start is cancelled because its prompt is rechecked under the expired run grant) |
| MA6 | `delivery.py` `_authorize` | delete `original.request_id != grant.request_id` from the lineage condition | `test_a_second_account_approval_on_the_device_cannot_take_over_a_run` |
| MA7 | `services/harness/approvals.py` `native_request_context` | delete `or request.project_id != run.project_id` | `test_a_native_request_recorded_for_another_project_is_refused` |
| MA8 | `approvals.py` `resolve_native_request` | delete the `if context.binding_kind == "account" and request.project_id != run.project_id: raise` | same test, second assertion |
| MA10 | `delivery.py` `lease_commands` | replace `context.binding_kind == "account"` in `projectId=` with `True` | `test_a_start_command_names_its_project_only_under_an_account_pairing` |

Not single-line-killable, by design: `test_a_project_pairing_still_cannot_lease_another_projects_run` (three independent guards: `_lease_scope`'s project filter, `run_project`/`run.project_id != project_id` in `_authorize`, and `validate_binding`'s thread-to-project check). Verify it as a pair: remove the `_authorize` comparison **and** `_lease_scope`'s project filter together; the test must still fail on the ingest assertion (thread check), proving the last guard is live.

**Step 2: Record** in the module docstring: `Mutation verification (YYYY-MM-DD, SQLite): MA1-MA8, MA10 each observed FAIL with the source restored after each.` Commit:

```sh
git add backend/tests/unit/services/harness/test_account_runs.py
git commit -m "test(harness): record mutation verification for account-run guards"
```

---

Bridge tasks. Every bridge command below runs from `<checkout>` with Node 24 on PATH (`export PATH=/opt/homebrew/opt/node@24/bin:$PATH`, or any Node 24) and has the form

```sh
pnpm --filter @nous/harness-bridge exec node --experimental-sqlite --import tsx --test --test-timeout=60000 test/<file>
```

### Task 8: Thread the start command's `projectId` through the bridge

**Files:**
- Modify: `packages/harness-bridge/src/connection.ts:17-28` (`BridgeCommand`), `:112-178` (`parseCommand`)
- Modify: `packages/harness-bridge/src/journal.ts:53` (constructor `optionsFor`), `:383` (start), `:482` (`append`)
- Create: `packages/harness-bridge/test/run-project.test.ts` (tests 1–2 now; test 3 in Task 10)

**Step 1: Write the failing tests.**

```ts
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { parseCommand } from "../src/connection.ts";
import { Journal } from "../src/journal.ts";
import type { HarnessAdapter, SessionOptions } from "../src/contracts.ts";

const PROJECT = "11111111-1111-4111-8111-111111111111";

function start(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    deviceId: randomUUID(),
    runId: randomUUID(),
    commandId: randomUUID(),
    workspaceId: randomUUID(),
    generation: 1,
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    body: { kind: "start", input: "hello" },
    ...extra,
  };
}

test("a start command may carry its run's projectId; other commands and malformed ids are refused", () => {
  assert.equal(parseCommand(start({ projectId: PROJECT })).projectId, PROJECT);
  assert.equal("projectId" in parseCommand(start()), false);
  for (const projectId of ["not-a-uuid", null, 7])
    assert.throws(() => parseCommand(start({ projectId })), /invalid bridge command/, String(projectId));
  assert.throws(
    () => parseCommand(start({ projectId: PROJECT, body: { kind: "interrupt", sessionId: "s", turnId: "t" } })),
    /invalid bridge command/,
  );
});

test("the journal gives the start's projectId to the session options and never uploads it", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-run-project-"));
  const seen: (string | undefined)[] = [];
  const options: SessionOptions = {
    cwd: dir,
    workspaceId: "local",
    policy: {
      sandbox: "workspace-write",
      approvalPolicy: "on-request",
      reviewer: "user",
      networkAccess: false,
      writableRoots: [dir],
    },
  };
  const adapter = {
    async startSession() {
      return { id: "session" };
    },
    async startTurn() {
      return { id: "turn" };
    },
  } as unknown as HarnessAdapter;
  const journal = new Journal(join(dir, "journal.sqlite"), (_workspaceId, projectId) => {
    seen.push(projectId);
    return options;
  });
  try {
    await journal.execute(parseCommand(start({ projectId: PROJECT })), adapter);
    assert.deepEqual(seen, [PROJECT]);
    const events = journal.pending(64);
    assert.ok(events.length > 0, "the running observation is journaled");
    for (const event of events) assert.equal("projectId" in event, false);
  } finally {
    journal.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
```

**Step 2: Run, expect failure.** `... test/run-project.test.ts`. Expected: test 1 fails with `invalid bridge command` thrown for the valid `projectId` (the `exact()` key list); test 2 fails the same way.

**Step 3: Implement.** `connection.ts` type:

```ts
export type BridgeCommand = BridgeIdentity & {
  expiresAt: string;
  // The run's NOUS project, on a start command leased under an account
  // pairing (backend schemas/harness.py BridgeCommand.projectId): the bridge
  // pins the run's NOUS tools to it. Never copied onto an event.
  projectId?: string;
  body: /* unchanged */;
};
```

In `parseCommand` add `"projectId"` to the `exact(value, [...])` list, add to the top-level condition

```ts
    (value.projectId !== undefined &&
      !(typeof value.projectId === "string" && uuid.test(value.projectId))) ||
```

and after the body checks, before `return structuredClone(...)`:

```ts
  if (value.projectId !== undefined && b.kind !== "start")
    throw new Error("invalid bridge command");
```

`journal.ts`: constructor parameter `private optionsFor: (workspaceId: string, projectId?: string) => SessionOptions,`; line 383 `const options = this.optionsFor(command.workspaceId, command.projectId);`; line 482

```ts
      // projectId selects the run's NOUS project locally; NOUS events never carry it.
      const { expiresAt, body: ignored, projectId: _project, ...identity } = command;
```

**Step 4: Run, expect pass.** Same command: `2 passed`. Then `test/recovery.test.ts test/denied-run.test.ts test/codex.test.ts` (journal regressions): all pass.

**Step 5: Commit.**

```sh
git add packages/harness-bridge/src/connection.ts packages/harness-bridge/src/journal.ts packages/harness-bridge/test/run-project.test.ts
git commit -m "feat(harness-bridge): accept a start command's projectId and hand it to the session"
```

---

### Task 9: Pin a run's NOUS tools to its project (`nous-harness mcp --project`)

**Files:**
- Create: `packages/harness-bridge/src/mcp/pin.ts`
- Modify: `packages/harness-bridge/src/mcp/server.ts:30-33` (`ToolCatalog` type)
- Modify: `packages/harness-bridge/src/mcp/client.ts:16-32` (`McpSession.project`)
- Modify: `packages/harness-bridge/src/mcp/config.ts:8-33` (`--project`)
- Modify: `packages/harness-bridge/src/mcp/stdio.ts:52-75`
- Modify: `packages/harness-bridge/src/actions/mcp.ts:18-40` (`FIELDS`), `:58-69` (`ACTION_FIELDS`), `:96-146` (`INPUT_PROPERTIES`), `:182-215` (`requestActionTool`)
- Modify: `packages/harness-bridge/src/cli.ts:1720-1732` (`mcp` branch of `main`)
- Create: `packages/harness-bridge/test/pin.test.ts`
- Modify: `packages/harness-bridge/test/actions.test.ts:26-37` (`ACTIONS`), `:227` (identity list)

**Step 0: Keep the existing action contract test honest.** `request_action offers every NOUS action and sends each with only its own fields` sends every other action's fields plus an identity list that includes `workspace_id`, and expects each action to drop what is not its own. `workspace_id` becomes a `create_folder` field and `project_id` a `create_project_note` field, so record them as such: in `ACTIONS` (whose comment says "each with all of its fields") set

```ts
  create_project_note: { title: "Findings", content: "# Findings", tags: ["a"], project_id: PROJECT },
  create_folder: { name: "Reading", description: "Papers to read", workspace_id: "88888888-8888-4888-8888-888888888888" },
```

and drop `"workspace_id"` from the identity key list on line 227 (it is no longer identity for this tool). The test keeps its name and count; it now fails until step 3 (the two new fields are not yet sent).

**Step 1: Write the failing tests** (`test/pin.test.ts`, 5 tests):

```ts
import assert from "node:assert/strict";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import type { ActionHttpClient, ActionStatus } from "../src/actions/client.ts";
import { requestActionTool } from "../src/actions/mcp.ts";
import type { ToolDescriptor, ToolInvocation, ToolResult } from "../src/mcp/client.ts";
import { buildManagedMcpConfig } from "../src/mcp/config.ts";
import { pinCatalog } from "../src/mcp/pin.ts";
import { createNousMcpServer, type ToolCatalog } from "../src/mcp/server.ts";

const PROJECT = "11111111-1111-4111-8111-111111111111";
const OTHER = "12121212-1212-4121-8121-121212121212";
const WORKSPACE = "13131313-1313-4131-8131-131313131313";
const DOC = "55555555-5555-4555-8555-555555555555";
const INVOCATION = "44444444-4444-4444-8444-444444444444";
const packageDir = fileURLToPath(new URL("..", import.meta.url));

/** An account grant's catalog as NOUS serves it: project tools take an optional project_id; list_library takes none. */
function accountCatalog(seen: ToolInvocation[]): ToolCatalog {
  const tools: ToolDescriptor[] = [
    {
      name: "search_documents",
      description: "Search",
      inputSchema: {
        type: "object",
        properties: { query: { type: "string" }, project_id: { type: "string", format: "uuid" } },
        required: ["query"],
      },
    },
    {
      name: "get_document_content",
      description: "Read",
      inputSchema: {
        type: "object",
        properties: { document_id: { type: "string" }, project_id: { type: "string", format: "uuid" } },
        required: ["document_id", "project_id"],
      },
    },
    { name: "list_library", description: "List", inputSchema: { type: "object", properties: {} } },
    {
      name: "search_arxiv",
      description: "arXiv",
      inputSchema: { type: "object", properties: { query: { type: "string" } }, required: ["query"] },
    },
  ];
  return {
    listTools: async () => structuredClone(tools),
    invokeRead: async (invocation) => {
      seen.push(invocation);
      return { content: [{ ok: true }], is_error: false, source_refs: [] } as ToolResult;
    },
  };
}

async function connected(catalog: ToolCatalog): Promise<Client> {
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const client = new Client({ name: "test", version: "0" });
  await createNousMcpServer(catalog).connect(serverTransport);
  await client.connect(clientTransport);
  return client;
}
const text = (result: any): string => result.content[0].text;

test("a pinned session fills a missing project_id and refuses another project", async () => {
  const seen: ToolInvocation[] = [];
  const mcp = await connected(pinCatalog(accountCatalog(seen), PROJECT));
  assert.equal((await mcp.callTool({ name: "search_documents", arguments: { query: "q" } })).isError, false);
  assert.deepEqual(seen[0]!.arguments, { query: "q", project_id: PROJECT });
  const same = await mcp.callTool({
    name: "get_document_content",
    arguments: { document_id: DOC, project_id: PROJECT.toUpperCase() },
  });
  assert.equal(same.isError, false);
  const other = await mcp.callTool({ name: "search_documents", arguments: { query: "q", project_id: OTHER } });
  assert.equal(other.isError, true);
  assert.match(text(other), new RegExp(`can use NOUS project ${PROJECT} only.*project_id names another project`));
  await mcp.callTool({ name: "search_arxiv", arguments: { query: "q" } });
  assert.deepEqual(seen.at(-1)!.arguments, { query: "q" }, "a tool without project_id gets none");
  assert.equal(seen.length, 3);
});

test("a pinned catalog hides list_library and offers project_id only as the run's project", async () => {
  const seen: ToolInvocation[] = [];
  const mcp = await connected(pinCatalog(accountCatalog(seen), PROJECT));
  const { tools } = await mcp.listTools();
  assert.deepEqual(tools.map((t) => t.name).sort(), ["get_document_content", "search_arxiv", "search_documents"]);
  const read = tools.find((t) => t.name === "get_document_content")!;
  assert.deepEqual((read.inputSchema.properties as any).project_id.enum, [PROJECT]);
  assert.deepEqual(read.inputSchema.required, ["document_id"]);
  const refused = await mcp.callTool({ name: "list_library", arguments: {} });
  assert.equal(refused.isError, true);
  assert.match(text(refused), /list_library reaches the whole account/);
  assert.equal(seen.length, 0);
});

test("a pinned request_action fills project_id and refuses another project, new folders, moves and metadata edits", async () => {
  const sent: { action: string; args: Record<string, unknown> }[] = [];
  const client = {
    request: async (action: string, _id: string, args: Record<string, unknown>) => {
      sent.push({ action, args });
      return { invocation_id: INVOCATION, state: "succeeded", tool_name: action, result: null, approval_url: null } as ActionStatus;
    },
  } as unknown as ActionHttpClient;
  const tool = requestActionTool(client, PROJECT);
  assert.deepEqual((tool.descriptor.inputSchema.properties as any).project_id.enum, [PROJECT]);
  await tool.call({ action: "save_papers_to_folder", invocation_id: INVOCATION, document_ids: [DOC] });
  await tool.call({ action: "ingest_arxiv_papers", invocation_id: INVOCATION, paper_ids: ["2401.00001"] });
  await tool.call({ action: "create_project_note", invocation_id: INVOCATION, title: "t", content: "c" });
  assert.deepEqual(
    sent.map((s) => [s.action, s.args.project_id]),
    [
      ["save_papers_to_folder", PROJECT],
      ["ingest_arxiv_papers", PROJECT],
      ["create_project_note", PROJECT],
    ],
  );
  const other = await tool.call({ action: "delete_folder", invocation_id: INVOCATION, project_id: OTHER });
  assert.equal(other.isError, true);
  assert.match(other.text, /project_id names another project/);
  for (const [action, args] of [
    ["create_folder", { name: "x", workspace_id: WORKSPACE }],
    ["move_papers_between_folders", { document_ids: [DOC], from_project_id: PROJECT, to_project_id: PROJECT }],
    ["update_document_metadata", { document_id: DOC, title: "t" }],
  ] as const) {
    const refused = await tool.call({ action, invocation_id: INVOCATION, ...args });
    assert.equal(refused.isError, true, action);
    assert.match(refused.text, /start Codex yourself/, action);
  }
  assert.equal(sent.length, 3);
});

test("an unpinned request_action sends create_folder's workspace_id and a note's project_id when given", async () => {
  const sent: Record<string, unknown>[] = [];
  const client = {
    request: async (_action: string, _id: string, args: Record<string, unknown>) => {
      sent.push(args);
      return { invocation_id: INVOCATION, state: "succeeded", tool_name: "x", result: null, approval_url: null } as ActionStatus;
    },
  } as unknown as ActionHttpClient;
  const tool = requestActionTool(client);
  await tool.call({ action: "create_folder", invocation_id: INVOCATION, name: "Reading", workspace_id: WORKSPACE });
  await tool.call({ action: "create_project_note", invocation_id: INVOCATION, title: "t", content: "c", project_id: OTHER });
  await tool.call({ action: "create_project_note", invocation_id: INVOCATION, title: "t", content: "c" });
  assert.deepEqual(sent, [
    { name: "Reading", workspace_id: WORKSPACE },
    { title: "t", content: "c", project_id: OTHER },
    { title: "t", content: "c" },
  ]);
  const bad = await tool.call({ action: "create_folder", invocation_id: INVOCATION, name: "x", workspace_id: "nope" });
  assert.equal(bad.isError, true);
});

test("the MCP child gets --project only as a UUID, and the CLI refuses a malformed one", () => {
  const base = { apiOrigin: "https://nous.test/api/v1", credentialHandle: "h", stateDir: packageDir };
  const args = buildManagedMcpConfig({ ...base, project: PROJECT }).nous!.args;
  assert.equal(args[args.indexOf("--project") + 1], PROJECT);
  assert.equal(buildManagedMcpConfig(base).nous!.args.includes("--project"), false);
  assert.throws(() => buildManagedMcpConfig({ ...base, project: "x" }), /MCP project must be a project UUID/);
  const run = spawnSync(
    process.execPath,
    ["--import", "tsx", "src/cli.ts", "mcp", "--api", "https://nous.test/api/v1", "--session", "h", "--project", "bad"],
    { cwd: packageDir, encoding: "utf8", timeout: 20_000 },
  );
  assert.equal(run.status, 1);
  assert.match(run.stderr, /--project must be a project UUID/);
});
```

**Step 2: Run, expect failure.** `... test/pin.test.ts`. Expected: `ERR_MODULE_NOT_FOUND` for `../src/mcp/pin.ts`.

**Step 3: Implement `src/mcp/pin.ts`.**

```ts
import { ToolRequestRejected, type ToolDescriptor, type ToolInvocation, type ToolResult } from "./client.ts";
import type { ToolCatalog } from "./server.ts";

/** Fields that name a NOUS project in a tool or action call. */
export const PROJECT_FIELDS = ["project_id", "from_project_id", "to_project_id"] as const;
/** Read tools that reach beyond one project. */
const ACCOUNT_WIDE_TOOLS = new Set(["list_library"]);

export const pinnedMessage = (project: string): string =>
  `this Local Codex run can use NOUS project ${project} only (its chat's project)`;

/**
 * A pinned call's arguments: every project field must name the pinned
 * project, and `project_id` is filled in when the call takes one and leaves it
 * out. Returns the refusal text otherwise.
 */
export function pinnedArguments(
  project: string,
  args: Record<string, unknown>,
  takesProjectId: boolean,
): Record<string, unknown> | string {
  const pinned = { ...args };
  for (const field of PROJECT_FIELDS) {
    const value = pinned[field];
    if (value === undefined || value === null) continue;
    if (typeof value !== "string" || value.toLowerCase() !== project.toLowerCase())
      return `${pinnedMessage(project)}; ${field} names another project`;
  }
  if (takesProjectId && (pinned.project_id === undefined || pinned.project_id === null))
    pinned.project_id = project;
  return pinned;
}

/** project_id as a pinned session advertises it: the run's project, never required. */
export function pinnedProperty(project: string): Record<string, unknown> {
  return {
    type: "string",
    enum: [project],
    description: "This run's NOUS project (its chat's); filled in when left out.",
  };
}

function pinnedDescriptor(project: string, tool: ToolDescriptor): ToolDescriptor {
  const properties = (tool.inputSchema.properties ?? {}) as Record<string, unknown>;
  if (!("project_id" in properties)) return tool;
  const required = tool.inputSchema.required;
  return {
    ...tool,
    inputSchema: {
      ...tool.inputSchema,
      properties: { ...properties, project_id: pinnedProperty(project) },
      ...(Array.isArray(required) ? { required: required.filter((key) => key !== "project_id") } : {}),
    },
  };
}

/**
 * The NOUS read tools of a Local Codex run under an account pairing, pinned
 * to the run's project (account-connection design §3). Not a security
 * boundary: the MCP child holds the account credential. It keeps the model
 * inside the chat's project; account-wide reach stays with a Codex the user
 * starts (`nous-harness mcp install`).
 */
export function pinCatalog(catalog: ToolCatalog, project: string): ToolCatalog {
  let takesProject: Set<string> | null = null;
  const listTools = async (): Promise<ToolDescriptor[]> => {
    const tools = (await catalog.listTools()).filter((tool) => !ACCOUNT_WIDE_TOOLS.has(tool.name));
    takesProject = new Set(
      tools
        .filter((tool) => "project_id" in ((tool.inputSchema.properties ?? {}) as object))
        .map((tool) => tool.name),
    );
    return tools.map((tool) => pinnedDescriptor(project, tool));
  };
  return {
    listTools,
    async invokeRead(invocation: ToolInvocation): Promise<ToolResult> {
      if (ACCOUNT_WIDE_TOOLS.has(invocation.tool_name))
        throw new ToolRequestRejected(`${pinnedMessage(project)}; ${invocation.tool_name} reaches the whole account`);
      if (takesProject === null) await listTools();
      const args = pinnedArguments(
        project,
        (invocation.arguments ?? {}) as Record<string, unknown>,
        takesProject!.has(invocation.tool_name),
      );
      if (typeof args === "string") throw new ToolRequestRejected(args);
      return catalog.invokeRead({ ...invocation, arguments: args });
    },
  };
}
```

`server.ts`: add `export type ToolCatalog = Pick<CapabilityClient, "listTools" | "invokeRead">;` (import `CapabilityClient` as a type alongside the others) and change the parameter to `client: ToolCatalog`.

`client.ts` `McpSession`: add

```ts
  // The run's NOUS project (account pairings only): every tool call is held
  // to it (mcp/pin.ts). Absent for a project connection and a standalone install.
  project?: string;
```

`config.ts` `mcpArgs`: after the `outputRoot` check add

```ts
  if (
    session.project !== undefined &&
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(session.project)
  )
    throw new Error("MCP project must be a project UUID");
```

and append `...(session.project ? ["--project", session.project] : []),` to the returned array.

`stdio.ts` (lines 54–75):

```ts
  if (session.actions || session.library) {
    // Requests only: approval stays a browser action the model cannot take.
    const actions = new ActionHttpClient(session.apiOrigin, credentials, keeper.fetch);
    local.push(requestActionTool(actions, session.project), actionStatusTool(actions));
  }
  // ...handoff and context unchanged...
  const catalog = new CapabilityClient(session.apiOrigin, credentials, keeper.fetch);
  const server = createNousMcpServer(
    session.project ? pinCatalog(catalog, session.project) : catalog,
    local,
  );
```

(import `pinCatalog` from `./pin.ts`).

`actions/mcp.ts`: in `FIELDS` add `workspace_id: { valid: uuid, expected: "a NOUS workspace UUID" },`; in `ACTION_FIELDS` change

```ts
  create_project_note: { required: ["title", "content"], optional: ["tags", "project_id"] },
  create_folder: { required: ["name"], optional: ["description", "workspace_id"], nullable: ["description"] },
```

In `INPUT_PROPERTIES` add

```ts
  workspace_id: {
    ...UUID_STRING,
    description: "create_folder on an account connection: the NOUS workspace the new folder goes in; list_library gives the ids",
  },
```

and extend `project_id`'s description with `"; on an account connection every action that names a folder needs it, and create_project_note takes the note's project"`. Add above `requestActionTool`:

```ts
/**
 * Actions a run pinned to one project cannot take: a new folder or a move
 * between two (a project connection cannot either), and a paper's metadata,
 * which names a document, not a project the bridge can hold to the pin.
 */
const UNPINNED_ACTIONS = new Set(["create_folder", "move_papers_between_folders", "update_document_metadata"]);
```

and change `requestActionTool`:

```ts
export function requestActionTool(client: ActionHttpClient, pin?: string): LocalTool {
  return {
    descriptor: {
      name: "request_action",
      description:
        "…existing text…" +
        (pin ? ` This Local Codex run acts on NOUS project ${pin} only; project_id is filled in.` : ""),
      inputSchema: {
        type: "object",
        additionalProperties: false,
        required: ["action", "invocation_id"],
        properties: pin ? { ...INPUT_PROPERTIES, project_id: pinnedProperty(pin) } : INPUT_PROPERTIES,
      },
    },
    async call(args) {
      const { action, invocation_id: invocationId } = args;
      if (typeof action !== "string" || !Object.hasOwn(ACTION_FIELDS, action))
        return { text: `request_action supports action=${ACTIONS.join(" | ")}`, isError: true };
      if (!uuid(invocationId)) return { text: "request_action requires a UUID invocation_id", isError: true };
      let given = args;
      if (pin !== undefined) {
        if (UNPINNED_ACTIONS.has(action))
          return {
            text: `request_action ${action}: ${pinnedMessage(pin)}; start Codex yourself with this computer's account connection (nous-harness mcp install) for this action`,
            isError: true,
          };
        const { required, optional = [] } = ACTION_FIELDS[action]!;
        const pinned = pinnedArguments(pin, args, [...required, ...optional].includes("project_id"));
        if (typeof pinned === "string") return { text: `request_action ${action}: ${pinned}`, isError: true };
        given = pinned;
      }
      const fields = actionArguments(action, given);
      // ...the rest unchanged...
```

(import `pinnedArguments`, `pinnedMessage`, `pinnedProperty` from `../mcp/pin.ts`).

`cli.ts` `mcp` branch of `main` (1720–1732):

```ts
  else if (positionals.join(" ") === "mcp") {
    if (!values.session || !values.api)
      throw new Error("mcp requires --api and --session");
    if (values.project !== undefined && !uuid(values.project))
      throw new Error("--project must be a project UUID");
    await runStdioMcp({
      // ...unchanged fields...
      ...(values.project ? { project: values.project } : {}),
    });
  }
```

**Step 4: Run, expect pass.** `... test/pin.test.ts`: `5 passed`. Then `test/actions.test.ts test/mcp.test.ts`: all pass (`actions` 15, unchanged count, with step 0's two fields now sent).

**Step 5: Commit.**

```sh
git add packages/harness-bridge/src/mcp/pin.ts packages/harness-bridge/src/mcp/server.ts packages/harness-bridge/src/mcp/client.ts \
  packages/harness-bridge/src/mcp/config.ts packages/harness-bridge/src/mcp/stdio.ts packages/harness-bridge/src/actions/mcp.ts \
  packages/harness-bridge/src/cli.ts packages/harness-bridge/test/pin.test.ts packages/harness-bridge/test/actions.test.ts
git commit -m "feat(harness-bridge): pin a Local Codex run's NOUS tools to its chat's project"
```

---

### Task 10: Account connections in the bridge (`connect --account`, state, folders, managed sessions)

**Files:**
- Modify: `packages/harness-bridge/src/cli.ts` — `WORKSPACE_MCP_ONLY` comment 42–44, `LocalState` 46–61, `storedConnection` 165–178, `connect` 296–539, `addWorkspace` 613–660, `runBridge` 1238–1249, `mcpSession` 1311–1342, `sessionOptionsFor` 1343–1376, `mcpInstallCommand` 1415–1429, `status` 1465–1479, `handoffContext` 1491–1494, `help` 1621–1628, `main` 1644–1703
- Modify: `packages/harness-bridge/test/mcp.test.ts:481-531` (exactly-one message), plus 4 new tests
- Modify: `packages/harness-bridge/test/connect-reuse.test.ts` (5 new tests)
- Modify: `packages/harness-bridge/test/run-project.test.ts` (test 3)

**Step 1: Write the failing tests.**

`test/run-project.test.ts`, append (add `import { sessionOptionsFor, type LocalState } from "../src/cli.ts";`):

```ts
test("an account connection pins each run's MCP child to the start's project; a project connection never gets --project", () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-run-project-state-"));
  const folder = { id: randomUUID(), root: dir, label: "w" };
  const account: LocalState = {
    apiUrl: "https://nous.example/api/v1",
    deviceId: randomUUID(),
    account: true,
    credentialHandle: randomUUID(),
    scopes: ["harness:execute", "tools:read"],
    workspaces: [folder],
  };
  try {
    const args = sessionOptionsFor(dir, account, folder.id, PROJECT).mcpConfig?.nous?.args ?? [];
    assert.equal(args[args.indexOf("--project") + 1], PROJECT);
    // Without the run's project an account run gets no NOUS tools rather than unpinned ones.
    assert.equal("mcpConfig" in sessionOptionsFor(dir, account, folder.id), false);
    const { account: _account, ...rest } = account;
    const project: LocalState = { ...rest, projectId: PROJECT, workspaces: [{ ...folder, projectId: PROJECT }] };
    const plain = sessionOptionsFor(dir, project, folder.id, randomUUID()).mcpConfig?.nous?.args ?? [];
    assert.equal(plain.includes("--project"), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
```

`test/mcp.test.ts`: in `connect refuses contradictory binding and scope flags before any request` replace both `/--project or --workspace \(not both\) required/` with `/--project, --workspace or --account \(exactly one\) required/`, and add after the second one:

```ts
    await assert.rejects(
      connect({ ...base, projectId: PROJECT, account: true, tools: true }),
      /--project, --workspace or --account \(exactly one\) required/,
    );
```

Then append (4 tests):

```ts
test("connect --account asks for the whole account with execute, read and library:read; --write and --library add theirs", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-mcp-account-"));
  const calls: { url: string; body: any }[] = [];
  const base = { stateDir: dir, fetchFn: consentFetch(calls), announce: () => {}, apiUrl: "https://nous.test/api/v1", label: "dev" };
  const consent = () => calls.filter((c) => c.url.endsWith("/grant-requests")).at(-1)!.body;
  try {
    await connect({ ...base, account: true });
    assert.deepEqual(consent(), {
      account: true,
      device_id: DEVICE,
      scopes: ["harness:execute", "tools:read", "library:read"],
    });
    await connect({ ...base, account: true, write: true, library: true });
    assert.deepEqual(consent().scopes, ["harness:execute", "tools:read", "tools:write", "library:read", "library:write"]);
    const state = JSON.parse(readFileSync(join(dir, "connection.json"), "utf8"));
    assert.equal(state.account, true);
    assert.equal("projectId" in state || "workspaceId" in state, false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("connect --account refuses --publish, --context, --chat and --handoff before any request", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-mcp-account-flags-"));
  const calls: { url: string; body: any }[] = [];
  const account = { stateDir: dir, fetchFn: consentFetch(calls), announce: () => {}, apiUrl: "https://nous.test/api/v1", label: "dev", account: true };
  try {
    await assert.rejects(connect({ ...account, tools: true, publish: true }), /--account cannot be combined with --publish/);
    await assert.rejects(connect({ ...account, tools: true, context: true }), /--account cannot be combined with --context/);
    await assert.rejects(connect({ ...account, threadId: CHAT }), /--account cannot be combined with --chat/);
    await assert.rejects(connect({ ...account, tools: true, handoff: true }), /--account cannot be combined with --handoff/);
    await assert.rejects(connect({ ...account, library: true }), /--library requires --write/);
    assert.equal(calls.length, 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("an account connection registers folders without a project, and carries a project pairing's folders as account folders", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-mcp-account-folders-"));
  const keep = realpathSync(mkdtempSync(join(tmpdir(), "nous-keep-")));
  const added = realpathSync(mkdtempSync(join(tmpdir(), "nous-add-")));
  const calls: { url: string; body: any }[] = [];
  try {
    await new CredentialStore(dir).writeLocal("connection", {
      apiUrl: "https://nous.test/api/v1",
      deviceId: "44444444-4444-4444-8444-444444444444",
      projectId: PROJECT,
      credentialHandle: "55555555-5555-4555-8555-555555555555",
      scopes: ["harness:execute", "tools:read"],
      workspaces: [{ id: "66666666-6666-4666-8666-666666666666", root: keep, label: "Kept", projectId: PROJECT }],
    } satisfies LocalState);
    await connect({ stateDir: dir, fetchFn: consentFetch(calls), announce: () => {}, apiUrl: "https://nous.test/api/v1", label: "dev", account: true });
    const carried = calls.filter((c) => /\/devices\/[^/]+\/workspaces$/.test(c.url));
    assert.equal(carried.length, 1);
    assert.equal(carried[0]!.body.label, "Kept");
    assert.equal("project_id" in carried[0]!.body, false);
    await addWorkspace({ stateDir: dir, fetchFn: consentFetch(calls), announce: () => {}, root: added });
    const registered = calls.filter((c) => /\/devices\/[^/]+\/workspaces$/.test(c.url)).at(-1)!;
    assert.equal("project_id" in registered.body, false);
    const state = JSON.parse(readFileSync(join(dir, "connection.json"), "utf8"));
    assert.deepEqual(state.workspaces.map((w: any) => [w.root, "projectId" in w]), [[keep, false], [added, false]]);
  } finally {
    for (const path of [dir, keep, added]) rmSync(path, { recursive: true, force: true });
  }
});

test("mcp install for an account connection never pins a project and says its tools take a project_id", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nous-mcp-account-install-"));
  const messages: string[] = [];
  try {
    await new CredentialStore(dir).writeLocal("connection", {
      apiUrl: "https://nous.test/api/v1",
      deviceId: DEVICE,
      account: true,
      credentialHandle: "55555555-5555-4555-8555-555555555555",
      scopes: ["harness:execute", "tools:read", "library:read"],
      workspaces: [],
    } satisfies LocalState);
    const command = await mcpInstallCommand(dir, { announce: (m) => messages.push(m) });
    assert.equal(command.includes("'--project'"), false);
    assert.ok(messages.some((m) => /every project in your NOUS account.*project_id.*list_library/.test(m)), messages.join("\n"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
```

(`realpathSync` is already imported in `mcp.test.ts`.)

`test/connect-reuse.test.ts`, append (5 tests):

```ts
test("an account binding is reused like a project one, and never reuses a project binding or is reused by one", async () => {
  const t = setup();
  try {
    const { projectId: _project, ...noProject } = t.base;
    await connect({ ...noProject, account: true });
    assert.equal(t.state().account, true);
    assert.deepEqual(t.state().scopes, ["harness:execute", "tools:read", "library:read"]);
    let mark = t.calls.length;
    await connect({ ...noProject, account: true });
    assert.deepEqual(t.since(mark), [`https://nous.test/api/v1/integrations/grants/${GRANT}/renew`]);
    assert.match(t.messages.at(-1)!, /^Reusing binding: your NOUS account; no new login or consent needed\.$/);
    mark = t.calls.length;
    await connect({ ...t.base, tools: true });
    assert.ok(t.since(mark).some((u) => u.endsWith("/cli-auth/start")), "account never stands in for a project");
    mark = t.calls.length;
    await connect({ ...noProject, account: true });
    assert.ok(t.since(mark).some((u) => u.endsWith("/cli-auth/start")), "a project never stands in for the account");
  } finally {
    t.cleanup();
  }
});

test("a lapsed account grant resumes from its consent with no login, consent or new device", async () => {
  const t = lapsing(resumedGrant);
  try {
    const account = { ...t.base, projectId: undefined, account: true };
    const first = await connect(account);
    t.lapse();
    const mark = t.calls.length;
    assert.deepEqual(await connect(account), first);
    assert.equal(t.since(mark).some((u) => u.endsWith("/cli-auth/start") || u.endsWith("/grant-requests")), false);
    assert.match(t.messages.at(-1)!, /^Resumed binding: your NOUS account; your earlier approval still holds/);
  } finally {
    t.cleanup();
  }
});

test("a stored account connection loads; one that also names a project, or says account: false, is not connected", async () => {
  for (const [name, binding, connected] of [
    ["account", { account: true }, true],
    ["account and project", { account: true, projectId: PROJECT }, false],
    ["account false", { account: false }, false],
  ] as const) {
    const t = setup();
    try {
      await connect({ ...t.base, tools: true });
      const { projectId: _project, ...rest } = t.state();
      await new CredentialStore(t.dir).writeLocal("connection", { ...rest, ...binding });
      const lines: string[] = [];
      await status({ stateDir: t.dir, announce: (m) => lines.push(m) });
      assert.equal(/not connected/i.test(lines.join("\n")), !connected, name);
    } finally {
      t.cleanup();
    }
  }
});

test("status names an account binding and shows neither a project nor a chat", async () => {
  const t = setup();
  try {
    await connect({ ...t.base, projectId: undefined, account: true });
    const lines: string[] = [];
    await status({ stateDir: t.dir, announce: (m) => lines.push(m) });
    const out = lines.join("\n");
    assert.match(out, /^Account: your whole NOUS account \(every project you own or belong to in your organization\)$/m);
    assert.doesNotMatch(out, /Project:|Chat:|Workspace:/);
    assert.match(out, /Scopes: harness:execute tools:read library:read/);
  } finally {
    t.cleanup();
  }
});

test("handoff commands refuse an account connection and say how to get a chat one", async () => {
  const t = setup();
  try {
    await connect({ ...t.base, projectId: undefined, account: true });
    const mark = t.calls.length;
    const refusal = /whole NOUS account, which has no chat handoff.*connect --project UUID --chat UUID --tools --handoff/;
    const options = { stateDir: t.dir, fetchFn: t.base.fetchFn };
    await assert.rejects(handoffShow(options), refusal);
    await assert.rejects(handoffFlush(options), refusal);
    assert.equal(t.calls.length, mark);
  } finally {
    t.cleanup();
  }
});
```

**Step 2: Run, expect failure.**

```sh
pnpm --filter @nous/harness-bridge exec node --experimental-sqlite --import tsx --test --test-timeout=60000 \
  test/run-project.test.ts test/mcp.test.ts test/connect-reuse.test.ts
```

Expected: the new tests fail with `--project or --workspace (not both) required` (connect has no `account` option); the modified refusal test fails on the old message; `sessionOptionsFor` accepts no fourth argument (`--project` missing).

**Step 3: Implement in `cli.ts`.**

`WORKSPACE_MCP_ONLY` comment: `// Harness sessions need a project or the whole account; a workspace grant cannot hold harness:execute.` (message text unchanged.)

`LocalState`:

```ts
export type LocalState = {
  apiUrl: string;
  deviceId: string;
  // Exactly one binding. A project connection, or an account connection
  // (every project in the user's NOUS account, design 2026-10-14), can run
  // harness sessions; a workspace connection (one NOUS workspace) is MCP-only.
  projectId?: string;
  workspaceId?: string;
  account?: true;
  // ...threadId, label, credentialHandle, scopes unchanged...
  // A folder of an account connection has no projectId: any chat in the
  // account can run Codex in it.
  workspaces: { id: string; root: string; label: string; projectId?: string }[];
};
```

`storedConnection`:

```ts
function storedConnection(value: unknown): LocalState | null {
  if (
    !record(value) ||
    typeof value.apiUrl !== "string" ||
    !uuid(value.deviceId) ||
    typeof value.credentialHandle !== "string" ||
    !Array.isArray(value.workspaces)
  )
    return null;
  // Exactly one binding: a project, a workspace (MCP-only) or the account.
  // State written before account connections names a project or a workspace.
  const bindings = [uuid(value.projectId), uuid(value.workspaceId), value.account === true];
  if (bindings.filter(Boolean).length !== 1 || (value.account !== undefined && value.account !== true))
    return null;
  return value as LocalState;
}
```

Add above `connect`:

```ts
/** What `connect` prints when NOUS cannot serve an account pairing. */
export const ACCOUNT_UNSUPPORTED = "this NOUS server doesn't support account connections yet";
/**
 * fetch for the account consent request: a server before account
 * connections answers 422 (GrantRequestCreate refuses the unknown `account`
 * field) and one with them switched off 503 "Account connections are
 * disabled"; both become ACCOUNT_UNSUPPORTED. Any other answer passes through.
 */
function refusingAccount(fetchFn: typeof fetch): typeof fetch {
  return (async (...args: Parameters<typeof fetch>) => {
    const response = await fetchFn(...args);
    if (response.status === 422) throw new Error(ACCOUNT_UNSUPPORTED);
    if (response.status === 503) {
      const data: unknown = await response.clone().json().catch(() => null);
      if (record(data) && data.detail === "Account connections are disabled")
        throw new Error(ACCOUNT_UNSUPPORTED);
    }
    return response;
  }) as typeof fetch;
}
```

`connect` options: add `// Exactly one of projectId / workspaceId / account.` and `account?: boolean;`. Replace the head of `connect` (from `const projectId = options.projectId;` through the `scopes` array, lines 319–371) with:

```ts
  const projectId = options.projectId;
  const account = options.account === true;
  if ([projectId !== undefined, options.workspaceId !== undefined, account].filter(Boolean).length !== 1)
    throw new Error("--project, --workspace or --account (exactly one) required");
  const target = projectId !== undefined ? "project" : options.workspaceId !== undefined ? "workspace" : "account";
  // An account approval runs Codex for any chat in the account and serves
  // tools across it, but publishing, a chat's handoff and a project's selected
  // memories each belong to one project (NOUS refuses those scopes there).
  if (account)
    for (const [flag, on] of [
      ["--publish", options.publish],
      ["--context", options.context],
      ["--chat", options.threadId !== undefined],
      ["--handoff", options.handoff],
    ] as const)
      if (on) throw new Error(`--account cannot be combined with ${flag}`);
  if (options.threadId !== undefined) {
    if (!options.projectId) throw new Error("--chat requires --project");
    if (!uuid(options.threadId)) throw new Error("--chat must be a chat UUID");
  }
  if ((!account && !uuid(projectId ?? options.workspaceId)) || !options.label.trim())
    throw new Error(account ? "device label required" : `${target} UUID and device label required`);
  // An account connection always reads NOUS tools; the others opt in.
  const tools = options.tools === true || account;
  // NOUS capabilities are opt-in and each scope is shown on the consent page.
  if (options.publish && !tools) throw new Error("--publish requires --tools");
  if (options.write && !tools) throw new Error("--write requires --tools");
  if (options.handoff && !tools) throw new Error("--handoff requires --tools");
  // A handoff lives in one chat of one project; a workspace grant has neither.
  if (options.handoff && target === "workspace")
    throw new Error("--workspace cannot be combined with --handoff");
  if (options.handoff && options.threadId === undefined)
    throw new Error("--handoff requires --chat");
  if (options.library && !tools) throw new Error("--library requires --tools");
  if (options.library && !options.write) throw new Error("--library requires --write");
  if (options.context && !tools) throw new Error("--context requires --tools");
  // ...the existing `if (projectId === undefined) {...}` workspace block, with its
  // condition changed to `if (target === "workspace") {`, unchanged inside...
  const scopes = [
    ...(target !== "workspace" ? ["harness:execute"] : []),
    ...(tools ? ["tools:read"] : []),
    ...(options.publish ? ["artifacts:publish"] : []),
    ...(options.write ? ["tools:write"] : []),
    ...(options.handoff ? ["handoff:read", "handoff:write"] : []),
    // Workspace and account grants need a project_id on every project tool,
    // and only list_library (library:read) names them (WG-3). library:write,
    // which auto-runs library changes, still needs --library.
    ...(options.library || target !== "project" ? ["library:read"] : []),
    ...(options.library ? ["library:write"] : []),
    ...(options.context ? ["context:read"] : []),
  ];
```

`sameBinding` (382–387): add `(previous.account === true) === account &&`. Reuse announce (399–416): replace `${target} ${projectId ?? options.workspaceId}` with `${subject}` where `const subject = account ? "your NOUS account" : \`${target} ${projectId ?? options.workspaceId}\`;` is declared just before `const kept`, and replace both `projectId !== undefined` tests in the retained-scope text with `target !== "workspace"` (an account connection runs Codex and has folders too).

Consent request (429–442):

```ts
  const consent = await request(
    account ? refusingAccount(fetchFn) : fetchFn,
    base,
    "/integrations/grant-requests",
    token,
    {
      ...(target === "project"
        ? { project_id: projectId }
        : target === "workspace"
          ? { workspace_id: options.workspaceId }
          : { account: true }),
      device_id: device.id,
      scopes,
      ...(options.threadId ? { thread_id: options.threadId } : {}),
    },
  );
```

Approval announce (450–452):

```ts
  const where = approval.startsWith("/") ? " (open this path on your NOUS website)" : "";
  announce(
    account
      ? `Approve this device's access to your whole NOUS account in your browser: ${approval}${where}`
      : `Approve this device and ${target} in your browser: ${approval}${where}`,
  );
```

Folder carry-over (480–505): change the condition to `if (target !== "workspace" && previous !== null)`, the registration body to `{ workspace_id: workspaceId, label: old.label, ...(projectId !== undefined ? { project_id: projectId } : {}) }`, and the push to `carried.push({ id: workspaceId, root, label: old.label, ...(projectId !== undefined ? { projectId } : {}) });`. Update the comment: `// … re-register each root with the new device (an account connection's carry no project) …`.

State write (506–517): `...(target === "project" ? { projectId } : target === "workspace" ? { workspaceId: options.workspaceId } : { account: true as const }),`.

Final announce (533–537):

```ts
  announce(
    options.threadId
      ? `Connected to project ${projectId}, chat ${chatLabel}(${options.threadId}).`
      : account
        ? "Connected to your NOUS account."
        : `Connected to ${target} ${projectId ?? options.workspaceId}.`,
  );
```

`addWorkspace` (618–654):

```ts
  if (
    !record(value) ||
    !uuid(value.deviceId) ||
    !(uuid(value.projectId) || value.account === true) ||
    typeof value.apiUrl !== "string" ||
    !Array.isArray(value.workspaces) ||
    typeof value.credentialHandle !== "string"
  )
    throw new Error("connect this device first");
  const state = value as LocalState;
  // A project connection's folder is tied to its project; an account
  // connection's folder (no project) serves every chat in the account.
  const projectId = uuid(value.projectId) ? value.projectId : undefined;
  // ...realpath / existing check / credentials unchanged...
    { workspace_id: workspaceId, label, ...(projectId !== undefined ? { project_id: projectId } : {}) },
  // ...
  state.workspaces.push({ id: workspaceId, root, label, ...(projectId !== undefined ? { projectId } : {}) });
```

`runBridge` (1247–1249): `new Journal(join(stateDir, "journal.sqlite"), (workspaceId, projectId) => sessionOptionsFor(stateDir, state, workspaceId, projectId))`.

`mcpSession(stateDir, state, outputRoot?, project?)`: change both `state.workspaceId === undefined` tests to `state.projectId !== undefined` (publication and selected memories belong to a project connection only) and add `...(project ? { project } : {}),`.

`sessionOptionsFor`:

```ts
export function sessionOptionsFor(
  stateDir: string,
  state: LocalState,
  workspaceId: string,
  projectId?: string,
): SessionOptions {
  if (state.workspaceId !== undefined) throw new Error(WORKSPACE_MCP_ONLY);
  const workspace = state.workspaces.find((w) => w.id === workspaceId);
  if (!workspace) throw new Error("unregistered local workspace");
  // An account connection reaches every project in the account; each run's
  // NOUS tools are pinned to the project NOUS names in its start command.
  // Without one, the run gets no NOUS tools rather than unpinned ones.
  const pin = state.account === true ? (uuid(projectId) ? projectId : null) : undefined;
  if (pin === null) console.error("NOUS did not name this run's project; starting it without NOUS tools.");
  const model = process.env.NOUS_HARNESS_CODEX_MODEL?.trim();
  return {
    // ...cwd, workspaceId, policy, model unchanged...
    ...(state.scopes?.includes("tools:read") && pin !== null
      ? { mcpConfig: buildManagedMcpConfig(mcpSession(stateDir, state, workspace.root, pin)) }
      : {}),
  };
}
```

`mcpInstallCommand`, after the RT-2 announce: `if (state.account === true) announce("This registration reaches every project in your NOUS account: its NOUS tools take a project_id, and list_library lists the projects and their workspaces.");`.

`status` (1465–1472):

```ts
      ...(state.projectId !== undefined
        ? [`Project: ${state.projectId}`, `Chat: ${state.threadId ?? "none (project only)"}`]
        : state.account === true
          ? ["Account: your whole NOUS account (every project you own or belong to in your organization)"]
          : [`Workspace: ${state.workspaceId} (every project in it; MCP-only)`]),
```

`handoffContext` (1490): before the workspace check add

```ts
  if (state.account === true)
    throw new Error(
      "this device is connected to your whole NOUS account, which has no chat handoff; reconnect with nous-harness connect --project UUID --chat UUID --tools --handoff",
    );
```

`main`: add `account: { type: "boolean" },` to `parseArgs`; the connect condition becomes `(values.project || values.workspace || values.account)` and passes `account: values.account,`. `help`: first line `connect --api https://host/api/v1 (--project UUID | --workspace UUID | --account) --label NAME …`, and add after the `--workspace` line:

```
  connect --account binds the grant to your whole NOUS account (every project in workspaces of your organization that you own or belong to). It runs Local Codex from any chat in the account and always requests harness:execute, tools:read and library:read (list_library names the projects tools need); --write and --library add theirs; it cannot take --publish, --chat, --handoff or --context. workspace add registers folders that any chat in the account can use.
```

**Step 4: Run, expect pass.** Step 2's command: all pass (`run-project` 3, `mcp` 24, `connect-reuse` 29).

**Step 5: Commit.**

```sh
git add packages/harness-bridge/src/cli.ts packages/harness-bridge/test/run-project.test.ts \
  packages/harness-bridge/test/mcp.test.ts packages/harness-bridge/test/connect-reuse.test.ts
git commit -m "feat(harness-bridge): connect --account, account folders and pinned managed sessions"
```

---

### Task 11: `nous --rc` becomes account-only

**Files:**
- Modify: `packages/harness-bridge/src/cli.ts` — import line 19, delete 661–678 and 690–825 (keep `authorizedGet` 678–689), `RcOptions` 884–896, `rc` 897–1021, `main` rc branch 1764–1782
- Replace: `packages/harness-bridge/test/rc.test.ts` (24 tests → 20; names below)

Test changes, by name: rewritten — `picks a listed project…` → `pairs the whole account with five scopes…`; `the same project again…` → `a second run reuses the account pairing…`; `a different project than the stored pairing…` → `a stored project pairing is replaced…`. Updated (no answers; `/auth/me` probe): the other 15 kept names. Deleted (no picker or project creation any more): `n creates a project in the only workspace and pairs with it`, `n with several workspaces asks which one and re-prompts on a bad pick`, `a bad choice re-prompts instead of exiting`, `creating a project fails closed on an unexpected API path or workspaces body`, `project names are printed without control characters`, `a truncated project list says so`. New: `an old NOUS that answers the account request with 422 …`, `account connections switched off …`.

**Step 1: Replace `test/rc.test.ts`.**

```ts
import assert from "node:assert/strict";
import { test } from "node:test";
import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { addWorkspace, connect, rc } from "../src/cli.ts";

const PROJECT_A = "11111111-1111-4111-8111-111111111111";
const WORKSPACE = "77777777-7777-4777-8777-777777777777";
const DEVICE = "22222222-2222-4222-8222-222222222222";
const GRANT = "33333333-3333-4333-8333-333333333333";
const SCOPES = ["harness:execute", "tools:read", "tools:write", "library:read", "library:write"];
const APPROVE = `Approve this device's access to your whole NOUS account in your browser: https://nous.test/integrations/approve?request_id=${GRANT}`;
const UNSUPPORTED = "this NOUS server doesn't support account connections yet";

type Call = { url: string; method: string; auth?: string; body?: any };

/**
 * Fake NOUS for rc: the sign-in check (`/auth/me`), and the login, consent
 * and folder endpoints `connect` uses. `tokenOk` decides whether a CLI token
 * is still accepted.
 */
function fakeNous(calls: Call[], { tokenOk = (_auth?: string): boolean => true, status = 200 } = {}): typeof fetch {
  let grants = 0;
  return (async (input: any, init: any = {}) => {
    const url = String(input);
    const auth = (init.headers?.Authorization ?? init.headers?.authorization) as string | undefined;
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ url, method: init.method ?? "GET", auth, body });
    const json = (data: object, code = 200) => new Response(JSON.stringify(data), { status: code });
    if (status !== 200 && url.includes("/integrations/")) return json({ detail: "off" }, status);
    if (url.endsWith("/auth/me")) return tokenOk(auth) ? json({ id: "user" }) : json({ detail: "expired" }, 401);
    if (url.endsWith("/cli-auth/start"))
      return json({ session_id: "s", poll_token: "p", browser_url: "https://nous.test/l", verification_code: "ABCD-1234" });
    if (url.includes("/cli-auth/status/")) return json({ status: "approved", token: "cli-secret" });
    if (url.endsWith("/integrations/devices")) return json({ id: DEVICE });
    // NOUS sends the approval page root-relative (integrations/context.py).
    if (url.endsWith("/grant-requests")) return json({ id: GRANT, approval_url: `/integrations/approve?request_id=${GRANT}` });
    if (url.endsWith("/exchange")) return json({ token: `grant-${(grants += 1)}`, grant_id: GRANT });
    if (url.endsWith("/renew")) return json({ token: `renewed-${(grants += 1)}`, grant_id: GRANT });
    if (url.includes("/workspaces") && init.method === "POST") return json({ ok: true });
    return json({ status: "approved" });
  }) as typeof fetch;
}

/** Wraps a fake so one URL test answers with `reply` instead. */
function override(inner: typeof fetch, match: (url: string, init: any) => boolean, reply: () => Response): typeof fetch {
  return (async (input: any, init: any = {}) => (match(String(input), init) ? reply() : inner(input, init))) as typeof fetch;
}
const jsonResponse = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
const folderCall = (c: Call) => /\/integrations\/devices\/[^/]+\/workspaces$/.test(c.url);

function setup(fake?: typeof fetch) {
  const dir = mkdtempSync(join(tmpdir(), "nous-rc-"));
  const folder = realpathSync(mkdtempSync(join(tmpdir(), "nous-rc-folder-")));
  const calls: Call[] = [];
  const messages: string[] = [];
  const runs: string[] = [];
  const options = {
    stateDir: dir,
    apiUrl: "https://nous.test/api/v1",
    dir: folder,
    label: "Laptop",
    // Unset: the real home folder.
    home: undefined as string | undefined,
    fetchFn: fake ?? fakeNous(calls),
    announce: (m: string) => messages.push(m),
    delay: async () => {},
    probe: async () => ({ version: "0.161.0" }),
    run: async (stateDir: string) => {
      runs.push(stateDir);
    },
  };
  // Tests that rerun against an earlier run's store repoint options.stateDir.
  const state = () => JSON.parse(readFileSync(join(options.stateDir, "connection.json"), "utf8"));
  const credential = () => JSON.parse(readFileSync(join(options.stateDir, `${state().credentialHandle}.json`), "utf8"));
  const urls = () => calls.map((c) => c.url);
  const cleanup = () => {
    rmSync(dir, { recursive: true, force: true });
    rmSync(folder, { recursive: true, force: true });
  };
  return { dir, folder, calls, urls, messages, runs, options, state, credential, cleanup };
}

test("pairs the whole account with five scopes, registers the folder as an account folder, runs", async () => {
  const s = setup();
  try {
    await rc(s.options);
    assert.equal(s.state().account, true);
    assert.equal("projectId" in s.state(), false);
    assert.deepEqual(s.state().scopes, SCOPES);
    assert.deepEqual(s.calls.find((c) => c.url.endsWith("/grant-requests"))?.body, {
      account: true,
      device_id: DEVICE,
      scopes: SCOPES,
    });
    const registered = s.calls.filter(folderCall);
    assert.equal(registered.length, 1);
    assert.equal(registered[0]!.body.label, basename(s.folder));
    assert.equal("project_id" in registered[0]!.body, false);
    assert.deepEqual(s.state().workspaces.map((w: any) => [w.root, "projectId" in w]), [[s.folder, false]]);
    assert.deepEqual(s.runs, [s.dir]);
    assert.ok(s.messages.includes(APPROVE), s.messages.join("\n"));
    assert.ok(s.messages.includes(`Connected: your NOUS account ↔ ${s.folder}  (Codex 0.161.0)`));
    assert.ok(
      s.messages.includes(`In any chat on NOUS, choose Local Codex → Laptop → ${basename(s.folder)}.\nPress Ctrl-C to stop.`),
    );
    // No project picker, list or creation any more.
    assert.equal(s.calls.some((c) => c.url.includes("/projects") || c.url.includes("/api/v2/")), false);
  } finally {
    s.cleanup();
  }
});

test("a second run reuses the account pairing with no login or consent call", async () => {
  const first = setup();
  try {
    await rc(first.options);
    const again = setup();
    try {
      again.options.stateDir = first.dir;
      again.options.dir = first.folder;
      await rc(again.options);
      const u = again.urls();
      assert.equal(u.filter((x) => x.includes("/cli-auth/")).length, 0);
      assert.equal(u.filter((x) => x.endsWith("/grant-requests")).length, 0);
      assert.equal(again.calls.filter(folderCall).length, 0, "folder registered once");
      assert.ok(again.messages.some((m) => m.startsWith("Reusing binding: your NOUS account")));
      assert.equal(again.messages.some((m) => m.includes("stays active until you revoke")), false);
    } finally {
      again.cleanup();
    }
  } finally {
    first.cleanup();
  }
});

test("a stored token NOUS rejects triggers exactly one new login, and the reused pairing keeps it", async () => {
  const first = setup();
  try {
    await rc(first.options);
    const calls: Call[] = [];
    const inner = fakeNous(calls, { tokenOk: (auth) => auth === "Bearer cli-secret-2" });
    let logins = 0;
    const again = setup((async (input: any, init: any = {}) => {
      const url = String(input);
      if (url.includes("/cli-auth/status/")) {
        logins += 1;
        return jsonResponse({ status: "approved", token: "cli-secret-2" });
      }
      // Grant renewal and folder registration sign with the stored CLI token.
      const signed = url.endsWith("/renew") || /\/integrations\/devices\/[^/]+\/workspaces$/.test(url);
      if (signed && init.headers?.Authorization !== "Bearer cli-secret-2") return jsonResponse({ detail: "expired" }, 401);
      return inner(input, init);
    }) as typeof fetch);
    try {
      again.options.stateDir = first.dir;
      // again.options.dir is a new folder, so this run registers it.
      await rc(again.options);
      assert.equal(logins, 1);
      assert.equal(again.state().account, true);
      assert.equal(calls.filter((c) => c.url.endsWith("/grant-requests")).length, 0, "the pairing was reused");
      assert.ok(again.state().workspaces.some((w: { root: string }) => w.root === again.folder));
      assert.equal(again.credential().accessToken, "cli-secret-2");
      assert.deepEqual(again.runs, [first.dir]);
    } finally {
      again.cleanup();
    }
  } finally {
    first.cleanup();
  }
});

test("a different NOUS never receives the stored sign-in", async () => {
  const first = setup();
  try {
    await rc(first.options);
    const calls: Call[] = [];
    const fake = override(
      fakeNous(calls),
      (url) => url.includes("/cli-auth/status/"),
      () => jsonResponse({ status: "approved", token: "cli-other" }),
    );
    const again = setup(fake);
    try {
      again.options.stateDir = first.dir;
      again.options.apiUrl = "https://other.test/api/v1";
      await rc(again.options);
      const other = calls.filter((c) => c.url.startsWith("https://other.test/"));
      assert.ok(other.length > 0);
      assert.equal(other.filter((c) => c.auth === "Bearer cli-secret").length, 0);
      assert.equal(calls.filter((c) => c.url.endsWith("/cli-auth/start")).length, 1);
      // A server switch prints no revoke note.
      assert.equal(again.messages.some((m) => m.includes("stays active until you revoke")), false);
    } finally {
      again.cleanup();
    }
  } finally {
    first.cleanup();
  }
});

test("a stored project pairing is replaced: revoke hint after the new pairing, folders re-registered as account folders", async () => {
  const s = setup();
  const kept = realpathSync(mkdtempSync(join(tmpdir(), "nous-rc-kept-")));
  try {
    const quiet = { stateDir: s.dir, fetchFn: s.options.fetchFn, announce: () => {}, delay: async () => {} };
    await connect({ ...quiet, apiUrl: s.options.apiUrl, projectId: PROJECT_A, label: "Laptop", tools: true });
    await addWorkspace({ ...quiet, root: kept });
    const mark = s.calls.length;
    await rc(s.options);
    const after = s.calls.slice(mark);
    assert.equal(s.state().account, true);
    assert.equal(after.find((c) => c.url.endsWith("/grant-requests"))?.body.account, true);
    const registered = after.filter(folderCall);
    assert.deepEqual(registered.map((c) => [c.body.label, "project_id" in c.body]), [
      [basename(kept), false],
      [basename(s.folder), false],
    ]);
    assert.deepEqual(s.state().workspaces.map((w: any) => [w.root, "projectId" in w]), [
      [kept, false],
      [s.folder, false],
    ]);
    const connected = s.messages.indexOf("Connected to your NOUS account.");
    const hint = s.messages.findIndex(
      (m) =>
        m ===
        `Your previous approval for project ${PROJECT_A} stays active until you revoke it under Settings → Connected devices (/integrations/devices) in NOUS.`,
    );
    assert.ok(connected >= 0 && hint > connected, s.messages.join("\n"));
  } finally {
    s.cleanup();
    rmSync(kept, { recursive: true, force: true });
  }
});

test("a later run that reuses the stored sign-in still prints the full approval link", async () => {
  const s = setup();
  try {
    // A project pairing made by hand: its login recorded the website origin.
    await connect({
      stateDir: s.dir,
      fetchFn: s.options.fetchFn,
      announce: () => {},
      delay: async () => {},
      apiUrl: s.options.apiUrl,
      projectId: PROJECT_A,
      label: "Laptop",
      tools: true,
    });
    const mark = s.calls.length;
    await rc(s.options);
    assert.equal(s.calls.slice(mark).filter((c) => c.url.includes("/cli-auth/")).length, 0, "no new sign-in");
    assert.ok(s.messages.includes(APPROVE), s.messages.join("\n"));
  } finally {
    s.cleanup();
  }
});

test("a previous workspace connection gets its own revoke hint", async () => {
  const s = setup();
  try {
    await connect({
      stateDir: s.dir,
      fetchFn: s.options.fetchFn,
      announce: () => {},
      delay: async () => {},
      apiUrl: s.options.apiUrl,
      workspaceId: WORKSPACE,
      label: "Laptop",
      tools: true,
    });
    await rc(s.options);
    assert.equal(s.state().account, true);
    assert.ok(s.messages.some((m) => m.startsWith("Your previous workspace connection") && m.includes(WORKSPACE)));
  } finally {
    s.cleanup();
  }
});

test("an old NOUS that answers the account request with 422 says it does not support account connections yet", async () => {
  const calls: Call[] = [];
  const s = setup(
    override(
      fakeNous(calls),
      (url, init) => url.endsWith("/grant-requests") && init.method === "POST",
      () => jsonResponse({ detail: [{ msg: "Extra inputs are not permitted" }] }, 422),
    ),
  );
  try {
    await assert.rejects(rc(s.options), { message: UNSUPPORTED });
    assert.equal(existsSync(join(s.dir, "connection.json")), false);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("account connections switched off say the same; any other 503 passes through", async () => {
  for (const [detail, message] of [
    ["Account connections are disabled", UNSUPPORTED],
    ["Service unavailable", "NOUS request failed (503) /integrations/grant-requests"],
  ] as const) {
    const calls: Call[] = [];
    const s = setup(
      override(
        fakeNous(calls),
        (url, init) => url.endsWith("/grant-requests") && init.method === "POST",
        () => jsonResponse({ detail }, 503),
      ),
    );
    try {
      await assert.rejects(rc(s.options), { message });
      assert.equal(s.runs.length, 0);
    } finally {
      s.cleanup();
    }
  }
});

test("a failed Codex probe makes no network call", async () => {
  const s = setup();
  try {
    s.options.probe = async () => {
      throw new Error("Codex 0.158.0 is not tested with NOUS; tested versions: 0.153.4, 0.161.0");
    };
    await assert.rejects(rc(s.options), /not tested with NOUS/);
    assert.equal(s.calls.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a folder that does not exist fails before any network call", async () => {
  const s = setup();
  try {
    s.options.dir = join(s.folder, "missing");
    await assert.rejects(rc(s.options), /missing is not a folder$/);
    const file = join(s.folder, "notes.txt");
    writeFileSync(file, "x");
    s.options.dir = file;
    await assert.rejects(rc(s.options), /notes\.txt is not a folder$/);
    assert.equal(s.calls.length, 0);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a folder too broad for Codex is refused before any network call", async () => {
  const s = setup();
  const tooBroad = (root: string) => ({
    message: `${root} is too broad for Codex to work in; run nous --rc in a project folder`,
  });
  try {
    s.options.home = s.folder;
    await assert.rejects(rc(s.options), tooBroad(s.folder));
    s.options.home = undefined;
    s.options.dir = "/";
    await assert.rejects(rc(s.options), tooBroad("/"));
    s.options.dir = s.folder;
    s.options.stateDir = join(s.folder, "state");
    await assert.rejects(rc(s.options), tooBroad(s.folder));
    assert.equal(s.calls.length, 0);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a project folder inside the home folder still works", async () => {
  const s = setup();
  try {
    s.options.home = realpathSync(tmpdir());
    await rc(s.options);
    assert.equal(s.state().workspaces[0].root, s.folder);
    assert.deepEqual(s.runs, [s.dir]);
  } finally {
    s.cleanup();
  }
});

test("a server link that is not http(s) is refused", async () => {
  const cases = [
    {
      match: (url: string) => url.endsWith("/cli-auth/start"),
      reply: { session_id: "s", poll_token: "p", browser_url: "javascript:alert(1)", verification_code: "ABCD-1234" },
    },
    ...["javascript:alert(1)", "//evil.example/x", "/\\evil.example/x", "/\t/evil.example/x"].map((link) => ({
      match: (url: string) => url.endsWith("/grant-requests"),
      reply: { id: GRANT, approval_url: link },
    })),
  ];
  for (const { match, reply } of cases) {
    const calls: Call[] = [];
    const s = setup(override(fakeNous(calls), match, () => jsonResponse(reply)));
    try {
      await assert.rejects(rc(s.options), { message: "invalid NOUS response" });
      assert.ok(!s.messages.some((m) => m.includes("javascript:")));
      assert.equal(s.runs.length, 0);
    } finally {
      s.cleanup();
    }
  }
});

test("control characters in a server link are percent-encoded", async () => {
  const calls: Call[] = [];
  const login = override(
    fakeNous(calls),
    (url) => url.endsWith("/cli-auth/start"),
    () =>
      jsonResponse({
        session_id: "s",
        poll_token: "p",
        browser_url: "https://nous.test/l\u001b[31m",
        verification_code: "ABCD-1234",
      }),
  );
  const fake = override(
    login,
    (url) => url.endsWith("/grant-requests"),
    () => jsonResponse({ id: GRANT, approval_url: "https://nous.test/a\u001b[31m" }),
  );
  const s = setup(fake);
  try {
    await rc(s.options);
    assert.ok(s.messages.some((m) => m.includes("https://nous.test/l%1B[31m")));
    assert.ok(s.messages.some((m) => m.includes("https://nous.test/a%1B[31m")));
    assert.ok(!s.messages.some((m) => m.includes("\u001b")));
  } finally {
    s.cleanup();
  }
});

test("an unreachable NOUS is named instead of a bare fetch failed", async () => {
  const unreachable = (async () => {
    throw new TypeError("fetch failed", {
      cause: Object.assign(new Error("connect ECONNREFUSED"), { code: "ECONNREFUSED" }),
    });
  }) as typeof fetch;
  const s = setup(unreachable);
  try {
    await assert.rejects(rc(s.options), (error: Error) => {
      assert.equal(error.message, "Could not reach NOUS at https://nous.test/api/v1 (ECONNREFUSED)");
      assert.ok(error.cause instanceof TypeError);
      return true;
    });
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a 503 from the pairing endpoints passes through unchanged", async () => {
  const calls: Call[] = [];
  const s = setup(fakeNous(calls, { status: 503 }));
  try {
    await assert.rejects(rc(s.options), /NOUS request failed \(503\) \/integrations\/devices/);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a second 401 after a fresh sign-in is an error", async () => {
  const calls: Call[] = [];
  const s = setup(fakeNous(calls, { tokenOk: () => false }));
  try {
    await assert.rejects(rc(s.options), /^Error: NOUS refused the new sign-in$/);
    assert.equal(calls.filter((c) => c.url.endsWith("/cli-auth/start")).length, 1);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a refused approval says to run nous --rc again", async () => {
  const calls: Call[] = [];
  const fake = override(
    fakeNous(calls),
    (url) => url.includes("/grant-requests/") && !url.endsWith("/exchange"),
    () => jsonResponse({ status: "denied" }),
  );
  const s = setup(fake);
  try {
    await assert.rejects(rc(s.options), (error: Error) => {
      assert.equal(error.message, "Not approved; run nous --rc again");
      assert.equal((error.cause as Error | undefined)?.message, "authorization denied or expired");
      return true;
    });
    assert.equal(existsSync(join(s.dir, "connection.json")), false);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a refused sign-in says to run nous --rc again", async () => {
  const calls: Call[] = [];
  const fake = override(
    fakeNous(calls),
    (url) => url.includes("/cli-auth/status/"),
    () => jsonResponse({ status: "denied" }),
  );
  const s = setup(fake);
  try {
    await assert.rejects(rc(s.options), (error: Error) => {
      assert.equal(error.message, "Not approved; run nous --rc again");
      assert.equal((error.cause as Error | undefined)?.message, "authorization denied or expired");
      return true;
    });
    assert.equal(existsSync(join(s.dir, "connection.json")), false);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});
```

**Step 2: Run, expect failure.** `... test/rc.test.ts`. Expected: the type-strip run still executes; most tests fail because `rc` probes `/projects?limit=50` (the fake answers it with the catch-all `{ status: "approved" }`, so `listProjects` throws `invalid NOUS response`), and the 422/503 tests fail on the message.

**Step 3: Implement.** In `cli.ts`:

- Line 19: `import { parseArgs } from "node:util";`.
- Delete `RcProject`, `RcProjectPage`, `PROJECT_PAGE`, `plain` (661–677), `menuChoice`, `listProjects`, `createProject`, `pickProject` (690–825). Keep `authorizedGet` and add below it:

```ts
/** Whether NOUS still accepts this CLI sign-in: false on 401 only. */
async function signedIn(fetchFn: typeof fetch, base: string, token: string): Promise<boolean> {
  const response = await authorizedGet(fetchFn, `${base}/auth/me`, token);
  if (response.status === 401) return false;
  if (!response.ok) throw new Error(`NOUS request failed (${response.status}) /auth/me`);
  return true;
}
```

- `RcOptions`: delete the `prompt` member.
- Replace `rc` (897–1021):

```ts
/**
 * `nous --rc`: sign in, pair this computer with the whole NOUS account
 * (account-connection design 2026-10-14: harness:execute, tools:read,
 * tools:write, library:read, library:write), register the current folder as
 * an account folder, run. No project picker: any chat in the account can
 * choose this computer and folder. Reuses a stored account pairing; replaces
 * a project or workspace one (one pairing per computer).
 */
export async function rc(options: RcOptions): Promise<void> {
  // ...unchanged from `const base = apiBase(options.apiUrl);` through the
  // `let token: string | undefined = ...` declaration (904–943)...
  if (token === undefined || !(await signedIn(fetchFn, base, token))) {
    token = await approved(() => login(base, { ...options, fetchFn }));
    if (!(await signedIn(fetchFn, base, token))) throw new Error("NOUS refused the new sign-in");
    // connect keeps a reused pairing's stored credential, which grant renewal
    // and folder registration sign with: give it the new sign-in.
    if (sameApi && previous !== null) {
      const stored = await store.load(previous.credentialHandle).catch(() => null);
      if (stored !== null)
        await store.update(previous.credentialHandle, { ...stored, accessToken: token });
    }
  }
  const signedInToken = token;
  announce("Signed in.");
  const label = options.label ?? hostname();
  // Recorded by this run's login(), or by the one whose sign-in it reuses.
  const websiteOrigin = await storedWebsiteOrigin(store, base);
  await approved(() =>
    connect({
      stateDir: options.stateDir,
      fetchFn,
      announce,
      delay: options.delay,
      apiUrl: base,
      account: true,
      label,
      write: true,
      library: true,
      accessToken: signedInToken,
      websiteOrigin,
    }),
  );
  // The account pairing replaced a project or workspace one on this NOUS;
  // that approval outlives it there, and only the browser can revoke it now
  // (the bridge no longer holds its grant).
  if (sameApi && previous !== null && previous.account !== true) {
    const until =
      "stays active until you revoke it under Settings → Connected devices (/integrations/devices) in NOUS.";
    announce(
      previous.projectId !== undefined
        ? `Your previous approval for project ${previous.projectId} ${until}`
        : `Your previous workspace connection (${previous.workspaceId}) ${until}`,
    );
  }
  const { workspaceId } = await addWorkspace({
    stateDir: options.stateDir,
    fetchFn,
    // addWorkspace's own notice is about write roots; rc prints its summary below.
    announce: () => {},
    root,
  });
  // Name the device and folder as the web chat lists them: the stored labels.
  const stored = storedConnection(await store.readLocal("connection").catch(() => null));
  const device = stored?.label ?? label;
  const folder = stored?.workspaces.find((w) => w.id === workspaceId)?.label ?? basename(root);
  announce(`Connected: your NOUS account ↔ ${root}  (Codex ${version})`);
  announce(`In any chat on NOUS, choose Local Codex → ${device} → ${folder}.\nPress Ctrl-C to stop.`);
  await options.run(options.stateDir);
}
```

- `main` rc branch: delete `prompt: terminal.prompt,` (the `terminalPrompt` stays as the cancel path); `help`'s rc line becomes `nous-harness rc --api URL --root PATH [--label NAME]    Interactive: sign in, pair this computer with your whole NOUS account (harness:execute, tools:read, tools:write, library:read, library:write), register PATH as a folder any chat in the account can use, and run. The repository's \`nous --rc\` launcher calls this with the current folder.`

**Step 4: Run, expect pass.** `... test/rc.test.ts test/rc-main.test.ts test/rc-terminal.test.ts test/launcher.test.ts`: `rc` 20 passed; the other three unchanged and passing.

**Step 5: Commit.**

```sh
git add packages/harness-bridge/src/cli.ts packages/harness-bridge/test/rc.test.ts
git commit -m "feat(harness-bridge): nous --rc pairs the whole account, without a project picker"
```

---

### Task 12: Documentation

**Files:**
- Modify: `nous:9-11` (launcher comment)
- Modify: `terminal/README.md:36-130` (`## nous --rc` section)
- Modify: `packages/harness-bridge/README.md:5-17` (Quick start), and add `### Account connections` after the workspace subsection (before line 113 `## Binding reuse…`)
- Modify: `docs/engineering/harness-bridge.md:7` (User workflow step 1), and add `### Account connections` after the Workspace subsection (after line 86)
- Modify: `docs/plans/2026-10-12-nous-rc-design.md` (dated amendment after the 2026-10-13 block; original text untouched)

**Step 1: Edit.**

`nous:9-11`:

```bash
# `nous --rc` pairs this computer with your whole NOUS account for Local Codex
# and registers the current folder (packages/harness-bridge). Symlink this file
# into ~/.local/bin from a checkout that tracks origin/develop.
```

`terminal/README.md` — rewrite these paragraphs of the section (the install, Node, Codex, lock, cancel and server paragraphs stay):

- Opening: "`nous --rc` pairs this computer with your whole NOUS account so that any chat on NOUS can run Codex in the current folder."
- Flow: "It checks Codex first, then signs you in (a browser link and a code, unless the saved sign-in still works), asks you to approve this computer for your whole NOUS account in the browser when needed, registers the current folder, and runs the bridge. Then, in any chat on NOUS, choose **Local Codex**, this computer and the folder; the last lines `nous --rc` prints name both: `Connected: your NOUS account ↔ <folder>  (Codex <version>)` and `In any chat on NOUS, choose Local Codex → <device> → <folder>.` A server without account connections prints `this NOUS server doesn't support account connections yet`; pair one project by hand there with `nous-harness connect --project` (see the bridge README)."
- Scopes: "The approval covers every project in workspaces of your organization that you own or belong to (not archived), checked again on every call, so new projects are included and removed ones drop out. It allows reading documents, requesting notes and library changes, and running Codex. Reversible library changes (saving papers to a folder, renaming one, editing a paper's title or tags) run without asking; deleting a folder and ingesting papers still ask in the browser. A Codex run started from a chat uses only that chat's project. A pairing `nous --rc` creates cannot publish files, read a chat's handoff or a project's selected memories."
- Pairings: replace "Running `nous --rc` again for the same project…" with "Running `nous --rc` again skips the browser while the account pairing is still live (it lapses 3 to 15 minutes after the bridge stops) or can resume from your approval (up to 30 days after it lapsed). Otherwise it asks you to approve again, and NOUS lists this computer as a new device. …(keep the revoke/label/Disconnect sentences)…" and replace "Picking a different project also asks…" with "If this computer was paired with one project (or one workspace) before, the first `nous --rc` replaces that pairing, moves the folders you registered to the account pairing, and prints that the old approval stays active until you revoke it under Settings → Connected devices (`/integrations/devices`); a server switch prints no such note."

`packages/harness-bridge/README.md` Quick start (line 13): replace "shows a numbered project list with `n) New project`, pairs this computer with the chosen project" with "pairs this computer with your whole NOUS account (`connect --account --write --library`)"; line 17: replace "It requests `harness:execute` and `tools:read` only; a reused pairing keeps…" with "It requests `harness:execute`, `tools:read`, `tools:write`, `library:read` and `library:write`; a server without account connections answers with `this NOUS server doesn't support account connections yet`." Replace the paragraph starting "The browser approval is skipped only when the stored pairing is for the same project" with the same reuse sentence used in `terminal/README.md`. New subsection:

```markdown
### Account connections

Connect with `--account` instead of `--project` or `--workspace` to bind the grant to your whole NOUS account: every project in workspaces of your organization that you own or belong to, not archived, re-checked on every call. `nous --rc` does this for you.

    pnpm --filter @nous/harness-bridge start connect --api https://nous.example/api/v1 --account --label "My computer" --write --library

- **Scopes.** Always `harness:execute`, `tools:read` and `library:read` (so `list_library` can name the projects); `--write` adds `tools:write`, `--library` adds `library:write`. `--publish`, `--chat`, `--handoff` and `--context` are refused: each belongs to one project.
- **Folders.** `workspace add` registers a folder with no project; any chat in the account can pick it. One run per folder at a time, as before.
- **Runs.** A chat in project P that picks this computer gets a run grant for P under the account approval. The start command names P (`projectId`), and the run's MCP child (`nous-harness mcp --project P`) fills in or enforces `project_id` on every tool, hides `list_library`, and refuses `create_folder`, `move_papers_between_folders` and `update_document_metadata`. This keeps the model in the chat's project; it is not a security boundary (the child holds the account credential).
- **Standalone Codex.** `mcp install` prints an unpinned registration: its tools take a `project_id`, and `list_library` lists the choices with their workspaces; `create_folder` takes a `workspace_id`.
- **Switching.** Connecting with `--account` replaces a project or workspace connection and carries its folders over as account folders; the old approval stays until revoked at `/integrations/devices`.
- **Old servers.** A NOUS without account connections (422) or with them switched off (503 `Account connections are disabled`) makes `connect --account` print `this NOUS server doesn't support account connections yet`.

The backend contract is in [`docs/engineering/harness-bridge.md`](../../docs/engineering/harness-bridge.md#account-connections).
```

`docs/engineering/harness-bridge.md` line 7: replace "for the current folder with `harness:execute` and `tools:read`" with "for the current folder with an account-wide approval (`harness:execute`, `tools:read`, `tools:write`, `library:read`, `library:write`; see [Account connections](#account-connections))". New subsection after the workspace one:

```markdown
### Account connections

Design: [`docs/plans/2026-10-14-account-connection-design.md`](../plans/2026-10-14-account-connection-design.md). PR 1 owns the data model, the account rule and grant validation; this section records the Local Codex part.

- **Start.** `create_chat_context` (`backend/src/api/agent/harness_streaming.py`) accepts a folder tied to the chat's project, or an account folder (`project_id` NULL) when the device holds exactly one live account approval and the chat's project is in the account on that call. It mints the run's project-kind grant with `mint_integration_grant(..., consent_kind=...)`: the folder's kind picks the approval, so a device holding both a project and an account approval is deterministic. Every folder, device or project refusal is an opaque `IntegrationAccessDenied`.
- **Authorization.** `authorize_external_submission` (`services/harness/runs.py`) is the single place a run's project is checked against the account: under an account approval (its pairing grant or a run grant minted from it) the project must be in the account on every call, and an account folder serves it. `delivery._authorize` takes the project from the grant, or from the run for an account pairing grant (`services/harness/account.py` `run_project`).
- **Leasing and the long-run swap.** For an account pairing, `lease_commands` / `lease_runs` select the device's locked runs whose project is in the account, then authorize each. The swap of `HarnessSession.grant_id` to the bridge's pairing grant is unchanged; `dispatch_pending`, `build_harness_prompt`, `native_request_context` / `resolve_native_request` and `context_for_accepted_run` all work with an account grant on the session (the request's project must be its run's). The consent-lineage check (`request_id`) is unchanged: the run grant and the pairing grant share the account approval.
- **Start command.** A `start` command leased under an account pairing carries `projectId`; it is omitted from the wire otherwise, because bridges before account connections refuse unknown keys. Events never carry it.
- **A project leaving the account.** Before the start is delivered, the run fails with `This project is no longer reachable from this computer.`; once running, its next command is refused like a run whose chat moved project (no chat notice yet; follow-up).
```

`docs/plans/2026-10-12-nous-rc-design.md`, after the `**Amendments 2026-10-13**` block:

```markdown
**Amendment 2026-10-1N** (account connections, [design](2026-10-14-account-connection-design.md), PR 2): `nous --rc` is account-only. It no longer lists, picks or creates projects; it pairs the whole NOUS account (`harness:execute`, `tools:read`, `tools:write`, `library:read`, `library:write`), registers the current folder as an account folder, and prints `Connected: your NOUS account ↔ <folder>  (Codex <v>)` and `In any chat on NOUS, choose Local Codex → <device> → <folder>.` The stored sign-in is checked with `GET /auth/me`. Replacing a stored project or workspace pairing prints the revoke hint and carries its folders over as account folders. A server without account connections prints `this NOUS server doesn't support account connections yet`. The "Pairings per computer" and "replaced project's consent" decisions still hold, applied to the account pairing.
```

(Use the merge date for `2026-10-1N`.)

**Step 2: Check.**

```sh
make docs-lint
git diff --check -- docs/ terminal/README.md packages/harness-bridge/README.md nous
pnpm --filter @nous/harness-bridge exec node --experimental-sqlite --import tsx --test --test-timeout=60000 test/mcp.test.ts
```

Expected: docs-lint clean; no whitespace errors; `mcp.test.ts` still passes (its Status-paragraph test reads `harness-bridge.md`; the Status line is unchanged). Resolve each new relative link by hand.

**Step 3: Commit.**

```sh
git add nous terminal/README.md packages/harness-bridge/README.md docs/engineering/harness-bridge.md docs/plans/2026-10-12-nous-rc-design.md
git commit -m "docs: nous --rc pairs the whole account; account connections for Local Codex"
```

---

### Task 13: PR 2 verification and PR

**Step 1: Targeted backend tests.**

```sh
PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q --no-cov \
  backend/tests/unit/services/harness backend/tests/unit/services/integrations \
  backend/tests/unit/api/test_harness_workspace_guard.py backend/tests/unit/api/agent/test_harness_streaming.py \
  backend/tests/unit/api/test_agent_streaming_error_categories.py backend/tests/unit/architecture
```

Expected: all pass; `test_account_runs.py` `22 passed`.

**Step 2: PostgreSQL** (Task 6 step 1 server):

```sh
ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch ENVIRONMENT=testing \
  PYTHONPATH=backend <PY> -m pytest -c backend/pytest.ini -q \
  backend/tests/integration/test_harness_account_postgres.py \
  backend/tests/integration/test_harness_dispatch_postgres.py \
  backend/tests/integration/test_harness_cancel_delivery_postgres.py
pg_ctl -D "$PGDIR/data" stop -m fast
```

Expected: `5 passed`, `2 passed`, `2 passed`. A `skipped` is NOT RUN.

**Step 3: Bridge suite and type-check.**

```sh
pnpm --filter @nous/harness-bridge test
pnpm --filter @nous/harness-bridge type-check
```

Expected: all pass; totals = Task 0 baseline + 3 (`run-project`) + 5 (`pin`) + 4 (`mcp`) + 5 (`connect-reuse`) − 4 (`rc`: 24 → 20) = baseline + 13 tests. `tsc --noEmit` clean.

**Step 4: Contract and lint gates.**

```sh
<PY> scripts/ci/generate_openapi.py --check          # expect no drift: BridgeCommand is a socket schema, not in openapi.json
(cd backend && <PY> ../scripts/ci/check_alembic.py)  # no new migration in PR 2
scripts/ci/run_local_ci.sh --base origin/develop --skip-tests
<PY> -m ruff check backend/src
<PY> -m black --check backend/src/services/harness backend/src/api/harness.py backend/src/api/agent/harness_streaming.py \
  backend/src/schemas/harness.py backend/src/services/integrations/context.py backend/tests/unit/services/harness \
  backend/tests/integration/test_harness_account_postgres.py backend/tests/unit/api/test_harness_workspace_guard.py
<PY> -m isort --check-only <same paths>
<PY> -m mypy --disallow-untyped-defs backend/src/services/harness/account.py   # the one added source file
```

Expected: all clean.

**Step 5: Personal-data check before pushing** (the repository is public): `git diff origin/develop... | grep -nE '@[a-z0-9.-]+\.[a-z]{2,}|/Users/'` prints nothing new.

**Step 6: Push and open the PR.**

```sh
git push
gh pr create --base develop --title "feat(harness): Local Codex from any chat under an account approval; nous --rc pairs the account" --body-file <scratch>/pr2-body.md
```

PR body outline:

- **Summary.** Account approvals run Local Codex from any chat in the account (design §3/§4); `nous-harness connect --account`; account folders; per-run project pinning of NOUS tools; `nous --rc` account-only (no picker). Depends on PR 1 (#…); PR 3 (website) follows.
- **Security invariants and evidence.** The single account check (`authorize_external_submission`), the long-run swap, consent lineage, approval-card project match, project-pairing regression; table of mutations MA1–MA8, MA10 with dates and FAIL observations; the three-guard regression explained.
- **Compatibility.** `projectId` only on account starts, omitted on the wire otherwise (old bridges); old `connection.json` files load; `nous --rc` on a server without PR 1 prints `this NOUS server doesn't support account connections yet`; no OpenAPI change, no migration.
- **Contract deviations** (from the shared plan contract): items 1–9 at the top of this section.
- **Tests run.** Backend unit (counts), PostgreSQL (`5 passed` + existing), bridge `pnpm test` totals, type-check, lint gate, OpenAPI check.
- **NOT RUN.** Live on dev: `nous --rc` pairs the account; chats in two projects run Codex in one folder in turn; a self-started Codex (`mcp install`) lists folders across workspaces; a reversible library change runs without asking and a folder deletion asks; a project archived mid-run is refused at its next command; dev flag `ACCOUNT_CONNECTIONS_ENABLED` turned on in `values-aws.yaml` (separate GitOps change, after merge).
- **Follow-ups.** A chat-visible notice when a running run's project leaves the account; let `update_document_metadata` take an optional `project_id` so pinned runs can edit metadata in their own project; PR 3 maps the `create_chat_context` refusal to `DeviceNotConnectedToProject`.
- Footer: `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

**Step 7: After CI.** Verify review-bot findings against the current head; do not self-approve or merge over a failing required check.

---

# PR 3 — Website

## PR 3: Website changes for the account connection (approval page, Connected devices, composer)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this section task by task.

**Goal:** Make an account approval (design §5) visible and safe on the website. The approval page states plainly what "the whole account" covers and warns about unattended library changes. Connected devices shows "Whole account" consents and account folders. The chat composer offers only computers and folders that can run Codex for the chat's project, and names a server refusal instead of showing the generic error.

**Architecture:** TanStack Query stays the only cache for these server reads. The approval page and Connected devices read PR 1's new fields through the generated types: `GrantRequestDTO.binding_kind` / `account_summary` and `ConnectionConsent.kind === 'account'`. Account folders come from the existing `GET /integrations/devices/{id}/workspaces`, under the composer's query key, so there is one cache. `useHarnessConnection` takes the chat's project id from `useChatStreaming`, which already resolves it (`selectCurrentThreadProjectId` plus `resolveBoundProjectId`). It filters with PR 1's `DeviceListItemDTO.account_wide` / `project_ids` and the nullable `WorkspaceBindingDTO.project_id`. The server refusal gets a stable category, as `device_bound_to_another_chat` already does. That takes a small backend change, which lives in this PR (Tasks 1–2).

**Tech Stack:** Next.js App Router, React, TanStack Query, Vitest and Testing Library (`@/test/test-utils`), FastAPI and pytest for Tasks 1–2.

**Preconditions:** PR 1 and PR 2 are merged into `develop`. `frontend/src/types/generated/api.d.ts` already carries PR 1's fields. Cut the branch from a fresh `origin/develop` in your own worktree (`.worktrees/account-website`). Line ranges below are from the preview merge (`fdc9c00fc` + #1983/#1986/#1987). PR 1 and PR 2 move some of them, so match on the quoted code, not on the number. End every commit message with the session's attribution trailer.

### Notes for the reviewer: guesses and contract deviations

**Additions to the contract** (all in PR 3; no PR 1 or PR 2 name changes):
- `DeviceNotConnectedToProject(IntegrationAccessDenied)` in `backend/src/services/integrations/context.py`, with `client_message = "This computer isn't connected to this project."`
- `AgentErrorCategory.DEVICE_NOT_CONNECTED_TO_PROJECT = "device_not_connected_to_project"` in `backend/src/shared/enums.py`, with frontend mirrors in `frontend/src/services/agentStreamEvents.ts` and `ERROR_CATEGORY_HELP`.
- `AgentErrorCategory` is **not** in `backend/openapi.json` (checked: `device_bound_to_another_chat` does not appear there), so nothing is regenerated. Task 10 still runs `generate_openapi.py --check` to prove that.
- `create_chat_context` becomes a thin wrapper around `_authorize_chat_run`. The wrapper turns every `IntegrationAccessDenied` (except `DeviceBoundToAnotherChat`) into `DeviceNotConnectedToProject`. A folder the chat cannot use now raises `IntegrationAccessDenied` instead of `ValueError`.
- Frontend type alias `ApiBindingKind = ApiConnectionConsent['kind']` in `frontend/src/types/api/integration-connections-contract.ts`.

**Why the backend change is in PR 3 and not PR 1:** the raise site is PR 2's `create_chat_context`, which does not exist in its final form until PR 2 merges. The change is a few lines plus tests, and the copy is a website decision. PR 1 needs nothing for it. PR 2 must keep device, folder and project refusals in `create_chat_context` as `IntegrationAccessDenied` (or a subclass), not `ValueError`, so the wrapper can name them. Every refusal maps to one message, including a foreign or unknown device or folder id, so the text is not an enumeration oracle.

**Guesses:**
1. **Optional or required generated fields.** I don't know whether PR 1's fields come out required or optional in TypeScript (`binding_kind`, `account_summary`, `account_wide`, `project_ids`). The code reads all of them defensively (`unknown` checks, `??`), so it works either way. It also works against a backend older than PR 1, because Vercel can ship before the backend image.
2. **Copy.** The copy is mine and needs owner review: the "Covers" rule text, the account permission sentences and summaries, the warning, "Codex runs only in chats that belong to a project.", "None of your paired computers is connected to this project.", "No account folders on this computer yet." The refusal text is the design's sentence plus a final period.
3. **Three states for the chat's project.** `useHarnessConnection(threadId, projectId?)`:
   - a string filters the lists;
   - `null` (a loaded chat with no project) lists nothing and says why;
   - `undefined` (not known yet: a new chat with no `?projectId=`, or the thread not in the store) filters nothing. That is today's behaviour, and every existing caller and test is unchanged.
4. **Account folders on a device without account access.** An account folder is offered only on a computer whose `account_wide` is `true`. A stale account folder on a project-only computer is hidden, because the server would refuse it (design §3).
5. **Stale selection.** A remembered computer that can no longer serve the chat is treated as unselected ("Choose a paired computer."), because the picker no longer shows it.
6. **Warning scope.** The `library:write` warning appears only for account requests. Project and workspace wording and layout are unchanged, as instructed.

### What PR 1 must provide (requirements on PR 1's section)

- **R1. Device list fields.** `list_devices` fills `DeviceListItemDTO.account_wide` and `project_ids` from the same consent set it already reads for `bound_thread_ids`: consumed, not revoked, not deleted, and holding `harness:execute`, which is what a Codex mint needs.
  - `account_wide` is `true` while such an account consent is live.
  - `project_ids` lists the projects of such project consents, chat-bound ones included. The composer checks chat binding separately through `bound_thread_ids`.
  - Both fields are always present on the response.
- **R2. Account folders in the folder list.** `list_workspaces` (`GET /integrations/devices/{id}/workspaces`) returns account folders (`project_id` null) while the device holds a live account consent. Today it calls `authorized_project(binding.project_id)` on every row (`context.py:984-1005`), which would drop or break on `None`.
- **R3. Account consents on Connected devices.** `list_connections` keeps account consents (`kind='account'`, project and workspace ids and labels all null). Design §5: `_binding_label` must not drop a consent with no project or workspace label.
- **R4. Grant request fields.** `GrantRequestDTO.binding_kind` is always sent. `account_summary` is set for pending account requests.

### What PR 2 must know (not a PR 3 change)

- Keep `create_chat_context` refusals as `IntegrationAccessDenied` (see "Why the backend change is in PR 3" above).
- **Risk: two live consents fail closed.** After `nous --rc` replaces a project pairing, the old project approval stays live until the user revokes it (design §4). `mint_integration_grant` fails closed when more than one consent matches (`context.py:465-479`, `len(matching_consents) != 1`). PR 2 must define precedence (account approval, or the one the bridge's pairing grant descends from). Otherwise the composer lists the computer and every run is refused with "This computer isn't connected to this project."

---

### Task 1: Backend: a named, stable refusal on the chat stream

**Files:**
- Modify: `backend/src/services/integrations/context.py:45-57` (add a class after `DeviceBoundToAnotherChat`)
- Modify: `backend/src/shared/enums.py:367-370` (add an enum member after `DEVICE_BOUND_TO_ANOTHER_CHAT`)
- Modify: `backend/src/api/agent/streaming.py:77` (import), `:287-317` (`_stream_failure_category`, `_stream_failure_message`)
- Test: `backend/tests/unit/api/test_agent_streaming_error_categories.py:23-28` (imports); add tests after `test_a_device_bound_to_another_chat_is_named_on_the_wire` (`:322-348`)
- Test: `backend/tests/contract/test_sse_error_categories.py:44-55` (`_EXPECTED_CATEGORY_VALUES`)

**Step 1: Write the failing tests**

In `backend/tests/unit/api/test_agent_streaming_error_categories.py`, change the import block at `:24-27` to:

```python
from src.services.integrations.context import (
    DeviceBoundToAnotherChat,
    DeviceNotConnectedToProject,
    IntegrationAccessDenied,
)
```

Then add after `test_a_device_bound_to_another_chat_is_named_on_the_wire`:

```python
NOT_CONNECTED_MESSAGE = "This computer isn't connected to this project."


@pytest.mark.asyncio
async def test_a_device_not_connected_to_the_project_is_named_on_the_wire() -> None:
    """Account connections: a computer with no approval covering the chat's
    project (or a folder the chat cannot use) is named, not a generic error."""
    thread = SimpleNamespace(id="11111111-1111-1111-1111-111111111302")
    with (
        patch(
            "src.api.agent.streaming._resolve_thread",
            new=AsyncMock(return_value=(thread, None)),
        ),
        patch(
            "src.api.agent.harness_streaming.create_chat_context",
            new=AsyncMock(side_effect=DeviceNotConnectedToProject()),
        ) as mint,
    ):
        frames = await _run_stream(
            _RaisingGraph(AssertionError("the graph must not run")),
            execution_provider="codex",
            device_id=uuid4(),
            workspace_id=uuid4(),
        )
    mint.assert_awaited_once()
    payload = _error_payload(frames)
    assert payload["category"] == "device_not_connected_to_project"
    assert payload["error"] == NOT_CONNECTED_MESSAGE


def test_the_two_device_refusals_keep_their_own_categories() -> None:
    assert (
        _stream_failure_category(DeviceNotConnectedToProject())
        is AgentErrorCategory.DEVICE_NOT_CONNECTED_TO_PROJECT
    )
    assert _stream_failure_message(DeviceNotConnectedToProject()) == (
        NOT_CONNECTED_MESSAGE
    )
    assert (
        _stream_failure_category(DeviceBoundToAnotherChat())
        is AgentErrorCategory.DEVICE_BOUND_TO_ANOTHER_CHAT
    )
    # A bare denial stays generic: only the named subclass is surfaced.
    assert _stream_failure_category(IntegrationAccessDenied()) is (
        AgentErrorCategory.INTERNAL
    )
```

In `backend/tests/contract/test_sse_error_categories.py`, append to `_EXPECTED_CATEGORY_VALUES` (after `"device_bound_to_another_chat",`):

```python
    "device_not_connected_to_project",
```

**Step 2: Run the tests and confirm they fail**

Run: `<PY> -m pytest -c backend/pytest.ini -q backend/tests/unit/api/test_agent_streaming_error_categories.py backend/tests/contract/test_sse_error_categories.py`
Expected: FAIL. Collection error: `ImportError: cannot import name 'DeviceNotConnectedToProject' from 'src.services.integrations.context'`; the contract list assertion also fails.

**Step 3: Write the minimal implementation**

`backend/src/services/integrations/context.py`: add directly after the `DeviceBoundToAnotherChat` class (after line 57):

```python
class DeviceNotConnectedToProject(IntegrationAccessDenied):
    """This computer cannot run Codex for the chat's project: it holds neither
    an account approval covering the project nor an approval for the project,
    or the chosen folder is not usable from this project.

    One message for every such cause, including a device or folder id that is
    not the caller's, so the text reveals nothing a guess could not. Still an
    IntegrationAccessDenied, so every caller that maps denials keeps doing so.
    """

    client_message: ClassVar[str] = "This computer isn't connected to this project."
```

`backend/src/shared/enums.py`: add after `DEVICE_BOUND_TO_ANOTHER_CHAT = "device_bound_to_another_chat"` (line 370):

```python
    #: The chosen computer holds no approval covering this chat's project
    #: (account or project), or the chosen folder is not usable here.
    DEVICE_NOT_CONNECTED_TO_PROJECT = "device_not_connected_to_project"
```

`backend/src/api/agent/streaming.py`: replace line 77 with:

```python
from src.services.integrations.context import (
    DeviceBoundToAnotherChat,
    DeviceNotConnectedToProject,
)
```

In `_stream_failure_category`, add after the `DeviceBoundToAnotherChat` branch:

```python
    if isinstance(exc, DeviceNotConnectedToProject):
        return AgentErrorCategory.DEVICE_NOT_CONNECTED_TO_PROJECT
```

In `_stream_failure_message`, add after the `DeviceBoundToAnotherChat` branch:

```python
    if isinstance(exc, DeviceNotConnectedToProject):
        return DeviceNotConnectedToProject.client_message
```

Update `_stream_failure_category`'s docstring list to read "...unknown thread, a device bound to another chat or not connected to this chat's project...".

**Step 4: Run the tests and confirm they pass**

Run: `<PY> -m pytest -c backend/pytest.ini -q backend/tests/unit/api/test_agent_streaming_error_categories.py backend/tests/contract/test_sse_error_categories.py backend/tests/unit/agent/test_agent_error_categories.py`
Expected: PASS.

Mutation check: delete the new `_stream_failure_category` branch. `test_a_device_not_connected_to_the_project_is_named_on_the_wire` must fail with `'internal' == 'device_not_connected_to_project'`. Restore it.

**Step 5: Lint and commit**

```bash
<PY> -m ruff check backend/src/services/integrations/context.py backend/src/shared/enums.py backend/src/api/agent/streaming.py backend/tests/unit/api/test_agent_streaming_error_categories.py backend/tests/contract/test_sse_error_categories.py
<PY> -m black --check backend/src/services/integrations/context.py backend/src/shared/enums.py backend/src/api/agent/streaming.py backend/tests/unit/api/test_agent_streaming_error_categories.py backend/tests/contract/test_sse_error_categories.py
<PY> -m isort --check-only backend/src/services/integrations/context.py backend/src/api/agent/streaming.py backend/tests/unit/api/test_agent_streaming_error_categories.py
git add backend/src/services/integrations/context.py backend/src/shared/enums.py backend/src/api/agent/streaming.py backend/tests/unit/api/test_agent_streaming_error_categories.py backend/tests/contract/test_sse_error_categories.py
git commit -m "feat(harness): name a computer not connected to the chat's project on the stream"
```

### Task 2: Backend: `create_chat_context` raises the named refusal

**Files:**
- Modify: `backend/src/api/agent/harness_streaming.py:19` (import), `:33-84` (`create_chat_context`). PR 2 rewrites this body for account folders; keep PR 2's body and move it as described.
- Test: `backend/tests/unit/api/agent/test_harness_streaming.py` (append)

**Step 1: Write the failing tests**

Append to `backend/tests/unit/api/agent/test_harness_streaming.py`, and add the new imports to the file's import block:

```python
from unittest.mock import AsyncMock

from src.core.config import settings
from src.services.integrations.context import (
    DeviceBoundToAnotherChat,
    DeviceNotConnectedToProject,
    IntegrationAccessDenied,
)

_OWNER = SimpleNamespace(id=uuid4(), organization_id=uuid4())
_CHAT = SimpleNamespace(id=uuid4(), source_project_id=uuid4())


async def _start_codex(db: Any = None) -> Any:
    return await harness_streaming.create_chat_context(
        db if db is not None else SimpleNamespace(),
        current_user=_OWNER,
        thread=_CHAT,
        device_id=uuid4(),
        workspace_id=uuid4(),
    )


@pytest.mark.asyncio
async def test_a_computer_without_access_to_the_project_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
    monkeypatch.setattr(
        harness_streaming,
        "_authorize_chat_run",
        AsyncMock(side_effect=IntegrationAccessDenied()),
    )
    with pytest.raises(DeviceNotConnectedToProject):
        await _start_codex()


@pytest.mark.asyncio
async def test_a_chat_bound_computer_keeps_its_own_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
    monkeypatch.setattr(
        harness_streaming,
        "_authorize_chat_run",
        AsyncMock(side_effect=DeviceBoundToAnotherChat()),
    )
    with pytest.raises(DeviceBoundToAnotherChat) as raised:
        await _start_codex()
    assert type(raised.value) is DeviceBoundToAnotherChat


@pytest.mark.asyncio
async def test_server_policy_refusals_stay_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorize = AsyncMock()
    monkeypatch.setattr(harness_streaming, "_authorize_chat_run", authorize)
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", False)
    with pytest.raises(ValueError, match="disabled by server policy"):
        await _start_codex()
    authorize.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_folder_this_chat_cannot_use_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No folder row matches this device, folder and chat project. If PR 2's
    # folder lookup reads through db.execute instead of db.scalar, stub that
    # call to return no row instead; the assertion is the point.
    monkeypatch.setattr(settings, "HARNESS_BRIDGE_ENABLED", True)
    mint = AsyncMock()
    monkeypatch.setattr(harness_streaming, "mint_integration_grant", mint)
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))
    with pytest.raises(DeviceNotConnectedToProject):
        await _start_codex(db)
    mint.assert_not_awaited()
```

**Step 2: Run the tests and confirm they fail**

Run: `<PY> -m pytest -c backend/pytest.ini -q backend/tests/unit/api/agent/test_harness_streaming.py`
Expected: FAIL. The first two tests raise `AttributeError: <module 'src.api.agent.harness_streaming'> has no attribute '_authorize_chat_run'`. The folder test raises `ValueError: The selected workspace is not bound to this chat project`.

**Step 3: Write the minimal implementation**

In `backend/src/api/agent/harness_streaming.py`, replace the import at line 19 with:

```python
from src.services.integrations.context import (
    DeviceBoundToAnotherChat,
    DeviceNotConnectedToProject,
    IntegrationAccessDenied,
    mint_integration_grant,
)
```

Split `create_chat_context` into a wrapper and `_authorize_chat_run`. The body below is the preview's. With PR 2 merged, move PR 2's folder check, mint and re-resolve into `_authorize_chat_run` verbatim, changing only the "folder not usable" raise to `IntegrationAccessDenied()`:

```python
async def create_chat_context(
    db: Any,
    *,
    current_user: Any,
    thread: Any,
    device_id: UUID,
    workspace_id: UUID,
) -> IntegrationContext:
    """Mint a short-lived run-authority from prior browser-approved consent.

    The returned token is intentionally discarded. It is never returned to the
    browser; only its database identity is attached to the accepted run.

    Every refusal about this computer, its folder or the chat's project is
    raised as DeviceNotConnectedToProject, one message for all of them, so the
    chat can name it without revealing which check failed. A computer bound to
    another chat keeps its own message; server-policy refusals stay ValueError
    and so the generic error.
    """
    from src.core.config import settings

    if not settings.HARNESS_BRIDGE_ENABLED:
        raise ValueError("Local harness execution is disabled by server policy")
    if thread is None or thread.source_project_id is None:
        raise ValueError("Codex runs require a project-bound NOUS chat thread")
    if current_user.organization_id is None:
        raise ValueError("Codex runs require an organization-backed account")
    try:
        return await _authorize_chat_run(
            db,
            current_user=current_user,
            thread=thread,
            device_id=device_id,
            workspace_id=workspace_id,
        )
    except DeviceBoundToAnotherChat:
        raise
    except IntegrationAccessDenied as exc:
        raise DeviceNotConnectedToProject() from exc


async def _authorize_chat_run(
    db: Any,
    *,
    current_user: Any,
    thread: Any,
    device_id: UUID,
    workspace_id: UUID,
) -> IntegrationContext:
    """Check the folder and mint the run's grant (guards already passed)."""
    binding = await db.scalar(
        select(WorkspaceBinding).where(
            WorkspaceBinding.device_id == device_id,
            WorkspaceBinding.workspace_id == workspace_id,
            WorkspaceBinding.project_id == thread.source_project_id,
            WorkspaceBinding.is_deleted.is_(False),
        )
    )
    if binding is None:
        # Opaque here; the caller names it for the chat.
        raise IntegrationAccessDenied()
    issued = await mint_integration_grant(
        db,
        user_id=current_user.id,
        organization_id=current_user.organization_id,
        project_id=thread.source_project_id,
        scopes=frozenset({"harness:execute"}),
        thread_id=thread.id,
        device_id=device_id,
    )
    # Re-resolve through the same validation gate used by the bridge transport.
    grant = await db.get(IntegrationGrant, issued.grant_id, populate_existing=True)
    if grant is None:
        raise PermissionError("Could not bind Codex run to its accepted grant")
    return IntegrationContext(
        user_id=grant.user_id,
        organization_id=grant.organization_id,
        project_id=grant.project_id,
        thread_id=grant.thread_id,
        run_id=None,
        grant_id=grant.id,
    )
```

The plain `PermissionError` is deliberately not an `IntegrationAccessDenied`, so it keeps the generic treatment.

**Step 4: Run the tests and confirm they pass**

Run: `<PY> -m pytest -c backend/pytest.ini -q backend/tests/unit/api/agent/test_harness_streaming.py backend/tests/unit/api/test_agent_streaming_error_categories.py backend/tests/unit/services/integrations backend/tests/unit/architecture/test_agent_api_boundaries.py`
Expected: PASS.

Mutation checks:
- Delete the `except IntegrationAccessDenied` clause. `test_a_computer_without_access_to_the_project_is_named` and `test_a_folder_this_chat_cannot_use_is_named` must fail.
- Swap the two `except` clauses. `test_a_chat_bound_computer_keeps_its_own_refusal` must fail, because `DeviceBoundToAnotherChat` is an `IntegrationAccessDenied`.

Restore both.

**Step 5: Lint and commit**

```bash
<PY> -m ruff check backend/src/api/agent/harness_streaming.py backend/tests/unit/api/agent/test_harness_streaming.py
<PY> -m black --check backend/src/api/agent/harness_streaming.py backend/tests/unit/api/agent/test_harness_streaming.py
<PY> -m isort --check-only backend/src/api/agent/harness_streaming.py backend/tests/unit/api/agent/test_harness_streaming.py
git add backend/src/api/agent/harness_streaming.py backend/tests/unit/api/agent/test_harness_streaming.py
git commit -m "feat(harness): refuse a computer or folder outside the chat's project by name"
```

### Task 3: Website: show the named refusal in the chat

**Files:**
- Modify: `frontend/src/services/agentStreamEvents.ts:48-59` (`AGENT_ERROR_CATEGORIES`)
- Modify: `frontend/src/hooks/chat/useHarnessConnection.ts:76-79` (add an exported message after `BOUND_TO_ANOTHER_CHAT_MESSAGE`)
- Modify: `frontend/src/components/chat/aui/AuiMessage.tsx:56` (import), `:668-681` (`ERROR_CATEGORY_HELP`), `:686-700` (retry comment)
- Test: `frontend/src/components/chat/aui/__tests__/AuiMessage.errorCategory.test.tsx` (add after the `device_bound_to_another_chat` case, about `:153-163`)
- Test: `frontend/src/services/__tests__/agentChatService.errorCategory.test.ts:89-106` (the "accepts every category" list)

**Step 1: Write the failing tests**

In `AuiMessage.errorCategory.test.tsx`, add after `it('names a computer bound to another chat and keeps Retry', …)`:

```tsx
  it('names a computer not connected to this project and keeps Retry', () => {
    // Retry re-sends with the composer's CURRENT computer and folder, so it
    // succeeds once the user picks one that can run this chat's project.
    renderErrorMessage('device_not_connected_to_project');
    expect(
      screen.getByText("This computer isn't connected to this project.")
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });
```

In `agentChatService.errorCategory.test.ts`, add `'device_not_connected_to_project',` after `'device_bound_to_another_chat',` in the `for (const known of [...])` list.

**Step 2: Run the tests and confirm they fail**

Run: `pnpm --dir frontend exec vitest run src/components/chat/aui/__tests__/AuiMessage.errorCategory.test.tsx src/services/__tests__/agentChatService.errorCategory.test.ts`
Expected: FAIL.
- AuiMessage: `Unable to find an element with the text: This computer isn't connected to this project.`, because the default helper renders.
- agentChatService: `expected undefined to be 'device_not_connected_to_project'`.

**Step 3: Write the minimal implementation**

`frontend/src/services/agentStreamEvents.ts`: append to `AGENT_ERROR_CATEGORIES` after `'device_bound_to_another_chat',`:

```ts
  'device_not_connected_to_project',
```

`frontend/src/hooks/chat/useHarnessConnection.ts`: add after `BOUND_TO_ANOTHER_CHAT_MESSAGE` (line 79):

```ts
/** Shown in the chat bubble for the server's `device_not_connected_to_project`
 * error category: the computer holds neither an account approval nor an
 * approval for this chat's project, or the folder cannot be used here. */
export const NOT_CONNECTED_TO_PROJECT_MESSAGE =
  "This computer isn't connected to this project.";
```

`frontend/src/components/chat/aui/AuiMessage.tsx`: replace line 56 with:

```tsx
import {
  BOUND_TO_ANOTHER_CHAT_MESSAGE,
  NOT_CONNECTED_TO_PROJECT_MESSAGE,
} from '@/hooks/chat/useHarnessConnection';
```

Add to `ERROR_CATEGORY_HELP` after `device_bound_to_another_chat: BOUND_TO_ANOTHER_CHAT_MESSAGE,`:

```tsx
  device_not_connected_to_project: NOT_CONNECTED_TO_PROJECT_MESSAGE,
```

Append this to the comment above `NON_RETRYABLE_ERROR_CATEGORIES`. The set itself is unchanged: the category stays retryable.

```tsx
 * `device_not_connected_to_project` stays retryable for the same reason:
 * Retry uses whichever computer and folder are selected when it is pressed.
```

**Step 4: Run the tests and confirm they pass**

Run: `pnpm --dir frontend exec vitest run src/components/chat/aui/__tests__/AuiMessage.errorCategory.test.tsx src/services/__tests__/agentChatService.errorCategory.test.ts src/services/__tests__/agentStreamEvents.contract.test.ts`
Expected: PASS.

**Step 5: Commit**

```bash
git add frontend/src/services/agentStreamEvents.ts frontend/src/hooks/chat/useHarnessConnection.ts frontend/src/components/chat/aui/AuiMessage.tsx frontend/src/components/chat/aui/__tests__/AuiMessage.errorCategory.test.tsx frontend/src/services/__tests__/agentChatService.errorCategory.test.ts
git commit -m "feat(chat): say when the chosen computer isn't connected to this project"
```

### Task 4: Permission wording that knows the approval's kind

**Files:**
- Modify: `frontend/src/types/api/integration-connections-contract.ts:7-8` (add a type alias)
- Modify: `frontend/src/lib/integrations/scopeLabels.ts:1-44`
- Test: `frontend/src/lib/integrations/__tests__/scopeLabels.test.ts`

**Step 1: Write the failing tests**

Append to `frontend/src/lib/integrations/__tests__/scopeLabels.test.ts`:

```ts
describe('account wording', () => {
  it('says Codex can run from a chat in any project in the account', () => {
    expect(scopeLabel('harness:execute', 'account')).toMatch(
      /any project in your account/
    );
    expect(scopeSummary('harness:execute', 'account')).toBe(
      'run Local Codex from any chat'
    );
  });
  it('reaches every project in the account for library:write, and keeps what still asks', () => {
    const label = scopeLabel('library:write', 'account');
    expect(label).toMatch(/any project in your account/);
    expect(label).toMatch(/without asking each time/);
    expect(label).toMatch(
      /Deleting folders and ingesting papers still require your approval/
    );
    expect(label).not.toMatch(/this workspace/);
  });
  it('lists the workspaces and projects of the account for library:read', () => {
    expect(scopeLabel('library:read', 'account')).toBe(
      'List the workspaces and projects in your account'
    );
  });
});

describe('project and workspace wording', () => {
  it.each(STANDARD_SCOPES)('is unchanged for %s', (scope) => {
    expect(scopeLabel(scope, 'project')).toBe(scopeLabel(scope));
    expect(scopeLabel(scope, 'workspace')).toBe(scopeLabel(scope));
    expect(scopeSummary(scope, 'workspace')).toBe(scopeSummary(scope));
  });
  it('still names the workspace for library:write', () => {
    expect(scopeLabel('library:write', 'workspace')).toMatch(
      /items in this workspace/
    );
  });
});

describe.each([
  ['scopeLabel', scopeLabel],
  ['scopeSummary', scopeSummary],
] as const)('%s for an account', (_name, describeScope) => {
  it.each(STANDARD_SCOPES)('labels %s in words', (scope) => {
    expect(describeScope(scope, 'account')).not.toBe(scope);
  });
  it.each(['weird:scope', 'constructor', '__proto__'])(
    'falls back to the raw name for %s',
    (scope) => {
      expect(describeScope(scope, 'account')).toBe(scope);
    }
  );
});
```

**Step 2: Run the tests and confirm they fail**

Run: `pnpm --dir frontend exec vitest run src/lib/integrations/__tests__/scopeLabels.test.ts`
Expected: FAIL. For example, `expected 'Run harness sessions bound to this connection' to match /any project in your account/`: the second argument is ignored today.

**Step 3: Write the minimal implementation**

`frontend/src/types/api/integration-connections-contract.ts`: append:

```ts
/** What one approval reaches: one project, one workspace, or the whole
 * account. GrantRequestDTO.binding_kind carries the same values. */
export type ApiBindingKind = ApiConnectionConsent['kind'];
```

Replace `frontend/src/lib/integrations/scopeLabels.ts` with:

```ts
import type { ApiBindingKind } from '@/types/api/integration-connections-contract';

/**
 * Plain-language text for integration scopes. The consent page shows a full
 * sentence (`scopeLabel`) next to the raw scope; the Connected devices page
 * joins short fragments (`scopeSummary`) into one line per consent. A scope a
 * map does not know falls back to its raw name, so a label can never hide one.
 *
 * Project and workspace approvals share one wording. An account approval
 * reaches every project in the account, so its scopes say so. The scopes an
 * account approval cannot hold (publishing, memories, handoffs) have no
 * account wording and fall back to the shared one.
 */
const LABELS: Record<string, string> = {
  'harness:execute': 'Run harness sessions bound to this connection',
  'tools:read': 'Read documents, drafts and search results in scope',
  'tools:write': 'Request changes that you approve one by one',
  'artifacts:publish': 'Publish files from the registered output folder',
  'context:read': 'Read selected memories',
  'handoff:read':
    "Read this chat's saved handoff (goal, decisions, remaining work and results)",
  'handoff:write':
    'Save a handoff for this chat (goal, decisions, remaining work and results)',
  'library:read': 'List the folders (projects) in this workspace',
  // update_document_metadata runs without asking too, and edits the paper
  // itself, so the sentence names it and where the change shows.
  'library:write':
    'Add, remove, move and rename items in this workspace, and change the title and tags of a paper in it (including removing every tag), without asking each time. An edited paper changes everywhere it appears. Deleting folders and ingesting papers still require your approval.',
};

const ACCOUNT_LABELS: Record<string, string> = {
  'harness:execute':
    'Run Local Codex on this computer from a chat in any project in your account',
  'tools:read':
    'Read documents, drafts and search results in every project in your account',
  'tools:write':
    'Request changes in any project in your account that you approve one by one',
  'library:read': 'List the workspaces and projects in your account',
  'library:write':
    'Add, remove, move and rename items in any project in your account, and change the title and tags of a paper (including removing every tag), without asking each time. An edited paper changes everywhere it appears. Deleting folders and ingesting papers still require your approval.',
};

const SUMMARIES: Record<string, string> = {
  'harness:execute': 'run coding sessions',
  'tools:read': 'read project documents',
  'tools:write': 'request notes (you approve each one)',
  'context:read': 'read memories you chose to share',
  'artifacts:publish': 'publish files to chats',
  'handoff:read': "read this chat's handoff",
  'handoff:write': "save this chat's handoff",
  'library:read': 'list the folders in the workspace',
  'library:write': 'change folders and paper details without asking each time',
};

const ACCOUNT_SUMMARIES: Record<string, string> = {
  'harness:execute': 'run Local Codex from any chat',
  'tools:read': 'read documents in every project',
  'tools:write': 'request notes in any project (you approve each one)',
  'library:read': 'list your workspaces and projects',
  'library:write':
    'change folders and paper details in any project without asking each time',
};

// Own keys only: a scope named like an Object.prototype member ("constructor")
// must fall back to its raw name, not resolve to an inherited function.
const own = (
  texts: Record<string, string>,
  scope: string
): string | undefined =>
  Object.prototype.hasOwnProperty.call(texts, scope) ? texts[scope] : undefined;

const textFor = (
  accountTexts: Record<string, string>,
  texts: Record<string, string>,
  scope: string,
  kind: ApiBindingKind
): string =>
  (kind === 'account' ? own(accountTexts, scope) : undefined) ??
  own(texts, scope) ??
  scope;

export const scopeLabel = (
  scope: string,
  kind: ApiBindingKind = 'project'
): string => textFor(ACCOUNT_LABELS, LABELS, scope, kind);

export const scopeSummary = (
  scope: string,
  kind: ApiBindingKind = 'project'
): string => textFor(ACCOUNT_SUMMARIES, SUMMARIES, scope, kind);
```

Fix the one caller that passes `scopeSummary` point-free. Otherwise `Array.map` would pass the index as `kind`, which `tsc` rejects. In `frontend/src/components/integrations/ConnectedDevices.tsx:161`, change:

```tsx
                        {consent.scopes.map(scopeSummary).join(', ')}
```

to:

```tsx
                        {consent.scopes
                          .map((scope) => scopeSummary(scope, consent.kind))
                          .join(', ')}
```

**Step 4: Run the tests and confirm they pass**

Run: `pnpm --dir frontend exec vitest run src/lib/integrations/__tests__/scopeLabels.test.ts src/components/integrations/__tests__/ConnectedDevices.test.tsx src/components/integrations/__tests__/IntegrationApprovalPage.test.tsx`
Expected: PASS. Existing project and workspace wording is unchanged. The old-backend case (`kind` undefined) falls through to the shared wording.

**Step 5: Commit**

```bash
git add frontend/src/types/api/integration-connections-contract.ts frontend/src/lib/integrations/scopeLabels.ts frontend/src/lib/integrations/__tests__/scopeLabels.test.ts frontend/src/components/integrations/ConnectedDevices.tsx
git commit -m "feat(integrations): word scopes for a whole-account approval"
```

### Task 5: Approval page, account variant

**Files:**
- Modify: `frontend/app/(dashboard)/integrations/approve/page.tsx:1-169` (full file below)
- Test: `frontend/src/components/integrations/__tests__/IntegrationApprovalPage.test.tsx` (add fixtures after `:39`, add cases inside the `describe`)

**Step 1: Write the failing tests**

Add after `workspaceRequest` (line 39):

```tsx
// An account request binds neither a project nor a workspace (PR 1:
// binding_kind 'account'; account_summary holds today's counts).
const accountRequest = {
  ...request,
  project_id: null,
  project_label: null,
  workspace_id: null,
  workspace_label: null,
  binding_kind: 'account',
  account_summary: { workspace_count: 5, project_count: 33 },
  scopes: [
    'harness:execute',
    'library:read',
    'library:write',
    'tools:read',
    'tools:write',
  ],
};
```

Add inside `describe('IntegrationApprovalPage', …)`:

```tsx
  it('names the whole account and what it covers today', async () => {
    mockGet.mockResolvedValue(accountRequest);
    render(<IntegrationApprovalPage />);
    expect(
      await screen.findByRole('heading', {
        name: 'Laptop wants access to your whole NOUS account',
      })
    ).toBeInTheDocument();
    expect(screen.getByText('Covers')).toBeInTheDocument();
    expect(
      screen.getByText(
        /Every project in the workspaces you own or belong to in your organization, including projects added later/
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Archived and deleted workspaces and projects are not included/)
    ).toBeInTheDocument();
    expect(
      screen.getByText('Today: 5 workspaces and 33 projects.')
    ).toBeInTheDocument();
    expect(screen.queryByText('Project')).not.toBeInTheDocument();
    expect(screen.queryByText('Workspace')).not.toBeInTheDocument();
  });

  it('counts one workspace and one project in the singular', async () => {
    mockGet.mockResolvedValue({
      ...accountRequest,
      account_summary: { workspace_count: 1, project_count: 1 },
    });
    render(<IntegrationApprovalPage />);
    expect(
      await screen.findByText('Today: 1 workspace and 1 project.')
    ).toBeInTheDocument();
  });

  it('leaves the counts out when the server sends none', async () => {
    mockGet.mockResolvedValue({ ...accountRequest, account_summary: null });
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Covers')).toBeInTheDocument();
    expect(screen.queryByText(/^Today:/)).not.toBeInTheDocument();
  });

  it('words each permission for the whole account and still shows every raw scope', async () => {
    mockGet.mockResolvedValue(accountRequest);
    render(<IntegrationApprovalPage />);
    expect(
      await screen.findByText(
        /Run Local Codex on this computer from a chat in any project in your account/
      )
    ).toBeInTheDocument();
    expect(
      screen.getByText(/List the workspaces and projects in your account/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/in this workspace/)).not.toBeInTheDocument();
    for (const scope of accountRequest.scopes) {
      expect(screen.getByText(scope, { selector: 'code' })).toBeInTheDocument();
    }
  });

  it('warns plainly that library changes run without asking', async () => {
    mockGet.mockResolvedValue(accountRequest);
    render(<IntegrationApprovalPage />);
    const warning = await screen.findByRole('alert');
    expect(warning).toHaveTextContent(
      'This device can change your library without asking.'
    );
    expect(warning).toHaveTextContent(/every project in your account without asking you first/);
    expect(warning).toHaveTextContent(
      /Deleting folders and ingesting papers still ask you first/
    );
  });

  it('shows no warning for an account request without library:write', async () => {
    mockGet.mockResolvedValue({
      ...accountRequest,
      scopes: ['harness:execute', 'tools:read'],
    });
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Covers')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps the workspace page as it was', async () => {
    mockGet.mockResolvedValue({
      ...workspaceRequest,
      binding_kind: 'workspace',
      account_summary: null,
    });
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Workspace')).toBeInTheDocument();
    expect(screen.getByText(/items in this workspace/)).toBeInTheDocument();
    expect(screen.queryByText(/whole NOUS account/)).not.toBeInTheDocument();
    expect(screen.queryByText('Covers')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
```

**Step 2: Run the tests and confirm they fail**

Run: `pnpm --dir frontend exec vitest run src/components/integrations/__tests__/IntegrationApprovalPage.test.tsx`
Expected: FAIL. The account cases fail with `Unable to find role="heading" and name "Laptop wants access to your whole NOUS account"` (or `…text: Covers`). The workspace case passes already.

**Step 3: Write the minimal implementation**

Replace `frontend/app/(dashboard)/integrations/approve/page.tsx` with:

```tsx
'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense } from 'react';

import { Alert, AlertDescription } from '@/components/ui/alert';
import { useAuth } from '@/hooks/useAuth';
import { scopeLabel } from '@/lib/integrations/scopeLabels';
import { api } from '@/services/api-client';
import type { components } from '@/types/generated/api';

type GrantRequest = components['schemas']['GrantRequestDTO'];
type GrantDecision = components['schemas']['GrantDecision'];
type AccountSummary = NonNullable<GrantRequest['account_summary']>;

const counted = (count: number, one: string, many: string): string =>
  `${count} ${count === 1 ? one : many}`;

/** What an account approval reaches: the rule the server applies on every
 * call (design §2), and today's counts under it. */
function AccountCoverage({
  summary,
}: {
  summary: AccountSummary | null | undefined;
}): React.JSX.Element {
  return (
    <dd className="space-y-1">
      <p>
        Every project in the workspaces you own or belong to in your
        organization, including projects added later. Archived and deleted
        workspaces and projects are not included.
      </p>
      {summary && (
        <p>{`Today: ${counted(summary.workspace_count, 'workspace', 'workspaces')} and ${counted(summary.project_count, 'project', 'projects')}.`}</p>
      )}
    </dd>
  );
}

function ApprovalContent(): React.JSX.Element {
  const requestId = useSearchParams().get('request_id');
  const { user, isAuthenticated } = useAuth();
  const queryClient = useQueryClient();
  const queryKey = ['integration-grant-request', user?.id, requestId];
  const request = useQuery({
    queryKey,
    enabled: Boolean(requestId && isAuthenticated),
    queryFn: () =>
      api.get<GrantRequest>(
        `/integrations/grant-requests/${encodeURIComponent(requestId ?? '')}`
      ),
    staleTime: 0,
    gcTime: 0,
  });
  const decision = useMutation({
    mutationFn: (data: GrantDecision) =>
      api.post<GrantRequest>(
        `/integrations/grant-requests/${encodeURIComponent(requestId ?? '')}/decision`,
        data
      ),
    onSuccess: (data) => queryClient.setQueryData(queryKey, data),
  });
  const consent = request.data;
  // The API derives the expiry state from the authoritative server clock.
  // Avoid a client clock comparison during render, which can drift and is
  // rejected by React's render-purity lint rule.
  const expired = consent?.status === 'expired';
  const canDecide =
    consent?.status === 'pending' && !expired && !decision.isPending;
  // A backend older than account connections sends no binding_kind; its
  // requests are a workspace or a project, told apart as before.
  const isAccount = consent?.binding_kind === 'account';
  const kind =
    consent?.binding_kind ?? (consent?.workspace_id ? 'workspace' : 'project');

  return (
    <section className="mx-auto max-w-xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Approve integration access</h1>
      <p>
        Only approve a request you started. Check the device, project or
        workspace, and permissions below against your terminal.
      </p>
      <p>
        <Link href="/integrations/devices" className="underline">
          Manage connected devices
        </Link>
      </p>
      {!requestId && (
        <p role="alert">This approval link is missing its request ID.</p>
      )}
      {request.isPending && requestId && <p role="status">Loading request…</p>}
      {(request.isError || decision.isError) && (
        <p role="alert">
          This request could not be processed. It may have expired or access may
          have changed.
        </p>
      )}
      {consent && (
        <>
          {isAccount && (
            <h2 className="text-xl font-semibold">{`${consent.device_label} wants access to your whole NOUS account`}</h2>
          )}
          <dl className="space-y-3 break-words">
            <div>
              <dt className="font-semibold">Device</dt>
              <dd>
                {consent.device_label} ({consent.device_id})
              </dd>
            </div>
            {isAccount ? (
              <div>
                <dt className="font-semibold">Covers</dt>
                <AccountCoverage summary={consent.account_summary} />
              </div>
            ) : consent.workspace_id ? (
              <div>
                <dt className="font-semibold">Workspace</dt>
                <dd>
                  {consent.workspace_label} ({consent.workspace_id})
                </dd>
              </div>
            ) : (
              <div>
                <dt className="font-semibold">Project</dt>
                <dd>
                  {consent.project_label} ({consent.project_id})
                </dd>
              </div>
            )}
            {consent.thread_id && (
              <div>
                <dt className="font-semibold">Chat</dt>
                <dd>
                  {consent.thread_label} ({consent.thread_id})
                </dd>
              </div>
            )}
            <div>
              <dt className="font-semibold">Request</dt>
              <dd>{consent.id}</dd>
            </div>
            <div>
              <dt className="font-semibold">Permissions</dt>
              <dd>
                <ul>
                  {consent.scopes.map((scope) => (
                    <li key={scope}>
                      {scopeLabel(scope, kind)} <code>{scope}</code>
                    </li>
                  ))}
                </ul>
              </dd>
            </div>
            <div>
              <dt className="font-semibold">Expires</dt>
              <dd>{new Date(consent.expires_at).toLocaleString()}</dd>
            </div>
          </dl>
          {isAccount && consent.scopes.includes('library:write') && (
            <Alert variant="destructive">
              <AlertDescription className="space-y-2">
                <p className="font-medium">
                  This device can change your library without asking.
                </p>
                <p>
                  It can make reversible changes in every project in your
                  account without asking you first: adding, removing, moving
                  and renaming items, and changing a paper&apos;s title and
                  tags. Deleting folders and ingesting papers still ask you
                  first. You can revoke this access at any time from Manage
                  connected devices.
                </p>
              </AlertDescription>
            </Alert>
          )}
          <p role="status">
            {expired && consent.status === 'pending'
              ? 'expired'
              : consent.status}
          </p>
          {consent.scopes.includes('context:read') &&
            (consent.status === 'approved' ||
              consent.status === 'consumed') && (
              <p>
                <Link
                  href={`/integrations/context/${encodeURIComponent(consent.id)}`}
                  className="underline"
                >
                  Choose which project memories this device may read
                </Link>
              </p>
            )}
          <div className="flex gap-3">
            <button
              type="button"
              disabled={!canDecide}
              onClick={() => decision.mutate({ approved: false })}
              className="rounded border px-4 py-2 disabled:opacity-50"
            >
              Deny
            </button>
            <button
              type="button"
              disabled={!canDecide}
              onClick={() => decision.mutate({ approved: true })}
              className="rounded bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
            >
              Approve access
            </button>
          </div>
        </>
      )}
    </section>
  );
}

export default function IntegrationApprovalPage(): React.JSX.Element {
  return (
    <Suspense fallback={<p role="status">Loading request…</p>}>
      <ApprovalContent />
    </Suspense>
  );
}
```

Accessibility:
- The warning reuses the repo's `Alert` (`role="alert"`, theme tokens via `variant="destructive"`, the same component `SkillReviewPanel` uses for warnings). It leaves out `AlertTitle`, which renders an `h5` and would skip heading levels under this page's `h1`/`h2`.
- The account header is an `h2` under the page's `h1`.
- The Covers row keeps the `dt`/`dd` structure of the other rows.
- No hardcoded colours.

**Step 4: Run the tests and confirm they pass**

Run: `pnpm --dir frontend exec vitest run src/components/integrations/__tests__/IntegrationApprovalPage.test.tsx`
Expected: PASS: the 6 existing cases plus the 7 new ones.

**Step 5: Commit**

```bash
git add "frontend/app/(dashboard)/integrations/approve/page.tsx" frontend/src/components/integrations/__tests__/IntegrationApprovalPage.test.tsx
git commit -m "feat(integrations): approval page for a whole-account connection"
```

### Task 6: Connected devices: "Whole account" consent with Revoke

**Files:**
- Modify: `frontend/src/components/integrations/ConnectedDevices.tsx:13-31` (`consentTarget`, `consentName`)
- Test: `frontend/src/components/integrations/__tests__/ConnectedDevices.test.tsx` (add a fixture after `:57`, a case inside the `describe`)

**Step 1: Write the failing test**

Add after `workspaceConsent`:

```tsx
// An account consent binds neither a project nor a workspace (PR 1).
const accountConsent: ApiConnectionConsent = {
  request_id: 'r4',
  kind: 'account',
  project_id: null,
  project_label: null,
  workspace_id: null,
  workspace_label: null,
  thread_id: null,
  thread_label: null,
  scopes: [
    'harness:execute',
    'library:read',
    'library:write',
    'tools:read',
    'tools:write',
  ],
  status: 'consumed',
  approved_at: null,
};
```

Add inside the `describe`:

```tsx
  it('lists an account consent as the whole account and revokes it on its own', async () => {
    vi.mocked(integrationConnectionsService.list)
      .mockResolvedValueOnce([
        { ...laptop, consents: [...laptop.consents, accountConsent] },
      ])
      .mockResolvedValue([laptop]);
    vi.mocked(integrationConnectionsService.revokeConsent).mockResolvedValue();
    const { user } = render(<ConnectedDevices />);
    expect(await screen.findByText('Whole account')).toBeInTheDocument();
    expect(
      screen.getByText(
        'run Local Codex from any chat, list your workspaces and projects, change folders and paper details in any project without asking each time, read documents in every project, request notes in any project (you approve each one)'
      )
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole('button', {
        name: 'Revoke Laptop (d1) access to your whole account',
      })
    );
    expect(integrationConnectionsService.revokeConsent).toHaveBeenCalledTimes(
      1
    );
    expect(integrationConnectionsService.revokeConsent).toHaveBeenCalledWith(
      'r4'
    );
    await waitFor(() =>
      expect(screen.queryByText('Whole account')).not.toBeInTheDocument()
    );
    expect(screen.getByText('Thesis (p1)')).toBeInTheDocument();
  });
```

**Step 2: Run the test and confirm it fails**

Run: `pnpm --dir frontend exec vitest run src/components/integrations/__tests__/ConnectedDevices.test.tsx -t "whole account"`
Expected: FAIL with `Unable to find an element with the text: Whole account`. Today the account consent renders as ` ()`.

**Step 3: Write the minimal implementation**

Replace `ConnectedDevices.tsx:13-31` with:

```tsx
// A consent reaches one project, every project of one workspace (Plan 07),
// or every project in the account (account connections).
const consentTarget = (consent: ApiConnectionConsent): string =>
  consent.kind === 'account'
    ? 'Whole account'
    : consent.kind === 'workspace'
      ? `Workspace ${consent.workspace_label ?? ''} (${consent.workspace_id ?? ''}), every project in it`
      : `${consent.project_label ?? ''} (${consent.project_id ?? ''})`;

// Names a consent apart from its siblings in control labels: two consents of
// one device for the same project differ only by the chat they are bound to.
// Truthiness, not `!== null`: a backend older than this page omits thread_id.
// An account consent is never chat-bound.
const consentName = (consent: ApiConnectionConsent): string => {
  const target =
    consent.kind === 'account'
      ? 'your whole account'
      : consent.kind === 'workspace'
        ? `workspace ${consent.workspace_label ?? ''}`
        : (consent.project_label ?? '');
  if (!consent.thread_id) return target;
  return consent.thread_label
    ? `${target}, only from chat ${consent.thread_label}`
    : `${target}, only from a chat that is no longer available`;
};
```

**Step 4: Run the tests and confirm they pass**

Run: `pnpm --dir frontend exec vitest run src/components/integrations/__tests__/ConnectedDevices.test.tsx`
Expected: PASS, including the old-backend case (`kind` undefined is still read as a project).

**Step 5: Commit**

```bash
git add frontend/src/components/integrations/ConnectedDevices.tsx frontend/src/components/integrations/__tests__/ConnectedDevices.test.tsx
git commit -m "feat(integrations): show whole-account access on Connected devices"
```

### Task 7: Connected devices: list a computer's account folders

**Files:**
- Modify: `frontend/src/components/integrations/ConnectedDevices.tsx:3-11` (imports), add an `AccountFolders` component above `ConnectedDevices`, render it in the device item after the consent list (`:134-187`)
- Test: `frontend/src/components/integrations/__tests__/ConnectedDevices.test.tsx` (imports `:1-22`, a `beforeEach`, new cases)

**Step 1: Write the failing tests**

At the top of the test file, change line 1 to import `within`, and mock the folder service next to the existing mocks:

```tsx
import { screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
```

```tsx
vi.mock('@/services/harnessService', () => ({
  harnessService: { listWorkspaces: vi.fn() },
}));
```

```tsx
import { harnessService } from '@/services/harnessService';
```

Add after `accountConsent`:

```tsx
const accountLaptop: ApiConnectedDevice = {
  ...laptop,
  consents: [accountConsent],
};
```

Inside `describe('ConnectedDevices', …)`, first add a `beforeEach`. The Task 6 case also renders an account consent, so it needs a folder answer.

```tsx
  beforeEach(() => {
    vi.mocked(harnessService.listWorkspaces).mockResolvedValue([]);
  });
```

Then add these cases:

```tsx
  it('lists the account folders of a computer with account access', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      accountLaptop,
    ]);
    vi.mocked(harnessService.listWorkspaces).mockResolvedValue([
      { workspace_id: 'f1', project_id: null, label: 'thesis-code' },
      { workspace_id: 'f2', project_id: 'p1', label: 'project-only' },
    ]);
    render(<ConnectedDevices />);
    const folders = await screen.findByRole('list', {
      name: 'Account folders on Laptop (d1)',
    });
    expect(
      within(folders)
        .getAllByRole('listitem')
        .map((item) => item.textContent)
    ).toEqual(['thesis-code']);
    expect(screen.queryByText('project-only')).not.toBeInTheDocument();
    expect(harnessService.listWorkspaces).toHaveBeenCalledWith('d1');
  });

  it('does not look up folders for a computer without account access', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([laptop]);
    render(<ConnectedDevices />);
    expect(await screen.findByText('Thesis (p1)')).toBeInTheDocument();
    expect(screen.queryByText('Account folders')).not.toBeInTheDocument();
    expect(harnessService.listWorkspaces).not.toHaveBeenCalled();
  });

  it('says when a computer has no account folders yet', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      accountLaptop,
    ]);
    vi.mocked(harnessService.listWorkspaces).mockResolvedValue([
      { workspace_id: 'f2', project_id: 'p1', label: 'project-only' },
    ]);
    render(<ConnectedDevices />);
    expect(
      await screen.findByText('No account folders on this computer yet.')
    ).toBeInTheDocument();
  });

  it('reports account folders that could not be loaded', async () => {
    vi.mocked(integrationConnectionsService.list).mockResolvedValue([
      accountLaptop,
    ]);
    vi.mocked(harnessService.listWorkspaces).mockRejectedValue(
      new Error('500')
    );
    render(<ConnectedDevices />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Account folders could not be loaded.'
    );
  });
```

**Step 2: Run the tests and confirm they fail**

Run: `pnpm --dir frontend exec vitest run src/components/integrations/__tests__/ConnectedDevices.test.tsx -t "folders"`
Expected: FAIL. `Unable to find role="list" and name "Account folders on Laptop (d1)"`, and the other folder cases fail for the same missing section. "does not look up folders" passes already.

**Step 3: Write the minimal implementation**

Imports (`ConnectedDevices.tsx:3-11`): add `useId` and the folder service.

```tsx
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useId, useState, type ReactElement } from 'react';

import { getSelectedThreadUrl } from '@/components/chat/shared/chatNavigation';
import { useAuth } from '@/hooks/useAuth';
import { scopeSummary } from '@/lib/integrations/scopeLabels';
import { harnessService } from '@/services/harnessService';
import { integrationConnectionsService } from '@/services/integrationConnectionsService';
import type { ApiConnectionConsent } from '@/types/api/integration-connections-contract';
```

Add above `export function ConnectedDevices`:

```tsx
/**
 * Folders on this computer that belong to no project (account folders): any
 * chat in the account can run Codex in them. Read through the same query key
 * as the chat composer's folder list, so the folder list has one cache.
 */
function AccountFolders({
  deviceId,
  deviceLabel,
  userId,
}: {
  deviceId: string;
  deviceLabel: string;
  userId: string | undefined;
}): ReactElement {
  const headingId = useId();
  const folders = useQuery({
    queryKey: ['harness', 'workspaces', userId, deviceId],
    enabled: Boolean(userId),
    queryFn: () => harnessService.listWorkspaces(deviceId),
    retry: false,
    staleTime: 0,
  });
  const accountFolders =
    folders.data?.filter((folder) => folder.project_id === null) ?? [];
  return (
    <div className="space-y-1">
      <h3 id={headingId} className="text-sm font-semibold">
        Account folders
        <span className="sr-only">{` on ${deviceLabel} (${deviceId})`}</span>
      </h3>
      {folders.isPending && (
        <p role="status" className="text-sm">
          Loading folders…
        </p>
      )}
      {folders.isError && (
        <p role="alert" className="text-sm">
          Account folders could not be loaded.
        </p>
      )}
      {folders.isSuccess && accountFolders.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No account folders on this computer yet.
        </p>
      )}
      {accountFolders.length > 0 && (
        <ul aria-labelledby={headingId} className="list-disc pl-5 text-sm">
          {accountFolders.map((folder) => (
            <li key={folder.workspace_id}>{folder.label}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

In the device `<li>`, after the closing `)}` of the `device.consents.length === 0 ? … : …` block (line 187), add:

```tsx
            {device.consents.some((consent) => consent.kind === 'account') && (
              <AccountFolders
                deviceId={device.device_id}
                deviceLabel={device.device_label}
                userId={user?.id}
              />
            )}
```

This depends on PR 1 R2. Without it, `listWorkspaces` never returns `project_id: null` rows, and the section shows "No account folders on this computer yet."

**Step 4: Run the tests and confirm they pass**

Run: `pnpm --dir frontend exec vitest run src/components/integrations/__tests__/ConnectedDevices.test.tsx`
Expected: PASS (all cases, including the existing alert case, which has no account consent).

**Step 5: Commit**

```bash
git add frontend/src/components/integrations/ConnectedDevices.tsx frontend/src/components/integrations/__tests__/ConnectedDevices.test.tsx
git commit -m "feat(integrations): list account folders under each connected computer"
```

### Task 8: Composer: only the computers and folders that can run this chat's project

**Files:**
- Modify: `frontend/src/hooks/chat/useHarnessConnection.ts`:
  - `:81-100`: add helpers and messages after `boundToAnotherChat`
  - `:132-138`: signature and doc
  - `:328-349`: filtered lists and `disabledReason`
  - `:367-414`: returned lists and deps
- Test: `frontend/src/hooks/chat/__tests__/useHarnessConnection.test.tsx`:
  - `:8`: import `within`
  - `:55-74`: helper gains `projectId`
  - append new cases

`HarnessSelector.tsx` needs no change: it renders `controller.devices` and `controller.workspaces`, which become the filtered lists.

**Step 1: Write the failing tests**

Add `within` to the `@testing-library/react` import (lines 2-8). Change the helper's signature and its `renderHook` call (lines 55-64) so it can pass a project:

```tsx
function renderConnectedHarness(
  threadId: string | null = 'thread-a',
  projectId?: string | null
): ConnectedHarness {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }): ReactElement => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(() => useHarnessConnection(threadId, projectId), {
    wrapper,
  });
```

(The rest of the helper is unchanged.) Append inside `describe('useHarnessConnection', …)`:

```tsx
  // PR 1's device fields: account_wide (a live account approval) and
  // project_ids (live project approvals that can run Codex).
  const accountLaptop = {
    id: 'device-account',
    label: 'Account laptop',
    bound_thread_ids: [],
    account_wide: true,
    project_ids: [],
  };
  const projectDesk = {
    id: 'device-project',
    label: 'Project desk',
    bound_thread_ids: [],
    account_wide: false,
    project_ids: ['project-a'],
  };
  const otherDesk = {
    id: 'device-other',
    label: 'Other desk',
    bound_thread_ids: [],
    account_wide: false,
    project_ids: ['project-b'],
  };
  const folders = [
    { workspace_id: 'folder-account', project_id: null, label: 'Any project' },
    { workspace_id: 'folder-here', project_id: 'project-a', label: 'This one' },
    { workspace_id: 'folder-there', project_id: 'project-b', label: 'Other' },
  ];

  it("lists only computers that can run Codex for the chat's project", async () => {
    listDevices.mockResolvedValue([accountLaptop, projectDesk, otherDesk]);
    const view = renderConnectedHarness('thread-a', 'project-a');
    act(() => view.result.current.selectProvider('codex'));
    await waitFor(() =>
      expect(view.result.current.devices.map((device) => device.id)).toEqual([
        'device-account',
        'device-project',
      ])
    );
    render(
      <QueryClientProvider client={view.client}>
        <HarnessSelector controller={view.result.current} />
      </QueryClientProvider>
    );
    const picker = screen.getByRole('combobox', { name: 'Paired computer' });
    expect(
      within(picker)
        .getAllByRole('option')
        .map((option) => option.textContent)
    ).toEqual(['Choose computer', 'Account laptop', 'Project desk']);
  });

  it('never hides a computer from a backend without account fields', async () => {
    listDevices.mockResolvedValue([{ id: 'device-a', label: 'My laptop' }]);
    const view = renderConnectedHarness('thread-a', 'project-a');
    act(() => view.result.current.selectProvider('codex'));
    await waitFor(() => expect(view.result.current.devices).toHaveLength(1));
  });

  it('offers account folders and this project\'s folders on an account computer', async () => {
    listDevices.mockResolvedValue([accountLaptop]);
    listWorkspaces.mockResolvedValue(folders);
    const view = renderConnectedHarness('thread-a', 'project-a');
    act(() => view.result.current.selectProvider('codex'));
    await waitFor(() => expect(view.result.current.devices).toHaveLength(1));
    act(() => view.result.current.selectDevice('device-account'));
    await waitFor(() =>
      expect(
        view.result.current.workspaces.map((folder) => folder.workspace_id)
      ).toEqual(['folder-account', 'folder-here'])
    );
    act(() => view.result.current.selectWorkspace('folder-account'));
    expect(view.result.current.disabledReason).toBeNull();
    expect(view.canSend()).toBe(true);
  });

  it('hides account folders on a computer without an account approval', async () => {
    listDevices.mockResolvedValue([projectDesk]);
    listWorkspaces.mockResolvedValue(folders);
    const view = renderConnectedHarness('thread-a', 'project-a');
    act(() => view.result.current.selectProvider('codex'));
    await waitFor(() => expect(view.result.current.devices).toHaveLength(1));
    act(() => view.result.current.selectDevice('device-project'));
    await waitFor(() =>
      expect(
        view.result.current.workspaces.map((folder) => folder.workspace_id)
      ).toEqual(['folder-here'])
    );
  });

  it('says when no paired computer can run this project', async () => {
    listDevices.mockResolvedValue([otherDesk]);
    const view = renderConnectedHarness('thread-a', 'project-a');
    act(() => view.result.current.selectProvider('codex'));
    await waitFor(() =>
      expect(view.result.current.disabledReason).toBe(
        'None of your paired computers is connected to this project.'
      )
    );
    expect(view.result.current.devices).toEqual([]);
    expect(view.canSend()).toBe(false);
  });

  it('treats a remembered computer that cannot run this project as unchosen', async () => {
    listDevices.mockResolvedValue([accountLaptop, otherDesk]);
    listWorkspaces.mockResolvedValue(folders);
    const view = renderConnectedHarness('thread-a', 'project-a');
    act(() => view.result.current.selectProvider('codex'));
    await waitFor(() => expect(view.result.current.devices).toHaveLength(1));
    act(() => {
      view.result.current.selectDevice('device-other');
      view.result.current.selectWorkspace('folder-there');
    });
    await waitFor(() =>
      expect(view.result.current.disabledReason).toBe(
        'Choose a paired computer.'
      )
    );
    expect(view.canSend()).toBe(false);
  });

  it('needs a chat in a project before Codex can run', async () => {
    listDevices.mockResolvedValue([accountLaptop]);
    const view = renderConnectedHarness('thread-a', null);
    act(() => view.result.current.selectProvider('codex'));
    await waitFor(() =>
      expect(view.result.current.disabledReason).toBe(
        'Codex runs only in chats that belong to a project.'
      )
    );
    expect(view.result.current.devices).toEqual([]);
    expect(view.canSend()).toBe(false);
  });

  it('filters nothing while the chat\'s project is not known yet', async () => {
    listDevices.mockResolvedValue([accountLaptop, otherDesk]);
    listWorkspaces.mockResolvedValue(folders);
    const view = renderConnectedHarness('thread-a');
    act(() => view.result.current.selectProvider('codex'));
    await waitFor(() => expect(view.result.current.devices).toHaveLength(2));
    act(() => view.result.current.selectDevice('device-other'));
    await waitFor(() => expect(view.result.current.workspaces).toHaveLength(3));
  });
```

**Step 2: Run the tests and confirm they fail**

Run: `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useHarnessConnection.test.tsx`
Expected: FAIL. The new filtering cases fail, for example `expected [ 'device-account', 'device-project', 'device-other' ] to deeply equal [ 'device-account', 'device-project' ]`. "never hides" and "filters nothing" pass already. All existing cases pass, because they pass no project.

**Step 3: Write the minimal implementation**

In `useHarnessConnection.ts`, add after `boundToAnotherChat` (after line 100):

```ts
const NO_PROJECT_MESSAGE = 'Codex runs only in chats that belong to a project.';
const NO_CONNECTED_COMPUTER_MESSAGE =
  'None of your paired computers is connected to this project.';

/**
 * Whether this computer may run Codex for the chat's project: it holds an
 * account approval, or a project approval for that project. A backend older
 * than account connections sends neither field; missing data never hides a
 * computer, and the server still refuses one that cannot run here.
 */
function servesProject(device: HarnessDevice, projectId: string): boolean {
  const accountWide: unknown = device.account_wide;
  const projectIds: unknown = device.project_ids;
  if (typeof accountWide !== 'boolean' || !Array.isArray(projectIds)) {
    return true;
  }
  return accountWide || projectIds.includes(projectId);
}

/**
 * A folder this chat can use: one tied to the chat's project, or an account
 * folder (no project) on a computer that holds an account approval.
 */
function usableFolder(
  folder: HarnessWorkspace,
  device: HarnessDevice | undefined,
  projectId: string
): boolean {
  if (folder.project_id === projectId) return true;
  return folder.project_id === null && device?.account_wide === true;
}
```

Replace the doc comment and signature at lines 132-138 with:

```ts
/**
 * Thread-owned bridge controls. Only display preferences are persisted in the
 * browser; grant tokens and native process state remain server/device-owned.
 *
 * `projectId` is the chat's project. A string keeps only the computers and
 * folders that can run Codex for it; `null` is a chat with no project, where
 * Codex cannot run; `undefined` means not known yet and filters nothing.
 */
export function useHarnessConnection(
  threadId: string | null,
  projectId?: string | null
): HarnessConnectionController {
```

Replace lines 328-349 (`selectedDevice` through the end of `disabledReason`) with:

```ts
  const pairedDevices = devicesQuery.data;
  const devices = useMemo(
    () =>
      projectId === undefined
        ? (pairedDevices ?? [])
        : projectId === null
          ? []
          : (pairedDevices ?? []).filter((device) =>
              servesProject(device, projectId)
            ),
    [pairedDevices, projectId]
  );
  const selectedDevice = devices.find(
    (device) => device.id === selection.deviceId
  );
  const boundFolders = workspacesQuery.data;
  const workspaces = useMemo(
    () =>
      projectId === undefined
        ? (boundFolders ?? [])
        : projectId === null
          ? []
          : (boundFolders ?? []).filter((folder) =>
              usableFolder(folder, selectedDevice, projectId)
            ),
    [boundFolders, projectId, selectedDevice]
  );
  const workspaceExists = workspaces.some(
    (workspace) => workspace.workspace_id === selection.workspaceId
  );
  const disabledReason =
    selection.executionProvider === 'nous'
      ? null
      : !userId
        ? 'Sign in to connect Codex.'
        : projectId === null
          ? NO_PROJECT_MESSAGE
          : !pairedDevices?.length
            ? 'Pair this computer with NOUS before using Codex.'
            : !devices.length
              ? NO_CONNECTED_COMPUTER_MESSAGE
              : !selection.deviceId || !selectedDevice
                ? 'Choose a paired computer.'
                : boundToAnotherChat(selectedDevice, threadId)
                  ? BOUND_TO_ANOTHER_CHAT_MESSAGE
                  : !workspaces.length
                    ? 'Bind a project workspace to this computer.'
                    : !selection.workspaceId || !workspaceExists
                      ? 'Choose a bound project workspace.'
                      : null;
```

In the returned `useMemo` (lines 367-414):
- Replace `devices: devicesQuery.data ?? [],` with `devices,`.
- Replace `workspaces: workspacesQuery.data ?? [],` with `workspaces,`.
- Leave `isBoundToAnotherChat` reading `devicesQuery.data` (every paired computer).
- In the dependency array, replace `devicesQuery.data` with `devices` and `pairedDevices`, and `workspacesQuery.data` with `workspaces`.

Leave the folder query enabled on `selection.deviceId` as it is. A stale device id only fetches a list that the filter then discards.

**Step 4: Run the tests and confirm they pass**

Run: `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useHarnessConnection.test.tsx src/hooks/chat/__tests__/useChatStreaming.harnessReloadApproval.test.tsx`
Expected: PASS (all existing and 8 new cases).

Mutation checks (the server stays the authority, but these pin what the composer offers):
- Replace `return accountWide || projectIds.includes(projectId);` with `return true;`. "lists only computers…" and "says when no paired computer…" must fail.
- Drop `&& device?.account_wide === true` in `usableFolder`. "hides account folders on a computer without an account approval" must fail.

Restore both.

**Step 5: Commit**

```bash
git add frontend/src/hooks/chat/useHarnessConnection.ts frontend/src/hooks/chat/__tests__/useHarnessConnection.test.tsx
git commit -m "feat(chat): offer only computers and folders that can run this chat's project"
```

### Task 9: Composer: give the picker the chat's project

**Files:**
- Modify: `frontend/src/hooks/chat/useChatStreaming.ts:620-621` (the `useHarnessConnection` call; `threadProjectId` and `boundProjectId` are already computed at `:484-488`)
- Create: `frontend/src/hooks/chat/__tests__/useChatStreaming.harnessProject.test.tsx`

**Step 1: Write the failing test**

Create `frontend/src/hooks/chat/__tests__/useChatStreaming.harnessProject.test.tsx`:

```tsx
// The Codex picker filters computers and folders by the chat's project, so
// useChatStreaming must hand useHarnessConnection the same project binding it
// uses for page_context: the thread row first, ?projectId= only while the
// thread has not loaded, and null for a loaded chat with no project.
// Command: pnpm --dir frontend exec vitest run
// src/hooks/chat/__tests__/useChatStreaming.harnessProject.test.tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook } from '@testing-library/react';
import { createElement, type ReactElement, type ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type { UseChatStreamingParams } from '@/hooks/chat/useChatStreaming';
import { useChatStore } from '@/store/chat-store';
import type { Thread } from '@/types/workspace';
import { ThreadStatus } from '@/types/workspace';

const mocks = vi.hoisted(() => ({
  search: new URLSearchParams(),
  harnessArgs: [] as unknown[][],
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => mocks.search,
}));

vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: vi.fn(),
    streamConfirm: vi.fn(),
    resumeStream: vi.fn(async () => ({ status: 'idle' })),
    cancelPendingConfirmation: vi.fn(),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    listMessages: vi.fn(async () => ({ messages: [], has_more: false })),
  },
}));

const harnessConnection = {
  executionProvider: 'nous',
  deviceId: null,
  workspaceId: null,
  devices: [],
  workspaces: [],
  pendingRequests: [],
  connectionState: 'idle',
  statusLabel: 'NOUS',
  disabledReason: null,
  canSend: true,
  runId: null,
  loadApproval: vi.fn(),
  receive: vi.fn(),
  markConnectionLost: vi.fn(),
};
vi.mock('@/hooks/chat/useHarnessConnection', () => ({
  useHarnessConnection: (...args: unknown[]) => {
    mocks.harnessArgs.push(args);
    return harnessConnection;
  },
}));

function wrapper({ children }: { children: ReactNode }): ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

function makeParams(): UseChatStreamingParams {
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: [] as ChatPageMessage[],
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    dbConversation: null,
    enableRAG: false,
  };
}

const iso = new Date().toISOString();
const makeThread = (id: string, extra: Partial<Thread> = {}): Thread => ({
  id,
  conversation_id: 'conv-1',
  title: id,
  status: ThreadStatus.ACTIVE,
  last_message_at: iso,
  message_count: 0,
  token_count: 0,
  created_at: iso,
  updated_at: iso,
  ...extra,
});

async function renderStreaming(): Promise<void> {
  const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
  renderHook(() => useChatStreaming(makeParams()), { wrapper });
}

describe('useChatStreaming gives the Codex picker the chat project', () => {
  beforeEach(() => {
    mocks.harnessArgs.length = 0;
    mocks.search = new URLSearchParams();
    useChatStore.setState({
      currentThreadId: null,
      threads: {},
      isStreaming: false,
      streamingThreadId: null,
    });
  });

  it("passes the thread's project", async () => {
    useChatStore.setState({
      currentThreadId: 'thread-A',
      threads: {
        'conv-1': [makeThread('thread-A', { source_project_id: 'project-1' })],
      },
    });
    await renderStreaming();
    expect(mocks.harnessArgs.at(-1)).toEqual(['thread-A', 'project-1']);
  });

  it('passes null for a loaded chat with no project, ignoring a stale ?projectId=', async () => {
    mocks.search = new URLSearchParams('projectId=stale');
    useChatStore.setState({
      currentThreadId: 'thread-A',
      threads: { 'conv-1': [makeThread('thread-A')] },
    });
    await renderStreaming();
    expect(mocks.harnessArgs.at(-1)).toEqual(['thread-A', null]);
  });

  it('passes the ?projectId= intent while the chat has not loaded', async () => {
    mocks.search = new URLSearchParams('projectId=project-2');
    useChatStore.setState({ currentThreadId: 'ghost', threads: {} });
    await renderStreaming();
    expect(mocks.harnessArgs.at(-1)).toEqual(['ghost', 'project-2']);
  });

  it('passes no project for a new chat with no project intent', async () => {
    await renderStreaming();
    expect(mocks.harnessArgs.at(-1)).toEqual([null, undefined]);
  });
});
```

**Step 2: Run the test and confirm it fails**

Run: `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.harnessProject.test.tsx`
Expected: FAIL. The first three cases fail with `expected [ 'thread-A' ] to deeply equal [ 'thread-A', 'project-1' ]` (and similar), because the hook is called with one argument. The last case passes only if `[null]` equals `[null, undefined]` under `toEqual`. Treat it as a guard, not as the failing proof.

**Step 3: Write the minimal implementation**

In `useChatStreaming.ts`, replace line 621:

```ts
  const harnessConnection = useHarnessConnection(activeThreadId);
```

with:

```ts
  // The Codex picker offers only computers and folders that can run this
  // chat's project. null: a loaded chat with no project (a stale ?projectId=
  // is ignored); undefined: not known yet, which filters nothing.
  const harnessProjectId =
    boundProjectId ?? (threadProjectId === null ? null : undefined);
  const harnessConnection = useHarnessConnection(
    activeThreadId,
    harnessProjectId
  );
```

**Step 4: Run the tests and confirm they pass**

Run: `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useChatStreaming.harnessProject.test.tsx src/hooks/chat/__tests__/useChatStreaming.harnessReloadApproval.test.tsx src/test/architecture/__tests__/maintenanceContracts.test.ts`
Expected: PASS.

Mutation check: change `threadProjectId === null ? null : undefined` to `undefined`. "passes null for a loaded chat with no project" must fail. Restore it.

**Step 5: Commit**

```bash
git add frontend/src/hooks/chat/useChatStreaming.ts frontend/src/hooks/chat/__tests__/useChatStreaming.harnessProject.test.tsx
git commit -m "feat(chat): give the Codex picker the chat's project"
```

### Task 10: Verification and PR

**Step 1: Targeted tests**

```bash
pnpm --dir frontend exec vitest run \
  src/lib/integrations/__tests__/scopeLabels.test.ts \
  src/components/integrations/__tests__/IntegrationApprovalPage.test.tsx \
  src/components/integrations/__tests__/ConnectedDevices.test.tsx \
  src/hooks/chat/__tests__/useHarnessConnection.test.tsx \
  src/hooks/chat/__tests__/useChatStreaming.harnessProject.test.tsx \
  src/hooks/chat/__tests__/useChatStreaming.harnessReloadApproval.test.tsx \
  src/components/chat/aui/__tests__/AuiMessage.errorCategory.test.tsx \
  src/services/__tests__/agentChatService.errorCategory.test.ts \
  src/test/architecture/__tests__/maintenanceContracts.test.ts
<PY> -m pytest -c backend/pytest.ini -q \
  backend/tests/unit/api/agent/test_harness_streaming.py \
  backend/tests/unit/api/test_agent_streaming_error_categories.py \
  backend/tests/contract/test_sse_error_categories.py \
  backend/tests/unit/agent/test_agent_error_categories.py \
  backend/tests/unit/services/integrations
```

Expected: all PASS. Record the counts in the PR body.

**Step 2: Type-check and full frontend suite**

```bash
pnpm --dir frontend type-check
pnpm --dir frontend test
```

Expected: `tsc` exits 0. This proves the generated `api.d.ts` from PR 1 has the fields used here: `binding_kind`, `account_summary`, `account_wide`, `project_ids`, nullable `project_id`, and `kind: 'account'`. If `tsc` reports one missing, PR 1's regeneration is incomplete; stop and fix it there, never by hand-editing the generated file. Full `vitest run` is green.

**Step 3: OpenAPI unchanged and blocking ratchets**

```bash
<PY> scripts/ci/generate_openapi.py --check
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

Expected:
- `--check` reports no drift. The new error category is not part of the OpenAPI snapshot.
- `run_local_ci.sh` passes ruff, black and isort on the changed backend files and mypy on added code. `lint:changed` and `quality:exclusions` add no new errors and no exclusions.

If the wrapper cannot run (missing toolchain or base ref), report it as NOT RUN with the reason. Never report it as passing.

**Step 4: Manual check on goodwiinz.tech after deploy (NOT RUN until done)**

Needs PR 1, PR 2 and this PR live on dev, and `ACCOUNT_CONNECTIONS_ENABLED=true`.

1. Run `nous --rc` and open the approval link:
   - The heading reads "<computer> wants access to your whole NOUS account".
   - "Covers" shows the rule and today's counts, matching the owner's 5 workspaces and 33 projects.
   - The permissions use the account wording.
   - The red warning is announced: check with VoiceOver, or confirm `role="alert"` in devtools.
2. Approve, then open Connected devices:
   - "Whole account" is listed with Revoke.
   - The `nous --rc` folder appears under "Account folders".
3. In a chat in project A, choose Local Codex:
   - The computer and the account folder are offered.
   - A folder tied to another project is not offered.
   - A run starts.
4. In a chat in project B, run Codex in the same folder once A's run has finished.
5. In a chat with no project, choose Local Codex: the status reads "Codex runs only in chats that belong to a project."
6. Revoke "Whole account" on Connected devices, then send a Codex turn from a chat whose picker still holds the computer (before it refreshes): the bubble reads "This computer isn't connected to this project." with Retry.
7. Repeat steps 1–2 with a project pairing (`nous-harness connect --project`): the project and workspace pages look exactly as before.

**Step 5: PR body outline**

```markdown
## Summary
Website for account-wide harness connections (design 2026-10-14 §5; PR 3 of 3, after #<PR1> and #<PR2>):
- Approval page: account variant (header, "Covers" rule + live counts, account permission wording, `library:write` warning as an accessible alert).
- Connected devices: "Whole account" consent with Revoke; account folders listed per computer.
- Composer: Local Codex lists only computers (account approval or this project's approval) and folders (account folders + this project's) that can run the chat's project; a server refusal reads "This computer isn't connected to this project." (new `device_not_connected_to_project` category; `create_chat_context` names every device/folder/project refusal with one message).

## Contract notes
- New `AgentErrorCategory.DEVICE_NOT_CONNECTED_TO_PROJECT`; not in OpenAPI (`generate_openapi.py --check` clean).
- No generated-file edits; uses PR 1's regenerated types.

## Test plan
- [x] Targeted vitest files (counts)
- [x] Backend: harness_streaming, streaming error categories, SSE contract, integrations unit
- [x] `pnpm --dir frontend type-check`, `pnpm --dir frontend test`
- [x] `scripts/ci/run_local_ci.sh --base origin/develop --frontend` (or NOT RUN + reason)
- Mutation checks: refusal mapping (Task 1/2), device and folder filters (Task 8), project wiring (Task 9)
- [ ] Manual on goodwiinz.tech after deploy: NOT RUN (checklist in plan Task 10 Step 4)

Fixes GOO-<n>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Push the branch, open the PR against `develop`, and do not merge it yourself. Before pushing, grep the diff for email addresses and absolute home paths, because the repository is public.
