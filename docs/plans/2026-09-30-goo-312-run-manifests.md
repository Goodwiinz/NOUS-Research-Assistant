# GOO-312 Experiment and Figure Lineage on Run Manifests Plan (Academic R6)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A computational experiment runs as one step of an existing, protocol-bound `ResearchRun`, so the run registry stays single. When the run reaches a terminal state, one versioned manifest (`nous.run-manifest/2`) is persisted. It binds:
- the dataset inputs and their checksums;
- the code bytes and their hash;
- the sandbox environment identity;
- the effective parameters and seed;
- start, end, status and deviations;
- the output hashes and declared metrics;
- the hypothesis (question version) and protocol version.

Every input, code, environment and output file is retained as private bytes, addressed by sha256. A figure or table record points at one exact output of one exact run. A manuscript claim cites that figure through a new GOO-306 link kind, `figure`, so the chain output → run → code/environment/data → hypothesis/protocol resolves from the manuscript. Missing fields are listed explicitly and block a "complete" label. Legacy runs stay readable as `incomplete` and never receive invented hashes or environment data.

**Architecture:**
- **One new blueprint step type**, `analyze`. Its code, command, parameters, seed, declared inputs, declared outputs and pinned dependency list live in the step params, so they are inside the approved protocol's `execution_plan` and covered by `effective_plan_hash`. The existing rejection of methodological overrides (`run_conformance.create_approved_run`, `require_run_conformance`) applies unchanged.
- **One new sandbox method**, `SandboxManager.run_isolated(spec)`. It creates a throwaway E2B sandbox outside the per-thread cache, installs only pinned packages, writes inputs, runs the command, reads the declared outputs, captures the environment lock and kills the sandbox. The conversation tool path (`get_or_create_sandbox`, `_tool_execute_code`) is not touched.
- **Three insert-only tables:** `research_run_manifests` (one per run), `research_run_artifacts` (retained files) and `research_figures` (versioned figure/table records). Plus one additive column, `research_claim_evidence_links.figure_id`, with the claim-link CHECKs rewritten for a fifth kind.
- **One pure module**, `manifest_rules.py` (manifest assembly, completeness, hashes). **One service**, `experiment_service.py`. **One router**, `api/research_engine/experiments.py`. **One ledger family**, `research_experiment`, plus `claim.linked` schema v3.
- **Staleness** extends GOO-307's `_graph` with `experiment_service.graph_part`. A superseded figure stales only the releases whose claims cite it, through the existing `draft_release_service.invalidate_dependents`.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, `e2b-code-interpreter==2.10.0` (`backend/requirements.txt:134`; the local venv has 2.7.0 with e2b 2.30.0, so check the four calls named below against the pinned version in Task 2), the private artifact storage (`backend/src/services/artifacts/storage.py`), the PostgreSQL integration fixture (`postgres_container`, `backend/tests/integration/conftest.py:208`), openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):** This PR stacks on **GOO-311** (`a6c8e0b2d4f5`, being implemented now) → GOO-310 `f4b6d8a0c2e3` → GOO-309 `e2a4c6b8d0f1` → `it02_merge_integration_heads` on `develop`. It opens after GOO-311 merges. Line numbers are for `c935dde6e` (`feat/goo-310-outcome-certainty`) plus the GOO-311 plan, so re-locate them after each rebase.

