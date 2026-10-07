# Same-project standalone harness continuation — implementation plan (Plan 06)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or superpowers:subagent-driven-development) to implement this plan task-by-task. Every slice is one PR. Push, merge, flag and live-proof steps stay gated on the owner's explicit word.

**Status:** proposal, 2026-10-05. Checked against `origin/develop` `d861a7f66`. Nothing below is implemented. Source of requirements: Linear *Plan (recovered): NOUS harness bridge and artifact workspace* → "Same project local harness planning addendum 5 October 2026" (`fc72434cbed4`), coordinated through *Plan 00 (recovered): implementation index* (`ddbcb3ba0251`) and `docs/plans/2026-10-04-harness-plan-amendment.md`.

**Goal:** A standalone Codex session (not launched by NOUS) binds once to an existing NOUS project **and chat**, reads sources, works locally, publishes chosen files plus a structured handoff back to that same project/chat, and a later browser session or local session continues from it.

**Architecture:** Three small backend additions on the existing integration grant / artifact foundations: (1) project-scoped artifact discovery, (2) a chat (thread) binding carried on the browser consent and therefore on every grant it issues, (3) a versioned, harness-written `IntegrationHandoff` row per thread with expected-parent checks. The bridge reuses its binding across sessions and gets `handoff` CLI/MCP tools. Plan 04's LLM-generated context document is *not* this; it stays deferred.

**Tech Stack:** FastAPI/Pydantic/SQLAlchemy/Alembic, Next.js + TanStack Query, `packages/harness-bridge` (Node 24, MCP SDK, `node:test`).

---

## 0. Inventory (what exists, what is missing)

| Area | On `develop` `d861a7f66` | Gap vs addendum |
| --- | --- | --- |
| Consent → grant | `GrantRequestCreate{project_id, device_id, scopes}`; `exchange_request` (`services/integrations/context.py:434-465`) mints a grant with **no** `thread_id`/`run_id`. Only `mint_integration_grant` (server-internal, NOUS-launched runs) sets them. `validate_binding` already knows how to verify a thread (owner workspace + `source_project_id == project_id`). | Standalone grant cannot be bound to a chat, so standalone publications carry `thread_id=None` and never appear in a chat. |
| Artifact discovery | `list_thread_artifacts` (`services/artifacts/service.py:488`), `GET /api/v1/artifacts/threads/{thread_id}`, `useThreadArtifacts.ts`. No project-level list in backend, UI or MCP. | Journey step 4 ("find files through normal project navigation without knowing a UUID") unprovable. |
| Context / handoff | Nothing. Plan 04 (stale) proposes LLM context docs + triggers + 2 migrations. `context:read` scope reserved for open PR #1784 (selected memories). | No versioned handoff record; no read/update API; no session linkage. |
| Bridge | `connect --tools [--write] [--publish]`, `disconnect`, `mcp`, `mcp install`, `recover-interrupt`; `CredentialStore` local `connection` state; `GrantKeeper` renews 15-min grants; local MCP tools pattern (`LocalTool` in `mcp/server.ts`, artifacts in `artifacts/mcp.ts`). | No chat selection, no binding reuse semantics, no handoff tools, no hooks. |
| Open PRs | #1784 (ic01 migration, `context:read`) and #1788 both `CONFLICTING`. Remediation #1863/#1865-#1869 all MERGED 2026-10-05. | Serial-migration rule: one migration-bearing PR in flight at a time. |
| Flags | `NOUS_MCP_ENABLED`, `ARTIFACTS_ENABLED`, `HARNESS_BRIDGE_ENABLED` default false; none set in `values-aws.yaml`. | Live proof still NOT RUN for everything. |
| Linear issues | No issue for this scope (searched "harness", "artifact", "MCP"). Linear is at its free issue cap; status lives in docs. | Track in Plan 00 index amendment, not new issues. |

## 1. Decisions to confirm before Slice 2 (owner)

