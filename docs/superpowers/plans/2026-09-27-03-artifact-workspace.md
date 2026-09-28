# Artifact Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish durable, versioned outputs from NOUS and local harnesses into scoped chat cards and editable, shareable side-panel tabs.

**Architecture:** Private object storage holds immutable bytes; SQL owns versions, publication receipts, references, and an independent lifecycle outbox. TanStack Query owns artifact data while the existing panel store owns tab selection. Live HTML executes in an opaque-origin sandbox and authenticated sharing pins a version.

**Tech Stack:** FastAPI, SQLAlchemy/Alembic, existing storage helpers, Node 24, `pnpm@10.18.2`, React, TanStack Query, Zustand, Vitest, pytest, Playwright.

**Spec:** [Harness bridge and artifacts design](../../plans/2026-09-27-harness-bridge-and-artifacts.md), sections 6, 8, and 9; consume the integration contracts from [Plan 01](2026-09-27-01-local-codex-bridge.md) and [Plan 02](2026-09-27-02-nous-capabilities.md).

**Record:** Proposed implementation; 2026-09-27; inspected source `16f824197f85436c84558769e72936e3c1decf2b`. Planning only.

## Global Constraints

- “Keep generated artifacts separate from knowledge documents.”
- “Returning an artifact URL must not bypass those checks.”
- “Carry references, not bytes or long-lived URLs, within the existing event limit.” The current limit is 16 KiB.
- “Use TanStack Query for artifact lists/metadata/content; never duplicate that server state in Zustand.”
- “Saving an artifact does not silently edit the original local file.”
- “Begin sharing with authenticated users and an exact version.”
- Node 24, pnpm 10.18.2, Ruff 0.15.15, Black 26.5.1, isort 5.13.2; regenerate HTTP schemas and frontend types together.
- Proposed implementation defaults, not earlier spec requirements: 10 MiB/file, 2 MiB editable UTF-8 text, 100 MiB/project including reservations; 15-minute upload expiry; no automatic version deletion; 2-second visible-thread polling; 200 CSV rows/50 columns; JSON depth 20; seven-day share expiry maximum.
- Proposed local publisher support: macOS/Linux, Python >=3.11 stdlib helper for descriptor-relative traversal. Probe Python and helper availability before enabling publication; Windows remains unsupported until equivalent handle-based validation exists.

## Review Focus

1. Closed/failed producer runs and delayed uploads still yield saved outputs without reopening the run (Tasks 1, 7).
2. Directory replacement, symlink traversal, hardlinks, and concurrent file writes never publish unintended bytes (Task 2).
3. Account/thread switches while requests resolve cannot expose previous-scope content or steal pinned focus (Task 3).
4. Concurrent edits preserve the local buffer and remote version; retries cannot duplicate versions (Tasks 1, 4).
5. Malicious HTML and revoked shares cannot obtain ambient authority or change shared snapshots (Tasks 5, 6).

---

## File structure and shared interfaces

All **Create** paths are proposed. Verified extension points: `ArtifactPanel`, `useArtifactPanelStore`, `AuiAssistantMessage`, `append_event`, `RunEventType`, `DraftGenerationService._generate_draft_async`, `S3StorageHelper`, and `StorageHelper`. Preserve existing note/draft/document panel behavior.

- `backend/src/{models,schemas}/artifact.py`: persistence/schema definitions; `services/artifacts/{service,access,storage,lifecycle,sharing}.py`: narrowly separated responsibilities; `api/artifacts.py`: authenticated transport.
- `packages/harness-bridge/src/artifacts/`: local snapshot, upload client, MCP registration; no terminal scraping.
- `frontend/src/services/artifactService.ts` and `types/api/artifact-contract.ts`: generated HTTP adapter; `components/chat/artifact-panel/`: Query hooks, cards, tabs, previews/editor.
- `tests/e2e/tests/artifacts/`: persisted-byte and browser-isolation acceptance.

