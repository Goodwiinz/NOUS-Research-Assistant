# Harness live-proof evidence, local stack, 2026-10-05

Date: 2026-10-05 (UTC timestamps below).

Source commit under test: `47add9891` (`origin/develop` at the start of the day) plus the uncommitted local patches listed below. The bridge fixes were made in a separate worktree checked out at that SHA; the evidence bundle itself is on `0f1679484`.

Runbook: [harness-live-proof.md](../../harness-live-proof.md). The "Evidence table" section of the runbook defines the rows used here.

Authorization scope: a local-stack run only. The target is a local backend, Celery worker and beat, and a local frontend on `localhost`, with the bridge on the same machine. Nothing was pushed, deployed or changed on the AWS dev lane (`dev-api.goodwiinz.tech`, Argo CD `nous-dev-aws`, `goodwiinz.tech`). No flag was changed in `values-aws.yaml`.

## Status

This bundle does not clear any live row of the runbook for the AWS dev lane. The runbook limits its live rows to the deployed image, and this run used a locally patched tree. Every result below is classified **LOCAL (patched)** at best. Gate 1 and the rows 0a-0c, G1, G1-flags and RB stay **NOT RUN** here, and the AWS-lane rows P1-1 to P2-3 stay **NOT RUN** until a bundle against the deployed image exists.

## Local stack

| Component | Value |
| --- | --- |
| Backend | local uvicorn on `localhost:8000`; Celery worker (`-P solo`, consumes `celery`, `document_processing`, `high_priority`, `low_priority`, `agent_runs`); Celery beat running (`dispatch-harness` every 5 s, `drain-integration-actions` and `drain-artifacts` every 2 s) |
| Frontend | local Next.js `dev:offline` on `localhost:3000`, API calls same-origin through the `/api/v1` rewrite |
| Database | local PostgreSQL (`initdb`, port 55432), `alembic current` = `d4a6c8e0f2b3 (head)` |
| Redis | local `redis-server` on port 6380 (the CLI-bearer cutoff written by `disconnect` needs it) |
| Bridge | `@nous/harness-bridge` from the patched tree, `nous-harness run` in its own terminal, default store directory |
| Codex | `codex --version` = `codex-cli 0.153.4`, ChatGPT login, model `gpt-5.6-luna` (set through `NOUS_HARNESS_CODEX_MODEL`) |
| Flags | `HARNESS_BRIDGE_ENABLED=true`, `NOUS_MCP_ENABLED=true`, `ARTIFACTS_ENABLED=true`; the three inert artifact flags unset; `STORAGE_BACKEND=local` |
| Scopes at `connect` | `harness:execute`, `tools:read`, `artifacts:publish`, `tools:write` (Phase 2 connection) |
| Identifiers | `device_id=46c873c7-5fa0-4323-b8e3-7272a9cf57b2`, `workspace_id=6f1807de-e678-424f-947f-c157b0a9e910`, `project_id=98475988-e0a1-4e1e-8818-96bac4ff8451` |

