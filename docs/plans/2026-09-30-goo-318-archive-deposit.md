# GOO-318 Resumable Archive Deposit with Verified Receipts Plan (Academic R8)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** An authorized person can deposit one exact GOO-315 release package to **one** archive repository, the **Zenodo sandbox**. The deposit runs as separate, durable phases: `prepared → draft_created → files_uploaded → published → verified`. Every phase is an insert-only attempt row that keeps the operation id, the attempt id, the target repository and account, the exact release and file hashes, and the remote draft, record and version ids as soon as the remote side returns them. An **approval** binds the exact release id, its package hash, the target account and the action `publish`. Holding a project role is never enough, and a changed release, account or action voids the approval. An ambiguous outcome (timeout, worker crash, local commit failure after remote success) is always **reconciled by the known remote id before any retry**, so a retry can never create a second deposition or a second DOI. A partial upload or an unpublished draft never reads as published. The final receipt is accepted only when the remote file checksums, the record id and the DOI read back from Zenodo match the authorized release.

**Architecture:**
- **Two insert-only tables**, each with GOO-309's `prevent_research_insert_only_mutation()` trigger:
  - `archive_deposit_approvals`: one row per approval or revocation.
  - `archive_deposit_attempts`: one row per phase attempt, in a chain per operation.
- **One mutable outbox table**, `archive_deposit_outbox`, copied from the `artifact_lifecycle_outbox` shape (`status`, `attempts`, `last_error`, claimed by a conditional UPDATE, stale-claim re-eligible). It is the only mutable row and it holds no evidence: it is the work queue, never the record.
- **One pure module**, `deposit_rules.py`, which derives the operation status from the attempt chain, decides the next phase and checks the read-back against the release.
- **One adapter**, `services/research_engine/archives/zenodo.py`, with five calls (`create_draft`, `upload_file`, `publish`, `get_deposition`, `find_by_operation`). It is the only module that talks HTTP.
- **One ledger family**, `research_deposit`, with one stream per Collection, one service `deposit_service.py`, one router `api/research_engine/deposits.py`, and one beat task `deposit_tasks.drain_deposits`.
- **Derived status only.** `status` is never a column on the evidence tables; it is `deposit_rules.operation_status(chain)` on every read.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, Celery beat (`backend/src/tasks/celery_app.py`), `httpx` (already pinned), the PostgreSQL integration fixture (`postgres_container`, `backend/tests/integration/conftest.py:208`), openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):**
- This PR stacks on **GOO-315** (release snapshots) and **GOO-316** (statements, licence and venue checks), which stack on GOO-314 → GOO-313 → GOO-312 → GOO-311 → GOO-310.
- It opens after GOO-316 merges. Line numbers below are for `c935dde6e` (GOO-310 head), so re-locate them after each rebase.
- GOO-315 and GOO-316 are **planned in parallel**. Every name marked *planned by GOO-315/316* below is taken from the ticket contract and must be re-checked against the merged GOO-315/316 code before Task 1. If a name differs, update this plan's table in the same PR; do not add an alias.

