# NOUS MCP alphaXiv Parity Implementation Plan (Plan 07)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development) to implement this plan task-by-task. Owner's model policy for this plan (2026-10-05): **Sonnet at maximum effort ("ultra") for implementation subagents**; Fable only for planning/review. Supersedes the Opus default for this work. One slice = one PR, cut from `origin/develop` in its own worktree (`git worktree add .worktrees/<name> -b <branch> origin/develop`). Python interpreter: `PY=${PY:-$(git rev-parse --show-toplevel)/backend/.venv/bin/python}` from the main checkout, or whatever `PY` the executor exports; worktrees have no venv of their own. Nothing below hard-codes a machine path. Push, merge, flag flips and live proof wait for the owner's word.

**Goal:** Give the harness-bridge MCP server the job coverage of alphaXiv's MCP (find, read and query papers; look up researchers; curate a library) over NOUS data, under NOUS's grant and consent model.

**Architecture:** Two existing allowlists stay the only entry points: `READ_TOOL_NAMES`/`invoke_read` in `backend/src/services/integrations/read_tools.py` and `ALLOWED_ACTIONS`/`request_action` in `backend/src/services/agent/tool_actions.py`. One migration lets a grant bind a **workspace** instead of a single Collection and adds scopes `library:read` / `library:write`; reversible library actions auto-run under `library:write`, destructive ones keep the approve page. A transient arXiv full-text read (Redis cache, no DB write) covers papers not yet ingested. Design: `docs/plans/2026-10-05-nous-mcp-alphaxiv-parity-design.md`.

**Tech Stack:** FastAPI + Pydantic v2 + SQLAlchemy async + Alembic; pytest (`unit` marker, in-memory aiosqlite fixtures); `packages/harness-bridge` (Node 24, MCP SDK, `node:test`, tsx); Next.js approve page; Redis via existing `src.core.redis` helpers; `ArXivIngestionService`.

**Baseline:** `origin/develop` `2ba9b0e97`, 2026-10-05.

---

## Conventions used by every task

- Backend tests: `cd backend && $PY -m pytest -q <path> -k <name>` from the worktree, with `PY` resolved as in the header. Marker `unit` is registered; use `pytestmark = pytest.mark.unit`.
- Lint before each commit: `$PY -m ruff check backend/src backend/tests` and `$PY -m black --check <changed files>`; `$PY -m isort --check <changed files>`.
- Bridge: `pnpm --filter @nous/harness-bridge type-check && pnpm --filter @nous/harness-bridge test`.
- Any Pydantic schema change → `$PY scripts/ci/generate_openapi.py` then `pnpm --dir frontend generate:api-types`; commit both outputs in the same PR (the bridge imports `frontend/src/types/generated/api.d.ts`).
- Public errors: stable strings only. Never put `str(exc)` in a `ToolResult`.
- Every tool resolves its target from the grant (`IntegrationContext`), never from client arguments, except the explicit `project_id` **selector** introduced in Slice 1 for workspace grants, which is validated against the grant's workspace before use.
- Commit messages: conventional (`feat(integrations): ...`), end with the attribution lines from the session reminder.

---

# Slice 1 — Workspace-scoped grants, library scopes, `list_library`