An earlier Phase 1 connection (`--tools` only, device `86f6375f-…`) produced the first successful chat runs (`3d75f982-…`, `c6c5ddf9-…`, marker file written) and the first MCP read-tool calls from Codex Desktop (`GET /integrations/tools` 200, `POST /integrations/tools/read` 200 at 22:27Z). Reconnecting for Phase 2 registered a new device; the old grant was left live (bug #13a below).

## Local patches on top of `47add9891`

None of these is on `develop`. Each was needed to run the harness against a loopback stack or to make the pinned Codex work at all, and each changes behavior that the deployed lane does not have.

| # | Patch | Why this run needed it | Affects which row |
| --- | --- | --- | --- |
| 1 | Bridge: plain `ws://` for loopback API origins (`packages/harness-bridge/src/cli.ts`, `src/connection.ts`) | `run` forced `wss:` and could never connect to a non-TLS local backend | all |
| 2 | Backend: loopback exception in `/api/v1/harness/connect` scheme check (`backend/src/api/harness.py`) | the backend refused a non-`wss` bridge socket | all |
| 3 | Adapter: accept `thread/start` replies whose `sandbox.writableRoots` omits the cwd (`src/adapters/codex.ts`) | real Codex 0.153.4 reports `[]` (cwd implicit); the adapter required `[cwd]`, so every start failed "policy mismatch" and was quarantined silently | P1-1, P2-1 |
| 4 | `NOUS_HARNESS_CODEX_MODEL` → `config.model` in `thread/start` | the user's global Codex model is rejected for a ChatGPT login on CLI 0.153.4 | all |
| 5 | `project_terminal` non-empty fallback text (`backend/src/services/harness/delivery.py`) | a failed turn with no deltas persisted an empty assistant row; `ChatMessageResponse` then 500ed every read of the thread | P1-1 |
| 6 | `publish_version` flush order (`backend/src/services/artifacts/service.py`) | the outbox and reference rows were inserted before the version row; Postgres rejected the FK and the handler reported a 409 conflict | P2-1 |

Because patches 1-4 and 6 sit on the path the Playwright test and the MCP tools exercise, a PASS in this bundle is evidence that the patched tree works locally. It is not evidence that `47add9891` or the deployed image works.

## Fixture note

The e2e thread was created by a SQL insert, not through the UI. The UI cannot bind a thread to a project before its first message (the thread row is created on send), and the runbook expects a thread that is already bound (`?thread=` in the URL). The insert cloned the conversation and project binding of an existing bound thread into one new `threads` row with zero messages, `status=active`. Nothing else was inserted (no `project_threads` link row).

- Thread id: `3c4a804b-afe9-4428-8905-81743244d9b3`
- Bound to workspace `6f1807de-…` and project `98475988-…`
- Rows touched by the insert: 1

Two earlier threads also lost their `source_project_id` (set to NULL by a UI flow) during the session and were re-bound by SQL; see bug #8.

## Evidence rows

Result vocabulary: `PASS (local, patched)`, `PARTIAL`, `FAIL`, `BLOCKED`, `NOT RUN`. A skipped Playwright test is NOT RUN.

| # | Check | Evidence to record | Result |
| --- | --- | --- | --- |
| P1-1 | Chat run, reload, single approval, Playwright plus manual | See the P1-1 section | `NOT RUN` for the approval scenario; the chat-run path itself completed twice on the fixture thread |
| P2-1 | Publish and SHA-256 | See the P2-1 section | `PARTIAL (local, patched)`: idempotent within one grant PASS; across a grant renewal FAIL (bug #15) |
| P2-2 | Approval after at least 16 minutes | See the P2-2 section | `NOT RUN` for the 16-minute case; the request → browser approval → worker → note mechanics PASSED (local, patched) in under 2 minutes |
| P1-3 / P2-3 | Disconnect, then probes | See the P1-3 / P2-3 section | `NOT RUN` |

### P1-1: chat run, reload, single approval

Thread `3c4a804b-afe9-4428-8905-81743244d9b3`.

**Playwright.** The spec as committed asks Codex for an in-root write; under the bridge policy (`workspace-write`, `on-request`) that runs inside the sandbox and never raises an approval, so the committed spec cannot pass against a real Codex (bug #12). This bundle ran the spec with the fixture path moved outside the registered root (the change is in this PR). Command shape (credentials were supplied through the environment and are not recorded):

```text
CI=1 BASE_URL=http://localhost:3000 NOUS_HARNESS_E2E=1 \
  pnpm --dir tests/e2e exec playwright test tests/harness-bridge.spec.ts --project=chromium --retries=0 --reporter=line,json
```

- Summary line: NOT RUN (the corrected spec was not executed before the session ended)
- Annotation line: none
- What did run on the fixture thread: two chat-driven Codex runs completed (`0bde345c-…` at 23:04Z, leased only after the bridge was restarted, see bug #17; `6425e2ad-…` at 23:17Z). Both were in-root prompts, so neither raised a `harness_native_requests` row; no approval was exercised.

**Manual.** NOT RUN. Expected on this tree, from the code trace behind bugs #11 and #12: the card appears for an out-of-root write, is lost on a full reload before the fix in this PR, and approval once produces one `respond` row in `harness_commands`.

### P2-1: artifact publish

Runs under the Phase 2 scopes, from Codex Desktop through the standalone MCP server (`nous-harness mcp install --root <workspace>`), not from a chat run.

First attempts at 22:44Z and 22:57Z returned `409 Artifact publication conflict` from `POST /api/v1/artifacts/versions` (bug #14, two layers: outbox FK order, then reference FK order). After patch 6:

- Local file SHA-256 (`shasum -a 256` of the fixture `proof.md`, 40 bytes): `97bef64dd805ce9bae61ceab3f3b45e1985cb218423060dfee8c734fad1fb607`
- `publication_id`: `ff88740a-4eb4-4497-910a-ff182e8be440`
- Call 1 (23:01:40Z): `artifact_id=74872656-4d8d-4253-bbbd-47a4c43120bc`, `version_id=ee39f1df-8c25-469f-a1b2-4572044db337`, `sha256=97bef64d…`
- Call 2 (23:01:43Z, same arguments): same upload reused, same `version_id=ee39f1df-…`
- Idempotent (same `version_id` in both calls): YES, within the same grant
- Stored SHA-256 equals the local value: YES (verified in `artifact_versions`)
- Second file `marker.txt` (`publication_id=7f3c2b1e-4d5a-4e6f-9a8b-0c1d2e3f4a5b`): `version_id=0adee3ec-ace5-4565-9758-5dd9d467aabf`, 6 bytes, SHA-256 `5891b5b5…be03`
- Re-run at 23:09Z with the **same** publication ids after a grant renewal minted **new** artifacts (`53518db2-…` / `3e533449-…`): the reservation is keyed on `(grant_id, publication_id)` (`uq_artifact_upload_publication`) and grants rotate every renewal (bug #15)

Known gap, recorded, not counted as a failure: the standalone MCP publish has no thread or run, so `artifact_references.run_id/thread_id` are NULL, the `artifact.version_created` outbox rows are `skipped`, and nothing appears in a chat artifact panel. Orphaned reservations from the failed attempts (`5b5e8020-…`, `aec3a7aa-…`) are left for the sweeper.

### P2-2: approval after the grant lifetime

Grants live 15 minutes. The approval must rest on the consent, not the original grant.

The 16-minute case was NOT RUN: the prepared invocation (`47a38f32-…`) was never submitted.

The mechanics were exercised once, user-driven from Codex Desktop over the standalone MCP server, with the approval 78 s after the request (so it says nothing about grant expiry):

- `invocation_id`: `a12293ac-8496-4c3c-a549-33fad6aa5003`
- `requested_at` (UTC): 23:21:17Z (`POST /integrations/actions` 200, state `awaiting_approval`)
- browser review 23:22:34Z, decision 23:22:35Z by the project owner (`POST …/decision` 200, `approved=t`)
- worker executed 23:22:38Z (`drain-integration-actions` beat task), final state `succeeded`
- `project_notes` row created (`636f50cf-…`)
- `approved_at - requested_at`: 78 s (under 16 minutes, so this is not the consent-outlives-grant proof)

### P1-3 / P2-3: disconnect, then probes

One run, taken under the Phase 2 scopes. The tree contains slice A (`613fe13bf` is an ancestor of `47add9891`: checked with `git merge-base --is-ancestor`, result yes), so the expected result is the P2-3 one.

NOT RUN. The procedure (copy the state directory, probe `GET /integrations/tools` with bearer + grant and `GET /integrations/devices` with bearer only before and after `disconnect`, expect 200/200 then 401/401, check `integration_grants.revoked_at`, `integration_grant_requests.consent_revoked_at` and the Redis key `cli_revoked_before:<user>` on the local Redis) is ready in the runbook and needs no local adaptation beyond the API origin.

| Probe | Request | Expected | Observed |
| --- | --- | --- | --- |
| `before grant` | `GET /integrations/tools` with bearer and grant | 200 | NOT RUN |
| `before bearer` | `GET /integrations/devices` with bearer only | 200 | NOT RUN |
| `after grant` | same as `before grant`, after `disconnect` | 401 | NOT RUN |
| `after bearer` | same as `before bearer`, after `disconnect` | 401 | NOT RUN |

## Defects found by this run

Numbering continues the list kept during the session. "Fixed" means fixed in the patched worktree used for this run, not on `develop`.

| # | Defect | Status |
| --- | --- | --- |
| 1 | `POST /integrations/grant-requests` 403 on the dev lane for a project whose workspace has `organization_id` NULL (`authorized_project` requires an active org row; the web project route falls back to the owner's org) | fix in this PR (mirrors `resolve_project`; not reproducible locally, new workspaces carry an org) |
| 2 | Frontend `getPublicApiOrigin()` falls back to `http://localhost:8000` on localhost, which the page CSP (`connect-src 'self' https: wss:`) blocks → "Failed to fetch" on login | fix in this PR (development-only `connect-src` allowance for `localhost:8000`); the run itself used `NEXT_PUBLIC_API_URL=http://localhost:3000` |
| 3 | Bridge `run` forces `wss:` even for loopback | fixed (patch 1) |
| 4 | Backend `/harness/connect` forces `wss:` even for loopback | fixed (patch 2) |
| 5 | Adapter `verify()` rejects real Codex `writableRoots: []`; fixture server echoes `[root]` | fixed (patch 3 + fixture test) |
| 6 | Start failures swallowed (`.catch(() => quarantine)`), nothing on stderr; a quarantined start with no native session can never reconcile, so the workspace stays locked | open |
| 7 | `lease_commands`/`lease_runs` roll back the whole poll when one locked session fails authorization; the socket closes 4403 and every run on the device starves | fix in this PR (see below) |
| 8 | Thread `source_project_id` dropped to NULL by a UI flow; a new thread cannot be bound before its first message | open |
| 9 | Codex turn errors reduced to "External execution failed." in the transcript | open |
| 10 | Failed turn with no deltas persists an empty assistant row → thread `GET …/messages` 500 | fixed (patch 5 + unit test) |
| 11 | Pending approval card not restored after a full reload: the cold-load probe aborts on the first replayed `status` frame and has no `onApprovalRequired` | fix in this PR |
| 12 | Committed e2e spec asks for an in-root write, which never prompts under the policy; the package fixture emits approvals on demand | fix in this PR (spec + doc) |
| 13 | Reconnect registers a new device/workspace ids/handle and leaves the previous grant live (hygiene) | open, low |
| 14 | `publish_version` inserts outbox/reference before the version row (FK violation on Postgres, masked by SQLite tests) | fixed (patch 6); Postgres regression test still owed |
| 15 | `publication_id` idempotency keyed on `(grant_id, publication_id)`; grants rotate on renewal, so the same publication mints a new artifact after ~10 min | open; needs a consent-lineage key + migration |
| 16 | `BaseModel.created_at/updated_at` default `datetime.utcnow` (naive) on `timestamptz` columns: 4 h skew under a non-UTC DB session timezone | open; repo-wide, masked when DB/pods run UTC |
| 17 | Bridge WebSocket has no liveness or idle timeout: after an API restart that drops the peer without a close frame, `run` stays alive with zero sockets, never polls and never retries (observed after the 23:02Z restart) | open; unblock is restarting `run` |
| 18 | A `start` command that is never leased has no TTL or sweeper (`terminalize` only covers an undeliverable outbox); the run stays `queued`, the thread reports "already in progress" and the session stays `workspace_locked` until a bridge reconnects (observed: run `9023f3c0-…` queued 35 min past its command expiry) | open |

## Limitations

- The target is a local stack, not the AWS dev lane. TLS, Argo CD sync, Release Dev images, KEDA, the migration job, and the flags in `values-aws.yaml` were not exercised.
- Six local patches sit between this tree and `47add9891`. See the patch table above for which rows they touch.
- The e2e thread was created by SQL (fixture note). The UI thread-creation path is not covered.
- Each row is a single execution. No repeat run or flake rate is claimed.
- The 16-minute approval in P2-2 ran against local clocks and the local database. It shows that the consent outlives the grant. It does not show the behavior behind a load balancer or a deployed worker.
- Rows 0a, 0b, 0c, G1, G1-flags, P1-2 and RB were not part of this run and stay NOT RUN.

## Retained rule

This bundle records ids, counts, hashes, codes and timestamps only. It contains no tokens, passwords, emails, local absolute paths or source text. Raw terminal output stays out of the bundle unless it was checked for those items first.