Define in `backend/src/schemas/artifact.py`:

```python
class ArtifactProvenance(BaseModel):
    producer: Literal["harness", "nous", "user"]
    native_item_id: str | None = None
    source_ids: list[UUID] = Field(default_factory=list, max_length=50)
    command: str | None = Field(default=None, max_length=2000)
    code_revision: str | None = Field(default=None, max_length=128)

class ReserveArtifactUploadRequest(BaseModel):
    publication_id: UUID
    byte_size: int
    mime_type: str
    sha256: str

class PublishVersionRequest(BaseModel):
    publication_id: UUID
    upload_id: UUID
    title: str
    provenance: ArtifactProvenance
    artifact_id: UUID | None = None
    expected_parent_version_id: UUID | None = None
```

All schemas forbid extra fields. `ArtifactReferenceDTO(artifact_id:UUID,version_id:UUID,run_id:UUID|None,thread_id:UUID|None,message_id:UUID|None)` and `ThreadArtifactDTO(version:ArtifactVersionDTO,reference:ArtifactReferenceDTO)` provide per-message association. `ArtifactUploadDTO(upload_id:UUID,expires_at:datetime)`; `ArtifactVersionDTO(artifact_id:UUID,version_id:UUID,parent_version_id:UUID|None,title:str,mime_type:str,byte_size:int,sha256:str,created_at:datetime,provenance:ArtifactProvenance)` expose no object key. Identity/scope never comes from tool arguments. Plan 01's `IntegrationContext` supplies actor, organization, project, optional thread/run, and grant. Integration routes compare JWT identity with `resolve_integration_context(..., required_scope="artifacts:publish")`. Browser edits/sharing use Plan 01's `require_interactive_user` plus `authorize_artifact`; a CLI token cannot gain these rights by omitting its integration header. Reads recheck current principal access; no bridge device is required. Source IDs are permission-checked; unavailable provenance stays null and is displayed as unknown. Label harness-supplied commands/revisions as reported; only server-observed identities/digests are verified. All Python service functions listed below are async and accept `db:AsyncSession`.

### Task 1: Private publication and independent lifecycle

**Files:** Create `backend/src/models/artifact.py`, `backend/src/schemas/artifact.py`, `backend/src/services/artifacts/{__init__,access,storage,service,lifecycle}.py`, `backend/src/api/artifacts.py`, `backend/src/tasks/artifact_tasks.py`, `backend/alembic/versions/20260927_artifact_workspace.py`, `backend/tests/unit/services/artifacts/{conftest,test_publication}.py`, `backend/tests/integration/test_artifact_lifecycle.py`. Modify `backend/src/models/__init__.py`, `backend/src/main.py`, `backend/src/core/config.py`, `backend/src/tasks/celery_app.py`, `backend/src/services/agent/{run_event_types,run_event_store}.py`, `backend/tests/unit/services/agent/{test_run_event_types,test_run_event_store}.py`; regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

**Interfaces:** Consumes `IntegrationContext`. Produces `reserve_upload(db,context:IntegrationContext,request:ReserveArtifactUploadRequest)->ArtifactUploadDTO`, `store_upload(db,context:IntegrationContext,upload_id:UUID,content:bytes)->None`, `publish_version(db:AsyncSession,context:IntegrationContext,request:PublishVersionRequest)->ArtifactVersionDTO`, `authorize_artifact(db,*,user_id:UUID,organization_id:UUID,artifact_id:UUID,action:Literal["read","edit","share"])->Artifact`, `list_thread_artifacts(db,*,user_id:UUID,organization_id:UUID,thread_id:UUID)->list[ThreadArtifactDTO]`, and `drain_artifact_outbox(db,*,limit:int=100)->int`. Models: `Artifact` stores owner/org/project/title/current_version_id/deleted_at; immutable `ArtifactVersion` stores DTO fields plus private storage key and producer run/thread/item; `ArtifactUpload` stores grant/publication/hash/size/reservation/expiry; `ArtifactReference` stores exact version/run/thread/message; `ArtifactLifecycleOutbox` stores unique version/event identity and delivery status. Include created/updated timestamps and foreign keys, with no delete cascade to committed bytes. Synchronous Celery wrappers `drain_artifacts()->int` and `sweep_artifact_uploads()->int` call async services via existing `run_async`.