### Consumed names

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Release snapshot (*planned by GOO-315*) | the immutable release row (id, `collection_id`, `state ∈ {candidate, verified}`, package file list with per-file `sha256`, package hash) and its package-file reader | Immutable once written; a `verified` release is the only deposit input. |
| Licence and statements (*planned by GOO-316*) | the release's approved licence/data-availability statement and contributor list | Zenodo `metadata.license` and `creators` come only from these; unknown stays unknown and blocks publish. |
| Registration receipt pattern | `ProtocolRegistrationOperation` (`backend/src/models/research_protocol.py:165`): `idempotency_key`, `request_fingerprint`, `receipt` JSONB, `external_identifier`, `uq_protocol_registration_idempotency` | Idempotency key + request fingerprint; a replay with another fingerprint is 409. Copied, **not** reused: protocol registration stays its own table. |
| Registration replay rule | `protocol_service.record_registration` (`backend/src/services/research_engine/protocol_service.py:542`), `canonical_hash` (`:49`) | `existing.request_fingerprint != fingerprint` → 409 `Idempotency conflict`. |
| Outbox shape | `ArtifactLifecycleOutbox` (`backend/src/models/artifact.py:115`), migration `aw02_artifact_lifecycle` | `status`, `attempts`, `delivered_at`, `last_error String(200)`, status index. |
| Outbox claim | `drain_artifact_outbox` (`backend/src/services/artifacts/lifecycle.py:36`), `MAX_ATTEMPTS = 5` (`:26`), `STALE_CLAIM` | Conditional UPDATE claim committed before work; stale `processing` re-eligible. |
| Beat registration | `celery_app` `beat_schedule` and `task_routes` (`backend/src/tasks/celery_app.py:120-197`), `artifact_tasks.drain_artifacts` (`backend/src/tasks/artifact_tasks.py`) with `run_async` (`backend/src/tasks/_async_utils.py`) | One entry per task; tasks open `AsyncSessionLocal`. |
| Insert-only trigger (GOO-309) | `prevent_research_insert_only_mutation()` (created in `e2a4c6b8d0f1`) | SQLSTATE `55000` on UPDATE or DELETE. |
| Roles | `ResearchAction.RELEASE` (`backend/src/services/research_engine/project_access.py:36`), `_DECISION_ROLE` (`:40`), `resolve_project` (`:186`) | RELEASE = adjudicator or supervisor; ownership grants none. Archived workspace → 409 on non-VIEW (`:161`). |
| Ledger | `lock_aggregate_stream` (`backend/src/services/research_decisions/ledger.py:1318`), `append_decision` (`:1321`), `replay_decisions` (`:1420`), `_RATIONALE_EVENTS` (`:424`), `_FAMILIES` (`:2235`), `identity_service._replayed_event` (`backend/src/services/research_engine/identity_service.py:76`) | Caller-owned append inside the service's single commit. |
| Canonical hash | `contracts.canonical_json_sha256` (`backend/src/services/research_engine/contracts.py:38`) | Stable fingerprints. |
| Bundle | `audit_bundle._sealed_part` (`backend/src/services/research_engine/audit_bundle.py:86`), `gather_parts` (`:315`) | One reader per part. |
| Settings secrets | `pydantic.SecretStr` in `backend/src/core/config.py:12` | Token never serialized. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| **Which repository** | **Zenodo sandbox** (`https://sandbox.zenodo.org/api`) through the REST deposition API. Production Zenodo is the same adapter with a different base URL, gated by `ZENODO_BASE_URL`; this ticket only enables the sandbox (`ZENODO_SANDBOX_ONLY=true` rejects any other host at startup). | Zenodo mints real DataCite DOIs on a free sandbox with the same API, supports draft → files → publish as separate calls, and returns stable deposition ids we can reconcile on. OSF and Figshare need extra project scaffolding. One adapter closes the ticket. |
| Remote flow | `POST /deposit/depositions` (draft, returns `id`, `links.bucket`, `metadata.prereserve_doi.doi`) → `PUT {bucket}/{filename}` per package file (returns `checksum: "md5:..."`) → `POST /deposit/depositions/{id}/actions/publish` (returns `record_id`, `doi`) → `GET /records/{record_id}` read-back. | These are the four distinct remote states the ticket names: preparation, draft upload, publication, verified completion. |
| **Reconcile-by-remote-id before retry** | Before any attempt of a phase, `deposit_service` reads the last attempt. If it holds a `remote_deposition_id`, the worker first calls `get_deposition(id)` and records what the remote side already has (`state`, `submitted`, file checksums, `record_id`, `doi`). Only a phase the remote side does **not** already show is re-sent. If no remote id was saved (crash between the HTTP response and the local insert), the worker calls `find_by_operation(operation_id)`, which lists the account's drafts filtered by our operation id stored in `metadata.notes` as `nous-operation:{operation_id}`, before creating a new draft. | Retry, restart and timeout-after-remote-success cannot duplicate a deposition, because the only call that creates a remote object is preceded by a lookup for it. |
| Remote ids survive a failed local commit | The adapter call and the attempt insert happen in this order: (1) claim the outbox row and commit; (2) call Zenodo; (3) insert the attempt row with the remote ids and commit. If (3) fails, the outbox row is still `processing`; after `STALE_CLAIM` it is re-claimed and the reconcile rule above recovers the ids from Zenodo. `find_by_operation` is what makes this safe. | This is the `artifact_lifecycle_outbox` claim pattern: no remote side effect without a durable intent, and no lost remote id even if the local write fails. |
| **Approval scope** | `archive_deposit_approvals` row: `{release_id, package_sha256, repository: "zenodo_sandbox", account_ref, action: "publish", approved_by_id, actor_role}`. The approval is valid only while: it is the newest row for that `(release_id, repository, account_ref, action)`, it is not a `revoked` row, the release's current package hash equals `package_sha256`, and the approver still holds RELEASE when the publish attempt starts (rechecked by the worker). Approver must hold `ResearchAction.RELEASE`; the person who requests the deposit may not approve it (`approved_by_id <> requested_by_id`, 422). | "Approval binds exact release + external action; local role membership alone never authorizes." Two-person rule because publishing a DOI is irreversible. |
| Who requests | `ResearchAction.RELEASE`. Draft creation and upload need a valid approval too, because a sandbox draft is already an external action. | Nothing leaves the system without an approval. |
| `account_ref` | A non-secret label (`zenodo_sandbox:{settings.ZENODO_ACCOUNT_LABEL}`). The token comes only from `settings.ZENODO_SANDBOX_TOKEN: SecretStr`. Rotating the token keeps the label; changing the account changes the label and so voids the approval. | Credentials from protected configuration, never manifests. |
| **Secret redaction** | Every retained `request`/`response` JSONB passes through one `deposit_rules.redact(obj)` that drops `Authorization`, `access_token`, any key matching `token|secret|password` and any query string on `links.*`. A unit test asserts the token string never appears in any attempt row or ledger payload. | Zenodo bucket links carry no token, but a misconfigured client could echo one. |
| Operation status (derived) | `operation_status(chain)` returns the highest phase whose attempt has `outcome = "succeeded"`, or `failed` when the last attempt is `failed` with `retryable = false`, or `ambiguous` when the last attempt is `unknown`. `published` is shown only after a `publish` attempt with a remote `submitted: true` **and** `record_id`. `verified` only after the read-back check passes. | A partial upload or an unpublished draft never appears published, because the status is computed from what the remote side reported, not from what we asked. |
| Read-back check | `deposit_rules.check_readback(release_files, remote)`: the remote file set equals the release file set by filename; every remote `md5` equals the md5 we computed from the same bytes at upload (we store both `sha256` and `md5` per file in the `prepared` attempt); `doi` equals `prereserve_doi` from the draft; `record_id` equals the published id. Any mismatch records a `verify` attempt with `outcome = "failed"`, `retryable = false`, `mismatch: [...]`, and the operation shows `failed`. | Zenodo exposes md5, not sha256; binding md5 to the same bytes as the release sha256 is the honest bridge. |
| Changed release/account/action | A new GOO-315 release is a new release id, so it has no approval. A revocation row voids it. The worker checks validity before **every** remote call, not only at request time; an invalid approval records a `failed` attempt with `reason: "approval_invalid"` and no remote call. A published deposition cannot be revoked remotely; revocation after publish only blocks further versions. | Stale authorization fails before it reaches Zenodo. |
| Duplicate requests | `POST .../deposits` takes `idempotency_key`; `UNIQUE(collection_id, idempotency_key)` on the `prepared` attempt plus a request fingerprint (the `record_registration` rule). One live operation per `(release_id, repository)`: partial unique on the `prepared` row `WHERE kind = 'prepared'`. A second request for the same release returns the existing operation (200, `replayed=true`). | Concurrent clicks resolve to one operation. |
| Insert-only vs the outbox | Evidence (approvals, attempts) is insert-only. The outbox row is mutable and disposable: deleting every outbox row loses no evidence, and `deposit_service.requeue(operation_id)` rebuilds it from the chain. | The insert-only rule protects records, not work queues. |