Branch: `feat/plan07-s1-workspace-grant`. **Contains the only migration of this plan.** Serial-migration rule: do not open this PR while another migration-bearing PR (#1784) is in flight unless the owner waives it.

### Task 1.1: Scopes

**Files:**
- Modify: `backend/src/schemas/integration_context.py:11-19`
- Test: `backend/tests/unit/services/integrations/test_context.py`

**Step 1: Failing test**

Append to `test_context.py`:

```python
def test_library_scopes_are_standard() -> None:
    from src.schemas.integration_context import STANDARD_SCOPES

    assert {"library:read", "library:write"} <= STANDARD_SCOPES
```

**Step 2:** Run `$PY -m pytest -q backend/tests/unit/services/integrations/test_context.py -k library_scopes` → FAIL (`AssertionError`).

**Step 3: Implement**

```python
STANDARD_SCOPES = frozenset(
    {
        "harness:execute",
        "tools:read",
        "tools:write",
        "context:read",
        "artifacts:publish",
        # Plan 07: workspace library. read = list folders; write = reversible
        # folder/document changes run without per-action approval.
        "library:read",
        "library:write",
    }
)
```

**Step 4:** Test → PASS. **Step 5:** `git commit -m "feat(integrations): add library:read and library:write scopes"`.

### Task 1.2: Grant and request models carry an optional workspace binding

**Files:**
- Modify: `backend/src/models/integration_grant.py`
- Modify: `backend/src/models/tool_action.py` (`IntegrationToolAction.project_id` → nullable, add `workspace_id`)
- Create: `backend/alembic/versions/hb03_workspace_grants.py`
- Test: `backend/tests/unit/services/integrations/test_context.py`

**Step 1: Failing test** (model-level, SQLite):

```python
async def test_grant_accepts_workspace_binding_without_project(db) -> None:
    from src.models.integration_grant import IntegrationGrant

    grant = IntegrationGrant(
        id=uuid4(), user_id=USER, organization_id=ORG, project_id=None,
        workspace_id=WORKSPACE, scopes=["library:read"], token_hash="x" * 64,
        expires_at=SOON, consented_at=datetime.now(timezone.utc),
    )
    db.add(grant)
    await db.commit()
    assert (await db.get(IntegrationGrant, grant.id)).workspace_id == WORKSPACE
```

(Reuse the file's existing `db` fixture and constants; add `WORKSPACE`/`SOON` if missing.)

**Step 2:** Run → FAIL (`TypeError: 'workspace_id' is an invalid keyword argument` or NOT NULL on `project_id`).

**Step 3: Implement**

In `integration_grant.py`, for **both** `IntegrationGrantRequest` and `IntegrationGrant`:

```python
    project_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("collections.id"), nullable=True
    )
    workspace_id: Mapped[UUID | None] = mapped_column(
        GUID(), ForeignKey("workspaces.id"), nullable=True
    )
    __table_args__ = (
        CheckConstraint(
            "(project_id IS NULL) <> (workspace_id IS NULL)",
            name="ck_<table>_one_binding",
        ),
    )
```

(`from sqlalchemy import CheckConstraint`; use the real table name in each constraint name.) On `IntegrationToolAction` in `tool_action.py` the two columns mean different things — `project_id` is the **target** Collection of the action, `workspace_id` is the grant **binding** — and a workspace-grant action targeting a Collection sets **both**. So the actions table gets an OR constraint, not XOR: `CheckConstraint("project_id IS NOT NULL OR workspace_id IS NOT NULL", name="ck_integration_tool_actions_some_binding")`. (Codex review on #1881, plan L1013.)

Migration `hb03_workspace_grants.py` (set `down_revision` to the output of `cd backend && $PY -m alembic heads`; must be a single head):

```python
"""Workspace-scoped integration grants (Plan 07, slice 1).

Revision ID: hb03_workspace_grants
Revises: <current head>
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "hb03_workspace_grants"
down_revision = "<current head>"
branch_labels = None
depends_on = None

TABLES = ("integration_grant_requests", "integration_grants", "integration_tool_actions")  # upgrade order; downgrade reverses it


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column(
                "workspace_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("workspaces.id"),
                nullable=True,
            ),
        )
        op.alter_column(table, "project_id", nullable=True)
        if table == "integration_tool_actions":
            op.create_check_constraint(
                f"ck_{table}_some_binding", table, "project_id IS NOT NULL OR workspace_id IS NOT NULL"
            )
        else:
            op.create_check_constraint(
                f"ck_{table}_one_binding", table, "(project_id IS NULL) <> (workspace_id IS NULL)"
            )
        op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])


def downgrade() -> None:
    # Children first: actions and grants hold FKs (request_id) into requests.
    for table in reversed(TABLES):
        op.drop_index(f"ix_{table}_workspace_id", table_name=table)
        name = "some_binding" if table == "integration_tool_actions" else "one_binding"
        op.drop_constraint(f"ck_{table}_{name}", table, type_="check")
        op.execute(f"DELETE FROM {table} WHERE project_id IS NULL")
        op.alter_column(table, "project_id", nullable=False)
        op.drop_column(table, "workspace_id")
```

**Step 4:** Test → PASS. `cd backend && $PY ../scripts/ci/check_alembic.py` → single head OK. Run `$PY -m pytest -q backend/tests/unit/services/integrations backend/tests/unit/services/agent/test_tool_actions.py` → all PASS (existing fixtures create tables from models, so the new nullable column must not break them).

**Step 5:** Commit `feat(integrations): optional workspace binding on grants and actions`.

### Task 1.3: `IntegrationContext` and `authorized_scope`

**Files:**
- Modify: `backend/src/schemas/integration_context.py` (`IntegrationContext`, `GrantRequestCreate`, `GrantRequestDTO`)
- Modify: `backend/src/services/integrations/context.py` (`authorized_project` → add `authorized_workspace`, `authorized_scope`, `validate_binding`, `_new_grant`, `mint_integration_grant`, `exchange_request`, `request_dto`, `create_request`, `resolve_integration_context`)
- Test: `backend/tests/unit/services/integrations/test_context.py`

**Step 1: Failing tests**

```python
async def test_workspace_grant_resolves_context_with_workspace(db) -> None:
    # seed: user USER in ORG owns workspace WORKSPACE with collections P1, P2
    token = await _mint(db, workspace_id=WORKSPACE, scopes={"library:read"})
    ctx = await resolve_integration_context(db, token, required_scope="library:read")
    assert ctx.project_id is None and ctx.workspace_id == WORKSPACE


async def test_authorized_scope_lists_live_collections_of_workspace(db) -> None:
    ctx = IntegrationContext(user_id=USER, organization_id=ORG, project_id=None,
                             workspace_id=WORKSPACE, grant_id=uuid4())
    ids = await authorized_scope(db, ctx)
    assert ids == {P1, P2}  # soft-deleted P3 excluded


async def test_authorized_scope_refuses_foreign_workspace(db) -> None:
    ctx = IntegrationContext(user_id=USER, organization_id=ORG, project_id=None,
                             workspace_id=OTHER_WORKSPACE, grant_id=uuid4())
    with pytest.raises(IntegrationAccessDenied):
        await authorized_scope(db, ctx)
```

Write a small `_mint(db, *, project_id=None, workspace_id=None, scopes)` helper in the test that inserts an `IntegrationGrant` row with a known token hash (pattern: `sha256(token.encode()).hexdigest()`).

**Step 2:** Run → FAIL (`TypeError` on `workspace_id`, `ImportError: authorized_scope`).

**Step 3: Implement**

`integration_context.py`:

```python
class IntegrationContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: UUID
    organization_id: UUID
    # Exactly one binding. project_id: one Collection (today's model).
    # workspace_id: every live Collection the user can see in that workspace.
    project_id: UUID | None = None
    workspace_id: UUID | None = None
    thread_id: UUID | None = None
    run_id: UUID | None = None
    grant_id: UUID
    consent_id: UUID | None = None

    @model_validator(mode="after")
    def _one_binding(self) -> "IntegrationContext":
        if (self.project_id is None) == (self.workspace_id is None):
            raise ValueError("exactly one of project_id or workspace_id")
        return self


class GrantRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID | None = None
    workspace_id: UUID | None = None
    device_id: UUID
    scopes: set[str]

    @model_validator(mode="after")
    def _one_binding(self) -> "GrantRequestCreate":
        if (self.project_id is None) == (self.workspace_id is None):
            raise ValueError("exactly one of project_id or workspace_id")
        return self
```

`GrantRequestDTO`: `project_id: UUID | None`, `project_label: str | None`, add `workspace_id: UUID | None = None`, `workspace_label: str | None = None`.

`context.py`:

```python
async def authorized_workspace(
    db: AsyncSession, user_id: UUID, organization_id: UUID, workspace_id: UUID
) -> Row[Any]:
    """The workspace if the user owns it or is a live member, with live org and user."""
    member = exists().where(
        WorkspaceMember.workspace_id == Workspace.id,
        WorkspaceMember.user_id == user_id,
        WorkspaceMember.is_deleted.is_(False),
    )
    row = (
        await db.execute(
            select(Workspace.id, Workspace.name).where(
                Workspace.id == workspace_id,
                Workspace.is_deleted.is_(False),
                Workspace.organization_id == organization_id,
                or_(Workspace.owner_id == user_id, member),
                exists().where(
                    User.id == user_id,
                    User.organization_id == organization_id,
                    User.is_active.is_(True),
                    User.is_deleted.is_(False),
                ),
                exists().where(
                    Organization.id == organization_id,
                    Organization.is_deleted.is_(False),
                    Organization.is_active.is_(True),
                ),
            )
        )
    ).first()
    if row is None:
        raise IntegrationAccessDenied()
    return row


async def authorized_scope(db: AsyncSession, context: IntegrationContext) -> set[UUID]:
    """Collection ids the grant may touch; re-checked on every call."""
    if context.project_id is not None:
        row = await authorized_project(
            db, context.user_id, context.organization_id, context.project_id
        )
        return {UUID(str(row.id))}
    assert context.workspace_id is not None
    await authorized_workspace(
        db, context.user_id, context.organization_id, context.workspace_id
    )
    rows = await db.execute(
        select(Collection.id).where(
            Collection.workspace_id == context.workspace_id,
            Collection.is_deleted.is_(False),
        )
    )
    return {UUID(str(value)) for value in rows.scalars().all()}
```

Thread through the binding:

- `validate_binding(..., project_id: UUID | None, workspace_id: UUID | None = None, ...)`: if `project_id` → `authorized_project`; else `authorized_workspace`. `thread_id`/`run_id` checks stay project-only; raise `IntegrationAccessDenied` if either is given with a workspace binding.
- `_new_grant` / `mint_integration_grant`: add `workspace_id: UUID | None = None`, set on the row.
- `create_request`: validate `data.workspace_id` via `authorized_workspace` when present; persist it.
- `request_dto`: label from `authorized_project` or `authorized_workspace`; fill `workspace_id`/`workspace_label`.
- `exchange_request`: pass `request.workspace_id` to `_new_grant`.
- `_validate_grant`: pass both to `validate_binding`; the consent lookup compares `project_id` **and** `workspace_id`.
- `resolve_integration_context`: `project_id=grant.project_id, workspace_id=grant.workspace_id`.
- `check_scopes`: scope implications so a grant can never carry a library scope without the gateway scope the `/tools` router requires: `library:read` ⇒ `tools:read`; `library:write` ⇒ `library:read` and `tools:write`. Raise `IntegrationAccessDenied` otherwise. Test both. (Codex #1881 L454: the `/integrations/tools` dependency stays `tools:read`.)
- Workspace grants are MCP-only: `create_request`/`mint_integration_grant` reject `harness:execute` and `artifacts:publish` when `workspace_id` is set (`IntegrationAccessDenied`). Test it. (Codex #1881 L493.)

**Step 4:** Tests → PASS. Run the whole `backend/tests/unit/services/integrations` + `backend/tests/unit/api/test_integration_*` → PASS.

**Step 5:** Commit `feat(integrations): workspace-scoped grant binding and authorized_scope`.

### Task 1.4: Existing consumers tolerate `project_id=None`

**Files:**
- Modify: `backend/src/services/integrations/read_tools.py` (`invoke_read`, `_live_project_documents`)
- Modify: `backend/src/services/agent/tool_actions.py` (`_actor` users, `_same_target`, `_authority_intact`, `get_action_for_review`)
- Modify: `backend/src/api/integrations/actions.py` (`_actor`), `backend/src/schemas/tool_actions.py` (`ActionActor.project_id: UUID | None`, add `workspace_id`)
- Modify: `backend/src/api/artifacts.py` and `backend/src/api/harness.py` where `context.project_id` is used: refuse workspace grants with 403 `"Integration access denied"` (artifacts and harness runs stay project-bound).
- Test: `backend/tests/unit/services/integrations/test_read_tools.py`, `backend/tests/unit/services/agent/test_tool_actions.py`

**Step 1: Failing tests**

```python
async def test_workspace_grant_needs_project_selector_for_project_tools(db) -> None:
    ctx = _workspace_context()
    result = await invoke_read(db, ctx, ToolInvocation(
        tool_name="list_project_documents", arguments={}, invocation_id=uuid4()))
    assert result.is_error and result.content[0]["error"] == "project_id_required"


async def test_workspace_grant_project_selector_outside_workspace_denied(db) -> None:
    ctx = _workspace_context()
    with pytest.raises(IntegrationAccessDenied):
        await invoke_read(db, ctx, ToolInvocation(
            tool_name="list_project_documents",
            arguments={"project_id": str(OTHER_PROJECT)}, invocation_id=uuid4()))
```

**Step 2:** Run → FAIL (`ValidationError` on `IntegrationContext` or `unknown arguments`).

**Step 3: Implement** in `read_tools.py`:

```python
async def _target_project(
    db: AsyncSession, context: IntegrationContext, arguments: dict[str, Any]
) -> UUID | None:
    """The Collection a call acts on.

    Project grants: always the bound Collection; a client project_id is an
    unknown argument. Workspace grants: project_id is a *selector* and must be
    one of the workspace's live Collections (``authorized_scope``).
    Returns None when a workspace grant omits the selector.
    """
    allowed = await authorized_scope(db, context)
    if context.project_id is not None:
        return context.project_id
    raw = arguments.pop("project_id", None)
    if raw is None:
        return None
    try:
        chosen = UUID(str(raw))
    except ValueError as error:
        raise ToolArgumentError("project_id must be a UUID") from error
    if chosen not in allowed:
        raise IntegrationAccessDenied()
    return chosen
```

- `_validate_arguments`: when `context.workspace_id is not None`, allow `project_id` (uuid string) in `allowed` for the project-scoped tools. Pass `context` into `_validate_arguments`.
- `_live_project_documents(context)` → `_live_project_documents(project_id, organization_id)`; update callers.
- `invoke_read`: replace `project_id = str(context.project_id)` with `target = await _target_project(...)`; if `None` for a project-scoped tool return `ToolResult(content=[{"error": "project_id_required"}], is_error=True, source_refs=[])`. `_verify_project_ownership(str(target), db, user)` stays.
- `list_read_tools()`: when advertising, add an optional `project_id` property with description "Required for workspace grants: which project to act on." Keep `additionalProperties: False`.
- `tool_actions.py`: `IntegrationToolAction.project_id` may be None for future folder actions, but in this slice every action still has a project; add `workspace_id` to `ActionActor` and persist it on the row; `_same_target` compares both; `_authority_intact` compares `grant.workspace_id` too; `get_action_for_review` outer-joins `Collection` on `project_id` **and** joins `Workspace` on `IntegrationToolAction.workspace_id` (falling back to `Collection.workspace_id`), so `ActionReview` gains `workspace_id: UUID | None` and `workspace_label: str | None` next to a now-optional `project_label`; the review page renders the workspace row whenever `project_label` is null. Test: a workspace-level row yields a workspace label. (Codex #1881 L380.)
- Routers: `_actor` passes `workspace_id=context.workspace_id`. `artifacts.py` / `harness.py`: `if context.project_id is None: raise HTTPException(403, "Integration access denied")`.

**Step 4:** Tests → PASS; full `backend/tests/unit/services/integrations`, `backend/tests/unit/services/agent/test_tool_actions.py`, `backend/tests/unit/api/test_integration_*`, `backend/tests/unit/architecture` → PASS. Regenerate OpenAPI + api types (schemas changed). `pnpm --filter @nous/harness-bridge type-check` → PASS (the `ActionStatus` type is unchanged; `GrantRequestDTO` fields widened).

**Step 5:** Commit `feat(integrations): project selector for workspace grants; artifacts and harness stay project-bound`.

### Task 1.5: `list_library` read tool (`library:read`)

**Files:**
- Modify: `backend/src/services/integrations/read_tools.py`
- Modify: `backend/src/api/integrations/tools.py` (scope check per tool)
- Test: `backend/tests/unit/services/integrations/test_read_tools.py`

**Step 1: Failing test**

```python
async def test_list_library_returns_workspace_collections_with_counts(db) -> None:
    ctx = _workspace_context(scopes=["tools:read", "library:read"])
    result = await invoke_read(db, ctx, ToolInvocation(
        tool_name="list_library", arguments={}, invocation_id=uuid4()))
    folders = {f["id"]: f for f in result.content[0]["folders"]}
    assert set(folders) == {str(P1), str(P2)}
    assert folders[str(P1)]["document_count"] == 1
    assert result.source_refs == []


async def test_list_library_on_project_grant_lists_only_that_project(db) -> None:
    ...assert [f["id"] for f in folders] == [str(PROJECT)]
```

**Step 2:** Run → FAIL (`tool is not available to integrations`).

**Step 3: Implement**

`list_library` has no agent-registry twin, so `read_tools.py` gets a tiny local descriptor table:

```python
LOCAL_TOOLS: dict[str, tuple[str, dict[str, Any], str]] = {
    # name: (description, input_schema, required scope)
    "list_library": (
        "List the folders (NOUS projects) this connection may use, with document counts.",
        {"type": "object", "additionalProperties": False, "required": [],
         "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 50},
                        "offset": {"type": "integer", "minimum": 0, "default": 0}}},
        "library:read",
    ),
}
# Strict argument models for local tools; the registry path already validates
# through pydantic, local tools must not be weaker. (Codex #1881 L431.)
class _ListLibraryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(default=50, ge=1, le=50)
    offset: int = Field(default=0, ge=0)


LOCAL_TOOL_MODELS: dict[str, type[BaseModel]] = {"list_library": _ListLibraryArgs}
TOOL_SCOPES: dict[str, str] = {name: "tools:read" for name in READ_TOOL_NAMES} | {
    name: scope for name, (_d, _s, scope) in LOCAL_TOOLS.items()
}
```

- `list_read_tools(scopes: Iterable[str] | None = None)` → registry tools plus `LOCAL_TOOLS`, filtered to the grant's scopes when given.
- `_validate_arguments`: local tools validate with `LOCAL_TOOL_MODELS[name].model_validate(arguments, strict=True)`; `ValidationError` → `ToolArgumentError("invalid argument types")`, unknown keys → `"unknown arguments"` (same 422 contract as registry tools). Every later local tool (Slices 2–4) adds its own strict model here.
- Branch in `invoke_read`:

```python
    if name == "list_library":
        allowed = await authorized_scope(db, context)
        rows = await db.execute(
            select(Collection.id, Collection.name, Collection.description,
                   func.count(CollectionDocument.id))
            .outerjoin(CollectionDocument, and_(
                CollectionDocument.collection_id == Collection.id,
                CollectionDocument.is_deleted.is_(False)))
            .where(Collection.id.in_(allowed), Collection.is_deleted.is_(False))
            .group_by(Collection.id, Collection.name, Collection.description)
            .order_by(Collection.name, Collection.id)
            .offset(offset)
            .limit(limit + 1)
        )
        found = rows.all()
        payload = {"folders": [
            {"id": str(i), "name": n, "description": (d or "")[:500], "document_count": int(c)}
            for i, n, d, c in found[:limit]],
            "offset": offset, "next_offset": offset + limit if len(found) > limit else None}
```

`list_library` skips the per-project `_verify_project_ownership` block (it has no single target); restructure `invoke_read` so that block runs only for project-scoped tools.

Router `tools.py`: the dependency stays `tools:read` for the catalog; for `/read`, after resolving the context, check `TOOL_SCOPES[invocation.tool_name] in grant scopes`. `IntegrationContext` has no `scopes` field → add `scopes: frozenset[str]` to `IntegrationContext` (set in `resolve_integration_context`) and raise `IntegrationAccessDenied` in `invoke_read` when the tool's scope is missing. Catalog: `list_read_tools(context.scopes)`.

**Step 4:** Tests → PASS. Also add `test_list_library_requires_library_read_scope` (grant with only `tools:read` → `IntegrationAccessDenied`). **Mutation check:** temporarily drop `Collection.id.in_(allowed)` → the first test must fail; restore.

**Step 5:** Commit `feat(integrations): list_library read tool behind library:read`.

### Task 1.6: Bridge `--workspace` and `--library` flags

**Files:**
- Modify: `packages/harness-bridge/src/cli.ts:85-135` (connect), `:405-475` (`mcpSession`), `packages/harness-bridge/src/mcp/client.ts` (`McpSession`)
- Test: `packages/harness-bridge/test/mcp.test.ts`

**Step 1: Failing test** (pattern of "`--tools` persists tools:read" at `mcp.test.ts:246`):

```ts
test("connect --workspace --library requests workspace binding and library scopes", async () => {
  const { requests, origin, close } = await backend(/* consent stub returning approved */);
  try {
    await connect({ apiUrl: origin, workspaceId: WORKSPACE, label: "dev", tools: true, library: true, write: true, ... });
    const consent = requests.find((r) => r.path === "/api/v1/integrations/grant-requests");
    assert.deepEqual(JSON.parse(consent!.body), {
      workspace_id: WORKSPACE, device_id: DEVICE,
      scopes: ["harness:execute", "tools:read", "tools:write", "library:read", "library:write"],
    });
  } finally { await close(); }
});

test("--library requires --tools and --write", () => {
  assert.rejects(connect({ ...base, tools: false, library: true }), /--library requires --tools/);
  assert.rejects(connect({ ...base, tools: true, write: false, library: true }), /--library requires --write/);
});
```

**Step 2:** `pnpm --filter @nous/harness-bridge test` → FAIL (unknown option).

**Step 3: Implement** in `cli.ts` connect options: `projectId?: string; workspaceId?: string; library?: boolean`. Exactly one of project/workspace required (`throw new Error("--project or --workspace (not both) required")`). Scopes:

```ts
  if (options.library && !options.tools) throw new Error("--library requires --tools");
  if (options.library && !options.write) throw new Error("--library requires --write");
  const scopes = [
    "harness:execute",
    ...(options.tools ? ["tools:read"] : []),
    ...(options.publish ? ["artifacts:publish"] : []),
    ...(options.write ? ["tools:write"] : []),
    ...(options.library ? ["library:read", "library:write"] : []),
  ];
```

`--workspace` is MCP-only: throw `"--workspace cannot be combined with --publish"`, and build the scope list **without** `harness:execute` when `workspaceId` is set (harness runs and artifact publication stay project-bound; the backend refuses them anyway, Task 1.3). `mcpSession`/`sessionOptionsFor`: when `state.workspaceId` is set, never offer `outputRoot` and make the managed-session path throw `"workspace connections are MCP-only; reconnect with --project to run harness sessions"`. Tests for both. Consent body: `{ ...(options.projectId ? { project_id } : { workspace_id }), device_id, scopes }`. Persist `workspaceId` in `LocalState`. `mcpSession`: `...(state.scopes?.includes("library:write") ? { library: true } : {})`; `McpSession.library?: boolean`. Argv in `mcp/config.ts`: `...(session.library ? ["--library"] : [])` (used in Slice 3). README: document `--workspace`, `--library`.

**Step 4:** type-check + tests → PASS. **Step 5:** Commit `feat(harness-bridge): connect --workspace and --library`.

### Task 1.7: Approve page shows the binding and scope labels

**Files:**
- Modify: `frontend/app/(dashboard)/integrations/approve/page.tsx:60-90`
- Create: `frontend/src/lib/integrations/scopeLabels.ts`
- Test: `frontend/src/lib/integrations/scopeLabels.test.ts` (vitest)

**Step 1: Failing test**

```ts
import { describe, expect, it } from "vitest";
import { scopeLabel } from "./scopeLabels";

describe("scopeLabel", () => {
  it("explains library:write as running without per-action approval", () => {
    expect(scopeLabel("library:write")).toMatch(/without asking each time/);
  });
  it("falls back to the raw scope", () => {
    expect(scopeLabel("weird:scope")).toBe("weird:scope");
  });
});
```

**Step 2:** `pnpm --dir frontend exec vitest run src/lib/integrations/scopeLabels.test.ts` → FAIL.

**Step 3: Implement**

```ts
const LABELS: Record<string, string> = {
  "harness:execute": "Run harness sessions bound to this connection",
  "tools:read": "Read documents, drafts and search results in scope",
  "tools:write": "Request changes that you approve one by one",
  "artifacts:publish": "Publish files from the registered output folder",
  "context:read": "Read selected memories",
  "library:read": "List the folders (projects) in this workspace",
  "library:write":
    "Add, remove, move and rename items in this workspace without asking each time. Deleting folders and ingesting papers still require your approval.",
};
export const scopeLabel = (scope: string): string => LABELS[scope] ?? scope;
```

Page: render `<li key={scope}>{scopeLabel(scope)} <code>{scope}</code></li>`; show a "Workspace" row (`consent.workspace_label`) when `consent.workspace_id` is set, else the existing "Project" row. Uses the regenerated `api.d.ts` types; no `as unknown as`.

**Step 4:** vitest → PASS; `pnpm --dir frontend type-check` → PASS. **Step 5:** Commit `feat(integrations): consent page labels scopes and shows workspace binding`.

### Task 1.8: Docs + CI for Slice 1

- `docs/engineering/harness-bridge.md` §consent: describe workspace binding, `library:*` scopes, the auto-run rule (effective from Slice 3).
- `packages/harness-bridge/README.md`: new flags.
- Run `scripts/ci/run_local_ci.sh --base origin/develop --frontend`; fix anything blocking. Report any gate needing DB/browser as `NOT RUN`.
- Commit `docs(harness-bridge): workspace grants and library scopes`. Open PR titled `feat(integrations): workspace-scoped grants and library scopes (Plan 07 S1)`; body links the design doc. **Do not merge without the owner.**

---

# Slice 2 — Read tools: arXiv/connectors, paper content, passages

Branch: `feat/plan07-s2-read-tools` (base `origin/develop` after S1 merges, or stacked on S1 with the base retargeted later; never `--delete-branch` on a stacked chain).

### Task 2.1: Expose args-only registry primitives

**Files:**
- Modify: `backend/src/services/integrations/read_tools.py` (`READ_TOOL_NAMES`, `_SCHEMA_OVERRIDES`, `invoke_read`)
- Test: `backend/tests/unit/services/integrations/test_read_tools.py`

**Step 1: Failing tests**

```python
@pytest.mark.parametrize("name", ["search_arxiv", "search_external_database", "list_external_databases"])
def test_primitives_advertised_without_identity(name: str) -> None:
    tool = next(t for t in list_read_tools() if t.name == name)
    assert not (set(tool.input_schema["properties"]) & read_tools.IDENTITY_ARGUMENTS)


async def test_search_arxiv_is_dispatched_with_clamped_results(db, monkeypatch) -> None:
    seen: dict[str, Any] = {}
    async def fake(args):  # noqa: ANN001
        seen.update(args); return {"papers": [{"id": "2401.00001", "title": "T"}], "total": 1}
    monkeypatch.setattr(read_tools, "_tool_search_arxiv", fake)
    result = await invoke_read(db, _project_context(), ToolInvocation(
        tool_name="search_arxiv", arguments={"query": "mamba", "max_results": 99}, invocation_id=uuid4()))
    assert seen["max_results"] == 20
    assert result.source_refs == [{"arxiv_id": "2401.00001"}]
```

**Step 2:** Run → FAIL.

**Step 3: Implement**

```python
READ_TOOL_NAMES = (
    "search_documents", "list_project_documents", "do_kb_retrieve", "get_current_draft",
    "search_arxiv", "search_external_database", "list_external_databases",
)
MAX_EXTERNAL_RESULTS = 20
_SCHEMA_OVERRIDES |= {
    "search_arxiv": {"max_results": {"maximum": MAX_EXTERNAL_RESULTS}},
    "search_external_database": {"max_results": {"maximum": MAX_EXTERNAL_RESULTS}},
}
```

Import `_tool_search_arxiv, _tool_search_external_database, _tool_list_external_databases` from `tools_impl`. These need no project: run them **before** the project-ownership block, under `asyncio.wait_for(..., timeout=120)` for `search_arxiv` (the agent's SLOW budget; import the constant from `_nodes_tools` if it is public, else define `SLOW_TOOL_TIMEOUT_S = 120` with a comment pointing there). Timeout → `{"error": "upstream_timeout"}`. `_source_refs`: add `{"arxiv_id": p["id"]}` for `payload["papers"]` and `{"external_id": r["id"], "connector": payload.get("connector")}` for `payload["results"]` when present.

**Step 4:** Tests → PASS. **Step 5:** Commit `feat(integrations): expose search_arxiv and external connector search to harnesses`.

### Task 2.2: `get_arxiv_paper_content` (transient full text)

**Files:**
- Create: `backend/src/services/integrations/arxiv_fulltext.py`
- Modify: `read_tools.py` (`LOCAL_TOOLS`, branch)
- Test: `backend/tests/unit/services/integrations/test_arxiv_fulltext.py`

**Step 1: Failing tests**

```python
pytestmark = pytest.mark.unit


class _Redis:
    def __init__(self): self.store: dict[str, str] = {}
    async def get(self, k): return self.store.get(k)
    async def set(self, k, v, ex=None): self.store[k] = v


async def test_first_call_downloads_and_caches(monkeypatch) -> None:
    redis = _Redis()
    calls = []
    async def fetch(arxiv_id):  # noqa: ANN001
        calls.append(arxiv_id); return "A" * 100_000
    page = await arxiv_fulltext.get_page("2401.00001", offset=0, limit=48_000, redis=redis, fetch=fetch)
    assert page["total_chars"] == 100_000 and page["next_offset"] == 48_000 and len(page["text"]) == 48_000
    assert "arxiv:fulltext:2401.00001" in redis.store
    await arxiv_fulltext.get_page("2401.00001", offset=48_000, limit=48_000, redis=redis, fetch=fetch)
    assert calls == ["2401.00001"]


async def test_last_page_has_no_next_offset() -> None:
    ...assert page["next_offset"] is None


async def test_invalid_id_rejected() -> None:
    with pytest.raises(ValueError):
        await arxiv_fulltext.get_page("../etc", offset=0, limit=10, redis=_Redis(), fetch=None)
```

**Step 2:** Run → FAIL (`ModuleNotFoundError`).

**Step 3: Implement**

```python
"""Transient arXiv full text for harness reads. Nothing is persisted to
Postgres or object storage; ingest stays the only path to a NOUS document."""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

TTL_S = 24 * 3600
MAX_PAGE_CHARS = 48_000  # keeps one page under the 64 KiB gateway cap after JSON escaping
_ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}(v\d+)?|[a-z\-]+(\.[A-Z]{2})?/\d{7}(v\d+)?)$")
Fetch = Callable[[str], Awaitable[str]]


def _key(arxiv_id: str) -> str:
    return f"arxiv:fulltext:{arxiv_id}"


async def fetch_text(arxiv_id: str) -> str:
    """Download + extract through the existing service (rate gate, 429 handling)."""
    from src.services.arxiv.arxiv_service import ArXivIngestionService

    async with ArXivIngestionService() as svc:
        pdf = await svc.download_paper_pdf(arxiv_id)
        if not pdf:
            raise LookupError("arxiv_unavailable")
        extracted = await svc.extract_pdf_content(pdf)
    return str(extracted.get("full_text") or "")


async def get_page(
    arxiv_id: str, *, offset: int, limit: int, redis: Any, fetch: Fetch | None = None
) -> dict[str, Any]:
    if not _ARXIV_ID.match(arxiv_id):
        raise ValueError("invalid arxiv id")
    limit = max(1, min(int(limit), MAX_PAGE_CHARS))
    offset = max(0, int(offset))
    text = await redis.get(_key(arxiv_id))
    if text is None:
        text = await (fetch or fetch_text)(arxiv_id)
        await redis.set(_key(arxiv_id), text, ex=TTL_S)
    chunk = text[offset : offset + limit]
    nxt = offset + limit if offset + limit < len(text) else None
    return {"arxiv_id": arxiv_id, "offset": offset, "next_offset": nxt,
            "total_chars": len(text), "text": chunk}
```

Check `ArXivIngestionService.__aenter__` exists (it says "Use async context manager"); check whether `extract_pdf_content` returns `full_text` (read `arxiv_service.py:740-800`) and adjust the key. Check how other services obtain an async Redis client (`rg -n "get_redis|redis_client" backend/src/core`) and use that in `read_tools.py`; when Redis is unavailable, pass a no-op cache object (every call downloads; still correct).

`read_tools.py`:

```python
LOCAL_TOOLS["get_arxiv_paper_content"] = (
    "Read the full text of an arXiv paper by id without ingesting it. Paginated by characters; "
    "follow next_offset until it is null.",
    {"type": "object", "additionalProperties": False, "required": ["arxiv_id"],
     "properties": {
        "arxiv_id": {"type": "string", "maxLength": 32},
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 48000, "default": 48000}}},
    "tools:read",
)
```

Branch: `ValueError` → `ToolArgumentError("invalid arxiv id")`; `LookupError` → `{"error": "arxiv_unavailable"}`; other exceptions → log and `{"error": "arxiv_unavailable"}`. `source_refs=[{"arxiv_id": ...}]`.

**Step 4:** Tests → PASS. **Step 5:** Commit `feat(integrations): transient arXiv full-text read tool`.

### Task 2.3: `get_document_content` (ingested papers, paginated)

**Files:**
- Modify: `read_tools.py`
- Test: `test_read_tools.py`

**Step 1: Failing tests**

```python
async def test_get_document_content_pages_full_text(db) -> None:
    # seed DOC_IN_PROJECT with content_text "x" * 100_000
    r = await invoke_read(db, _project_context(), ToolInvocation(
        tool_name="get_document_content",
        arguments={"document_id": str(DOC_IN_PROJECT), "mode": "full", "limit": 48000},
        invocation_id=uuid4()))
    body = r.content[0]
    assert body["next_offset"] == 48000 and len(body["text"]) == 48000
    assert r.source_refs == [{"document_id": str(DOC_IN_PROJECT)}]


async def test_get_document_content_outside_project_is_unavailable(db) -> None:
    r = await invoke_read(db, _project_context(), ToolInvocation(
        tool_name="get_document_content",
        arguments={"document_id": str(DOC_OUTSIDE_PROJECT)}, invocation_id=uuid4()))
    assert r.is_error and r.content[0]["error"] == "requested_documents_unavailable"
```

**Step 2:** Run → FAIL.

**Step 3: Implement** — `LOCAL_TOOLS["get_document_content"]` schema `{document_id (uuid, required), mode: enum ["summary","full"] default "summary", offset, limit ≤ 48000}` scope `tools:read`. Branch:

```python
    elif name == "get_document_content":
        ids = await _project_document_ids(db, target, context.organization_id, [arguments["document_id"]])
        if ids is None:
            return _unavailable()
        doc = await db.get(Document, UUID(ids[0]))
        full = str(doc.content_text or "") if arguments.get("mode", "summary") == "full" \
            else str(doc.content_summary or doc.content_preview or "")
        offset = _clamp(arguments.get("offset", 0), 0, 2**31 - 1, 0)
        limit = _clamp(arguments.get("limit", MAX_PAGE_CHARS), 1, MAX_PAGE_CHARS, MAX_PAGE_CHARS)
        payload = {"document_id": ids[0], "title": doc.title, "mode": ..., "offset": offset,
                   "next_offset": offset + limit if offset + limit < len(full) else None,
                   "total_chars": len(full), "text": full[offset: offset + limit]}
```

Factor `_unavailable()` out of the existing `do_kb_retrieve` branch (same payload). Confirm the column names on `Document` (`content_text`, `content_summary`, `content_preview`) in `backend/src/models/document.py` before coding.

**Step 4:** Tests → PASS; mutation check: remove the membership check → cross-project test fails. **Step 5:** Commit `feat(integrations): get_document_content read tool`.

### Task 2.4: `retrieve_passages` (PostgreSQL full-text)

**Files:**
- Modify: `read_tools.py`
- Test: `test_read_tools.py`

**Step 1:** Read `backend/src/services/search/fulltext_search_service.py:64-110` and `backend/src/api/documents/documents.py:902-953` to find the async entry point used by `/documents/search` (the service is sync `Session`-based; the route shows how it is called). The branch must call **that** function with `organization_id` and a document-id filter; no new SQL in `read_tools.py`.

Failing test: fake the service function via `monkeypatch`, assert it is called with `document_ids` = the project's live documents when `document_ids` is omitted, and that a foreign id returns `requested_documents_unavailable`.

**Step 2:** FAIL. **Step 3:** `LOCAL_TOOLS["retrieve_passages"]` schema `{query (required), document_ids (uuid[] 1–20, optional), top_k ≤ 20 default 8}`, scope `tools:read`. Payload `{chunks:[{document_id,title,text,score}], total, query}`, `source_refs` from `document_id`. **Step 4:** PASS. **Step 5:** Commit `feat(integrations): retrieve_passages over PostgreSQL full-text`.

### Task 2.5: Docs, API test, PR

- `backend/tests/unit/api/test_integration_tools.py`: one test that `/integrations/tools` lists `get_arxiv_paper_content` (no DB needed; `list_read_tools` only).
- `docs/engineering/harness-bridge.md` + README tool table: all Slice 2 tools, with the explicit sentence "`get_arxiv_paper_content` persists nothing; use `ingest_arxiv_papers` to add a paper to NOUS".
- `run_local_ci.sh --base origin/develop`; PR `feat(integrations): harness read tools for arXiv, paper content and passages (Plan 07 S2)`.

---

# Slice 3 — Library writes with auto-run

Branch: `feat/plan07-s3-library-actions`. Depends on S1.

> **Review amendments (Codex on PR #1881, 2026-10-05) — apply these when executing S3; they supersede the task text below where they conflict:**
> 1. Scope per action, not per route. `request_action` checks `REQUIRED_SCOPE_FOR[tool_name]` ∈ `actor.scopes` (`tools:write` for `create_project_note`, `delete_folder`, `ingest_arxiv_papers`; `library:write` for the auto-run set) and `_authority_intact` re-checks the **same per-action scope** on the live grant. Tests: a `library:write`-only grant cannot request or execute `delete_folder` / `ingest_arxiv_papers`; a `tools:write`-only grant gets `awaiting_approval` for library actions. (L945)
> 2. Selector vs identity. Split `IDENTITY_KEYS` (actor identity: `user_id`, `organization_id`, `thread_id`, `run_id`, `grant_id`) from `SELECTOR_KEYS` (`project_id`, `from_project_id`, `to_project_id`): `_only` rejects identity keys, validators parse selectors, `_resolve_target` validates them against `authorized_scope`. Project grants still reject any selector that is not the bound project. (L877)
> 3. Targets without a project selector. `update_document_metadata` resolves its target Collection through **live `CollectionDocument` membership** of `document_id` within `authorized_scope` (ambiguous across several in-scope Collections → use the first by name, record all in the receipt). `ingest_arxiv_papers` under a workspace grant **requires** a `project_id` selector (add to its validator; project grants ignore/forbid it). (L1001, L912)
> 4. Non-committing effects only. `_tool_ingest_arxiv` opens its own sessions and writes object storage, so it cannot run inside `_run_effect`'s single transaction: keep `ingest_arxiv_papers` on the approval path **and** execute it through the existing Celery drain with its own idempotency key, recording the receipt after the fact (same pattern as today's approved notes if they already run via the worker; otherwise mark the row `executing` and let the worker `_finish`). Document the weaker atomicity in the receipt. (L1035)
> 5. `PUT /files/{id}` has no service; the router commits inline. Extract `services/documents/file_metadata_service.update_file_metadata(db, file_id, user, *, title, tags, commit=True)` in S3, point the router at it (behaviour unchanged, architecture tests green), then call it with `commit=False` from `_run_effect`. (L1033)
> 6. `IntegrationToolAction` rows for workspace actions set both `project_id` (target) and `workspace_id` (binding); `create_folder` sets only `workspace_id`. Matches the OR constraint from Task 1.2. (L1013)

### Task 3.1: Action registry with per-action validators and effect modes

**Files:**
- Modify: `backend/src/services/agent/tool_actions.py`
- Test: `backend/tests/unit/services/agent/test_tool_actions.py`

**Step 1: Failing tests**

```python
def test_action_catalogue() -> None:
    assert tool_actions.AUTO_RUN_ACTIONS <= tool_actions.ALLOWED_ACTIONS
    assert {"delete_folder", "ingest_arxiv_papers"}.isdisjoint(tool_actions.AUTO_RUN_ACTIONS)


@pytest.mark.parametrize("name,args", [
    ("save_papers_to_folder", {"document_ids": [str(uuid4())], "project_id": str(PROJECT)}),
    ("remove_papers_from_folder", {"document_ids": [str(uuid4())], "project_id": str(PROJECT)}),
    ("move_papers_between_folders", {"document_ids": [str(uuid4())], "from_project_id": str(PROJECT), "to_project_id": str(P2)}),
    ("create_folder", {"name": "Reading"}),
    ("rename_folder", {"project_id": str(PROJECT), "name": "Read"}),
    ("delete_folder", {"project_id": str(PROJECT)}),
    ("update_document_metadata", {"document_id": str(uuid4()), "title": "New"}),
    ("ingest_arxiv_papers", {"paper_ids": ["2401.00001"]}),
])
def test_validators_accept_minimal_valid_args(name: str, args: dict) -> None:
    assert tool_actions.validate_arguments(name, args) == args


def test_validators_reject_identity_and_unknown_keys() -> None:
    with pytest.raises(ToolActionArgumentError):
        tool_actions.validate_arguments("create_folder", {"name": "x", "user_id": "y"})
    with pytest.raises(ToolActionArgumentError):
        tool_actions.validate_arguments("save_papers_to_folder", {"document_ids": [], "project_id": str(PROJECT)})
```

**Step 2:** FAIL.

**Step 3: Implement** (replace `_validate_note_arguments` call site with a dispatcher; keep that function):

```python
ALLOWED_ACTIONS = frozenset({
    "create_project_note", "save_papers_to_folder", "remove_papers_from_folder",
    "move_papers_between_folders", "create_folder", "rename_folder", "delete_folder",
    "update_document_metadata", "ingest_arxiv_papers",
})
# Reversible changes a library:write grant may run without a per-action decision.
AUTO_RUN_ACTIONS = frozenset({
    "save_papers_to_folder", "remove_papers_from_folder", "move_papers_between_folders",
    "create_folder", "rename_folder", "update_document_metadata",
})
LIBRARY_SCOPE = "library:write"
MAX_DOCUMENT_IDS = 20
MAX_PAPER_IDS = 10


def _uuid_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_DOCUMENT_IDS:
        raise ToolActionArgumentError(f"{field} must contain 1-{MAX_DOCUMENT_IDS} UUIDs")
    try:
        return [str(UUID(str(v))) for v in value]
    except ValueError as error:
        raise ToolActionArgumentError(f"{field} must be UUIDs") from error


def _uuid(value: Any, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError) as error:
        raise ToolActionArgumentError(f"{field} must be a UUID") from error


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TITLE:
        raise ToolActionArgumentError("name must be 1-255 characters")
    return value.strip()


def _only(arguments: dict[str, Any], *keys: str) -> None:
    if IDENTITY_KEYS & arguments.keys():
        raise ToolActionArgumentError("identity arguments are not accepted")
    if arguments.keys() - set(keys):
        raise ToolActionArgumentError(f"only {', '.join(keys)} are accepted")


def validate_arguments(tool_name: str, a: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "create_project_note":
        return _validate_note_arguments(a)
    if tool_name in {"save_papers_to_folder", "remove_papers_from_folder"}:
        _only(a, "document_ids", "project_id")
        return {"document_ids": _uuid_list(a.get("document_ids"), "document_ids"),
                "project_id": _uuid(a.get("project_id"), "project_id")}
    if tool_name == "move_papers_between_folders":
        _only(a, "document_ids", "from_project_id", "to_project_id")
        src, dst = _uuid(a.get("from_project_id"), "from_project_id"), _uuid(a.get("to_project_id"), "to_project_id")
        if src == dst:
            raise ToolActionArgumentError("from_project_id and to_project_id must differ")
        return {"document_ids": _uuid_list(a.get("document_ids"), "document_ids"),
                "from_project_id": src, "to_project_id": dst}
    if tool_name == "create_folder":
        _only(a, "name", "description")
        out = {"name": _name(a.get("name"))}
        if a.get("description") is not None:
            if not isinstance(a["description"], str) or len(a["description"]) > 2000:
                raise ToolActionArgumentError("description must be at most 2000 characters")
            out["description"] = a["description"]
        return out
    if tool_name == "rename_folder":
        _only(a, "project_id", "name")
        return {"project_id": _uuid(a.get("project_id"), "project_id"), "name": _name(a.get("name"))}
    if tool_name == "delete_folder":
        _only(a, "project_id")
        return {"project_id": _uuid(a.get("project_id"), "project_id")}
    if tool_name == "update_document_metadata":
        _only(a, "document_id", "title", "tags")
        out = {"document_id": _uuid(a.get("document_id"), "document_id")}
        if "title" in a:
            out["title"] = _name(a["title"])
        if "tags" in a:
            tags = a["tags"]
            if not isinstance(tags, list) or len(tags) > MAX_TAGS or not all(
                isinstance(t, str) and 0 < len(t) <= MAX_TAG for t in tags):
                raise ToolActionArgumentError("tags must be at most 20 strings of 1-64 characters")
            out["tags"] = tags
        if len(out) == 1:
            raise ToolActionArgumentError("title or tags is required")
        return out
    if tool_name == "ingest_arxiv_papers":
        from src.services.agent.tool_helpers import _reject_invalid_arxiv_ids
        _only(a, "paper_ids")
        ids = a.get("paper_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_PAPER_IDS:
            raise ToolActionArgumentError("paper_ids must contain 1-10 ids")
        bad = _reject_invalid_arxiv_ids(ids)  # check its real signature/return first
        if bad:
            raise ToolActionArgumentError("paper_ids contains invalid arXiv ids")
        return {"paper_ids": ids}
    raise ToolActionArgumentError("tool is not available as an action")
```

**Step 4:** PASS. **Step 5:** Commit `feat(integrations): library action catalogue and validators`.

### Task 3.2: Target resolution and the auto-run path in `request_action`

**Files:**
- Modify: `tool_actions.py` (`request_action`, new `_resolve_target`, `_run_effect` dispatch)
- Modify: `backend/src/schemas/tool_actions.py` (`ActionActor.scopes: frozenset[str]`, `workspace_id`)
- Modify: `backend/src/api/integrations/actions.py` (`_actor` passes scopes; `_WRITE` dependency accepts `tools:write` **or** `library:write` — add `require_integration_context_any(("tools:write", "library:write"))` in `api/integrations/auth.py`)
- Test: `test_tool_actions.py`

**Step 1: Failing tests**

```python
async def test_library_write_grant_auto_runs_save_papers(db) -> None:
    actor = _actor(scopes={"tools:write", "library:write"}, workspace_id=WORKSPACE, project_id=None)
    status = await request_action(db, actor, ToolInvocation(
        tool_name="save_papers_to_folder", invocation_id=uuid4(),
        arguments={"document_ids": [str(DOC)], "project_id": str(PROJECT)}))
    assert status.state == "succeeded"
    assert await db.scalar(select(func.count()).select_from(CollectionDocument).where(
        CollectionDocument.collection_id == PROJECT, CollectionDocument.document_id == DOC,
        CollectionDocument.is_deleted.is_(False))) == 1


async def test_tools_write_only_grant_still_awaits_approval(db) -> None:
    actor = _actor(scopes={"tools:write"}, workspace_id=WORKSPACE, project_id=None)
    status = await request_action(db, actor, _save_invocation())
    assert status.state == "awaiting_approval"


async def test_delete_folder_never_auto_runs(db) -> None:
    actor = _actor(scopes={"tools:write", "library:write"}, workspace_id=WORKSPACE, project_id=None)
    status = await request_action(db, actor, ToolInvocation(
        tool_name="delete_folder", invocation_id=uuid4(), arguments={"project_id": str(PROJECT)}))
    assert status.state == "awaiting_approval"


async def test_target_outside_scope_is_denied(db) -> None:
    actor = _actor(scopes={"tools:write", "library:write"}, workspace_id=WORKSPACE, project_id=None)
    with pytest.raises(IntegrationAccessDenied):
        await request_action(db, actor, ToolInvocation(
            tool_name="rename_folder", invocation_id=uuid4(),
            arguments={"project_id": str(FOREIGN_PROJECT), "name": "x"}))


async def test_move_is_atomic(db, monkeypatch) -> None:
    # make add_documents_to_collection raise after remove succeeded → no row changed
    ...
    assert still_in_from and not_in_to
```

**Step 2:** FAIL.

**Step 3: Implement**

```python
async def _resolve_target(
    db: AsyncSession, actor: ActionActor, tool_name: str, arguments: dict[str, Any]
) -> UUID | None:
    """Collection the action binds to, validated against the grant's scope."""
    context = IntegrationContext(user_id=actor.user_id, organization_id=actor.organization_id,
                                 project_id=actor.project_id, workspace_id=actor.workspace_id,
                                 grant_id=actor.grant_id or uuid4(), scopes=actor.scopes)
    allowed = await authorized_scope(db, context)
    wanted = {UUID(v) for k, v in arguments.items() if k in {"project_id", "from_project_id", "to_project_id"}}
    if actor.project_id is not None:
        if wanted - {actor.project_id}:
            raise IntegrationAccessDenied()
        return actor.project_id
    if tool_name == "create_folder":
        return None  # workspace-level; row.project_id stays NULL, workspace_id set
    if not wanted or not wanted <= allowed:
        raise IntegrationAccessDenied()
    return UUID(arguments.get("project_id") or arguments["from_project_id"])
```

In `request_action`: `arguments = validate_arguments(invocation.tool_name, invocation.arguments)`; `target = await _resolve_target(...)`; row gets `project_id=target, workspace_id=actor.workspace_id`. After insert, if `tool_name in AUTO_RUN_ACTIONS and LIBRARY_SCOPE in actor.scopes`: set `state="approved", approved=True, decided_by=actor.user_id, decided_at=_now()` on the row **before** the commit, commit, then `return await _execute_row(db, row.id) or _status(row)`. `_execute_row` already claims, revalidates authority (`_authority_intact` must accept `LIBRARY_SCOPE` or `REQUIRED_SCOPE`) and commits effect + receipt together.

`_run_effect` dispatch (all `commit=False` / same session, one transaction, `_finish` commits):

```python
    if row.tool_name == "create_project_note": ... (existing)
    if row.tool_name == "save_papers_to_folder":
        out = await collection_service.add_documents_to_collection(db, UUID(a["project_id"]), [UUID(x) for x in a["document_ids"]], user.id)
    elif row.tool_name == "remove_papers_from_folder":
        out = await collection_service.remove_documents_from_collection(...)
    elif row.tool_name == "move_papers_between_folders":
        await collection_service.remove_documents_from_collection(db, UUID(a["from_project_id"]), ids, user.id)
        out = await collection_service.add_documents_to_collection(db, UUID(a["to_project_id"]), ids, user.id)
    elif row.tool_name == "create_folder":
        out = await collection_service.create_collection(db, CollectionCreate(workspace_id=row.workspace_id, name=a["name"], description=a.get("description")), user.id)
    elif row.tool_name == "rename_folder":
        out = await collection_service.update_collection(db, UUID(a["project_id"]), CollectionUpdate(name=a["name"]), user.id)
    elif row.tool_name == "delete_folder":
        out = await collection_service.delete_collection(db, UUID(a["project_id"]), user.id)
    elif row.tool_name == "update_document_metadata":
        out = await <files service update used by PUT /files/{id}>(...)
    elif row.tool_name == "ingest_arxiv_papers":
        out = await _tool_ingest_arxiv({"paper_ids": a["paper_ids"], "project_id": str(row.project_id)}, str(user.id), db, user)
```

**Verify first:** whether `collection_service.*` functions commit internally (`rg -n "commit\(" backend/src/services/threads/collection_service.py`). If they do, add a `commit: bool = True` keyword (pattern already used by `_tool_create_project_note`) so the action path can pass `commit=False`; the HTTP callers keep the default. `None` from a service → `"target not found"`; `PermissionError` → `"insufficient permissions"` (stable strings into `last_error`). Receipt content: `{"ok": True, "tool_name": ..., "project_id": ..., "document_ids": [...]}`; never echo service exceptions.

**Step 4:** Tests → PASS. Mutation checks: (a) remove `wanted <= allowed` → `test_target_outside_scope_is_denied` fails; (b) make `_run_effect` commit between the two halves of move → `test_move_is_atomic` fails. Restore both.

**Step 5:** Commit `feat(integrations): auto-run reversible library actions under library:write`.

### Task 3.3: Review page and `ActionReview` carry generic arguments

**Files:**
- Modify: `backend/src/schemas/tool_actions.py` (`ActionReview.arguments: dict[str, Any]`, `summary: str`; keep `title/content/tags` for notes)
- Modify: `tool_actions.py` (`get_action_for_review`: `summary` from a per-action template, e.g. `"Delete folder “{name}” (documents are kept)"`)
- Modify: `frontend/app/(dashboard)/integrations/actions/[id]/page.tsx`: render `summary`; for notes keep today's title/content block.
- Test: pytest for summary strings; vitest if a component is extracted.
- Regenerate OpenAPI + `api.d.ts`. Commit `feat(integrations): review page summarises library actions`.

### Task 3.4: Bridge `request_action` covers all actions

**Files:**
- Modify: `packages/harness-bridge/src/actions/mcp.ts`, `packages/harness-bridge/src/actions/client.ts` (`requestNote` → generic `request(toolName, invocationId, arguments)`; keep `requestNote` as a thin wrapper), `src/mcp/stdio.ts` (register when `session.actions || session.library`)
- Test: `packages/harness-bridge/test/actions.test.ts`

**Step 1: Failing test**

```ts
test("request_action forwards a library action with only its own fields", async () => {
  const server = await backend(() => ({ code: 200, body: status("succeeded") }));
  try {
    const tool = requestActionTool(new ActionHttpClient(server.origin, credentials));
    const outcome = await tool.call({ action: "save_papers_to_folder", invocation_id: INVOCATION,
      document_ids: [DOC], project_id: PROJECT, user_id: randomUUID() });
    assert.match(outcome.text, /^Done\./);
    assert.deepEqual(JSON.parse(server.requests[0]!.body), {
      tool_name: "save_papers_to_folder", invocation_id: INVOCATION,
      arguments: { document_ids: [DOC], project_id: PROJECT } });
  } finally { await server.close(); }
});
```

**Step 2:** FAIL. **Step 3:** Per-action field table in `mcp.ts`:

```ts
const ACTION_FIELDS: Record<string, readonly string[]> = {
  create_project_note: ["title", "content", "tags"],
  save_papers_to_folder: ["document_ids", "project_id"],
  remove_papers_from_folder: ["document_ids", "project_id"],
  move_papers_between_folders: ["document_ids", "from_project_id", "to_project_id"],
  create_folder: ["name", "description"],
  rename_folder: ["project_id", "name"],
  delete_folder: ["project_id"],
  update_document_metadata: ["document_id", "title", "tags"],
  ingest_arxiv_papers: ["paper_ids"],
};
```

`inputSchema.properties.action.enum = Object.keys(ACTION_FIELDS)`; add the new properties (uuid arrays `maxItems: 20`, `paper_ids` `maxItems: 10`). `call`: pick only `ACTION_FIELDS[action]` keys, local shape checks, forward. `describe()`: generalise wording ("Not created yet" → "Not done yet"; "Created." → "Done."). Update existing test regexes accordingly. Register the tools in `stdio.ts` when `session.actions || session.library`.

**Step 4:** type-check + tests PASS. **Step 5:** Commit `feat(harness-bridge): request_action supports library actions`.

### Task 3.5: Docs + PR

- `docs/engineering/harness-bridge.md`: table of actions × (auto-run | approval), the consent sentence, "move = unlink+link in one transaction".
- `run_local_ci.sh --base origin/develop --frontend`. PR `feat(integrations): library actions with library:write auto-run (Plan 07 S3)`.

---

# Slice 4 — Researcher tools

Branch: `feat/plan07-s4-researchers`. Depends on S1.

### Task 4.1: `find_researchers`

**Files:** `read_tools.py`, `test_read_tools.py`.

**Step 1: Failing test** — monkeypatch `read_tools._tool_search_knowledge_graph` to capture args; assert `entity_types == ["PERSON"]`, `project_id` = target, `limit` clamped ≤ 25; payload reshaped to `{"researchers": [{"entity_id", "name", "paper_count"}]}`; scope `tools:read`.

**Step 2:** FAIL. **Step 3:** `LOCAL_TOOLS["find_researchers"]` schema `{query (required), limit ≤ 25, project_id (selector)}`. Branch calls `_tool_search_knowledge_graph({"query": ..., "entity_types": ["PERSON"], "limit": ..., "project_id": str(target)}, user)`. Read `tools_impl.py:4508-4570` first to match the real argument names and the result shape; map `paper_count` from the entity's document-relationship count if present, else omit the key. Neo4j failure → payload has `error` → `is_error=True` with `"knowledge_graph_unavailable"`. Description states coverage: "authors of papers ingested into the selected project".

**Step 4:** PASS. **Step 5:** Commit `feat(integrations): find_researchers over knowledge-graph PERSON entities`.

### Task 4.2: `get_researcher`

Same pattern over `_tool_explore_entity_neighborhood({"entity_id", "project_id", "depth": 1}, user)`; reshape to `{researcher:{entity_id,name}, papers:[{document_id,title,arxiv_id?}], coauthors:[{entity_id,name}]}` (PERSON neighbours = coauthors, DOCUMENT neighbours = papers; check the relationship type strings in `arxiv_kg_integration.py:278-300`). `source_refs`: `{"document_id"}` for papers. Test: entity from another project refused via the neighborhood call's own project filter (assert `project_id` passed). Commit `feat(integrations): get_researcher read tool`.

### Task 4.3: Docs + PR

Caveat paragraph in `harness-bridge.md`: no affiliations/career history (alphaXiv has them; NOUS has no source). Live proof needs Neo4j up on `rag-dev` → `NOT RUN` until the owner runs it. PR `feat(integrations): researcher read tools (Plan 07 S4)`.

---

# Slice 5 — `discover_papers` composite (eval-gated)

Open only after a Harbor eval in `evals/` (pattern `evals/agent-arxiv-research-flow-v1/`) shows a harness LLM fails to combine `search_arxiv` + `search_documents` + `search_external_database`. If the eval passes with primitives, close this slice as **not needed** in the plan amendment.

Task sketch: `LOCAL_TOOLS["discover_papers"] {query, sources?: ["corpus","arxiv","external"], max_results ≤ 20}`; `asyncio.gather` over the three existing branches; de-duplicate on arXiv id / DOI / document title; order corpus → arXiv recency → external; each hit carries `source`. Tests: fan-out called once per source, dedup, partial failure of one source still returns the others with `warnings`.

---

## Live proof (owner-gated, record in `docs/testing/harness-live-proof.md`)

1. GitOps PR: `NOUS_MCP_ENABLED: "true"` in `infrastructure/helm/knowledge-graph-analytics/values-aws.yaml` `backend.env`.
2. `nous-harness connect --workspace <id> --tools --write --library`, approve in browser (check labels + workspace row).
3. From Claude Code: `list_library` → `search_arxiv` → `get_arxiv_paper_content` (two pages) → `ingest_arxiv_papers` (approve) → `get_document_content` → `retrieve_passages` → `save_papers_to_folder` (no approval) → `move_papers_between_folders` → `find_researchers` → `get_researcher` → `delete_folder` (approval).
4. Each step: record only ids, states, result sizes, `sha256` of the `ToolResult` body and a one-line redacted assertion ("folder X now has N docs"). Never paste raw `ToolResult` bodies, prompts, document excerpts or whole `agent_runs` / `integration_tool_actions` rows into tracked docs (Codex #1881 L1140). Anything not executed stays `NOT RUN`.

## Plan index amendment

When S1 opens, add a dated row to `docs/plans/2026-10-04-harness-plan-amendment.md` (or its successor) pointing at this plan and the design doc. Do not edit older plan files.