### Consumed names

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Run registry | `ResearchRun` (`backend/src/models/research_run.py:24-73`): `protocol_version_id`, `effective_plan_hash`, `conformance_status` (default `legacy_unbound`), `status`, `started_at`, `completed_at`, `reproducibility_manifest` JSONB | It stays the only run record. No column is added. |
| Step rows | `ResearchStep` (`backend/src/models/research_step.py:30-64`): `inputs_hash`, `outputs_hash`, `seed`, `output` | The `analyze` step persists like every other step. |
| Plan binding | `run_conformance.effective_plan_hash` (`backend/src/services/research_engine/run_conformance.py:23`), `create_approved_run` (`:103`, override rejection at `:116-119` and `:128-129`), `require_run_conformance` (`:145`) | Code and parameters inside the approved plan are hash-bound. A non-empty `parameters_override` is refused. |
| Run completion | `stream_run`'s `run_complete` branch (`backend/src/api/research_engine/runs.py:1061-1087`), which locks with `db.refresh(run, with_for_update=True)` and commits once; the existing legacy manifest route `get_manifest` (`:519-534`); `_PAUSE_REQUESTED_KEY` (`:96-107`) | The v2 manifest is written in the same transaction as the terminal run status. The legacy route and the pause keys stay byte-identical. |
| Step dispatch | `StepExecutor.execute` (`backend/src/services/research_engine/step_executor.py:200-225`), `_LEGACY_HANDLER_NAMES` (`:176-185`), `StepType` (`backend/src/schemas/research_engine.py:90-98`), `validate_blueprint_runtime` (`:65`), `MAX_NESTED_PAYLOAD_BYTES = 32 KiB` (`:15`) | `analyze` is dispatched before the contract-stage branch. Its params are bounded by the existing payload limit. |
| Step export | `ExportService._provenance` (`backend/src/services/research_engine/export_service.py:533-571`) and its `stage_hashes` | The `analyze` output is a dict, so its stage hash joins `stage_hashes` with no format change. |
| Sandbox | `SandboxManager` (`backend/src/services/sandbox/e2b_sandbox_manager.py:78`), `is_available` (`:89`), `get_or_create_sandbox` (`:93`), `DEFAULT_PACKAGES` (`:59-66`), `MAX_EXECUTION_TIMEOUT` (`:72`), `_cap_log` (`:44`); e2b `AsyncSandbox.create(template=…, timeout=…, envs=…)`, `sandbox.files.write`, `sandbox.files.read(path, format="bytes")`, `sandbox.commands.run`, `sandbox.get_info().template_id`, `sandbox.kill()` | The thread cache is never used by `run_isolated`. |
| Conversation tool | `_tool_execute_code` (`backend/src/services/agent/tools_impl.py:5024`) | Not touched; guarded in Task 8. |
| Hypothesis | `ResearchQuestionVersion.hypothesis`, `content_hash` (`backend/src/models/research_protocol.py:38-64`); `ResearchProtocolVersion.question_version_id`, `content_hash` (`:97`) | The manifest binds `question_version_id` and `sha256(hypothesis)`. |
| Deviations | `ProtocolDeviation(run_id, output_reference, disposition)` (`backend/src/models/research_protocol.py:138-163`) | The manifest lists the ids of deviations recorded for the run. |
| Dataset inputs | `Document.checksum_sha256`, `storage_path`, `storage_backend`, `organization_id` (`backend/src/models/document.py:64-66,122`); GOO-310 `evidence_table_versions.content_hash` (`backend/src/models/research_evidence_table.py:46`) | Inputs are org-scoped documents or frozen table versions. |
| Private bytes | `get_artifact_storage()` and `ArtifactStorage.put/get/exists/delete` (`backend/src/services/artifacts/storage.py:14-22,132`) | Keys stay under `artifacts/{org}/…`, and nothing produces a public or expiring URL. |
| Claim links (GOO-306/311) | `ResearchClaimEvidenceLink` (`backend/src/models/research_claim.py:163`), `LINK_KIND_CHECK`/`LINK_SHAPE_CHECK` (`:36-51`, rewritten by GOO-311 for `synthesis_result`), `claim_rules.LINK_KINDS`/`check_link_shape`, `claims_service.link` (`backend/src/services/research/claims_service.py:656`), `_check_request_shape` (`:553`), `observe_stance` (`:803`) | Link shapes are mirrored in the pure rules. |
| Release gate (GOO-307) | `release_rules.ANCHORED_LINK_KINDS` (`backend/src/services/research/release_rules.py:34`), `node` (`:56`), `dependents` (`:146`); `draft_release_service._graph` (`backend/src/services/research/draft_release_service.py:129`, part loop at `:188-196`), `invalidate_dependents` (`:669`); `ledger._RELEASE_CAUSE_FAMILIES` (`backend/src/services/research_decisions/ledger.py:320`) | One walk. Invalidation runs in the caller's transaction and never commits. |
| GOO-311 seam | `synthesis_service.graph_part`, `claim.linked` schema 2, kind `synthesis_result` (GOO-311 plan, Tasks 4-5) | This plan adds schema 3 and kind `figure` on top. |
| Ledger | `append_decision` (`ledger.py:1321`), `replay_decisions` (`:1420`), `_FAMILIES` (`:2235`), `lock_aggregate_stream` (`:1318`), `identity_service._replayed_event` (`backend/src/services/research_engine/identity_service.py:76`) | The append is caller-owned. |
| Roles | `ResearchAction.VIEW/EDIT`, `resolve_project` (`backend/src/services/research_engine/project_access.py:28-48,186`) | Roles are reloaded after the Collection lock. Archived projects are read-only. |
| Canonical hash | `contracts.canonical_json_bytes`/`canonical_json_sha256` (`backend/src/services/research_engine/contracts.py:27,38`) | The download bytes are exactly the hashed bytes. |
| Insert-only trigger | `prevent_research_insert_only_mutation()` (created in `backend/alembic/versions/e2a4c6b8d0f1_create_appraisal_assessments.py`) | SQLSTATE `55000` on UPDATE or DELETE. |
| Bundle | `audit_bundle._sealed_part`, `gather_parts` (`backend/src/services/research_engine/audit_bundle.py:86,315`) | One reader per part. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| **Where an experiment lives** | As a step of type **`analyze`** inside a normal `ResearchRun`. Step params: `{code, code_sha256, command, parameters, seed, inputs: [{name, kind: "document"\|"evidence_table_version", id, sha256}], outputs: [{name, media_type, role: "figure"\|"table"\|"metrics"\|"data"}], requirements: ["pkg==x.y.z", …], template}`. Everything is in the approved plan. | "Do not invent a parallel run registry." Putting the code inside the plan means `effective_plan_hash` covers it, so a changed script needs a protocol amendment, exactly like any other method change. |
| Execution environment | `SandboxManager.run_isolated(spec)`: `AsyncSandbox.create(template=spec.template, timeout=…, envs={})`, never via `_sandboxes`. It runs `pip install --no-deps` over the **pinned** list (validation refuses any entry without `==`), writes inputs under `/work/in/`, runs the command in `/work`, reads `/work/out/<name>`, and captures `pip freeze --all`, `python -VV` and `/etc/os-release`. It kills the sandbox in `finally`. `DEFAULT_PACKAGES` (unpinned) is not installed. | An environment that kept earlier variables, or installed whatever version pip picked that day, cannot be described honestly. The thread path stays stateful for current users. |
| Environment identity | `environment = {provider: "e2b", template_id, sandbox_id, python, os_release_sha256, lock_sha256, lock_artifact_id, image_digest: {value: null, reason: "provider_exposes_no_digest"}}`. Completeness needs `template_id` and `lock_sha256`, not `image_digest`. | E2B exposes a template id but no content digest. Recording `null` with a reason is the truthful answer. The pinned lock plus the template id is the identity a rerun (GOO-313) can restore and check. |
| Code identity | `code = {sha256, artifact_id, commit: null, repository: null}`. If the step params carry `repository` and `commit`, they are recorded as **declared, unfetched** (`commit_verified: false`). | The bytes that ran are the identity. A git commit the server never fetched cannot be claimed as verified. |
| Secrets | `envs={}` always. The environment capture is limited to those three command outputs. `manifest_rules.assert_no_secrets(manifest)` rejects any string matching the `E2B_API_KEY` value or the patterns `(?i)(api[_-]?key\|secret\|token\|password)=`, and that rejection fails the run with `manifest_secret_detected`. | "No secrets in environment captures." The rejection is enforced, not trusted. |
| Retained artifacts | `research_run_artifacts(role IN ('input','code','environment','output'))`. The bytes are stored at `artifacts/{org}/research-runs/{run_id}/{sha256}` before the DB commit. On a failed commit, the service compensating-deletes the keys it wrote (the upload gotcha in `docs/engineering/gotchas.md`, "Storage"). `storage_key` never appears in a DTO. Downloads stream through `GET …/artifacts/{id}` (VIEW) with an `X-Content-SHA256` header. | "Retained artifact locations with access controls" and "no expiring private URLs in manifests". The manifest carries ids and hashes, never locations. |
| Manifest row | `research_run_manifests`: `id, run_id UNIQUE FK RESTRICT, collection_id, schema_version SMALLINT CHECK = 2, manifest JSONB, manifest_hash CHAR(64), completeness CHECK IN ('complete','incomplete'), missing JSONB, created_at`. There is one row per run, written once at terminal status (completed **or** failed), in the same transaction as the status change. | Insert-only. A different experiment is a new run, so no manifest is ever edited. The terminal status and the manifest commit or roll back together. |
| Manifest content | `manifest_rules.build` returns `{schema: "nous.run-manifest/2", run_id, protocol_version_id, protocol_content_hash, question_version_id, hypothesis_sha256, effective_plan_hash, blueprint_id, blueprint_version, step_index, command, parameters, seed, code, environment, inputs: [{name, kind, ref_id, sha256, byte_size, artifact_id}], outputs: [{name, role, media_type, sha256, byte_size, artifact_id}], metrics, started_at, completed_at, status, deviation_ids}`. `metrics` is the parsed `role: "metrics"` JSON output (≤ 32 KiB) or `null`. `manifest_hash = canonical_json_sha256(manifest)`. | All exact identities, digests and timestamps sit in one hashed document, and the downloaded bytes re-hash to `manifest_hash`. |
| Completeness (derived, never asserted) | `manifest_rules.completeness(manifest) -> (state, missing)`. `missing` lists the JSON paths that are `null` or absent among `code.sha256, environment.template_id, environment.lock_sha256, inputs[*].sha256, inputs[*].artifact_id, outputs[*].sha256, outputs[*].artifact_id, seed, protocol_version_id, question_version_id, started_at, completed_at`, plus `status != "completed"`. Any entry makes the state `incomplete`. UI, export and GOO-315 render "Reproducibility: incomplete (missing: …)" and never "complete" unless the state is `complete`. | "Missing/unknown fields explicit and block any complete reproducibility claim." |
| Legacy runs | No manifest row. `GET /runs/{id}/manifest/v2` returns `{schema: "nous.run-manifest/1", completeness: "incomplete", missing: ["schema_version<2"], legacy: <the existing get_manifest body>}`, with no hashes or environment fields synthesized. `GET /runs/{id}/manifest` stays byte-identical. | "Legacy runs remain readable as incomplete and never receive invented hashes/environment data." |
| Inputs | `document` inputs are loaded org-scoped. The bytes come from storage, and `sha256(bytes)` must equal both the plan's `sha256` and `Document.checksum_sha256`, otherwise the step fails with `input_checksum_mismatch`. `evidence_table_version` inputs are serialized with `canonical_json_bytes(rows)`, and the table's `content_hash` must equal the plan's. An external URL is not an accepted input kind in v1; it must be uploaded as a document first. | Inputs are exact archived bytes. `# ponytail: no URL fetch; add a fetch-and-archive input kind when a pilot needs one.` |
| Figure record | `research_figures`: `id, collection_id, figure_key VARCHAR(64), kind CHECK IN ('figure','table'), caption TEXT, output_artifact_id FK research_run_artifacts RESTRICT, run_id FK RESTRICT, manifest_id FK RESTRICT, supersedes_figure_id FK self UNIQUE NULL, created_by_id, actor_role CHECK = 'editor', created_at`. A partial unique `uq_research_figures_initial (collection_id, figure_key) WHERE supersedes_figure_id IS NULL`. Only `role IN ('figure','table')` outputs of a run with a manifest row can be registered. | Versioned, insert-only, and pointing at one exact output. Re-registering a key with a new output creates a successor and keeps the old bytes. |
| Manuscript binding | A new GOO-306 link kind, **`figure`**: `research_claim_evidence_links.figure_id` FK RESTRICT NULL. `LINK_KIND_CHECK` gains `'figure'`, and `LINK_SHAPE_CHECK` gains `OR (kind = 'figure' AND figure_id IS NOT NULL AND <every other target and span column> IS NULL AND synthesis_result_id IS NULL)`, while every existing branch adds `AND figure_id IS NULL`. Only a non-stale figure tip can be linked (otherwise 409 `Figure is not current`). `observe_stance` gives 422 `Figure links carry no model stance`. `ANCHORED_LINK_KINDS` adds `figure`. | This is the same mechanism GOO-311 uses for `synthesis_result`, and it lets GOO-315 resolve a release's figures from its claims. |
| Staleness (reusing GOO-307's walk) | `experiment_service.graph_part` adds `("run_input", f"document:{id}:{sha256}") → ("run", r)`, `("evidence_table", t) → ("run", r)`, `("run", r) → ("figure", f)` and `("figure", f) → ("link", l)` for each `figure` link. `changed` adds superseded figures, plus document inputs whose current `checksum_sha256` differs or whose document is deleted. Registering a successor figure calls `invalidate_dependents(changed={("figure", old)}, cause={"family": "research_experiment", …})` before its commit. `_RELEASE_CAUSE_FAMILIES` adds `research_experiment`. | "Stale only dependent figures/claims." A source change is derived on read (the GOO-307 "unwritten changes" rule). No protocol edge is added, so an amendment does not stale finished experiments. |
| Who acts | The manifest is written by the run (`actor_role = 'machine'`), and starting the run keeps today's EDIT plus approved-protocol rule. Figure registration and linking are EDIT (`editor`), because authoring is not an adjudication. | No new authority is invented. |

**Migration head:** new revision `b8e0c2d4f6a7_create_run_manifests.py`, with `down_revision = "a6c8e0b2d4f5"` (GOO-311). **Re-pointing rule:** `down_revision` always names the direct stack parent. After any rebase, set it to the single head that `(cd backend && python ../scripts/ci/check_alembic.py)` reports, and never add a merge revision. The migration imports nothing from `src` and copies `_deny_data_api`.

**`upgrade()`** does the following:
1. Creates `research_run_manifests`, `research_run_artifacts` and `research_figures`, each with `_deny_data_api` and an insert-only trigger on `prevent_research_insert_only_mutation()`.
2. `op.add_column('research_claim_evidence_links', figure_id UUID NULL FK research_figures RESTRICT)`.
3. Drops and recreates `ck_research_claim_links_kind` and `ck_research_claim_links_shape` with the frozen strings. Existing rows satisfy them because the column is NULL.

**`downgrade()`** raises `RuntimeError` if any link has `kind = 'figure'` or any manifest row exists ("never silently drop evidence"). Otherwise it restores GOO-311's CHECKs and drops the column and the three tables.

---

### Task 1: Pure rules (`manifest_rules.py`)

**Files:**
- Create `backend/src/services/research_engine/manifest_rules.py` (stdlib only: `hashlib`, `json`, `re`, `dataclasses`).
- Create `backend/tests/unit/services/test_manifest_rules.py`.

```python
SCHEMA = "nous.run-manifest/2"; SCHEMA_VERSION = 2
REQUIRED_PATHS = ("code.sha256", "environment.template_id", "environment.lock_sha256", "seed",
                  "protocol_version_id", "question_version_id", "started_at", "completed_at")
@dataclass(frozen=True) class AnalyzeSpec: code: str; code_sha256: str; command: str; parameters: dict; seed: int | None
    inputs: tuple[dict, ...]; outputs: tuple[dict, ...]; requirements: tuple[str, ...]; template: str
def parse_analyze_step(params: Mapping) -> AnalyzeSpec            # ValueError with a stable code
def build(*, run, protocol, question, spec, code, environment, inputs, outputs, metrics, status, deviation_ids) -> dict
def completeness(manifest: Mapping) -> tuple[Literal["complete", "incomplete"], list[str]]
def legacy_view(legacy: Mapping | None) -> dict
def assert_no_secrets(manifest: Mapping, *, secret_values: Iterable[str]) -> None
def manifest_hash(manifest: Mapping) -> str                        # canonical_json_sha256
```

`parse_analyze_step` raises these codes: `unpinned_requirement`, `code_hash_mismatch` (`sha256(code) != code_sha256`), `duplicate_output_name`, `unsafe_path` (`/`, `..` or a leading dot in a name), `unsupported_input_kind`, and `missing_seed` when `parameters` names a stochastic library but `seed` is null. `# ponytail: seed rule is a declared-field check, not static analysis.`

**Tests (they fail on the import):**
- `test_parse_rejects_unpinned_requirement`
- `test_parse_rejects_code_hash_mismatch_and_unsafe_names`
- `test_build_is_canonical_and_hash_stable`: the same inputs give the same `manifest_hash`, and one changed output byte gives a different one.
- `test_completeness_lists_every_missing_path`
- `test_failed_run_is_incomplete_even_with_all_hashes`
- `test_legacy_view_invents_nothing`: the result has no `code`, `environment`, `inputs` or `outputs` keys.
- `test_image_digest_null_does_not_block_completeness`
- `test_assert_no_secrets_rejects_key_value_and_patterns`

**Run:** `pytest -q backend/tests/unit/services/test_manifest_rules.py`
**Commit:** `feat(research): pure run-manifest v2 rules (GOO-312)`

---

### Task 2: Isolated sandbox execution

**Files:**
- Modify `backend/src/services/sandbox/e2b_sandbox_manager.py` to add `IsolatedSpec`, `IsolatedResult` and `async def run_isolated(self, spec) -> IsolatedResult`. Existing methods are untouched.
- Create `backend/tests/unit/services/test_sandbox_isolated.py` with a fake `AsyncSandbox` class patched in.

```python
@dataclass(frozen=True) class IsolatedSpec: template: str; requirements: tuple[str, ...]; files: Mapping[str, bytes]
    command: str; output_names: tuple[str, ...]; timeout: int = MAX_EXECUTION_TIMEOUT
@dataclass(frozen=True) class IsolatedResult: status: Literal["completed","failed","timeout","unavailable"]
    outputs: dict[str, bytes]; stdout: str; stderr: str; template_id: str | None; sandbox_id: str | None
    lock: bytes | None; python: str | None; os_release: bytes | None; started_at: datetime; completed_at: datetime
```

**Behaviour:**
- No E2B, or `create` raises: `unavailable`.
- A non-zero exit: `failed`, with logs capped through `_cap_log`.
- A missing declared output: `failed` with `missing_output:{name}`.
- `kill()` always runs, in `finally`.
- **Before coding**, confirm `create(template=…, envs=…)`, `files.write`, `files.read(format="bytes")`, `commands.run`, `get_info().template_id` and `kill` against `e2b-code-interpreter==2.10.0`. If the local 2.7.0 venv differs, follow the pinned version.

**Tests:**
- `test_run_isolated_never_touches_thread_cache`: `_sandboxes` stays empty and `get_or_create_sandbox` is not called.
- `test_run_isolated_passes_empty_envs_and_pinned_install_only`
- `test_run_isolated_kills_on_exception_and_timeout`
- `test_missing_output_fails`
- `test_unavailable_without_key`

**Commit:** `feat(sandbox): isolated one-shot E2B execution outside the thread cache (GOO-312)`

---

### Task 3: The `analyze` step

**Files:**
- Modify `backend/src/schemas/research_engine.py:90-98` to add `StepType.ANALYZE = "analyze"`. `validate_blueprint_runtime` (`:65`) calls `manifest_rules.parse_analyze_step` for `analyze` steps, so a bad plan fails at blueprint and protocol time, not mid-run.
- Modify `step_executor.py:176-225`: `"analyze": "_execute_analyze"` is dispatched before the contract-version branch. The new `_execute_analyze` loads the inputs (org-scoped, checksum-verified), calls `run_isolated`, writes every byte blob to artifact storage (content-addressed) and returns a `StepResult` whose `output` is `{stage_type: "analyze", status, outputs: [{name, role, media_type, sha256, byte_size, storage_key}], inputs: [...], code: {...}, environment: {...}, metrics}`. `storage_key` is used only server-side and is dropped from every response model.
- A `failed` or `unavailable` result raises the existing step error path, so pause and `step_error` semantics are unchanged.

**Tests** (`backend/tests/unit/services/test_step_executor_analyze.py`):
- `test_analyze_bad_plan_rejected_at_validation`
- `test_analyze_input_checksum_mismatch_fails_step`
- `test_analyze_output_stage_hash_joins_provenance`: `ExportService._provenance` includes the step index with no format change.
- `test_existing_step_types_dispatch_unchanged`

**oasdiff:** `StepType` widens in request and response bodies. A widened response enum may report ERR. In that case apply `api-breaking-approved` with "additive step type; existing clients never receive it unless they create an analyze step" (`docs/engineering/api-contracts.md`, Escape hatch).

**Commit:** `feat(research): analyze blueprint step bound to the approved plan (GOO-312)`

---

### Task 4: Models + migration

**Files:**
- Create `backend/src/models/research_experiment.py` (`ResearchRunManifest`, `ResearchRunArtifact`, `ResearchFigure`) and export them from `backend/src/models/__init__.py`.
- Modify `backend/src/models/research_claim.py:36-51,163-203` (the CHECK strings and `figure_id`) and `claim_rules.py` (`LINK_KINDS`, plus a `check_link_shape` branch with keyword-only `figure_id=None`).
- Create `backend/alembic/versions/b8e0c2d4f6a7_create_run_manifests.py`.
- Modify `backend/tests/integration/test_screening_queue_postgres.py` (`_REBUILT_TABLES` at `:81`, `_upgrade` at `:125`): drop `research_figures`, `research_run_artifacts` and `research_run_manifests` after `research_claim_evidence_links`.

**`research_run_artifacts`:**
- `id, run_id FK RESTRICT, collection_id FK RESTRICT, organization_id FK`
- `role VARCHAR(16) CHECK IN ('input','code','environment','output')`, `name VARCHAR(255)`, `media_type VARCHAR(255)`
- `sha256 CHAR(64)`, `byte_size BIGINT CHECK >= 0`, `storage_key VARCHAR(512)`
- `source_ref JSONB` (`{kind, id}` for inputs), `created_at`
- `UNIQUE(run_id, role, name)`

**Check:**
- `check_alembic.py` reports single head `b8e0c2d4f6a7`.
- `alembic upgrade head --sql | grep -c research_run_manifests` is > 0.
- `pytest -q backend/tests/unit/services/test_claim_rules.py`: existing shapes are unchanged, and `test_figure_link_shape` is added.

**Commit:** `feat(research): run manifest, artifact and figure tables plus figure link kind (GOO-312)`

---

### Task 5: Ledger

**Files:**
- Modify `ledger.py`: add the `research_experiment` family (subject type `experiment`), `("claim.linked", 3)` = the v2 keys ∪ `{figure_id}`, and `research_experiment` in `_RELEASE_CAUSE_FAMILIES` (`:320`).
- Modify `claims_service.link` so it writes schema 3. v1 and v2 events keep replaying.
- Modify `test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `run.manifest_recorded` | `collection_id, run_id, manifest_id, manifest_hash, completeness, missing, status, output_artifact_ids` | `machine` |
| `figure.registered` | `collection_id, figure_id, figure_key, kind, output_artifact_id, run_id, manifest_id, supersedes_figure_id` | `editor` |

**Replay rules:**
1. Every `collection_id` equals the `aggregate_id`.
2. A run has at most one `manifest_recorded`.
3. A figure names a manifest recorded earlier in the stream, and each `figure_key` has one tip.

**Tests:**
- `test_experiment_replay_rejects_second_manifest_for_run`
- `test_figure_replay_requires_prior_manifest`
- `test_claim_linked_v3_requires_figure_id_for_figure_kind`

**Commit:** `feat(research): research_experiment decision family and claim.linked v3 (GOO-312)`

---

### Task 6: Service + graph part + run completion hook

**Files:**
- Create `backend/src/services/research_engine/experiment_service.py`.
- Modify `runs.py:1061-1087` (`run_complete`) and the `run_failed` branch: after the status assignment and **before** the existing single `commit`, call `await experiment_service.record_manifest(db, run)` when the plan has an `analyze` step. It is a no-op for other runs, so their manifests stay byte-identical.
- Modify `draft_release_service._graph:188-196` to add `experiment_service.graph_part` to the part loop, and the link loop gains `elif row.kind == "figure": parent = rules.node("figure", row.figure_id)`.
- Modify `release_rules.py:34` (`ANCHORED_LINK_KINDS` adds `"figure"`) and `claims_service` (`_check_request_shape`, `link`, a new `_figure_target` next to `_extraction_target` (`:577`), and the `observe_stance` 422).
- Modify `backend/src/shared/claim_schemas.py` (optional `figure_id`).

```python
AGGREGATE_TYPE = "research_experiment"
async def record_manifest(db, run) -> ResearchRunManifest | None          # caller's transaction; never commits
async def manifest_v2(db, context, run_id) -> RunManifestV2Response      # VIEW; legacy view when absent
async def manifest_bytes(db, context, run_id) -> tuple[bytes, str]       # canonical bytes, manifest_hash
async def read_artifact(db, context, run_id, artifact_id) -> tuple[bytes, str, str]
async def register_figure(db, context, actor_id, data) -> tuple[FigureResponse, bool]   # EDIT; commits once
async def lineage(db, context, figure_id) -> FigureLineageResponse        # output→run→code/env/data→hypothesis/protocol
async def graph_part(db, collection_id) -> tuple[list[Edge], set[Node]]
```

**`record_manifest` write order (inside the run's locked transaction):**
1. Read the step output, the protocol version, the question version and `ProtocolDeviation` ids for the run.
2. `build`, then `assert_no_secrets`, then `completeness`.
3. Insert the artifact rows, then the manifest row.
4. Append `run.manifest_recorded`.

The caller commits once. On rollback, compensating-delete the blob keys written by this run that are not referenced by any committed artifact row.

**`register_figure` write order:**
1. `resolve_project(EDIT)`.
2. `lock_aggregate_stream(research_experiment)`.
3. `_replayed_event`.
4. The output must be `role IN (figure, table)` in this Collection, otherwise 404 or 422.
5. A tip check (409 `Figure is stale; reload` if the body names another tip).
6. Insert and append.
7. If a predecessor exists, call `invalidate_dependents`.
8. **One commit.**

**Commit:** `feat(research): manifest recording, figure lineage and figure claim links (GOO-312)`

---

### Task 7: API + contracts + bundle part

**Files:**
- Create `backend/src/api/research_engine/experiments.py` and register it in `__init__.py` and `main.py`.
- Add the schemas to `backend/src/schemas/research_engine.py`.
- Modify `audit_bundle.py` to add an `experiments.json` part (manifests and figures, with no storage keys), registered in `gather_parts` before `_prisma`.
- Regenerate the OpenAPI spec and the types.

| Route | Action | Notes |
|---|---|---|
| `GET /api/v1/research-engine/runs/{run_id}/manifest/v2` | VIEW | `RunManifestV2Response{schema, manifest, manifest_hash, completeness, missing}`, or the legacy view. |
| `GET …/runs/{run_id}/manifest/v2/download` | VIEW | `application/json` attachment of exactly `canonical_json_bytes(manifest)`, with `X-Content-SHA256 = manifest_hash`. 404 for legacy runs. |
| `GET …/runs/{run_id}/artifacts/{artifact_id}` | VIEW | Streams the bytes with `X-Content-SHA256`. 404 when the artifact belongs to a foreign run. |
| `POST /projects/{project_id}/figures` | EDIT | `FigureCreate{figure_key, kind, caption, output_artifact_id, supersedes_figure_id?, idempotency_key}`. |
| `GET /projects/{project_id}/figures` | VIEW | Tips and history, with `stale`. |
| `GET /projects/{project_id}/figures/{figure_id}/lineage` | VIEW | The chain, plus `completeness` and `missing`. |
| `POST /projects/{project_id}/claims/{claim_id}/links` (existing) | EDIT | Accepts `kind: "figure"`. |

**oasdiff:** new operations and new optional fields; the link `kind` widening is the same as GOO-311's. Expected: no ERR beyond the `StepType` note in Task 3.

**Tests** (`backend/tests/unit/api/test_experiment_routes.py`):
- `test_manifest_download_bytes_match_hash`
- `test_artifact_route_cross_project_404`
- `test_no_storage_key_in_any_response_model`: walk the response schemas.
- `test_legacy_manifest_route_unchanged`

**Commit:** `feat(research): manifest v2, artifact download and figure endpoints (GOO-312)`

---

### Task 8: Structural guard

**Files:**
- Create `backend/tests/unit/architecture/test_experiment_boundary.py`, an AST scan with three checks:
  - **(a)** `_tool_execute_code` and `get_or_create_sandbox` do not reference `run_isolated`, and `run_isolated` does not reference `_sandboxes`.
  - **(b)** `manifest_rules.py` imports only stdlib.
  - **(c)** No module outside `experiment_service.py` and the migration constructs `ResearchRunManifest(`, `ResearchRunArtifact(` or `ResearchFigure(`.

**Commit:** `test(research): guard isolated execution and the single manifest writer (GOO-312)`

---

### Task 9: PostgreSQL proof (one test)

**Files:**
- Create `backend/tests/integration/test_run_manifest_postgres.py` with `pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]`. It reuses `screening_factory` and `_upgrade` (through `b8e0c2d4f6a7`) and `seed_approved_protocol_binding` (`backend/tests/integration/research_engine_postgres_support.py:143`). The fake sandbox returns real deterministic bytes: an SVG figure, `metrics.json` and `table.csv` computed from the input CSV in-process by the same script text. The artifact storage is `LocalArtifactStorage(tmp_path)`.

**`test_manifest_lineage_legacy_and_source_change`** runs these steps in order:
1. **Seed:** an approved protocol whose question version has a hypothesis, a blueprint with one `analyze` step (CSV document input, three declared outputs, pinned requirements, seed 20260930), and a run started through `create_approved_run`.
2. **Execute** through the engine with the fake sandbox:
   - The run completes, and exactly one manifest row exists with `completeness = complete` and `missing = []`.
   - Every artifact's stored bytes re-hash to its `sha256`.
3. **Fresh session:** close and reopen the session factory. `manifest/v2/download` bytes hash to `manifest_hash`, and each output's `GET …/artifacts/{id}` bytes match the manifest's `outputs[*].sha256`.
4. **Figure:** register `fig-1` on the SVG output, link it from a draft claim (`kind="figure"`), assess `supporting` and promote (GOO-307), giving `verified`. `lineage` returns the output, run, code sha256, environment template and lock, the input document checksum, `question_version_id`/`hypothesis_sha256` and `protocol_version_id`.
5. **Legacy:** a pre-existing run with only a v1 `reproducibility_manifest` returns `schema nous.run-manifest/1`, `incomplete`, and no `code`/`environment`/`inputs`/`outputs` keys. `GET /manifest` is unchanged.
6. **Secrets:** a fake sandbox whose `pip freeze` output contains `E2B_API_KEY=<value>` fails the run with `manifest_secret_detected`. No manifest row exists, and the value appears nowhere in `research_steps` or the ledger.
7. **Source change:** replace the input document's bytes (a new `checksum_sha256`).
   - `fig-1` reads `stale`, and the release reads `stale` (derived).
   - Re-run, then register a successor `fig-1`: the release is stamped with `cause.family == "research_experiment"`.
   - A second verified draft citing an unrelated figure stays `verified`.
   - The old figure's bytes still download.
8. **Override rejection:** `RunCreate(parameters_override={"seed": 1})` gives 409 `Method override requires a protocol amendment` (unchanged).
9. **Insert-only:** UPDATE or DELETE on each of the three tables raises `55000`.
10. **Roles and tenancy:** a foreign user gets 404 on the manifest and the artifact, and an archived project gives 409 on figure registration and 200 on lineage.
11. **Replay:** the `research_experiment`, `research_claims` and `research_release` streams replay.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_run_manifest_postgres.py`. Without a database, report it as **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for manifest lineage, legacy runs and source staling (GOO-312)`

---

### Task 10: Frontend

**Files:**
- Create `frontend/src/types/api/research-experiment-contract.ts`, aliasing the generated shapes.
- Modify `frontend/src/services/researchEngineService.ts` to add `getManifestV2`, `downloadManifestV2`, `listFigures`, `registerFigure` and `getFigureLineage`.
- Modify `frontend/src/components/research-engine/RunView.tsx`: a "Reproducibility" section shows `complete` or `incomplete` with the missing list as human labels, the input/output tables with short hashes, and the environment (template, Python, lock hash) inside a `<details>`. Each output with role figure or table gets a "Register as figure" action (EDIT).
- Modify `frontend/src/components/research/DraftClaimsPanel.tsx`: the link form gains a "Figure" option listing current figure tips, and a figure link opens its lineage.
- Tests:
  - `incomplete manifest never shows complete`
  - `legacy run shows incomplete without hashes`
  - `figure lineage renders run, code, environment, data, protocol`

**Commit:** `feat(frontend): run reproducibility section and figure lineage (GOO-312)`

---

## Mutation verification

Follow `docs/engineering/testing.md`. Record each check in the test docstring and in `docs/testing/agent-orchestration-mutation-checks.md` under a GOO-312 section.

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| Completeness derivation | return `("complete", [])` | `pytest -q backend/tests/unit/services/test_manifest_rules.py -k completeness` | missing paths not listed |
| Legacy view invents nothing | copy `environment` from defaults | same `-k legacy` | `environment` present |
| Thread-cache isolation | call `get_or_create_sandbox` inside `run_isolated` | `pytest -q backend/tests/unit/services/test_sandbox_isolated.py -k thread_cache` | `_sandboxes` not empty |
| Input checksum check | skip it | `pytest -q backend/tests/unit/services/test_step_executor_analyze.py -k checksum` | the step completes |
| Secret rejection | make `assert_no_secrets` a no-op | integration step 6 | a manifest row exists |
| Manifest in the run transaction | commit the manifest separately after the status | integration step 2 with an injected failure after the status write | a completed run without a manifest |
| `graph_part` selection | link every figure to every link | integration step 7 | the unrelated draft goes stale |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_manifest_rules.py backend/tests/unit/services/test_sandbox_isolated.py backend/tests/unit/services/test_step_executor_analyze.py
pytest -q backend/tests/unit/services/test_claim_rules.py backend/tests/unit/services/test_release_rules.py backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/services/test_audit_bundle.py
pytest -q backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # single head b8e0c2d4f6a7
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research-engine src/components/research
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists.** Report each of these as NOT RUN, never as passing:
- **PostgreSQL proof:** needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Live E2B execution** of an `analyze` step: needs `E2B_API_KEY` and authorization to spend sandbox minutes. The unit and integration tests use a fake sandbox and do not close the live item.
- **e2b 2.10.0 API conformance:** needs the pinned package installed (the local venv has 2.7.0).
- **Live journey:** needs a deployed stack with `b8e0c2d4f6a7`.

## Authenticated journey list for Linear closure

Run this on dev after the deploy. `alembic current` should show `b8e0c2d4f6a7 (head)`.

1. **Plan:** amend the pilot protocol with one `analyze` step (CSV input document, SVG figure, `metrics.json`, pinned requirements, seed) and approve it.
2. **Run:** start and complete the run. The Run view shows `Reproducibility: complete`. Keep the manifest download and its SHA-256.
3. **Fresh session:** sign out and in, re-download the manifest and every output, and run `sha256sum` against the manifest values. Keep the transcript.
4. **Figure:** register the SVG, cite it from a draft claim, assess it and promote. The lineage shows output → run → code/environment/data → hypothesis/protocol.
5. **Legacy:** open a pre-R6 run. It shows `incomplete` and no invented environment.
6. **Change:** upload a corrected CSV under the same document. The figure and release show `Stale`, and an unrelated verified draft stays `Verified`.
7. **Deny:** a foreign user gets 404 on the manifest and artifacts.

Record the SHA/PR, CI and oasdiff links, the manifest and artifact hashes, the PostgreSQL junit, the mutation transcripts and the NOT RUN list.

## Seams

- **Upstream (GOO-306/307/311):** one more link kind (`figure`) and one more cause family (`research_experiment`). Gate semantics are otherwise unchanged.
- **Next (GOO-313):** consumes `run_isolated`, `research_run_artifacts` (as the archive to restore from) and `manifest_rules.completeness`. Only `complete` manifests are eligible, and the rerun path is added there.
- **GOO-315:** reads `lineage` for each figure linked from a release's claims, and reports `completeness` as the experiment-reproducibility result (not a gate).

## Out of scope

Each item below has a `ponytail:` marker at its seam:
- languages other than Python and the R kernel;
- URL-fetched external inputs;
- git checkout of code from a repository;
- GPU templates;
- a custom E2B template build pipeline;
- an orphan-blob sweeper (keys are content-addressed, so a retry overwrites identical bytes);
- notebooks;
- changing the conversation `execute_code` tool.