- [ ] **Step 1: Write failing tests.** Proposed fixtures provide two actors, a private project, completed run, real SQL session, and isolated storage; `request` references uploaded `b"report\n"`.

```python
async def test_finalize_is_idempotent_after_terminal(db, context, request):
    first = await publish_version(db, context, request)
    second = await publish_version(db, context, request)
    assert first.version_id == second.version_id
    assert first.sha256 == hashlib.sha256(b"report\n").hexdigest()
    assert len(await list_thread_artifacts(db, user_id=context.user_id,
        organization_id=context.organization_id, thread_id=context.thread_id)) == 1
```

Add named route cases in `test_publication.py`: `test_changed_replay` asserts `status_code == 409`; `test_digest_mismatch` asserts `status_code == 422`; parameterized `test_publication_denials(case,expected)` covers `("oversize",413)`, `("quota",413)`, `("expired_grant",403)`, `("revoked_grant",403)`, `("foreign_org",404)`, `("deleted_ancestor",404)` and asserts `response.status_code == expected`. `test_missing_blob` asserts `status_code == 503` and unchanged `current_version_id`. Fixtures prepare each concrete condition before calling the route. PostgreSQL test races finalize, terminal append, and duplicate finalize: one version/receipt, no post-terminal event, one lifecycle row.

- [ ] **Step 2:** Run `pytest -q backend/tests/unit/services/artifacts/test_publication.py backend/tests/integration/test_artifact_lifecycle.py`; expect missing artifact imports, then behavior failures until implemented.
- [ ] **Step 3:** Implement the interfaces and routes: POST `/api/v1/artifacts/uploads`, PUT `/uploads/{upload_id}/content`, POST `/versions`, GET `/threads/{thread_id}`, GET `/{artifact_id}/versions`, GET `/versions/{version_id}/content`. Under `/api/v1/artifacts`, stream-limit transport before materializing bytes. Validate MIME signatures/UTF-8; permit Markdown/plain/code/CSV/JSON, PNG/JPEG/PDF/HTML; other bytes download-only as octet-stream. Private `artifacts/{org}/{artifact}/{version}` keys reuse configured local/S3/Supabase storage; never CDN/public URLs. Reserve quota under project lock; verify bytes/digest before metadata commit. Unique `(grant_id,publication_id)` plus payload hash enforces replay. SQL transaction commits version, current pointer, durable run/thread/message reference and lifecycle outbox together; abandoned reservations/object uploads are swept by expiry. Services own transaction boundaries; routes delegate. Read/edit/share authorization requires same organization, current project membership or ownership, and live ancestors; public workspace visibility alone grants no artifact access. Sharing adds only Task 6's explicitly named audience. Use a new migration based on the execution-time current Alembic head; never rewrite a migration already applied by an earlier task.
- [ ] **Step 4:** Implement `artifact.created`/`artifact.version_created` ID-only run announcements through outbox delivery. In `append_event`, acquire the same run-row lock for artifact and terminal event types; test this intentionally narrower change to the existing non-locking strategy against PostgreSQL. A closed ledger suppresses only its announcement. Lifecycle rows remain queryable after closure; duplicate delivery uses outbox identity and same-transaction delivery receipt. Thread queries resolve delayed assistant-message association from durable run identity without retaining event history. Register the two Celery wrappers, routes and beat schedules on existing `agent_runs` queue: drain every 2 seconds, sweep every 60. Claim lifecycle rows with `FOR UPDATE SKIP LOCKED`; sweep only expired uncommitted uploads. Test worker restart, duplicate drains and expired-reservation quota release. Create all four default-false flags listed in Task 7 now; reads/reconciliation remain available when publication is disabled.
- [ ] **Step 5:** Rerun Step 2; expect PASS. Remove each uniqueness/race guard temporarily, observe its named test fail, restore and rerun. Regenerate API contracts using `python scripts/ci/generate_openapi.py` and `pnpm --dir frontend generate:api-types`.
- [ ] **Step 6:** Stage exactly this task's Files; commit `feat: persist scoped artifact versions and lifecycle receipts`.

