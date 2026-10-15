# Resume a lapsed harness pairing: implementation plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `connect` (and so `nous --rc`) resumes a lapsed pairing for the same
project from its existing browser consent, with no new approval and no new
device; `disconnect` can revoke a lapsed pairing.

**Architecture:** A CLI-authenticated `POST /integrations/grants/{id}/resume`
re-issues a grant that expired at most 30 days ago, when the caller presents
that grant's own token and its consent, device and binding still validate.
The bridge's `GrantKeeper` gains `resume()`; `connect` calls it when the reuse
probe fails, `disconnect` calls it when the stored grant has expired.
Design: `docs/plans/2026-10-13-grant-resume-design.md`.

**Tech stack:** FastAPI + SQLAlchemy async (SQLite unit tests, PostgreSQL
integration test), TypeScript bridge on Node's test runner with a fake fetch.

**Commands** (from the worktree root; `PY` is the main checkout's venv):

```sh
PY=/Users/<you>/development/RAG_system/backend/.venv/bin/python
cd backend && $PY -m pytest -o addopts='' -q tests/unit/services/integrations/test_grant_resume.py
cd backend && $PY -m pytest -o addopts='' -q tests/unit/api/test_integration_grant_resume.py
ORCHESTRATION_TEST_DATABASE_URL="postgresql://$USER@/postgres?host=/tmp" \
  $PY -m pytest -o addopts='' -q tests/unit/services/integrations/test_grant_resume_postgres.py
pnpm --dir packages/harness-bridge test test/grants.test.ts test/connect-reuse.test.ts test/disconnect.test.ts
pnpm --dir packages/harness-bridge type-check
```

---

### Task 1: `resume_grant` service

**Files:**
- Modify: `backend/src/services/integrations/context.py` (token shape helper,
  `_replace_grant` shared with `renew_grant`, `RESUME_WINDOW`, `resume_grant`)
- Create: `backend/tests/unit/services/integrations/test_grant_resume.py`

**Step 1: failing tests.** SQLite fixture as in `test_context.py` (org, user,
workspace, project, conversation and a chat thread in the project). A
`paired` fixture registers a device, creates, approves and exchanges a
consent for `{"harness:execute", "tools:read"}` and returns
`(device_id, consent_id, issued)`. A helper `lapse(db, grant_id, ago)` sets
`expires_at = now - ago`.

- `test_lapsed_grant_resumes_under_the_same_consent_and_device`: lapse 1 h;
  `resume_grant(db, OWNER, issued.grant_id, issued.token)` returns a new
  token; `resolve_integration_context(new)` has the same project, scopes and
  `consent_id`; the old token is refused; the old row has `revoked_at`; no new
  `IntegrationGrantRequest` or `BridgeDevice` row exists.
- `test_a_resumed_grant_cannot_be_resumed_twice` (single use: the second call
  with the same token is refused).
- `test_a_live_grant_resumes_like_a_renewal`.
- `test_resume_refuses_without_the_grant_token`: `""`, a JWT-looking string,
  another grant's token, a token of the right shape that is not this grant's.
- `test_resume_refuses_another_users_grant` (other user in the same org).
- `test_resume_refuses_a_token_renewed_away` (renew, lapse the new one, resume
  with the old token is refused; with the new token succeeds).
- `test_resume_refuses_after_cli_disconnect` (`revoke_integration_grant`).
- `test_resume_refuses_a_revoked_consent` (`connections.revoke_consent`).
- `test_resume_refuses_a_disconnected_device` (`connections.disconnect_device`
  with `revoke_user_cli_tokens` patched to a no-op coroutine).
- `test_resume_refuses_when_the_binding_no_longer_validates`, parametrized:
  project soft-deleted, workspace soft-deleted, user inactive, organization
  inactive.
- `test_resume_refuses_a_chat_bound_grant_whose_chat_moved` (consent with
  `thread_id`; set the thread's `source_project_id` to another project).
- `test_resume_refuses_scopes_wider_than_the_consent` (update the grant row's
  scopes to add `tools:write`).
- `test_resume_refuses_a_grant_without_device_or_consent` (a
  `mint_integration_grant` grant without device; a device grant whose
  `request_id` is NULL).
- `test_resume_refuses_a_run_grant` (set `run_id`).
- `test_resume_window_is_thirty_days`: lapse 29 days resumes; lapse 31 days
  is refused.

Run: FAIL (`ImportError: resume_grant`).

**Step 2: implement** in `context.py`:

```python
from hmac import compare_digest

# A lapsed pairing may be resumed from its consent until this long after its
# last grant expired (owner decision 2026-10-13); after that, approve again.
RESUME_WINDOW = timedelta(days=30)


def _grant_token_shape(token: str) -> bool:
    return token.startswith("nous_ig_") and len(token) == 51
```

`resolve_integration_context` uses `_grant_token_shape`. `renew_grant`'s CAS
and successor move into:

```python
async def _replace_grant(
    db: AsyncSession, grant: IntegrationGrant, *conditions: ColumnElement[bool]
) -> IssuedGrant:
    """Revoke ``grant`` while it is unrevoked (and ``conditions`` hold) and
    issue its successor: same binding, scopes, device, chat, run and consent.
    One caller wins; the other gets IntegrationConflict."""
```

`renew_grant` calls `_replace_grant(db, grant, IntegrationGrant.expires_at > now())`.

```python
async def resume_grant(
    db: AsyncSession, user: Any, grant_id: UUID, token: str
) -> IssuedGrant:
    grant = await owned_grant(db, user, grant_id)
    if (
        not _grant_token_shape(token)
        or not compare_digest(grant.token_hash, sha256(token.encode()).hexdigest())
        or grant.is_deleted
        or grant.revoked_at is not None
        or grant.device_id is None
        or grant.request_id is None
        or grant.run_id is not None
        or grant.expires_at.replace(tzinfo=timezone.utc) <= now() - RESUME_WINDOW
    ):
        raise IntegrationAccessDenied()
    check_scopes(grant.scopes, workspace_bound=grant.workspace_id is not None)
    await validate_binding(...same fields as _validate_grant...)
    consent = await db.scalar(select(IntegrationGrantRequest).where(
        id == grant.request_id, status == "consumed", user, org, project, workspace,
        device, thread_id == grant.thread_id, consent_revoked_at IS NULL,
        is_deleted IS FALSE))
    if consent is None or not set(grant.scopes) <= set(consent.scopes):
        raise IntegrationAccessDenied()
    return await _replace_grant(db, grant)
```

**Step 3:** run the new file and `test_context.py` + `test_grant_thread_binding.py`
(renewal regressions). Expected: PASS.

**Step 4: commit** `feat(integrations): resume a lapsed device grant from its consent`.

### Task 2: route

**Files:**
- Modify: `backend/src/api/integrations/grants.py`
- Create: `backend/tests/unit/api/test_integration_grant_resume.py`

**Step 1: failing tests** (FastAPI app with the grants router, `get_db`,
`get_current_user_token`, `get_current_user` overridden, `service.resume_grant`
monkeypatched to record its arguments):
- a CLI token with `X-NOUS-Integration-Grant: T` reaches the service with
  `(grant_id, "T")` and returns `{"token", "grant_id"}`;
- a browser token (`is_cli=False`) is 403 and the service is not called;
- no header reaches the service with `""` (the service refuses it);
- `IntegrationAccessDenied` → 403, `IntegrationConflict` → 409.

**Step 2: implement:**

```python
@router.post("/grants/{grant_id}/resume", response_model=IssuedGrant)
async def resume_grant(
    grant_id: UUID,
    request: Request,
    user: User = Depends(require_cli_user),
    db: AsyncSession = Depends(get_db),
) -> IssuedGrant:
    """Re-issue this device's lapsed grant from the browser consent it was
    exchanged from, without a new approval. Send the stored grant token, even
    expired, in X-NOUS-Integration-Grant: a CLI sign-in is not bound to a
    device, so the grant's own token proves which pairing is resumed. Refused
    (403) once the grant was renewed, revoked or disconnected, its consent or
    device revoked, its project or workspace out of reach, or it expired more
    than 30 days ago; 409 when another resume or renewal won."""
    return await call(service.resume_grant(
        db, user, grant_id, request.headers.get("x-nous-integration-grant", "")))
```

**Step 3:** run; PASS. **Step 4:** commit `feat(integrations): POST /integrations/grants/{id}/resume`.

### Task 3: PostgreSQL race

**Files:** Create `backend/tests/unit/services/integrations/test_grant_resume_postgres.py`
(markers `integration`, `requires_postgres`; schema-per-test fixture as in
`test_selected_skills_postgres.py`; skip without `ORCHESTRATION_TEST_DATABASE_URL`).

Test: two sessions resume the same lapsed grant concurrently
(`asyncio.gather`); exactly one returns a token and the other raises
`IntegrationConflict` or `IntegrationAccessDenied` (whichever read the row
after the winner committed); exactly one unrevoked grant remains for the
consent. Mutation-verify: drop `IntegrationGrant.revoked_at.is_(None)` from
the `_replace_grant` CAS and confirm the test fails (two successors), then
restore. Record the mutation in the module docstring. Commit
`test(integrations): concurrent resumes of one grant issue one successor`.

### Task 4: contract

Run `python scripts/ci/generate_openapi.py` (from the repo root, with `PY`)
and `pnpm --dir frontend generate:api-types` (or the pinned
`openapi-typescript` through `pnpm dlx` when `frontend/node_modules` is
absent). Expect one new path in `backend/openapi.json` and
`frontend/src/types/generated/api.d.ts`; check with
`python scripts/ci/generate_openapi.py --check`. Commit
`chore(api): regenerate OpenAPI snapshot and types for grant resume`.

### Task 5: `GrantKeeper.resume()`

**Files:** Modify `packages/harness-bridge/src/grants.ts`; test in
`packages/harness-bridge/test/grants.test.ts`.

```ts
export type ResumeOutcome =
  | { kind: "resumed"; credentials: IntegrationCredentials }
  | { kind: "refused" }
  | { kind: "unavailable"; reason: string };
```

`resume()` POSTs `/integrations/grants/{grantId}/resume` with
`integrationHeaders(stored)`; 200 writes `{...stored, grantToken, grantId,
renewedAt: now()}` under the same handle; 409 waits for another process's
write like `renew` does (shared private helper) and returns it, else
`refused`; 401/403/404 → `refused`; network error, 429, 5xx →
`unavailable`; a stored connection without a grant ID → `refused`; a 200
without a token or grant ID throws `invalid NOUS grant resume`.

Tests: each outcome above, the request's method, URL and both headers, and
that the stored credential is updated in place (same handle).

Commit `feat(harness-bridge): GrantKeeper.resume re-issues a lapsed grant`.

### Task 6: `connect` resumes on the reuse path

**Files:** Modify `packages/harness-bridge/src/cli.ts` (`connect` reuse
block, a `grantResumed` helper beside `grantIsLive`, help text);
`packages/harness-bridge/test/connect-reuse.test.ts`.

`fakeNous` gains a `resume` reply (default 404, so existing tests keep their
meaning). New tests:
- an expired grant (renew 403, resume 200) resumes: after the mark, calls are
  exactly `[…/renew, …/resume]`, no `/cli-auth/start`, no `/grant-requests`,
  no `/integrations/devices`; same device ID and credential handle; stored
  grant token is the resumed one; message starts `Resumed binding`;
- a refused resume (403) and an old server (404) run the full flow;
- a resume is not attempted when the binding or scopes rule reuse out;
- the retained-scopes notice is printed on a resume too.

Commit `feat(harness-bridge): connect resumes a lapsed pairing from its consent`.

### Task 7: `disconnect` resumes a lapsed grant, then revokes

**Files:** Modify `packages/harness-bridge/src/cli.ts` (`disconnect`);
`packages/harness-bridge/test/disconnect.test.ts`.

On `GrantExpired`: `keeper.resume()`; `resumed` → DELETE with the resumed
credentials (as today); `refused` → today's local removal and pointer;
`unavailable` → throw `could not reach NOUS to revoke this device; nothing
was removed (…)` and keep everything.

Tests (stored `renewedAt` 20 minutes ago so `current()` renews): renew 403 +
resume 200 + DELETE 204 → `{revoked: true}`, DELETE sent with the resumed
token; renew 403 + resume 403 → local removal, `{revoked: false}`; renew 403 +
resume 503 → rejects, nothing removed.

Commit `feat(harness-bridge): disconnect resumes a lapsed grant to revoke its consent`.

### Task 8: docs

- `docs/engineering/harness-bridge.md`: "Grant renewal" (resume route and its
  refusals), "Binding reuse" (resume step), the `disconnect` paragraph that
  says an expired grant cannot be revoked, and the "free the folder" steps
  (a lapse alone no longer gives a new device); a dated 2026-10-13 note.
- `packages/harness-bridge/README.md`: binding reuse and disconnect text.
- The `nous --rc` design amendment text (for `feat/nous-rc`, which holds that
  file) goes into the PR description.

Commit `docs(harness-bridge): resuming a lapsed pairing`.

### Task 9: verification

`ruff`, `black --check`, `isort --check` on changed Python files, `mypy
--disallow-untyped-defs` on added ones; the backend test files above plus
`tests/unit/services/integrations/` and `tests/unit/api/test_integration_*`;
bridge `type-check` and the full bridge suite; `generate_openapi.py --check`.
Then a code review of the branch diff before opening the PR.