**Migration head:** new revision `b0e2a4c6d8f9_create_archive_deposits.py`, with `down_revision = "a8c0e2f4b6d7"` (R7's last, GOO-317). If R7 merges with another head, re-point to the single head `check_alembic.py` reports. The migration imports nothing from `src`, copies `_deny_data_api`, creates the three tables, and attaches the insert-only trigger to the two evidence tables only. `downgrade()` drops the triggers and the tables, and **not** the function.

---

### Task 1: Pure rules (`deposit_rules.py`)

**Files:**
- Create `backend/src/services/research_engine/deposit_rules.py`.
- Create `backend/tests/unit/services/test_deposit_rules.py`.

```python
PHASES = ("prepared", "draft_created", "files_uploaded", "published", "verified")
OUTCOMES = ("succeeded", "failed", "unknown")
@dataclass(frozen=True) class Attempt: id; phase; outcome; retryable; remote_deposition_id; remote_record_id; doi; files; created_at
def operation_status(chain: Sequence[Attempt]) -> str          # phase | "failed" | "ambiguous"
def next_phase(chain: Sequence[Attempt]) -> str | None         # None when verified or terminal-failed
def approval_valid(approvals: Sequence[Mapping], release_hash: str, account_ref: str, action: str) -> bool
def check_readback(release_files: Sequence[Mapping], remote: Mapping) -> list[str]   # mismatch codes
def redact(obj: Any) -> Any
def operation_marker(operation_id: UUID) -> str               # "nous-operation:{id}"
```

**Tests (write first; they fail on the import):**
- `test_unpublished_draft_never_reports_published`
- `test_partial_upload_status_is_draft_created`
- `test_unknown_last_attempt_is_ambiguous_and_next_phase_is_reconcile`
- `test_revocation_or_changed_hash_voids_approval`
- `test_readback_md5_mismatch_and_missing_file_fail`
- `test_redact_drops_tokens_and_link_query_strings`

**Run:** `pytest -q backend/tests/unit/services/test_deposit_rules.py`
**Commit:** `feat(research): pure archive deposit status, approval and read-back rules (GOO-318)`

---

### Task 2: Models + migration

**Files:**
- Create `backend/src/models/research_deposit.py` (`ArchiveDepositApproval`, `ArchiveDepositAttempt`, `ArchiveDepositOutbox`) and export them from `backend/src/models/__init__.py`.
- Create `backend/alembic/versions/b0e2a4c6d8f9_create_archive_deposits.py`.
- Modify `backend/tests/integration/test_screening_queue_postgres.py`: prepend the three tables to `_REBUILT_TABLES` and append the filename to `_upgrade`'s list.

| Table | Columns | Constraints |
|---|---|---|
| `archive_deposit_approvals` | `id, collection_id FK RESTRICT, release_id FK (GOO-315) RESTRICT, package_sha256 CHAR(64), repository VARCHAR(32), account_ref VARCHAR(128), action VARCHAR(16), kind VARCHAR(16), approved_by_id FK users, actor_role VARCHAR(16), rationale TEXT, created_at` | `kind IN ('approved','revoked')`; `action = 'publish'`; `repository = 'zenodo_sandbox'`; `actor_role IN ('adjudicator','supervisor')` |
| `archive_deposit_attempts` | `id, collection_id, operation_id UUID NOT NULL, release_id FK, repository, account_ref, phase VARCHAR(16), outcome VARCHAR(16), retryable BOOL, idempotency_key VARCHAR(255) NULL, request_fingerprint CHAR(64) NULL, requested_by_id FK users, approval_id FK NULL, files JSONB, remote_deposition_id VARCHAR(64) NULL, remote_record_id VARCHAR(64) NULL, doi VARCHAR(255) NULL, request JSONB, response JSONB, reason VARCHAR(200) NULL, previous_id FK self NULL, created_at` | `phase IN PHASES`; `outcome IN OUTCOMES`; `(phase = 'prepared') = (previous_id IS NULL)`; `phase <> 'prepared' OR operation_id = id`; `UNIQUE(previous_id)`; partial unique `uq_deposit_idempotency (collection_id, idempotency_key) WHERE phase = 'prepared'`; partial unique `uq_deposit_live (release_id, repository) WHERE phase = 'prepared'` |
| `archive_deposit_outbox` | `id, operation_id UNIQUE, status VARCHAR(16) default 'pending', attempts INT default 0, updated_at, last_error VARCHAR(200)` | `status IN ('pending','processing','done','skipped')`; index on `status` |

Every partial index declares both `postgresql_where=` and `sqlite_where=`. `UNIQUE(previous_id)` makes each chain linear, so two workers cannot both append the next phase.

**Check:** `(cd backend && python ../scripts/ci/check_alembic.py)` reports single head `b0e2a4c6d8f9`; `alembic upgrade head --sql | grep -c archive_deposit_attempts` > 0.
**Commit:** `feat(research): insert-only archive deposit approvals and attempts plus outbox (GOO-318)`

---

### Task 3: Ledger family `research_deposit`

**Files:**
- Modify `backend/src/services/research_decisions/ledger.py`: add the vocabulary, add `deposit.approved` and `deposit.revoked` to `_RATIONALE_EVENTS`, add payload/transition validators and the `_FAMILIES` entry (subject type `archive_deposit`, `requires_subject_version=False`).
- Modify `backend/tests/unit/services/test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `deposit.approved` / `deposit.revoked` | `collection_id, approval_id, release_id, package_sha256, repository, account_ref, action` | `adjudicator` or `supervisor` |
| `deposit.requested` | `collection_id, operation_id, release_id, package_sha256, repository, account_ref, file_count` | `adjudicator` or `supervisor` |
| `deposit.phase_recorded` | `collection_id, operation_id, attempt_id, previous_id, phase, outcome, remote_deposition_id, remote_record_id, doi` | `system` (worker; `requested_by_id` is the attributed actor) |

**Replay rules:** `collection_id == aggregate_id`; every `previous_id` names the chain tip; `published` and `verified` succeed only after a succeeded `files_uploaded`; a `phase_recorded` with outcome `succeeded` for `draft_created`/`published` must name the approval valid at that point in the stream; the requester never equals the approver.

**Tests:** `test_deposit_replay_rejects_publish_before_upload`, `test_deposit_replay_rejects_self_approval`, `test_deposit_payload_has_no_secret_keys`.
**Commit:** `feat(research): research_deposit decision family and replay rules (GOO-318)`

---

### Task 4: Zenodo adapter

**Files:**
- Create `backend/src/services/research_engine/archives/__init__.py` and `archives/zenodo.py`.
- Modify `backend/src/core/config.py`: `ZENODO_BASE_URL: str = "https://sandbox.zenodo.org/api"`, `ZENODO_SANDBOX_TOKEN: SecretStr | None = None`, `ZENODO_ACCOUNT_LABEL: str = ""`, `ZENODO_SANDBOX_ONLY: bool = True` (validator: host must be `sandbox.zenodo.org` while true).
- Create `backend/tests/unit/services/test_zenodo_adapter.py` (`httpx.MockTransport`).

```python
class ZenodoError(Exception): retryable: bool; status: int | None
class ZenodoAdapter:
    async def create_draft(self, metadata: dict, marker: str) -> dict        # id, bucket, prereserve doi
    async def upload_file(self, bucket: str, name: str, data: bytes) -> dict  # checksum md5
    async def publish(self, deposition_id: str) -> dict                       # record_id, doi, submitted
    async def get_deposition(self, deposition_id: str) -> dict | None         # 404 -> None
    async def find_by_operation(self, marker: str) -> dict | None             # GET /deposit/depositions?q="marker"&all_versions
```

- A timeout or 5xx raises `ZenodoError(retryable=True)`, which the service records as outcome `unknown` (never `failed`), because the remote side may have acted.
- 4xx other than 404/409/429 is `retryable=False`.
- `upload_file` streams the bytes and computes md5 locally for the attempt row.

**Tests:** `test_timeout_is_unknown_not_failed`, `test_find_by_operation_matches_marker_only`, `test_token_never_in_logged_request`, `test_non_sandbox_host_rejected_by_settings`.
**Commit:** `feat(research): Zenodo sandbox deposition adapter (GOO-318)`

---

### Task 5: Service, worker and beat entry

**Files:**
- Create `backend/src/services/research_engine/deposit_service.py`.
- Create `backend/src/tasks/deposit_tasks.py` and register it in `celery_app` `include`, `task_routes` (`agent_runs` queue, like `drain_artifacts`) and `beat_schedule` (`"drain-deposits": {"task": "src.tasks.deposit_tasks.drain_deposits", "schedule": 10.0}`).

```python
AGGREGATE_TYPE = "research_deposit"; SUBJECT_TYPE = "archive_deposit"
async def approve(db, context, actor_id, actor_role, data) -> tuple[ApprovalResponse, bool]   # RELEASE; one commit
async def revoke(db, context, actor_id, actor_role, data) -> ApprovalResponse
async def request_deposit(db, context, actor_id, actor_role, data) -> tuple[DepositResponse, bool]  # inserts `prepared` + outbox, one commit
async def drain(db, adapter, *, limit=20) -> int        # claim outbox → reconcile → one phase → insert attempt → commit
async def list_deposits(db, context) -> DepositListResponse   # chains + derived status
async def requeue(db, context, operation_id) -> None    # RELEASE; rebuilds a missing/skipped outbox row
async def export_part(db, context) -> dict              # schema nous.academic.deposits.v1
```

- **Write order (API):** `resolve_project(RELEASE)` → `lock_aggregate_stream(research_deposit, collection_id)` → `_replayed_event` → load release (must be GOO-315 `verified`, 409 `Release is not verified`), compute validity → insert rows + append + **one commit**.
- **Worker step (`drain`):** claim the outbox row with the `drain_artifact_outbox` conditional UPDATE and commit. Load the chain. Re-resolve access as `requested_by_id` and recheck the approval (invalid → `failed` attempt `approval_invalid`, outbox `done`). If the last attempt has a remote id or is `unknown`, call `get_deposition`/`find_by_operation` first and record a reconcile attempt for the phase the remote side shows. Then run exactly one `next_phase` call, insert its attempt, append `deposit.phase_recorded`, set outbox `pending` (more phases) or `done`, commit. `MAX_ATTEMPTS` per phase → outbox `skipped`, status stays `ambiguous` and the UI offers "Retry" (`requeue`).
- The `published` phase is followed immediately by `verified` in the same drain pass using `GET /records/{id}` and `check_readback`.

**Commit:** `feat(research): resumable archive deposit service and beat worker (GOO-318)`

---

### Task 6: API + contracts + bundle part

**Files:**
- Create `backend/src/api/research_engine/deposits.py` (`DEPOSITS = "/projects/{project_id}/deposits"`); register it in `backend/src/api/research_engine/__init__.py` and `backend/src/main.py`.
- Add schemas to `backend/src/schemas/research_engine.py`.
- Modify `audit_bundle.py`: `_deposits` builder → `_sealed_part("deposits.json", "nous.academic.deposits.v1", ...)`, added to `gather_parts`.
- Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

| Route | Action | Notes |
|---|---|---|
| `GET {DEPOSITS}` | VIEW | Every operation with its attempt chain, derived `status`, approval validity and the DOI only when `verified`. |
| `POST {DEPOSITS}/approvals` | RELEASE | 201; 409 non-verified release; 422 approver = requester (checked again at request). |
| `POST {DEPOSITS}/approvals/{id}/revoke` | RELEASE | Insert-only revocation row. |
| `POST {DEPOSITS}` | RELEASE | 202 with operation id; 200 replay; 409 no valid approval; 503 `Archive deposits are not configured` when the token is unset. |
| `POST {DEPOSITS}/{operation_id}/requeue` | RELEASE | 202. |

**oasdiff:** five new operations, additive. **Expected: no ERR.**

**Tests** (`backend/tests/unit/api/test_deposit_routes.py`): `test_owner_without_release_role_403`, `test_request_without_approval_409`, `test_unconfigured_token_503_no_rows`, `test_list_never_shows_doi_before_verified`.
**Commit:** `feat(research): archive deposit endpoints and audit bundle part (GOO-318)`

---

### Task 7: Structural guard

**Files:** create `backend/tests/unit/architecture/test_deposit_boundary.py` (AST):
- **(a)** only `archives/zenodo.py` imports `httpx` among the deposit modules;
- **(b)** `deposit_service` never calls `update(`/`delete(`/`db.merge` on `ArchiveDepositApproval` or `ArchiveDepositAttempt`;
- **(c)** `protocol_service` does not import `deposit_service` and vice versa (registration stays distinct);
- **(d)** no deposit module reads `ZENODO_SANDBOX_TOKEN` except the adapter constructor.

**Commit:** `test(research): guard archive deposit boundaries (GOO-318)`

---

### Task 8: PostgreSQL proof (one test)

**Files:** create `backend/tests/integration/test_archive_deposit_postgres.py` (`pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]`), reusing `screening_factory`, `_upgrade` (through `b0e2a4c6d8f9`), `seed_approved_protocol_binding` (`backend/tests/integration/research_engine_postgres_support.py:143`) and GOO-315's verified-release seed helper. The adapter is a **fake Zenodo** (in-process state machine with the real response shapes) that can be told to time out after acting.

**`test_deposit_resumes_reconciles_and_never_duplicates`** runs these steps in order:
1. **Seed:** a verified release V1 with two package files; supervisor S, adjudicator J, owner O (no decision role), foreign user F.
2. **Authorization:** S requests without approval → 409. S approves and S requests → 422 self-approval. J approves V1; S requests → 202 with one `prepared` row and one outbox row. O gets 403, F gets 404.
3. **Duplicate/concurrency:** two concurrent `POST` with different idempotency keys for V1 → one operation (`uq_deposit_live`), the other returns 200 replayed. Same key with a different body → 409.
4. **Timeout after remote success:** the fake creates the draft then times out. `drain` records `draft_created` outcome `unknown`; status `ambiguous`. Next `drain` calls `find_by_operation`, records the remote id, and the fake shows **exactly one** draft.
5. **Crash after remote call, before local commit:** inject an exception after `upload_file` returns for file 1. The outbox row stays `processing`; after advancing past `STALE_CLAIM` the next `drain` reconciles via `get_deposition`, sees file 1 already present with the right md5, uploads only file 2. Status reads `draft_created` until both files are confirmed: **never** `published`.
6. **Stale approval:** revoke J's approval before publish. `drain` records `failed` `approval_invalid`, the fake receives no publish call. J re-approves; `requeue`; publish proceeds.
7. **Publish + verify:** status `verified`; receipt `doi == prereserve doi`, remote md5s match the `prepared` row, release sha256 list matches V1.
8. **Read-back mismatch:** a second release V2 where the fake corrupts one md5 → status `failed` with `mismatch: ["checksum:<file>"]`, DOI not shown.
9. **Changed release:** a new GOO-315 release V3 has no approval: request → 409.
10. **Redaction:** no attempt row, ledger payload or export contains the token string.
11. **Insert-only:** UPDATE/DELETE on approvals and attempts → `55000`; deleting every outbox row then `requeue` rebuilds work without changing the chain.
12. **Archived/deleted:** writes 409, reads 200; 404 after workspace deletion; the worker records `failed` `project_unavailable` for a queued operation on an archived project.
13. **Replay:** `replay_decisions(research_deposit, collection_id)` succeeds.
14. **Downgrade:** only the three tables drop; the GOO-309 function remains.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_archive_deposit_postgres.py`. Without a database, report **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for resumable archive deposits (GOO-318)`

---

### Task 9: Frontend

**Files:**
- Create `frontend/src/types/api/research-deposit-contract.ts` (aliases).
- Modify `frontend/src/services/researchEngineService.ts`: `listDeposits`, `approveDeposit`, `revokeDepositApproval`, `requestDeposit`, `requeueDeposit`.
- Create `frontend/src/components/research-engine/DepositPanel.tsx`, mounted on the GOO-315 release view: approval state (who, which package hash), a phase stepper driven by the derived status, `ambiguous` shown as "Checking with Zenodo…", the DOI link only when `verified`, and a "Sandbox" badge.
- Tests in `__tests__/DepositPanel.test.tsx`: `doi hidden until verified`, `ambiguous never reads published`, `request disabled without valid approval`.

**Commit:** `feat(frontend): archive deposit panel on releases (GOO-318)`

---

## Mutation verification

Follow `docs/engineering/testing.md`. Record each check in the test docstring and in `docs/testing/agent-orchestration-mutation-checks.md` under a GOO-318 section.

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| Reconcile-before-create (`find_by_operation` call) | skip it | `pytest -q backend/tests/integration/test_archive_deposit_postgres.py` | step 4: the fake holds two drafts |
| Timeout → `unknown` | record `failed` retryable | same | step 4: status `failed`, no reconcile |
| `operation_status` requires remote `submitted` + `record_id` | trust our own publish request | `pytest -q backend/tests/unit/services/test_deposit_rules.py -k unpublished` | an unpublished draft reports `published` |
| Approval recheck in the worker | check only at request time | integration | step 6: publish called after revocation |
| Self-approval guard | drop it | integration | step 2: S approves own request |
| `check_readback` md5 compare | return `[]` | integration | step 8: status `verified` |
| `redact` | identity | `pytest -q backend/tests/unit/services/test_deposit_rules.py -k redact` plus step 10 | token found |
| `uq_deposit_live` | drop the index in the test migration | integration | step 3: two operations |

After each check, restore the guard, confirm `git diff` on that file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_deposit_rules.py backend/tests/unit/services/test_zenodo_adapter.py backend/tests/unit/services/test_research_decision_ledger.py
pytest -q backend/tests/unit/services/test_audit_bundle.py backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # single head b0e2a4c6d8f9
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research-engine
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists.** Report each as NOT RUN, never as passing:
- **PostgreSQL proof:** needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Live Zenodo sandbox run** (upload, interruption, resume, publish, `GET /records/{id}` read-back tied to package hashes): needs `ZENODO_SANDBOX_TOKEN` with `deposit:write deposit:actions` scopes. **The fake adapter cannot close the ticket**; the closure requires the live transcript.
- **Live journey:** needs a deployed stack with `b0e2a4c6d8f9`, a sandbox token in Infisical and the saved principals.

## Authenticated journey list for Linear closure

Run on dev after deploy (`alembic current` shows `b0e2a4c6d8f9 (head)`), with the sandbox token configured.

1. **Approve:** the adjudicator approves the verified release; the panel shows the bound package hash and account label.
2. **Request:** the supervisor requests; the stepper moves `prepared → draft_created`.
3. **Interrupt:** restart the Celery worker mid-upload (`kubectl rollout restart` of the worker deployment in `rag-dev`). After restart the operation resumes, Zenodo sandbox shows **one** deposition, and no file is uploaded twice.
4. **Publish/verify:** status `verified`; the DOI resolves on `sandbox.zenodo.org`; downloaded file md5/sha256 match the release manifest.
5. **Deny:** the owner gets 403; the requester approving gets 422; a revoked approval blocks publish.
6. **Bundle:** `deposits.json` is in the audit bundle and `sha256sum -c SHA256SUMS` passes.

Record the SHA/PR, CI and oasdiff links, the Zenodo sandbox deposition URL, the PostgreSQL junit, the mutation transcripts and the NOT RUN list.

## Seams

- **Upstream (GOO-315/316):** reads the verified release, its package files and hashes, licence and creators. Never writes them; never changes release state. Packaging still never authorizes submission; only this ticket's approval does.
- **Protocol registration:** stays in `protocol_registration_operations`. The pattern is copied; the tables and services do not share code (Task 7 (c)).
- **Downstream (GOO-320):** a superseding release can be deposited as a new Zenodo **version** later. `# ponytail: POST /deposit/depositions/{id}/actions/newversion when a superseding release is deposited; out of scope here.`

## Out of scope

Each item has a `ponytail:` marker at its seam: production Zenodo; a second repository; new-version deposits; editing published metadata; embargo/restricted access; OAuth per-user Zenodo accounts (one service account label); remote deletion of drafts on revocation.