### Task 2: Publish validated local snapshots

**Files:** Create `packages/harness-bridge/src/artifacts/{contracts,publisher,mcp}.ts`, `packages/harness-bridge/src/artifacts/snapshot_file.py`, `packages/harness-bridge/src/artifacts/__tests__/publisher.test.ts`, `packages/harness-bridge/tests/test_snapshot_file.py`. Modify Plan 02's proposed `packages/harness-bridge/src/mcp/stdio.ts` composition function `runStdioMcp` to register artifact tools after `createNousMcpServer`.

**Interfaces:** `PublishFileInput={relativePath:string;title:string;publicationId:string;artifactId?:string;expectedParentVersionId?:string}`; `ArtifactApiClient` defines `reserve(request:ReserveArtifactUploadRequest):Promise<ArtifactUploadDTO>`, `upload(id:string,bytes:Uint8Array):Promise<void>`, `finalize(request:PublishVersionRequest):Promise<ArtifactVersionDTO>`. Produce `publishFile(input:PublishFileInput):Promise<ArtifactVersionDTO>` as the private closure of `createArtifactPublisher(client:ArtifactApiClient, root:GrantedOutputRoot):ArtifactPublisher`; `GrantedOutputRoot={path:string;device:number;inode:number}` is injected from the local binding, never MCP arguments; `registerArtifactTools(server:McpServer,publisher:ArtifactPublisher):void`, where `ArtifactPublisher.publish(input:PublishFileInput):Promise<ArtifactVersionDTO>`. HTTP request/DTO aliases match Task 1 exactly, converting UUID/datetime to strings. MCP input is `{relative_path,title,publication_id,artifact_id?,expected_parent_version_id?}` and maps explicitly to the camelCase local input.

- [ ] **Step 1: Write failures.** The test publisher fixture injects a pinned root inode and fake upload client.

```ts
import test from 'node:test';
import assert from 'node:assert/strict';
test('rejects escaping paths before upload', async () => {
  await assert.rejects(publisher.publish({relativePath:'../secret',title:'x',publicationId:id}),
    {code:'unsafe_path'});
  assert.equal(client.reserve.mock.callCount(), 0);
});
```

Python `test_rejects_unsafe_file` subtests final/ancestor symlinks, swapped directories, hardlinks, FIFO/device files and writes during read with `self.assertRaises(UnsafeArtifactPath)`; `test_stable_snapshot` asserts `sha256(result).hexdigest() == expected_digest`. Node `test('finalize retry keeps version')` asserts `first.version_id === second.version_id` using `assert.equal`.

- [ ] **Step 2:** Run `pnpm --filter @nous/harness-bridge test -- src/artifacts/__tests__/publisher.test.ts` and `python3 -m unittest discover -s packages/harness-bridge/tests -p 'test_snapshot_file.py'`; expect missing modules.
- [ ] **Step 3:** Implement `read_snapshot(root:str,relative_path:str,expected_device:int,expected_inode:int)->bytes` in the helper using component-wise `dir_fd`/`O_NOFOLLOW`, pinned root identity, regular-file/single-link checks, byte cap and before/after `fstat`. Invoke the bundled helper via argument-array subprocess with no shell; stdout contains bounded bytes only. Node computes digest and sequences reserve/upload/finalize. Expose `artifacts_publish` using the explicit MCP-to-local mapping above; inject root/provenance/context, require `artifacts:publish`, persist publication ID for retry. No auto-publication from filenames or directory scans.
- [ ] **Step 4:** Rerun Step 2; expect PASS, including an adversarial path-swap loop and absent Python probe. Mutation-check descriptor validation.
- [ ] **Step 5:** Stage exactly Task 2 Files; commit `feat: publish verified local artifact snapshots`.

