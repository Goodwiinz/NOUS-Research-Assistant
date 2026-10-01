# GOO-313 One Workflow Reruns From a Fresh Environment Plan (Academic R6)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** One scientific workflow can be rerun from its GOO-312 manifest in a genuinely fresh, isolated E2B sandbox. That workflow is a completed run whose single `analyze` step has a `complete` manifest. The rerun:
1. restores the exact archived inputs, code and pinned environment lock (never live documents);
2. verifies each restoration inside the sandbox;
3. executes the declared command with the recorded parameters and seed;
4. compares every required output against a comparison rule declared and hashed **before** execution.

Each attempt is persisted with its parent run, environment and input validation, status, outputs and a machine-readable comparison. *Executed* and *reproduced* are separate results. A missing or mismatched input, an unavailable template, a failed dependency restoration, a cancellation or an interrupted worker each end in an explicit terminal state, and none of them can publish partial output as executed or reproduced. Conversation-stateful `execute_code` is unchanged.

**Architecture:**
- **Two insert-only tables:**
  - `experiment_reruns`: one per admitted request, with the declared rule.
  - `experiment_rerun_attempts`: one terminal row per attempt, `UNIQUE(rerun_id, attempt)`.
- **Derived status.** An attempt with no terminal row is `running`, or `interrupted` once its lease expires.
- **One pure module**, `rerun_rules.py` (eligibility, rule validation, comparison), and **one service**, `rerun_service.py`.
- **One Celery task**, `src.tasks.research_run_tasks.execute_experiment_rerun`, enqueued with `enqueue_after_commit`, plus a sweep that marks expired leases `interrupted`.
- **One router**, `api/research_engine/reruns.py`, and **one ledger family**, `research_reproduction`.
- **Admission** reuses `require_run_conformance` against the run's retained plan, so a historical rerun goes through conformance instead of around it.
- **Execution** reuses GOO-312's `SandboxManager.run_isolated` in a stricter restore mode.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, Celery (`backend/src/tasks/celery_app.py`, `run_async` in `backend/src/tasks/_async_utils.py:74`, `enqueue_after_commit` in `backend/src/tasks/enqueue.py:36`), `e2b-code-interpreter==2.10.0`, the private artifact storage, the PostgreSQL integration fixture, openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):** This PR stacks on **GOO-312** (`b8e0c2d4f6a7`) → GOO-311 `a6c8e0b2d4f5` → GOO-310 → GOO-309 → `it02_merge_integration_heads`. It opens after GOO-312 merges.