| # | Decision | Default used by this plan |
| --- | --- | --- |
| D1 | Explicit vs automatic publication | **Explicit** (addendum's safe baseline). Hooks only call the explicit CLI; nothing auto-uploads. |
| D2 | How a standalone session selects the chat | `nous-harness connect --project <id> --chat <thread_id>`; approval page shows project + chat title. Creating a new chat from the CLI is **out** of v1 (create it in the browser first). |
| D3 | What "context" means in v1 | A harness-written structured **handoff** (goal, decisions, results→artifact version ids, remaining, session linkage). Not an LLM summary. Plan 04 unaffected. |
| D4 | Order vs #1784 | Slice 1 (no migration) now, in parallel with the #1784 rebase. Slice 2 (migration) only after #1784 merges **or** the owner says #1784 waits. |
| D5 | Conflict handling on handoff save | Reject with 409 + latest version; caller merges and retries. Rejected content is returned, not stored. (`ponytail:` ceiling; add a `conflicts` table only if real users hit it.) |

## 2. Slices and order

| Slice | PR | Migration | Depends on |
| --- | --- | --- | --- |
| 1 | Project-scoped artifact discovery (backend route + read tool + project "Files" tab) | none | — |
| 2 | Chat binding on consent → grants → publication | `hb05_grant_request_thread` | #1784 merged (D4) |
| 3 | Versioned handoff record + API + MCP tools + chat card | `hb06_integration_handoffs` | Slice 2 merged |
| 4 | Bridge binding reuse + `handoff` CLI + offline queue | none | Slice 3 merged |
| 5 | Acceptance journey test + runbook Phase 3 + Plan 00 amendment | none | Slices 1-4 merged |

Each PR: one feature, own worktree from fresh `origin/develop`, `scripts/ci/run_local_ci.sh --base origin/develop --frontend` green, regenerated `backend/openapi.json` + `frontend/src/types/generated/api.d.ts` committed with any HTTP change, PR body with evidence table (local / hosted CI / live = NOT RUN).

---

## Slice 1: Project-scoped artifact discovery

**Files:**
- Modify: `backend/src/services/artifacts/service.py` (after `list_thread_artifacts`)
- Modify: `backend/src/api/artifacts.py` (new route)
- Modify: `backend/src/schemas/artifacts.py` (`ProjectArtifactDTO`)
- Modify: `backend/src/services/integrations/read_tools.py` (allowlist `list_project_artifacts`)
- Test: `backend/tests/unit/services/artifacts/test_list_project_artifacts.py`
- Test: `backend/tests/unit/api/test_artifact_project_route.py`
- Modify: `frontend/src/services/artifactService.ts`, `frontend/src/hooks/chat/useThreadArtifacts.ts` (add `useProjectArtifacts`)
- Create: `frontend/src/components/research/ProjectArtifactsTab.tsx`, `__tests__/ProjectArtifactsTab.test.tsx`
- Modify: `frontend/app/(dashboard)/projects/[id]/page.tsx` (one tab entry, no logic)

### Task 1.1 Service

**Step 1: failing test** `backend/tests/unit/services/artifacts/test_list_project_artifacts.py`

```python
@pytest.mark.unit
async def test_lists_latest_version_per_artifact_for_member(db, project_with_two_artifacts):
    rows = await list_project_artifacts(db, user_id=member.id, organization_id=org.id, project_id=project.id)
    assert [r.artifact_id for r in rows] == [a2.id, a1.id]          # newest first
    assert rows[0].current_version.id == a2_v2.id                    # latest only
    assert rows[0].thread_id is None                                 # standalone artifact still listed

@pytest.mark.unit
async def test_foreign_org_and_deleted_ancestors_hidden(db, ...):
    with pytest.raises(ArtifactNotFound):
        await list_project_artifacts(db, user_id=outsider.id, organization_id=other_org.id, project_id=project.id)
    collection.is_deleted = True; await db.flush()
    with pytest.raises(ArtifactNotFound):
        await list_project_artifacts(db, user_id=member.id, organization_id=org.id, project_id=project.id)
```

**Step 2:** `pytest -q backend/tests/unit/services/artifacts/test_list_project_artifacts.py` → FAIL `ImportError: list_project_artifacts`.

**Step 3: implement**

```python
class ProjectArtifactDTO(BaseModel):
    artifact_id: UUID
    title: str
    kind: str
    current_version: ArtifactVersionDTO
    thread_id: UUID | None        # latest reference, if any
    updated_at: datetime

async def list_project_artifacts(
    db, *, user_id: UUID, organization_id: UUID, project_id: UUID, limit: int = 100
) -> list[ProjectArtifactDTO]:
    try:
        await authorized_project(db, user_id, organization_id, project_id)
    except IntegrationAccessDenied as error:
        raise ArtifactNotFound() from error
    # one row per artifact: its current version (Artifact.current_version_id)
    rows = (await db.execute(
        select(Artifact, ArtifactVersion)
        .join(ArtifactVersion, ArtifactVersion.id == Artifact.current_version_id)
        .join(Collection, Collection.id == Artifact.project_id)
        .join(Workspace, Workspace.id == Collection.workspace_id)
        .where(
            Artifact.project_id == project_id,
            Artifact.organization_id == organization_id,
            Artifact.is_deleted.is_(False),
            ArtifactVersion.is_deleted.is_(False),
            Collection.is_deleted.is_(False),
            Workspace.is_deleted.is_(False),
        )
        .order_by(ArtifactVersion.created_at.desc(), Artifact.id)
        .limit(limit)
    )).all()
    ...  # latest live ArtifactReference per artifact for thread_id (one extra query, IN (...))
```

`Artifact.current_version_id` exists (`backend/src/models/artifact.py:31`, nullable) — artifacts with no finalized version are excluded by the inner join, which is correct. `ArtifactVersion.thread_id` (`:57`) also exists, so `thread_id` on the DTO can come straight from the current version; no extra reference query.

**Step 4:** test → PASS. **Step 5:** `git add` the two files; `git commit -m "feat(artifacts): list a project's artifacts"`.

### Task 1.2 Route + read tool

Route (browser auth, same `_identity` pattern as `thread_artifacts`):

```python
@router.get("/projects/{project_id}", response_model=list[ProjectArtifactDTO])
async def project_artifacts(project_id: UUID, user: User = Depends(get_current_user), db=Depends(get_db)):
    user_id, org_id = _identity(user)
    try:
        return await list_project_artifacts(db, user_id=user_id, organization_id=org_id, project_id=project_id)
    except ArtifactError as error:
        raise _http(error) from error
```

Read tool: register `list_project_artifacts` in `read_tools.py` allowlist with JSON schema `{limit?: int}`; identity comes from `IntegrationContext` (never from args); returns `content` rows + `source_refs` with `artifact_id`/`version_id`. Required scope `tools:read`.

Tests (`test_artifact_project_route.py`): 200 member, 404 outsider, 404 deleted collection, and `backend/tests/unit/architecture` still green (router must not commit). Read-tool test: args with `project_id` are rejected (`extra=forbid`).

Commands: `pytest -q backend/tests/unit/api/test_artifact_project_route.py backend/tests/unit/architecture`; `python scripts/ci/generate_openapi.py`; `pnpm --dir frontend generate:api-types`. Commit route+tool+contracts: `feat(artifacts): project artifact route and read tool`.

### Task 1.3 Frontend tab

- `artifactService.listProjectArtifacts(projectId)` → `GET /api/v1/artifacts/projects/{id}`; alias generated shape in `types/api/artifact-contract.ts`.
- `useProjectArtifacts(projectId)` → `useQuery({ queryKey: ['project', projectId, 'artifacts'] })`, `staleTime: 5_000`, no polling.
- `ProjectArtifactsTab`: list rows (title, kind, version `created_at`, SHA-256 short, "Open in chat" link when `thread_id`, Download via existing `fetchVersionBlob`). Empty state text. No Zustand.
- `page.tsx`: add `'files'` to the tab union and render `<ProjectArtifactsTab projectId={id} />`. No other change.

Vitest (`render` from `@/test/test-utils`): renders rows from a mocked service; download button calls `fetchVersionBlob(versionId)`; empty state. Run `pnpm --dir frontend exec vitest run src/components/research/__tests__/ProjectArtifactsTab.test.tsx`, `pnpm --dir frontend type-check`, `pnpm --dir frontend lint:changed` via `run_local_ci.sh --frontend`. Commit `feat(frontend): project Files tab lists artifacts`.

**Slice 1 acceptance:** a version published with `thread_id=None` is visible on the project page and downloadable; its bytes hash equals `sha256` in the DTO (test asserts via `read_version_content`).

---

## Slice 2: Chat binding on consent

**Files:**
- Create: `backend/alembic/versions/hb05_grant_request_thread.py` (down_revision = current single head; verify with `python ../scripts/ci/check_alembic.py` on fresh develop first)
- Modify: `backend/src/models/integration_grant.py` (`IntegrationGrantRequest.thread_id: Mapped[UUID | None]`, FK `threads.id`)
- Modify: `backend/src/schemas/integration_context.py` (`GrantRequestCreate.thread_id: UUID | None = None`; `GrantRequestDTO.thread_id`, `thread_label: str | None`)
- Modify: `backend/src/services/integrations/context.py` (`create_request` validates via existing `validate_binding(thread_id=...)`; `exchange_request` passes `thread_id=request.thread_id`; `request_dto` loads thread title)
- Modify: `frontend/app/(dashboard)/integrations/approve/page.tsx` (show chat label)
- Modify: `packages/harness-bridge/src/cli.ts` (`--chat <threadId>` on `connect`), `connection` state gains `threadId`
- Modify: `frontend/src/hooks/chat/useThreadArtifacts.ts` (pending-poll fix below)
- Test: `backend/tests/unit/services/integrations/test_grant_thread_binding.py`, `packages/harness-bridge/src/cli.test.ts` (extend)

### Task 2.1 Backend binding

**Step 1: failing tests**

```python
async def test_request_with_thread_binds_the_issued_grant(db, user, project, thread_in_project, device):
    req = await create_request(db, user, GrantRequestCreate(project_id=project.id, device_id=device.id,
                                                            scopes={"tools:read", "artifacts:publish"}, thread_id=thread_in_project.id))
    await decide_request(db, user, req.id, approved=True)
    issued = await exchange_request(db, user, req.id)
    ctx = await resolve_integration_context(db, issued.token, required_scope="artifacts:publish")
    assert ctx.thread_id == thread_in_project.id and ctx.run_id is None

@pytest.mark.parametrize("bad", ["other_project_thread", "other_owner_thread", "deleted_thread"])
async def test_foreign_or_dead_thread_is_refused(db, user, project, device, bad, request):
    with pytest.raises(IntegrationAccessDenied):
        await create_request(db, user, GrantRequestCreate(..., thread_id=request.getfixturevalue(bad).id))

async def test_renewal_keeps_thread(db, ...):   # renew_grant already copies thread_id; prove it
async def test_standalone_publication_lands_in_the_bound_chat(db, ...):
    version = await publish_version(db, ctx, request_with_upload)
    rows = await list_thread_artifacts(db, user_id=user.id, organization_id=org.id, thread_id=thread.id)
    assert [r.reference.version_id for r in rows] == [version.id]
    assert rows[0].reference.run_id is None
```

**Step 2:** run → FAIL (`thread_id` unexpected field).

**Step 3: implement.** Migration adds nullable column + index. `create_request`: call `validate_binding(db, user_id=..., organization_id=..., project_id=..., thread_id=payload.thread_id, device_id=...)` (existing checks: live ancestors, `Workspace.owner_id == user`, `Thread.source_project_id == project_id`). `exchange_request`: `_new_grant(..., thread_id=request.thread_id)`. `request_dto`: `thread_label = thread.title` when bound.

**Step 4:** PASS. **Mutation check:** remove the `thread_id=` argument from `validate_binding` in `create_request` → `test_foreign_or_dead_thread_is_refused` must fail; restore. Record in PR body.

**Step 5:** regenerate contracts; commit `feat(integrations): bind a consent request to a chat`.

### Task 2.2 Approval page + bridge flag

- Approval page: render `thread_label` under project label ("Chat: …"); vitest snapshot of the label.
- `cli.ts`: `connect --project <id> --chat <threadId>` → `GrantRequestCreate.thread_id`; persist `threadId` in `connection` state; print binding on success. `node:test`: request body carries `thread_id`; `connect` without `--chat` unchanged.
- `useThreadArtifacts.ts`: `pending` currently means `messageId === null`, which is permanent for standalone rows (no run) → poll forever. Change to `a.reference.messageId === null && a.reference.runId !== null`. Vitest: standalone row does not keep `refetchInterval` on.

Commands: `pnpm --filter @nous/harness-bridge test`, `type-check`; `pnpm --dir frontend exec vitest run src/hooks/chat/__tests__/useThreadArtifacts.test.ts`. Commit `feat(harness-bridge): connect --chat binds the session to a NOUS chat`.

**Slice 2 acceptance:** `connect --chat` → approve in browser → `artifacts_publish` over MCP → file card appears in that chat after reload, and on the project Files tab (Slice 1).

---

## Slice 3: Versioned handoff record

**Files:**
- Create: `backend/alembic/versions/hb06_integration_handoffs.py`
- Create: `backend/src/models/integration_handoff.py`, export in `models/__init__.py`
- Create: `backend/src/schemas/integration_handoff.py`
- Create: `backend/src/services/integrations/handoffs.py`
- Create: `backend/src/api/integrations/handoffs.py` (+ register in `api/integrations/__init__.py`)
- Modify: `backend/src/api/threads/workspace_routes/threads.py` (browser read `GET /{thread_id}/handoff`) — must live in `workspace_routes/` so `test_workspace_boundaries.py` guards it
- Modify: `backend/src/schemas/integration_context.py` (`STANDARD_SCOPES` += `context:write`; `context:read` already present)
- Create: `packages/harness-bridge/src/handoffs/{client,mcp}.ts`; modify `cli.ts` (`mcp` registers tools when grant has scopes), `connect --context` requests `context:read context:write`
- Create: `frontend/src/hooks/chat/useThreadHandoff.ts`, `frontend/src/components/chat/artifact-panel/HandoffCard.tsx` (+ test)
- Tests: `backend/tests/unit/services/integrations/test_handoffs.py`, `backend/tests/unit/api/test_integration_handoff_routes.py`, `packages/harness-bridge/src/handoffs/mcp.test.ts`

### Task 3.1 Model + service

```python
class IntegrationHandoff(BaseModel):
    __tablename__ = "integration_handoffs"
    __table_args__ = (UniqueConstraint("thread_id", "version"), UniqueConstraint("handoff_id"))
    id, organization_id, project_id, thread_id (FK threads.id), version: int,
    handoff_id: UUID            # client-supplied idempotency key
    goal: str (≤ 2000), decisions: list[str] (JSON, ≤ 50×500), remaining: list[str],
    results: list[{"artifact_version_id": UUID, "summary": str}] (JSON, ≤ 50),
    harness_name: str (≤ 64), harness_session_id: str | None (≤ 128),
    grant_id, consent_id, created_by_user_id, created_at
```

```python
class HandoffCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handoff_id: UUID
    expected_parent_version: int | None      # None only for the first handoff
    goal: str; decisions: list[str]; remaining: list[str]; results: list[HandoffResult]
    harness_name: str; harness_session_id: str | None = None

async def read_latest(db, context: IntegrationContext) -> HandoffDTO | None   # requires context.thread_id
async def save(db, context: IntegrationContext, payload: HandoffCreate) -> HandoffDTO
```

`save` rules, each with a test:
1. `context.thread_id is None` → `IntegrationAccessDenied` (`test_unbound_grant_cannot_save`).
2. Existing row with same `handoff_id` → return it unchanged, no insert (`test_replay_returns_same_version`; replay with different body → 409).
3. `expected_parent_version != latest.version` → `HandoffConflict` carrying the latest DTO (`test_stale_parent_conflicts`). Insert uses `version = expected_parent + 1`; the unique constraint turns a race into `IntegrityError` → rollback → re-read → `HandoffConflict` (**mutation check:** drop the `UniqueConstraint` in a test-only metadata copy or patch the where-clause; the concurrent-save test must fail).
4. Every `results[].artifact_version_id` must resolve to a live version whose `Artifact.project_id == context.project_id` and org matches (`test_foreign_version_rejected`).
5. One transaction per call, commit in the service.

Commands: `pytest -q backend/tests/unit/services/integrations/test_handoffs.py`; `(cd backend && python ../scripts/ci/check_alembic.py)`. Commit `feat(integrations): versioned chat handoff record`.

### Task 3.2 Routes + scope

- `GET /api/v1/integrations/handoffs/latest` → `require_scope("context:read")`; `POST /api/v1/integrations/handoffs` → `require_scope("context:write")`; both derive thread from the grant; 409 body = latest DTO; `NOUS_MCP_ENABLED=false` → 503 for POST, GET still answers (matches existing read/kill-switch semantics; check `api/integrations/tools.py` for the exact helper).
- Browser: `GET /api/v2/threads/{thread_id}/handoff` in `workspace_routes/threads.py` via `workspace_access.get_thread(db, user, thread_id)` → latest DTO or 404.
- Tests: scope matrix (read-only grant POST → 403), revoked grant → 401, architecture guard green, oasdiff: additive only.
- Regenerate contracts. Commit `feat(api): handoff routes under integration and workspace scopes`.

### Task 3.3 MCP tools + chat card

- Bridge `handoffs/client.ts` (`getLatest`, `save`) using `GrantKeeper.fetch`; `handoffs/mcp.ts` exports two `LocalTool`s: `get_nous_handoff` (no args) and `save_nous_handoff` (args = `HandoffCreate` minus identity; `handoff_id` generated locally if absent and journaled, see Slice 4). `cli.ts` registers them when the connection has `context:*` scopes.
- `node:test`: tools advertised only with scopes; `save` sends exactly the bound grant header; 409 is surfaced as `isError` with latest version text, not thrown.
- Frontend `HandoffCard` in the artifact panel: goal, decisions, remaining, results linking to versions; "v{n} · {harness_name} · {created_at}". Query key `['thread', threadId, 'handoff']`; invalidated alongside `threadArtifactsKey`.

Commit `feat(harness-bridge): handoff MCP tools and chat handoff card`.

---

## Slice 4: Bridge binding reuse, `handoff` CLI, offline queue

**Files:** `packages/harness-bridge/src/cli.ts`, `src/handoffs/queue.ts` (new), `src/journal.ts` (reuse SQLite journal if it has a generic table; otherwise a JSON file under the state dir), `README.md`, `docs/engineering/harness-bridge.md`; tests `src/handoffs/queue.test.ts`, `src/cli.test.ts`.

- `nous-harness status`: prints project/chat/device/grant expiry/pending handoffs.
- `connect` with the same `--project/--chat` as the stored binding and a live grant → no new consent; prints "reusing binding". Different chat → new consent (explicit). Test both.
- `nous-harness handoff show` → `get_nous_handoff` over HTTP; `nous-harness handoff save --file handoff.json [--parent N]` → journals `{handoff_id, body}` first, then POSTs; on network failure leaves it `pending`; `nous-harness handoff flush` retries pending with the same `handoff_id` (server dedupes). A 409 marks the entry `conflicted` and prints the latest version; never auto-merges.
- Hooks: document the two commands as the hook targets. Do **not** wire Codex hooks in this slice; per-harness hook support is unverified (addendum). `ponytail:` manual `flush`, no daemon; add a background retry only if pending entries actually accumulate in practice.

Tests: offline save → pending → flush succeeds once → second flush is a no-op (`node:test` with a fake server counting POSTs by `handoff_id`); conflict path.

Commit `feat(harness-bridge): reuse chat binding and queue handoff saves`.

---

## Slice 5: Acceptance journey, runbook, index amendment

**Files:** `backend/tests/integration/test_same_project_continuation.py` (PostgreSQL, marker `integration`), `docs/testing/harness-live-proof.md` (Phase 3 section), `docs/plans/2026-10-04-harness-plan-amendment.md` (dated pointer to this plan), Linear Plan 00 index (dated pointer; owner-posted).

Journey test (mirrors addendum steps 1-6, all in-process, no model):
1. consent A bound to project P / chat T → grant A; read tools return P's documents.
2. publish `analysis.py`, `data.csv`, `plot.png` with digests; save handoff v1 (results → 3 version ids).
3. fresh browser identity: `GET /artifacts/projects/P` lists all three without any UUID known up front; `read_version_content` bytes hash == DTO sha256; `GET /threads/T/handoff` == v1.
4. consent B (new device/session) same P/T → grant B; `get latest` == v1; publish `plot.png` v2 → first version unchanged; handoff v2 with `expected_parent_version=1`.
5. denials: grant for project Q cannot read T's handoff (403); revoked grant A → 401 on save; replay of v2's `handoff_id` → same row; concurrent v3 saves with parent 2 → exactly one wins, other 409; wrong-project artifact version in results → 422.

Runbook Phase 3 rows (all NOT RUN until the owner runs them on dev): flags `ARTIFACTS_ENABLED`, `NOUS_MCP_ENABLED` true; `connect --tools --publish --context --chat`; Codex session; publish; close; fresh browser; second session; record run/grant ids, handoff versions, checksums in `docs/testing/evidence/`.

Commit `test(harness): same-project continuation journey and runbook phase 3`.

---

## Gates per PR

```sh
scripts/ci/run_local_ci.sh --base origin/develop --frontend          # blocking gates
python scripts/ci/generate_openapi.py --check                        # when HTTP changed
(cd backend && python ../scripts/ci/check_alembic.py)                # Slices 2, 3
pnpm --filter @nous/harness-bridge test && pnpm --filter @nous/harness-bridge type-check
pytest -q backend/tests/unit/architecture backend/tests/unit/api
```

Report hosted CI and live proof separately. A skipped live test is NOT RUN.

## Audit corrections — 2026-10-05 (4 read-only auditors against `d861a7f66`)

Slice 1 is implemented on branch `feat/project-artifact-discovery` (worktree `.worktrees/plan06-slice1`, 4 commits `c0c3d5763..dded99e61`, local CI exit 0, not pushed). Deviations from the text above: schema file is `backend/src/schemas/artifact.py` (singular); DTO `kind` = version `mime_type`, `updated_at` = current version `created_at`; `thread_id` is nulled unless Thread and Conversation are live and the conversation sits in the project's workspace (review finding, mutation-checked). Known low: read tool returns up to 100 rows but `_source_refs` caps at `MAX_RESULTS=50`.

Every behavioural premise for Slices 2–5 holds. Corrections the slices must absorb before implementation:

**Slice 2**
- `exchange_request` is at `context.py:445-474` (not 434-465). `owned_request` (`:389-395`) calls `validate_binding` without `thread_id`; pass `thread_id=request.thread_id` there so `decide_request`/`request_dto`/`exchange_request` re-validate a thread deleted or moved between create and approve.
- `Thread.title` is nullable (`models/thread.py:52`): `thread_label = thread.title or "Untitled chat"`, plus a NULL-title test.
- Bridge: `parseArgs` is strict → add `chat: { type: 'string' }`, validate with `uuid()`, require `--project` with it, add `threadId?: string` to `LocalState` (`cli.ts:22-30`) and write/clear it on every connect; document in help.
- Test paths: bridge tests live in `packages/harness-bridge/test/*.test.ts` (no `src/cli.test.ts`); connect body is inspected in `test/mcp.test.ts:258-307`. Frontend hook test is `src/hooks/chat/__tests__/useThreadArtifacts.test.tsx` (`.tsx`). Approval page has no test yet.
- Alembic head today `d4a6c8e0f2b3`; re-read on fresh develop before writing `hb05` (#1784 would move it).

**Slice 3**
- Scope collision with open #1784: it owns `context:read`, `connect --context`, `McpSession.context`, `read_selected_context`. Use **`handoff:read` / `handoff:write`** and `connect --handoff` (requires `--tools`); do not redefine `--context`.
- No `require_scope` helper exists; use `require_integration_context(<scope>)` from `src.api.integrations.auth`. Revoked/expired/wrong-scope grants → **403**, not 401 (401 only for a bad CLI JWT).
- Flag gating: add a `_require_enabled`-style helper for POST only (GET ungated, matching `actions.py` status reads).
- Idempotency key must be `UniqueConstraint('thread_id', 'handoff_id')` looked up with `organization_id` + `thread_id`; a global `handoff_id` uniqueness lets tenants probe each other. Replay with a different body → 409 inside that scope; add cross-thread/cross-org replay tests. Define `HandoffInvalid` → 422 for foreign artifact versions.
- Browser route: `@standalone_router.get("/threads/{thread_id}/handoff")` in `workspace_routes/threads.py` (full path `/api/v2/threads/{id}/handoff`), via `workspace_access.get_thread(db, thread_id, user.id, include_messages=False)` → 404 on None, then a `read_latest_for_thread(db, organization_id, thread_id)` service call. Also update `backend/tests/unit/api/test_workspace_route_contract.py` (standalone 19→20, total 48→49).
- Model: repo `BaseModel` already supplies `is_deleted`/`created_at`/`updated_at`/`deleted_at`; migration must still create them; follow the `id` type-ignore pattern; name the UniqueConstraints.
- 409 transport: bridge `throwForStatus` (`mcp/client.ts:58-71`) drops non-string detail → `handoffs/client.ts` must intercept 409 itself; declare `responses={409: {"model": HandoffDTO}}` on the route.
- Tool registration lives in `mcp/stdio.ts` + `McpSession` (`mcp/client.ts`) + `mcp/config.ts` argv + `cli.ts` `mcpSession()`/`parseArgs`/`connect`/help — all four files in the Modify list. Tests under `packages/harness-bridge/test/handoffs.test.ts`.

**Slice 4**
- `journal.ts` is not reusable: its constructor mutates live command state. Queue = one file per entry (`handoff-<handoff_id>`) via `CredentialStore.writeLocal/readLocal/removeLocal` plus a small `listLocal(prefix)`; record `thread_id` per entry so a different-chat reconnect cannot flush stale entries.
- Queue states: `pending` (network/timeout/5xx/429/401/GrantExpired — print reconnect hint, non-zero exit), `done` (2xx → delete), `conflicted` (409, store latest DTO), `rejected` (403/422, terminal). `handoffs/client.ts` adds `signal: AbortSignal.timeout(20_000)` and `redirect: 'error'` (GrantKeeper.fetch adds neither).
- Reuse predicate: stored connection exists AND same `apiBase(apiUrl)` AND same `projectId` AND same `threadId` (both absent = equal) AND stored scopes ⊇ requested scopes AND a server liveness probe (`keeper.current(0)` forced renew) succeeds. Short-circuit before `/cli-auth/start` (`cli.ts:109`); decide what happens to the superseded credential handle on a different-chat reconnect (today `connect` always does full login + consent and leaks the old handle).
- `save_nous_handoff` (MCP) and `handoff save` (CLI) share one `queue.ts`.
- Tests at `packages/harness-bridge/test/handoff-queue.test.ts` / `test/connect-reuse.test.ts`, inline `node:http` fake server (no shared helper exists). Local Node is 22.23.1 vs engines 24.x.

**Slice 5**
- No shared PG fixture: `pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres, pytest.mark.asyncio]`, read `ORCHESTRATION_TEST_DATABASE_URL`, skip when unset (hosted CI → NOT RUN; local throwaway-PG run is the evidence), hand-roll the schema from an explicit model list.
- Build an in-process FastAPI app including the artifacts router, integrations router and the threads `standalone_router`; override `get_db`, `get_current_user` (browser), `get_current_user_token` with `is_cli=True` for grant routes; monkeypatch `settings.ARTIFACTS_ENABLED` / `NOUS_MCP_ENABLED` True (else 503). Integration conftest auto-mocks Redis, so CLI-bearer revocation (401) cannot be proven there.
- Storage: monkeypatch `service.get_artifact_storage` → `MemoryArtifactStorage`; `_reserve/_publish/_published` helpers live only in `test_publication.py` — move to a shared module. `read_version_content` returns a 3-tuple (hash at index 0). v2 publish needs `artifact_id` + `expected_parent_version_id` + fresh `publication_id`/reservation.
- Status expectations: revoked grant → 403; "project Q grant cannot read T's handoff" = unbound grant → 403 on GET/POST; outsider on browser route → 404; foreign version → 422 (`HandoffInvalid`). Use `list_project_documents` for the step-1 read (no FTS seeding).
- Mutation verification for the concurrent-v3 and replay assertions is mandatory (drop the UniqueConstraint / expected-parent check, record the failure) using two sessions.
- Runbook edits are larger than "Phase 3 rows": Gate 0a regex/count/head (`hb05`, `hb06`), new Gate 0 row for the journey, rewrite the P2-1 known-gap paragraph, flags table, Gate 1 commit list; evidence bundle path `docs/testing/evidence/harness-live-proof-YYYYMMDD/README.md`, created only when run. Commit this plan file before any amendment links to it.

## Out of scope (deliberately)

LLM-generated context documents and cross-chat search (Plan 04); NOUS workflows/tracing (Plan 05); artifact tabs/editing/preview/sharing (Plan 03 T3-T7); automatic publication; Codex hook wiring; creating chats from the CLI; a second harness adapter; mirroring the filesystem.