### Task 3: Scoped cards and tabs

**Files:** Create `frontend/src/types/api/artifact-contract.ts`, `frontend/src/services/artifactService.ts`, `frontend/src/components/chat/artifact-panel/{useGeneratedArtifacts,GeneratedArtifactCards,ArtifactTabs,ArtifactPreview}.tsx`, `frontend/src/components/chat/artifact-panel/__tests__/GeneratedArtifacts.test.tsx`. Modify existing `backend/src/api/agent/streaming.py`, `frontend/src/services/agentChatService.ts`, `packages/chat-runtime/types.ts`, `frontend/src/store/artifactPanelStore.ts`, `frontend/src/store/__tests__/artifactPanelStore.test.ts`, `frontend/src/components/chat/artifact-panel/ArtifactPanel.tsx`, `frontend/src/components/chat/aui/AuiMessage.tsx`.

**Interfaces:** Produce `ArtifactScope={userId:string;organizationId:string;workspaceId:string;projectId:string;threadId:string}`, `useGeneratedArtifacts(scope:ArtifactScope):UseQueryResult<ThreadArtifact[]>`, `GeneratedArtifactCards({scope,messageId?}):ReactElement`, `ArtifactTabs():ReactElement`; store adds `setScope(scope:ArtifactScope|null):void`, `openVersion(artifactId:string,versionId:string):void`, `closeTab(versionId:string):void`. Domain `ArtifactVersion`, `ArtifactReference`, and `ThreadArtifact` alias their Task 1 DTOs through the adapter; the thread endpoint returns `ThreadArtifactDTO[]`. Store tab IDs are version IDs; `activeVersionId:string|null` selects one.

- [ ] **Step 1: Write failures.**

```ts
it('clears version tabs on account switch', () => {
  useArtifactPanelStore.getState().setScope(scopeA);
  useArtifactPanelStore.getState().openVersion('artifact-a', 'version-a');
  useArtifactPanelStore.getState().setScope({...scopeA,userId:'other-user'});
  expect(useArtifactPanelStore.getState().tabs).toEqual([]);
});
```

`it('ignores old account response')` delays A, switches to B, resolves A and asserts `expect(screen.queryByText('private-a.md')).toBeNull()`. `it('keeps pinned selection')` adds a new card and asserts `expect(useArtifactPanelStore.getState().activeVersionId).toBe('version-a')`.

- [ ] **Step 2:** Run `pnpm --dir frontend test src/components/chat/artifact-panel/__tests__/GeneratedArtifacts.test.tsx src/store/__tests__/artifactPanelStore.test.ts`; expect missing component/method failures.
- [ ] **Step 3:** Implement interfaces with scope-prefixed Query keys, cancellation/account cache removal, visible-thread polling beyond terminal state, and immediate invalidation on announcements. Extend Plan 01's SSE projection and shared runtime callback with `onArtifactVersion(ref:{artifactId:string;versionId:string}):void`; update event-contract fixtures together, carrying IDs only and leaving artifact metadata in Query. Cards require persisted DTOs; unassociated run outputs appear in the thread output shelf until a message exists. Store contains IDs/order/selection/pinning only. Extend existing panel union with generated version references; preserve document/note/draft/citation entry points, context rail, mobile sheet and focus restoration. Opening/closing tabs is keyboard operable with accessible names. Create the initial read-only `ArtifactPreview({version:ArtifactVersion}):ReactElement` now: escaped Markdown/plain/code and authenticated PNG/JPEG, with download/source fallback for every other type, including HTML. Test persisted preview content and hash-verified download in this phase. Task 4 extends this component rather than recreating it.
- [ ] **Step 4:** Rerun Step 2; expect PASS. Mutation-check scope key and stale-response protection.
- [ ] **Step 5:** Stage exactly Task 3 Files; commit `feat: show scoped generated artifact cards and tabs`.