### Consumed names

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Manifest (GOO-312) | `research_run_manifests(run_id UNIQUE, manifest, manifest_hash, completeness, missing)`, `manifest_rules.completeness`, `manifest_rules.parse_analyze_step` (GOO-312 plan, Tasks 1 and 4) | Only `complete` manifests are eligible. `missing` is reported as the ineligibility reason. |
| Archive (GOO-312) | `research_run_artifacts(role, name, sha256, byte_size, storage_key)`, `get_artifact_storage()` (`backend/src/services/artifacts/storage.py:132`) | The bytes are restored from here and re-hashed on read. |
| Fresh sandbox (GOO-312) | `SandboxManager.run_isolated`, `IsolatedSpec`, `IsolatedResult` (GOO-312 plan, Task 2) | It never touches `_sandboxes`, passes `envs={}` and kills in `finally`. |
| Conversation tool | `SandboxManager.get_or_create_sandbox`/`execute` (`backend/src/services/sandbox/e2b_sandbox_manager.py:93,125`), `_tool_execute_code` (`backend/src/services/agent/tools_impl.py:5024`) | Unchanged; guarded in Task 7. |
| Retained-plan conformance | `require_run_conformance(db, run, blueprint, context)` (`backend/src/services/research_engine/run_conformance.py:145`), which uses `_bound_version(new_run=False)` (`:61-100`; it accepts `approved` or `superseded`) | A superseded protocol still authorizes reruns of runs made under it. An override or a plan-hash drift refuses. |
| Run identity | `ResearchRun.status`, `conformance_status` (`backend/src/models/research_run.py:24-47`) | The parent must be `completed` and `conformant`. |
| Roles and lifecycle | `ResearchAction.REVIEW`, `resolve_project` (`backend/src/services/research_engine/project_access.py:28-48,186`), `lock_active_project` (`:166`) | REVIEW is an explicit decision role, and archived or deleted projects refuse. The worker re-checks the lifecycle. |
| Ledger | `lock_aggregate_stream` (`backend/src/services/research_decisions/ledger.py:1318`), `append_decision` (`:1321`), `replay_decisions` (`:1420`), `_FAMILIES` (`:2235`), `identity_service._replayed_event` (`backend/src/services/research_engine/identity_service.py:76`) | The append is caller-owned. |
| Stale-run sweep pattern | `sweep_stale_research_runs` (`backend/src/tasks/research_run_tasks.py:23`) | The same beat-driven shape is used for expired rerun leases. |
| Canonical hash | `contracts.canonical_json_sha256` (`backend/src/services/research_engine/contracts.py:38`) | The rule hash and the comparison hash. |
| Insert-only trigger | `prevent_research_insert_only_mutation()` | SQLSTATE `55000`. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| **The one workflow** | It is a GOO-312 run with exactly one `analyze` step whose manifest is `complete`: a Python script reading one CSV input, writing `figure.svg` (matplotlib, `rcParams["svg.hashsalt"]` fixed and `metadata={"Date": None}`), `metrics.json` and `table.csv`. Runs with zero or several `analyze` steps are ineligible with `unsupported_workflow_shape`. | The ticket asks for one workflow. This shape covers a manuscript figure, a table and numbers, and the SVG settings make byte equality achievable. `# ponytail: one analyze step per run; multi-step chains when a pilot needs one.` |
| **Comparison rule (declared before running)** | The admission body carries `rule = {"schema": "nous.rerun-rule/1", "outputs": [{"name", "mode": "bytes"} \| {"name", "mode": "json_numeric", "pointers": ["/pooled/estimate", …], "abs": float, "rel": float, "non_numeric": "exact"}]}`. **Every** manifest output must appear exactly once, otherwise 422 `rule_must_cover_every_output`. `rule_hash = canonical_json_sha256(rule)` is persisted on the admission row before the task is enqueued and is immutable. **Default** when the body omits it: `bytes` for every output. A `json_numeric` output compares the listed pointers with `abs(a−b) ≤ abs + rel·abs(b)`, and every other leaf is compared exactly. | "Declare byte equality or numerical tolerance rules BEFORE running." Hashing the rule on the admission row makes it impossible to loosen it after seeing the outputs. Covering every output means nothing passes silently. |
| Comparison output | `comparison = [{"name", "mode", "expected_sha256", "actual_sha256", "equal": bool, "numeric": [{"pointer", "expected", "actual", "abs_diff", "within": bool}] \| null, "reason": null \| "missing_output" \| "not_json" \| "pointer_missing"}]`, in manifest output order. `reproduced = all(row.equal or all(n.within))`. | "Persisted comparison lists every required output with expected/actual digest or numeric difference." |
| Executed vs reproduced | `status ∈ {restoration_failed, environment_unavailable, execution_failed, cancelled, interrupted, executed}` and `reproduction ∈ {reproduced, not_reproduced}`, which is non-NULL **iff** `status = 'executed'` (a CHECK). | "Separate successful execution from successful reproduction." |
| Fresh environment | `run_isolated(…, restore=True)`: a new sandbox from the manifest's `environment.template_id`, then `pip install --no-deps -r lock.txt` from the archived `environment` artifact (bytes re-hashed to `lock_sha256` first). Then `pip freeze --all` inside the sandbox must byte-equal the archived lock (`environment_lock_mismatch`), and `get_info().template_id` must equal the manifest's (`template_mismatch`). Inputs and code are written from archived bytes, after which `sha256sum` runs **inside** the sandbox and each digest must equal the manifest's (`input_mismatch:{name}`, `code_mismatch`). Every failure here is `restoration_failed` with its reason list. A `create` failure is `environment_unavailable`. | "Restore exact archived inputs, code and pinned dependencies/image." Checking inside the sandbox proves what the command actually saw. |
| Eligibility (structured) | `rerun_rules.eligibility(run, manifest, steps) -> list[reason]`, with these reasons: `no_manifest` (a legacy run, together with its `legacy_view.missing`), `manifest_incomplete:{path}` (each `missing` entry), `run_not_completed`, `run_not_conformant`, `unsupported_workflow_shape`, `artifact_missing:{name}` (a storage `exists()` false) and `artifact_corrupt:{name}` (a re-hash mismatch). Any reason gives 409 `{"detail": "Run is not eligible for rerun", "reasons": [...]}`. | "Legacy manifests lacking required inputs remain ineligible with reasons." |
| Admission and authorization | `POST …/runs/{run_id}/reruns` runs these steps, then commits **once**:<br>1. `resolve_project(REVIEW)`, which takes the Collection lock and refuses archived or deleted projects.<br>2. `require_run_conformance` on the retained plan.<br>3. `eligibility`, then rule validation.<br>4. Insert `experiment_reruns(run_id, manifest_id, manifest_hash, rule, rule_hash, requested_by_id, actor_role='reviewer', idempotency_key)`.<br>5. Append `rerun.admitted`.<br>6. Commit; the task is enqueued after commit. | "Admission/authorization binds the intended run and current project lifecycle." "Historical rerun needs an explicit authorized contract for a retained plan, not a bypass of run conformance": `require_run_conformance` is the contract and REVIEW is the authority. Ownership alone gives 403. |
| Attempts, leases and retries | The worker claims attempt `n` without a mutable row. Under `lock_aggregate_stream(research_reproduction, collection_id)` it checks that attempt `n` has not started, then appends a `rerun.attempt_started{attempt, lease_expires_at}` ledger event. The lease lives in that event, so no status column is needed. The terminal row is inserted with `UNIQUE(rerun_id, attempt)`. A late worker whose insert hits the unique violation (a cancel or interrupt row already exists) rolls back and **discards its outputs**: its blobs are compensating-deleted. `POST …/retry` (REVIEW) is allowed only when the latest attempt is terminal and not `executed`. It enqueues attempt `n+1` with the **same** `rule_hash`. | The terminal row is the only publication. Partial outputs never get a row, so "interrupt/retry/cancel cannot publish partial output as completed or reproduced". |
| Cancellation | `POST …/cancel` (REVIEW) inserts the `cancelled` terminal row for the current attempt if none exists (`200`, idempotent), then calls `sandbox.kill()` best-effort through the stored `sandbox_id`. | The unique constraint makes cancel-versus-finish a race with exactly one winner. |
| Interruption | `sweep_expired_reruns` (beat, every 60 s) inserts `interrupted` for an attempt whose lease expired without a terminal row. | Worker death surfaces as an explicit state, never as `running` forever. |
| Rerun outputs | The bytes are stored at `artifacts/{org}/research-reruns/{rerun_id}/{attempt}/{sha256}`. `outputs JSONB [{name, sha256, byte_size, storage_key}]` lives on the terminal row only, and `storage_key` stays out of DTOs. Downloads go through `GET …/reruns/{id}/attempts/{n}/outputs/{name}` (VIEW). | Retained and access-controlled, and never added to `research_run_artifacts` (the original run's archive stays the original). |
| Ledger family `research_reproduction` | Events: `rerun.admitted`, `rerun.attempt_started` and `rerun.attempt_finished`, each `actor_role` either `reviewer` (admission, cancel, retry) or `machine` (worker). | It is an explicit, versioned family, replayable. Reruns stale nothing, so there is no `_RELEASE_CAUSE_FAMILIES` entry. |
| Where GOO-315 reads it | `rerun_service.latest_reproduction(db, run_id) -> "reproduced" \| "not_reproduced" \| "not_attempted"` | It is a separate experiment-reproducibility result, never a release gate. |

**Migration head:** new revision `c0f2a4b6d8e9_create_experiment_reruns.py`, with `down_revision = "b8e0c2d4f6a7"` (GOO-312). The re-pointing rule is unchanged. The migration imports nothing from `src`.

**Tables:**
- **`experiment_reruns`:** `id, collection_id FK RESTRICT, run_id FK research_runs RESTRICT, manifest_id FK research_run_manifests RESTRICT, manifest_hash CHAR(64), rule JSONB, rule_hash CHAR(64), requested_by_id FK users, actor_role CHECK = 'reviewer', idempotency_key VARCHAR(255), created_at, UNIQUE(run_id, idempotency_key)`.
- **`experiment_rerun_attempts`:** `id, rerun_id FK RESTRICT, attempt SMALLINT CHECK >= 1, status VARCHAR(24) CHECK IN (...), reproduction VARCHAR(16) NULL CHECK IN ('reproduced','not_reproduced'), CHECK ((status = 'executed') = (reproduction IS NOT NULL)), environment_validation JSONB, input_validation JSONB, reasons JSONB, comparison JSONB NULL, comparison_hash CHAR(64) NULL, outputs JSONB, template_id VARCHAR(255) NULL, sandbox_id VARCHAR(255) NULL, started_at, finished_at, UNIQUE(rerun_id, attempt)`.

Both tables get `_deny_data_api` and the insert-only trigger. `downgrade()` refuses when any rows exist.

---

### Task 1: Pure rules (`rerun_rules.py`)

**Files:**
- Create `backend/src/services/research_engine/rerun_rules.py` (stdlib only).
- Create `backend/tests/unit/services/test_rerun_rules.py`.

```python
RULE_SCHEMA = "nous.rerun-rule/1"
def default_rule(manifest: Mapping) -> dict
def validate_rule(rule: Mapping, manifest: Mapping) -> dict          # ValueError: rule_must_cover_every_output, unknown_output, bad_tolerance
def rule_hash(rule: Mapping) -> str
def eligibility(*, run_status: str, conformance: str, manifest: Mapping | None, analyze_steps: int,
                artifacts: Mapping[str, tuple[bool, bool]]) -> list[str]   # name -> (exists, hash_ok)
def compare(rule: Mapping, manifest: Mapping, actual: Mapping[str, bytes]) -> tuple[list[dict], bool]
```

**Tests (they fail on the import):**
- `test_rule_must_cover_every_output`
- `test_rule_hash_changes_when_tolerance_changes`
- `test_bytes_mode_reports_both_digests`
- `test_json_numeric_within_and_outside_tolerance_reports_abs_diff`
- `test_json_numeric_non_listed_leaf_compared_exactly`
- `test_missing_output_not_reproduced_with_reason`
- `test_legacy_and_incomplete_manifest_ineligible_with_each_missing_path`
- `test_multi_analyze_run_unsupported_workflow_shape`

**Commit:** `feat(research): pure rerun eligibility and pre-declared comparison rules (GOO-313)`

---

### Task 2: Restore mode for isolated execution

**Files:**
- Modify GOO-312's `run_isolated` to accept `restore: RestoreSpec | None`, with `RestoreSpec(expected_template_id, lock_bytes, expected_sha256: dict[name, sha256])`. In restore mode, after `create` it:
  1. checks `get_info().template_id`;
  2. installs from `lock_bytes`;
  3. compares the in-sandbox `pip freeze --all` bytes with `lock_bytes`;
  4. writes the files, then runs `sha256sum` over `/work/in/*` and the code file and compares each digest.

  Any mismatch returns `IsolatedResult(status="restoration_failed", reasons=[...])` **without** running the command.
- Modify `backend/tests/unit/services/test_sandbox_isolated.py`.

**Tests:**
- `test_restore_template_mismatch_stops_before_command`
- `test_restore_lock_mismatch_stops_before_command`
- `test_restore_in_sandbox_input_hash_mismatch`
- `test_restore_success_runs_command_once`

**Commit:** `feat(sandbox): verified restore mode for isolated reruns (GOO-313)`

---

### Task 3: Models + migration

**Files:**
- Create `backend/src/models/research_rerun.py` (`ExperimentRerun`, `ExperimentRerunAttempt`) and export them.
- Create `backend/alembic/versions/c0f2a4b6d8e9_create_experiment_reruns.py`.
- Modify `test_screening_queue_postgres.py` (`_REBUILT_TABLES`): drop the two tables before GOO-312's.

**Check:** `check_alembic.py` reports single head `c0f2a4b6d8e9`, and `alembic upgrade head --sql | grep -c experiment_rerun_attempts` is > 0.
**Commit:** `feat(research): experiment rerun and attempt tables (GOO-313)`

---

### Task 4: Ledger

**Files:**
- Modify `ledger.py` to add the `research_reproduction` family.
- Modify `test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `rerun.admitted` | `collection_id, rerun_id, run_id, manifest_id, manifest_hash, rule_hash` | `reviewer` |
| `rerun.attempt_started` | `collection_id, rerun_id, attempt, lease_expires_at` | `machine` or `reviewer` (retry) |
| `rerun.attempt_finished` | `collection_id, rerun_id, attempt, status, reproduction, comparison_hash, output_sha256s, reasons` | `machine` or `reviewer` (cancel) |

**Replay rules:**
1. Every `collection_id` equals the `aggregate_id`.
2. Attempts start in order (1, 2, …) and each finishes at most once.
3. `reproduction` is non-null iff `status == "executed"`.
4. No attempt starts after an `executed` one.

**Tests:**
- `test_reproduction_replay_rejects_second_finish`
- `test_reproduction_replay_rejects_reproduced_without_executed`
- `test_reproduction_replay_rejects_attempt_after_executed`

**Commit:** `feat(research): research_reproduction decision family (GOO-313)`

---

### Task 5: Service + worker

**Files:**
- Create `backend/src/services/research_engine/rerun_service.py`.
- Modify `backend/src/tasks/research_run_tasks.py` to add `execute_experiment_rerun(rerun_id, attempt)` and `sweep_expired_reruns`, and register the beat entry next to `sweep_stale_research_runs`.

```python
LEASE_SECONDS = MAX_EXECUTION_TIMEOUT + 120
async def admit(db, context, actor_id, run_id, data: RerunCreate) -> tuple[RerunResponse, bool]   # REVIEW; commits once
async def execute_attempt(db_factory, rerun_id, attempt) -> None                                 # worker
async def cancel(db, context, actor_id, rerun_id) -> RerunResponse                               # REVIEW
async def retry(db, context, actor_id, rerun_id) -> RerunResponse                                # REVIEW
async def sweep_expired(db) -> int
async def latest_reproduction(db, run_id) -> str
async def get(db, context, rerun_id) -> RerunResponse                                            # VIEW; derived status
```

**`execute_attempt` order:**
1. Open a fresh session and run `lock_active_project`. An archived or deleted project inserts `cancelled` with reason `project_lifecycle`.
2. Append `attempt_started` and commit.
3. Load the archived bytes (re-hash each; a mismatch is `restoration_failed:artifact_corrupt`).
4. Call `run_isolated(restore=…)` outside any DB transaction.
5. Store the output blobs, then `compare`.
6. In a new transaction: insert the terminal row, append `attempt_finished`, commit. An `IntegrityError` on `(rerun_id, attempt)` triggers the rollback, compensating-deletes this attempt's blobs, and returns.

**Commit:** `feat(research): rerun admission, worker, cancel, retry and lease sweep (GOO-313)`

---

### Task 6: API + contracts

**Files:**
- Create `backend/src/api/research_engine/reruns.py` (prefix `/research-engine`) and register it.
- Add the schemas to `backend/src/schemas/research_engine.py`.
- Modify `audit_bundle.py` to add a `reproduction.json` part (rerun rows and attempts, no storage keys) before `_prisma`.
- Regenerate the OpenAPI spec and the types.

| Route | Action | Notes |
|---|---|---|
| `GET /research-engine/runs/{run_id}/rerun-eligibility` | VIEW | `{eligible, reasons, default_rule}`, zero writes. |
| `POST /research-engine/runs/{run_id}/reruns` | REVIEW | `RerunCreate{rule?, idempotency_key}`. Returns 202, 409 ineligible (with reasons), 403 without the role, 404 foreign. |
| `GET /research-engine/reruns/{rerun_id}` | VIEW | The rule, `rule_hash`, the attempts with derived status, the comparison and validation. |
| `POST /research-engine/reruns/{rerun_id}/cancel` | REVIEW | Idempotent. |
| `POST /research-engine/reruns/{rerun_id}/retry` | REVIEW | 409 unless the latest attempt is terminal and not `executed`. |
| `GET /research-engine/reruns/{rerun_id}/attempts/{attempt}/outputs/{name}` | VIEW | Bytes with `X-Content-SHA256`. |
| `GET /research-engine/reruns/{rerun_id}/comparison` | VIEW | A JSON attachment, `nous.rerun-comparison/1`. |

**oasdiff:** new operations only. Expected: no ERR.

**Tests** (`backend/tests/unit/api/test_rerun_routes.py`):
- `test_admit_owner_without_reviewer_role_403`
- `test_eligibility_zero_writes`
- `test_rule_cannot_change_on_retry`: the retry body is ignored and the stored `rule_hash` is reused.

**Commit:** `feat(research): rerun endpoints, comparison export and bundle part (GOO-313)`

---

### Task 7: Structural guard

**Files:**
- Create `backend/tests/unit/architecture/test_rerun_boundary.py`, an AST scan with three checks:
  - **(a)** `rerun_service` never calls `get_or_create_sandbox` or `execute(` on the manager.
  - **(b)** `_tool_execute_code`'s body is unchanged: compare its AST dump with a committed fixture string.
  - **(c)** `rerun_service.admit` calls `require_run_conformance` (the call is present in its AST).

**Commit:** `test(research): guard fresh-rerun isolation and retained-plan conformance (GOO-313)`

---

### Task 8: PostgreSQL proof (one test)

**Files:**
- Create `backend/tests/integration/test_experiment_rerun_postgres.py` with the integration and `requires_postgres` markers. It reuses GOO-312's seed (a completed run with a complete manifest) and calls `execute_attempt` directly with a fake sandbox whose restore mode really hashes the files it is given.

**`test_rerun_reproduces_and_every_failure_is_explicit`** runs these steps in order:
1. **Reproduced:** admit with a rule of `bytes` for SVG and CSV and `json_numeric` (abs 1e-12) for `/pooled/estimate`.
   - The attempt is `executed`/`reproduced`.
   - `comparison` has three rows with expected and actual digests, plus the numeric `abs_diff`.
   - The output bytes download and re-hash.
   - The row's `manifest_hash` and `rule_hash` match the admission.
2. **Mismatch:** a second rerun whose fake sandbox perturbs `/pooled/estimate` by 1e-6 ends `executed`/`not_reproduced`, with `within: false` and the actual difference recorded.
3. **Failed restoration:** corrupt one archived input blob in storage. Eligibility reports `artifact_corrupt:data.csv`. Admission gives 409. A rerun admitted before the corruption then ends `restoration_failed` with that reason and has no outputs.
4. **Environment unavailable:** `create` raises, giving `environment_unavailable`.
5. **Cancel race:** start an attempt, then cancel (a `cancelled` row). The worker's later terminal insert hits the unique violation: still exactly one row, `cancelled`, with no outputs and its blobs deleted.
6. **Interrupt and retry:**
   - An attempt with `attempt_started` and an expired lease is swept to `interrupted`.
   - Retry creates attempt 2 with the same `rule_hash`, and it ends `reproduced`.
   - A further retry gives 409.
7. **Ineligible legacy:** a v1-only run gives 409 with `no_manifest`. An incomplete manifest lists each `manifest_incomplete:{path}`.
8. **Authorization and lifecycle:** the owner with no roles gets 403, a foreign user gets 404. Archiving between admission and execution makes the attempt end `cancelled` with `project_lifecycle`.
9. **Conformance:** supersede the protocol, and admission still succeeds (retained plan). Tamper with `effective_plan_hash` on a copy run, and admission gives 409 `Run execution plan does not match its approved protocol`.
10. **Insert-only and replay:** UPDATE or DELETE raises `55000`, and `research_reproduction` replays.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_experiment_rerun_postgres.py`. Without a database, report it as **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for reproduction, mismatch, restoration, cancel and retry (GOO-313)`

---

### Task 9: Frontend

**Files:**
- Create `frontend/src/types/api/research-rerun-contract.ts`.
- Modify `researchEngineService.ts` to add `getRerunEligibility`, `admitRerun`, `getRerun`, `cancelRerun`, `retryRerun` and `downloadRerunComparison`.
- Modify `RunView.tsx`: a "Rerun from fresh environment" section shows the eligibility reasons as labels, the declared rule as an editable per-output table (bytes or numeric with tolerances), and the admission button (REVIEW). Each attempt shows **Executed** and **Reproduced** as separate badges, plus a per-output comparison table and Cancel and Retry actions.
- Tests:
  - `ineligible reasons listed and admit disabled`
  - `executed but not reproduced shows both badges`
  - `rule table requires every output`

**Commit:** `feat(frontend): fresh rerun admission, attempts and comparison (GOO-313)`

---

## Mutation verification

Follow `docs/engineering/testing.md`. Record each check under a GOO-313 section in `docs/testing/agent-orchestration-mutation-checks.md`.

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| Rule covers every output | skip the check | `pytest -q backend/tests/unit/services/test_rerun_rules.py -k cover` | a partial rule accepted |
| In-sandbox input re-hash | skip it | `pytest -q backend/tests/unit/services/test_sandbox_isolated.py -k input_hash` | the command runs |
| `UNIQUE(rerun_id, attempt)` publication | catch and ignore the IntegrityError and keep the outputs | `pytest -q backend/tests/integration/test_experiment_rerun_postgres.py` | step 5: outputs exist for a cancelled attempt |
| Lease sweep | make it a no-op | same | step 6: no `interrupted` row |
| `require_run_conformance` at admission | remove the call | same | step 9: the tampered run is admitted |
| Reproduction CHECK | drop the CHECK in a scratch migration | same | step 3: `restoration_failed` with `reproduced` accepted |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_rerun_rules.py backend/tests/unit/services/test_sandbox_isolated.py backend/tests/unit/services/test_research_decision_ledger.py
pytest -q backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # single head c0f2a4b6d8e9
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research-engine
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists.** Report each of these as NOT RUN, never as passing:
- **PostgreSQL proof:** needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Live E2B original run plus fresh rerun:** needs `E2B_API_KEY` and authorization to spend sandbox minutes. The ticket says mocks alone cannot close it, so the integration test's fake sandbox proves the state machine only.
- **Celery worker restart during an attempt** on a real broker: needs a running worker and Redis/Valkey.
- **Live journey:** needs a deployed stack with `c0f2a4b6d8e9`.

## Authenticated journey list for Linear closure

Run this on dev after the deploy, with E2B credentials authorized.

1. **Original:** the GOO-312 journey run. Keep the manifest id, `manifest_hash`, template id, lock hash, code hash and input hashes.
2. **Fresh rerun:** as a reviewer, declare bytes for SVG and CSV and numeric 1e-12 for metrics, then admit. Keep the rule hash, the attempt's sandbox id (different from the original), the outputs and `comparison.json`. Expect `Executed` and `Reproduced`.
3. **Failed restoration:** rerun a copy whose archived lock was replaced in a scratch project. Expect `restoration_failed: environment_lock_mismatch`.
4. **Mismatch:** rerun a script variant that adds 1e-6 to the metric (a separate approved plan). Expect `not_reproduced` with the difference shown.
5. **Cancel and retry:** cancel mid-attempt, then retry. Keep both attempts.
6. **Deny:** the owner with no roles gets 403, and an archived project gives 409.

Record the SHA/PR, CI links, all ids and digests, both comparisons, the PostgreSQL junit, the mutation transcripts and the NOT RUN list.

## Seams

- **Upstream (GOO-312):** reads the manifest and archive only, and never edits a manifest.
- **GOO-315:** `latest_reproduction(run_id)` feeds the separate experiment-reproducibility result for figures in a release. It never blocks promotion unless a later policy says so.
- **Later:** multi-step workflows, other languages and tolerance rules for images (perceptual diffs) are new rule schema versions.

## Out of scope

Each item below has a `ponytail:` marker at its seam:
- perceptual image comparison;
- more than one `analyze` step per run;
- reruns under a different template ("portability");
- scheduled reruns;
- network-isolated sandboxes (E2B needs internet to install the lock);
- re-executing non-`analyze` LLM steps.