### Task 4: Editing and bounded static previews

**Files:** Create `backend/src/services/artifacts/editing.py`, `backend/tests/unit/services/artifacts/test_editing.py`, `frontend/src/components/chat/artifact-panel/ArtifactEditor.tsx`, `frontend/src/components/chat/artifact-panel/__tests__/ArtifactEditor.test.tsx`. Modify Task 1 router/schema, Task 3 service/panel/`ArtifactPreview.tsx`; regenerate both HTTP outputs.

**Interfaces:** `edit_version(db,*,user_id:UUID,organization_id:UUID,artifact_id:UUID,expected_parent_version_id:UUID,publication_id:UUID,text:str)->ArtifactVersionDTO`; `ArtifactEditor({version:ArtifactVersion}):ReactElement`; `ArtifactPreview({version:ArtifactVersion}):ReactElement`.

- [ ] **Step 1: Write failures.**

```python
async def test_stale_parent_preserves_current(db, edit_args, newer):
    with pytest.raises(ArtifactConflict) as error:
        await edit_version(db, **edit_args)
    assert error.value.current_version_id == newer.version_id
```

`it('preserves a conflicting edit')` asserts `expect(editor).toHaveValue('local changes')` and `expect(screen.getByText('A newer version exists')).toBeVisible()` after 409. `test_old_download_immutable` asserts `sha256(downloaded).hexdigest() == old.sha256`.

- [ ] **Step 2:** Run `pytest -q backend/tests/unit/services/artifacts/test_editing.py` and `pnpm --dir frontend test src/components/chat/artifact-panel/__tests__/ArtifactEditor.test.tsx`; expect missing editor/service.
- [ ] **Step 3:** Add POST `/{artifact_id}/edits` with row-locked expected-parent comparison, `require_interactive_user`, and artifact edit authorization. Reuse upload/version internals with a server-derived edit context and durable receipt; never require a bridge. Preview escaped Markdown/code, bounded CSV/JSON, authenticated PNG/JPEG/PDF blobs and metadata/download fallback. Render HTML as source until Task 5; show version/provenance/history and preserve source-draft/note semantics. Conflict offers reload or copying local text; never overwrites either version or writes local workspace files.
- [ ] **Step 4:** Rerun Step 2; expect PASS. `test_invalid_edit(case,expected)` parameterizes malformed UTF-8 with 422 and 2 MiB+1 with 413; assert `status_code == expected`. `it('bounds untrusted previews')` asserts overflow displays download fallback, CSV `=SUM(A1)` stays literal text, and spoofed HTML never creates a script element. `test_missing_object` asserts `status_code == 503`.
- [ ] **Step 5:** Stage exactly Task 4 Files; commit `feat: edit artifact versions with conflict-safe previews`.

### Task 5: Isolated HTML tools

**Files:** Create `frontend/src/components/chat/artifact-panel/{InteractiveArtifactPreview,sandboxDocument}.tsx`, `frontend/src/components/chat/artifact-panel/__tests__/InteractiveArtifactPreview.test.tsx`, `tests/e2e/tests/artifacts/preview-isolation.spec.ts`. Modify `ArtifactPreview.tsx` and `backend/src/core/config.py`.

**Interfaces:** `buildSandboxDocument(html:string,nonce:string):string`; `InteractiveArtifactPreview({version:ArtifactVersion}):ReactElement`. No filesystem/API broker exists in this release.

- [ ] **Step 1: Write failures.** Playwright must override existing suite CSP bypass:

```ts
test.use({bypassCSP:false});
test('blocks active content authority', async ({page}) => {
  await page.goto('/chat'); // fixture selects malicious persisted HTML version
  await expect(page.getByTestId('preview')).toHaveAttribute('sandbox','allow-scripts');
  await expect(page.getByText('parent access blocked')).toBeVisible();
  expect(attemptedExternalRequests).toEqual([]);
});
```

Fixture HTML actually attempts fetch, image/beacon/WebSocket requests, parent cookie access, popup, top navigation and service-worker registration; observe failures and unchanged parent URL. Also assert a local calculator interaction succeeds.

- [ ] **Step 2:** Run `pnpm --dir tests/e2e exec playwright test tests/artifacts/preview-isolation.spec.ts --project=chromium`; expect missing preview behavior.
- [ ] **Step 3:** Fetch authenticated bytes in parent. Build a trusted opaque-origin wrapper whose CSP permits only its nonce bootstrap and `frame-src blob:`; the wrapper creates an inner blob iframe for generated HTML. The inner iframe has first-position restrictive CSP (`default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'`) and both frames have sandbox `allow-scripts` only. The wrapper must block inner self-navigation to any HTTP(S) URL, a gap in using only a single srcDoc iframe. Safely JSON-encode/escape embedded HTML so `</script>` cannot terminate bootstrap. No same-origin permission, tokens, top navigation, popups, downloads or service workers. Messages accept only the expected frame `contentWindow`, per-mount nonce, and bounded height/error payloads; never expose general RPC. Capability is self-contained HTML/JS; arbitrary React/npm builds and remote assets remain explicitly unsupported.
- [ ] **Step 4:** Rerun browser command and colocated Vitest test; expect PASS with CSP enforced. Preview flag off must revert to source/download.
- [ ] **Step 5:** Stage exactly Task 5 Files; commit `feat: isolate interactive HTML artifact previews`.

### Task 6: Authenticated version sharing

**Files:** Create `backend/src/services/artifacts/sharing.py`, `backend/tests/unit/services/artifacts/test_sharing.py`, `frontend/src/components/chat/artifact-panel/ArtifactShareDialog.tsx`, `frontend/app/(dashboard)/artifacts/shared/[shareId]/page.tsx`. Modify Task 1 models/schema/router and Task 3 service/panel; create `backend/alembic/versions/20260927_artifact_shares.py` after the current head; regenerate both HTTP outputs.

**Interfaces:** `create_share(db,*,user_id:UUID,organization_id:UUID,version_id:UUID,audience_user_ids:list[UUID],expires_at:datetime)->ArtifactShareDTO`; `read_share(db,*,user_id:UUID,share_id:UUID)->SharedArtifactVersionDTO`; `revoke_share(db,*,user_id:UUID,share_id:UUID)->None`. DTO fields: `id:UUID,version_id:UUID,expires_at:datetime,revoked_at:datetime|None`.

- [ ] **Step 1: Write failures.**

```python
async def test_share_remains_pinned(db, share, recipient, version_one):
    result = await read_share(db, user_id=recipient.id, share_id=share.id)
    assert result.version_id == version_one.version_id
    await revoke_share(db, user_id=share.creator_id, share_id=share.id)
    with pytest.raises(ArtifactNotFound):
        await read_share(db, user_id=recipient.id, share_id=share.id)
```

Fixture already created version two. `test_share_denied(case,expected)` parameterizes unauthenticated=401 and wrong audience/expired/deleted ancestor=404 with `assert response.status_code == expected`. `test_share_grants_no_source_access` asserts source-document fetch remains 404 and response contains no local path.

- [ ] **Step 2:** Run `pytest -q backend/tests/unit/services/artifacts/test_sharing.py`; expect missing sharing service.
- [ ] **Step 3:** Implement POST `/shares`, GET `/shares/{id}`, GET `/shares/{id}/content`, DELETE `/shares/{id}`. `require_interactive_user` verifies browser authority before share creation/revocation. Owner selects explicit existing same-organization users and expiry, max seven days; reject empty/unknown audience and public mode. Every content fetch revalidates audience, expiry, revocation and artifact ancestors; respond `Cache-Control: private, no-store`. Return `SharedArtifactVersionDTO(artifact_id:UUID,version_id:UUID,title:str,mime_type:str,byte_size:int,sha256:str)` from share reads; omit provenance/source IDs from the API, not just presentation. Share dialog requires explicit user action; previews retain Task 5 sandbox. No public bucket or bearerless link.
- [ ] **Step 4:** Rerun Step 2; expect PASS. Browser test revokes an already-open share, refetches, and verifies access fails while newer versions never replace shared bytes.
- [ ] **Step 5:** Stage exactly Task 6 Files; commit `feat: share exact artifact versions with authenticated audiences`.

### Task 7: Native completion, acceptance and rollout

**Files:** Create `backend/src/services/artifacts/native.py`, `backend/tests/unit/services/artifacts/test_native.py`, `tests/e2e/tests/artifacts/workspace.spec.ts`, `docs/testing/artifact-workspace-acceptance.md`. Modify existing `backend/src/services/research/draft_generation_service.py` and Task 1 lifecycle/worker/config.

**Interfaces:** `enqueue_native_artifact(db:AsyncSession,*,draft_id:UUID,context:IntegrationContext)->None` writes a durable completion intent in the draft transaction; lifecycle consumer publishes that exact persisted draft, using deterministic publication UUID from draft ID. `ARTIFACTS_ENABLED`, `ARTIFACT_EDITING_ENABLED`, `ARTIFACT_PREVIEW_ENABLED`, `ARTIFACT_SHARING_ENABLED` default false; separate editing/sharing flags stage their independent later permissions. Define all four in Task 1 config; read access/reconciliation survives disabled writes.

- [ ] **Step 1: Write failures.**

```python
async def test_started_draft_has_no_artifact(db, context):
    assert await list_thread_artifacts(db, user_id=context.user_id,
        organization_id=context.organization_id, thread_id=context.thread_id) == []
```

`test_completion_replay` commits draft+intent, restarts worker, drains twice and asserts `len(versions) == 1` and `versions[0].version.sha256 == expected_sha256` after run closure. Browser acceptance publishes Markdown/code/PNG, reloads, verifies downloaded hashes, exercises tabs/edit conflict/mobile focus, and preserves saved artifacts from a failed run.

- [ ] **Step 2:** Run `pytest -q backend/tests/unit/services/artifacts/test_native.py` and `pnpm --dir tests/e2e exec playwright test tests/artifacts/workspace.spec.ts --project=chromium`; expect missing completion integration.
- [ ] **Step 3:** Thread optional server-created context through `DraftGenerationService.generate_draft`/`_generate_draft_async`; enqueue before its existing draft commit, not from its in-memory status or tool-start response. Persist consent/scope with intent; revalidate current authorization at publication. Extend Task 1's existing lifecycle worker to claim and publish native completion intents; use the same transaction/deduplication rules and metadata-only failure/lag counters. No expired external grant becomes a trusted native context. Enable one owner/project only after the real Codex and standalone MCP journey from Plans 01/02 passes.
- [ ] **Step 4:** Run Step 2, all focused artifact suites, `scripts/ci/run_local_ci.sh --base origin/develop --frontend`, `pnpm --dir frontend test`, OpenAPI freshness checks and `git diff --check`. Record PostgreSQL migration upgrade/downgrade/re-upgrade, producer SHA, IDs/digests and mutation evidence. Separate local, hosted CI and live results; unavailable services are NOT RUN. Rollback flags preserve versions, receipts, reads and cleanup/reconciliation.
- [ ] **Step 5:** Stage exactly Task 7 Files; commit `test: verify artifact lifecycle and controlled rollout`.

Implement Tasks 1–3 before the first static-artifact release; Tasks 4–7 complete this plan. Additional renderers, public publishing, local apply-back and arbitrary dependency builds require separate explicit work.
