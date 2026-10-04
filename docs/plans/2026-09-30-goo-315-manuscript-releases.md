# GOO-315 Immutable Candidate and Verified Manuscript Releases Plan (Academic R7)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A manuscript release is an immutable, packaged snapshot of one exact saved draft version. It binds:
- the content hash;
- the exact GOO-306 claim versions and assessments;
- snapshotted bibliography records with stable `docN` keys (never live resolution);
- the applicable method, protocol and figure provenance (GOO-312 lineage for experiment figures, and the retained PRISMA and synthesis packages for review outputs);
- a package of files with sha256 hashes.

A **candidate** release can be built at any time and exposes every unresolved or unknown check. A **verified** release is the promotion of one exact candidate snapshot. It is allowed only when every applicable obligation passes at promotion time:
- the draft's GOO-307 release is live for the same content hash;
- the protocol-required methods are conformant or covered by an approved amendment;
- appraisal and synthesis are current where the protocol requires them;
- GOO-314 comments are resolved.

Reporting completeness, method adherence, claim support, experiment reproducibility and peer review stay separate results. Packaging never authorizes external submission. Ad-hoc draft exports keep working unchanged.

**Architecture:**
- **One insert-only table**, `manuscript_releases`. `stage` is `candidate` or `verified`, and a verified row points at its candidate.
- **Status is derived:** a verified row is `stale` when its GOO-307 `draft_release` is stale or the walk reaches it.
- **One pure module**, `manuscript_rules.py` (check aggregation, snapshot hash, package member list). **One service**, `manuscript_release_service.py`. **One router**, `api/research/manuscript_releases.py`. **One ledger family**, `research_manuscript`.
- **The package** is built with GOO-308's deterministic `audit_bundle.write_zip` and `Part`, plus a `schema` keyword. It is stored as private bytes.
- **GOO-307 is not duplicated.** Claim support is *read* from the draft's live `draft_releases` row, and the gate itself is never re-implemented.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, the private artifact storage, the PostgreSQL integration fixture, openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):** This PR stacks on **GOO-314** (`d2a4c6e8f0b1`) → GOO-313 → GOO-312 → GOO-311 `a6c8e0b2d4f5` → … It opens after GOO-314 merges.

### Consumed names

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Verified-draft gate (GOO-307) | `DraftRelease(draft_id, draft_version, content_hash, claim_version_ids, assessment_ids, interpretation_claim_version_ids, protocol_version_id, policy_version, stale_at)` (`backend/src/models/draft_release.py:41-72`); `draft_release_service.statuses` (`backend/src/services/research/draft_release_service.py:424`), `check` (`:469`), `export_header` (`:497`), `_graph` (`:129`) | A live row (`stale_at IS NULL`) for this content hash means claim support passed for exactly these bytes. |
| Labelled export | `DraftGenerationService.export_draft` (`backend/src/services/research/draft_generation_service.py:2342`), `release_rules.label_export`/`status_header` (`backend/src/services/research/release_rules.py:299,315`) | `manuscript.md` is the same labelled bytes an ad-hoc export gives at that moment, and the source bytes are stored beside it. |
| Bibliography | `DraftGenerationService._canonical_citation_records` (`:2501-2544`, which gives `docN` keys), `_generate_bib_entries` (`:2469`), `BibliographyService.format_bibtex` (`backend/src/services/research/bibliography_service.py:68`); `Citation.document_type` (`backend/src/models/citation.py:45`) | Snapshotted once into the release. Evidence snippets are never used as titles. |
| Figures (GOO-312) | `research_figures`, `experiment_service.lineage`, `manifest_rules.completeness`, link kind `figure` (GOO-312 plan) | Experiment figures carry run, code, environment, data and protocol lineage plus completeness. |
| Reproduction (GOO-313) | `rerun_service.latest_reproduction(db, run_id)` (GOO-313 plan, Task 5) | A separate result, never a gate. |
| Synthesis (GOO-311) | `synthesis_results(result_hash, status)`, link kind `synthesis_result`, `synthesis_service.export_package`, `protocol_methods.synthesis_selection` (GOO-311 plan) | Its own retained provenance. Required only when the protocol selects a synthesis. |
| Appraisal (GOO-309) | `protocol_methods.appraisal_method` (`backend/src/services/research_engine/protocol_methods.py:21`), `appraisal_service.graph_part` (`backend/src/services/research_engine/appraisal_service.py:394`) | Required only when the protocol declares appraisal. |
| Methods | `ResearchProtocolVersion.parent_version_id`, `change_kind`, `status` (`backend/src/models/research_protocol.py:97-137`); `audit_bundle._methods` (`backend/src/services/research_engine/audit_bundle.py:169`, which lists protocol versions and runs with `conformance_status`), `ProtocolDeviation(disposition)` (`backend/src/models/research_protocol.py:138`), `identity_service.current_protocol_version_id` (`backend/src/services/research_engine/identity_service.py:103`) | Method adherence reads runs and deviations bound to the release's protocol version. |
| PRISMA (GOO-303/308) | `audit_bundle._prisma` (`:105`) | The PRISMA package bytes are retained in the release package. |
| Peer review (GOO-314) | `peer_review_service.open_obligations(db, collection_id, draft_id)` (GOO-314 plan, Task 4) | An empty list means the obligation passes, and it is `not_applicable` when the project has no round. |
| Package writer (GOO-308) | `audit_bundle.Part` (`:64`), `write_zip` (`:367`), `verify_bundle` (`:432`), `SCHEMA` (`:52`) | Sorted members, a fixed zip timestamp and a `SHA256SUMS` member. |
| Draft retention | `delete_draft` union (`draft_generation_service.py:2214-2225`) | Released versions stay undeletable. |
| Roles | `ResearchAction.EDIT/RELEASE/VIEW`; RELEASE = adjudicator or supervisor (`backend/src/services/research_engine/project_access.py:36-47`) | The verified promotion authority is the same as GOO-307's. |
| Private bytes | `get_artifact_storage()` (`backend/src/services/artifacts/storage.py:132`) | No public or expiring URLs. |
| Ledger | `lock_aggregate_stream` (`backend/src/services/research_decisions/ledger.py:1318`), `append_decision` (`:1321`), `_FAMILIES` (`:2235`) | The append is caller-owned. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| **Relation to GOO-307 `draft_releases`** | `draft_releases` remains **the** verified-draft gate for claim support. A verified manuscript release must name a `draft_release_id` whose row is live and whose `content_hash` equals the snapshot's content hash at promotion time, and it records that id. This plan never re-evaluates claims, links or assessments itself. It copies the `draft_release`'s `claim_version_ids`/`assessment_ids` into the snapshot for reconstruction. A candidate built while the draft is `candidate` (no live `draft_release`) shows `claim_support: fail` with GOO-307's blockers from `draft_release_service.check`. | One gate, one place. The manuscript release only *packages* and *adds* obligations that the draft gate does not own (methods, synthesis, peer review, venue). Re-implementing claim checks would let the two disagree. |
| Release row | `manuscript_releases`:<br>- `id, collection_id FK RESTRICT, draft_id FK RESTRICT, draft_version, content_hash`<br>- `stage CHECK IN ('candidate','verified'), candidate_release_id FK self UNIQUE NULL, draft_release_id FK draft_releases RESTRICT NULL`<br>- `snapshot JSONB, snapshot_hash CHAR(64), checks JSONB, checks_hash CHAR(64)`<br>- `package_files JSONB, package_sha256 CHAR(64), package_storage_key VARCHAR(512)`<br>- `created_by_id, actor_role CHECK IN ('editor','adjudicator','supervisor'), created_at`<br>CHECK constraints:<br>- `(stage = 'verified') = (candidate_release_id IS NOT NULL AND draft_release_id IS NOT NULL)`;<br>- `stage = 'candidate'` implies `actor_role = 'editor'`;<br>- `stage = 'verified'` implies `actor_role IN ('adjudicator','supervisor')`. | It is insert-only and holds everything needed to reproduce the package. `UNIQUE(candidate_release_id)` means one verified promotion per candidate. |
| Snapshot content | `{schema: "nous.manuscript-snapshot/1", draft: {id, version, title, content_hash}, claims: {claim_version_ids, assessment_ids, interpretation_claim_version_ids, draft_release_id \| null}, references: [{key, type, title, authors: [str], year, venue, doi, arxiv_id, source: "citation"\|"document_metadata"}], figures: [{figure_id, figure_key, kind, output_sha256, run_id, manifest_id, manifest_hash, completeness, missing, reproduction}], synthesis: [{result_id, result_hash, status}], protocol: {protocol_version_id, content_hash, question_version_id}, runs: [{id, effective_plan_hash, conformance_status}], deviations: [{id, disposition}], prisma_body_sha256, peer_review: [{comment_root_id, state, anchor_state}]}`. `snapshot_hash = canonical_json_sha256(snapshot)`. `references[*]` come from `_canonical_citation_records` at build time, plus `type = Citation.document_type or metadata.get("type") or None`. | "Snapshot citation metadata, never live resolution." `type` is captured now so GOO-317 can serialize without re-resolving. |
| **Separate results** | `checks = {reporting_completeness, method_adherence, claim_support, experiment_reproducibility, peer_review, synthesis_appraisal}`. Each is `{state: "pass"\|"fail"\|"unknown"\|"not_applicable", items: [{code, detail, ref}]}`. GOO-316 adds `statements` and `venue`. | The results are never folded into one score. |
| Check rules (`manuscript_rules.evaluate`) | **claim_support**: `pass` iff a live `draft_release` with the same `content_hash` exists, otherwise `fail` with the GOO-307 blockers.<br>**method_adherence**: `pass` iff every run bound to the snapshot's protocol version is `conformant` and each of its `ProtocolDeviation` rows is covered by an **approved amendment**. An approved amendment is a `ResearchProtocolVersion` with `parent_version_id = deviation.protocol_version_id`, `change_kind != 'initial'` and `status IN ('approved','superseded')`. `disposition` is free text (`backend/src/schemas/research_engine.py:1342`), so it is reported but never trusted. `unknown` when the protocol version is NULL, and `fail` lists the run and deviation ids.<br>**synthesis_appraisal**: `not_applicable` unless `synthesis_selection` or `appraisal_method` is declared; then `pass` iff a current non-stale computed synthesis tip, or complete appraisals, exist.<br>**peer_review**: `not_applicable` with no rounds, `pass` when `open_obligations` is empty, otherwise `fail` with the items.<br>**experiment_reproducibility**: `not_applicable` with no experiment figures, `pass` iff every figure manifest is `complete` and `reproduced`, `unknown` if complete but not attempted, `fail` otherwise. **Reported only.**<br>**reporting_completeness**: the PRISMA package is present and consistent → `pass`; inconsistent → `fail`; no review corpus → `not_applicable`. **Reported only.** | "Verified promotion … only when its applicable obligations pass (accepted claim assessments, protocol-required methods or approved amendment; appraisal/synthesis only when protocol requires; peer-review obligations)." Reproducibility and reporting are separate results and not listed as obligations. |
| Verified obligations | `VERIFIED_OBLIGATIONS = ("claim_support", "method_adherence", "synthesis_appraisal", "peer_review")`. Each must be `pass` or `not_applicable` **on fresh re-evaluation at promotion time**. Otherwise 409 `{"detail": "Release obligations not met", "failing": [...]}`, and the candidate row is unchanged. | "Verified promotion rejects stale/missing required checks." The candidate's stored checks are never trusted at promotion. |
| Promotion binding | `POST …/{candidate_id}/promote` carries `{expected_snapshot_hash, expected_content_hash, idempotency_key}`, and the order is:<br>1. `resolve_project(RELEASE)`, which locks the Collection.<br>2. `lock_aggregate_stream(research_manuscript)`.<br>3. Rebuild the snapshot **from current data**, which must hash-equal the candidate's `snapshot_hash` (otherwise 409 `Candidate is stale; rebuild`).<br>4. Re-evaluate the obligations.<br>5. Copy the candidate's package bytes and `package_sha256` (re-hashed from storage, otherwise 409 `Package bytes changed`).<br>6. Insert the verified row and append.<br>7. **One commit.** | "Approval binds exact content/version, concurrent edits cannot change a promoted package." A concurrent draft revision, claim change or citation edit changes the rebuilt snapshot, so the promotion refuses. The verified package is byte-identical to the candidate's. |
| Package members | `manuscript.md` (the labelled export), `manuscript.source.md` (stored content; sha256 = `content_hash`), `references.bib` (from the snapshot references), `snapshot.json`, `checks.json`, `methods.json` (the `audit_bundle._methods` part), `prisma-flow.json` (`_prisma`, when applicable), `synthesis.json` (GOO-311 package, when applicable), `figures/{figure_key}.{ext}` (GOO-312 output bytes) plus `figures/lineage.json`, `manifest.json` and `SHA256SUMS`. It is written by `write_zip(parts, schema="nous.manuscript-release.v1", generated_at=<row created_at>, …)`. `package_files = [{path, sha256, bytes}]`. | Every member reconciles with the snapshot. Using the release's own timestamp makes the zip bytes reproducible from the same inputs. |
| `write_zip` change | Add a keyword `schema: str = SCHEMA`. The audit bundle callers are unchanged. | One deterministic writer, reused. |
| Ad-hoc exports | `export_draft` and its route are unchanged. `is_current` and `DraftReview.passed` are never read as release state. | "Keep ad-hoc draft exports working; add release endpoints/state rather than relabeling." |
| Derived status | A candidate is `candidate`. A verified release is `verified`, or `stale` when its `draft_release.stale_at` is set or the GOO-307 walk reaches its `draft_release` node. `manuscript_release_service.graph_part` adds `("release", draft_release_id) → ("manuscript", id)`. Nothing is stamped. | It reuses GOO-307's walk and stamping, so no second invalidation mechanism exists. |
| Reproducibility of prior releases | `GET …/{id}/verify` re-reads the stored bytes, recomputes `package_sha256` and every member hash, runs `audit_bundle.verify_bundle`, and recomputes the reference mapping (`docN` → snapshot record → the `references.bib` entry) from `snapshot.json`. All of this works after later draft, citation or source edits, because nothing is re-resolved. | "Prior releases remain reproducible after later citation/source/draft changes." |
| Retention and deletion | Package bytes live at `artifacts/{org}/manuscript-releases/{release_id}/package.zip`. Application code never deletes them while the row exists, and the rows are RESTRICT-referenced and insert-only. `delete_draft` refuses versions with a manuscript release (it joins the union). Org deletion follows the existing retention flow (`backend/src/tasks/retention_tasks.py`), unchanged. Candidates are retained like verified releases. | "Define retention/deletion behaviour for package files." `# ponytail: no candidate purge; add a time-boxed purge of unpromoted candidates if storage cost matters.` |
| No submission authority | There is no outbound HTTP in the service (AST guard). The DTO field `external_submission: "not_authorized"` is constant. GOO-318 owns deposits with their own approval. | "Packaging never authorizes external submission." |

**Migration head:** new revision `e4c6a8b0d2f3_create_manuscript_releases.py`, with `down_revision = "d2a4c6e8f0b1"` (GOO-314). Same re-pointing rule. It creates the table with `_deny_data_api`, the insert-only trigger, `UNIQUE(candidate_release_id)` and the CHECKs. `downgrade()` refuses when rows exist.

---

### Task 1: Pure rules (`manuscript_rules.py`) and the `write_zip` schema keyword

**Files:**
- Create `backend/src/services/research/manuscript_rules.py` (stdlib).
- Modify `audit_bundle.write_zip` (`:367`) to add `schema: str = SCHEMA`.
- Create `backend/tests/unit/services/test_manuscript_rules.py`.

```python
SNAPSHOT_SCHEMA = "nous.manuscript-snapshot/1"; PACKAGE_SCHEMA = "nous.manuscript-release.v1"
CHECK_KEYS = ("reporting_completeness","method_adherence","claim_support","experiment_reproducibility","peer_review","synthesis_appraisal")
VERIFIED_OBLIGATIONS = ("claim_support","method_adherence","synthesis_appraisal","peer_review")
def snapshot_hash(snapshot: Mapping) -> str
def evaluate(inputs: CheckInputs) -> dict[str, dict]
def failing_obligations(checks: Mapping, obligations=VERIFIED_OBLIGATIONS) -> list[str]
def reference_mapping(snapshot: Mapping, bibtex: str) -> list[tuple[str, str]]   # key -> entry hash
```

**Tests (they fail on the import):**
- `test_claim_support_requires_live_draft_release_same_hash`
- `test_method_adherence_accepts_approved_amendment_only`
- `test_synthesis_appraisal_not_applicable_unless_protocol_requires`
- `test_reproducibility_reported_never_obligation`
- `test_peer_review_not_applicable_without_rounds`
- `test_failing_obligations_lists_each`
- `test_snapshot_hash_stable_across_key_order`
- `test_write_zip_schema_keyword_default_unchanged`: the existing audit-bundle bytes are identical.

**Commit:** `feat(research): pure manuscript release checks and snapshot hashing (GOO-315)`

---

### Task 2: Model + migration

**Files:**
- Create `backend/src/models/manuscript_release.py` (`ManuscriptRelease`) and export it.
- Create `backend/alembic/versions/e4c6a8b0d2f3_create_manuscript_releases.py`.
- Modify `test_screening_queue_postgres.py` (`_REBUILT_TABLES`).

**Check:** `check_alembic.py` reports single head `e4c6a8b0d2f3`, and `alembic upgrade head --sql | grep -c manuscript_releases` is > 0.
**Commit:** `feat(research): manuscript_releases table (GOO-315)`

---

### Task 3: Ledger

**Files:**
- Modify `ledger.py` to add the `research_manuscript` family (subject type `manuscript_release`).
- Modify `test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `manuscript.candidate_created` | `collection_id, release_id, draft_id, draft_version, content_hash, snapshot_hash, checks_hash, package_sha256` | `editor` |
| `manuscript.verified` | `collection_id, release_id, candidate_release_id, draft_release_id, content_hash, snapshot_hash, package_sha256, obligations` | `adjudicator` or `supervisor` |

**Replay rules:**
1. Every `collection_id` equals the `aggregate_id`.
2. `verified` names a candidate created earlier, with the same `snapshot_hash` and `package_sha256`.
3. A candidate is verified at most once.

**Tests:**
- `test_manuscript_replay_rejects_verified_with_changed_package`
- `test_manuscript_replay_rejects_double_promotion`
- `test_manuscript_replay_rejects_editor_promotion`

**Commit:** `feat(research): research_manuscript decision family (GOO-315)`

---

### Task 4: Service

**Files:**
- Create `backend/src/services/research/manuscript_release_service.py`.
- Modify `draft_release_service._graph`'s part loop to add `manuscript_release_service.graph_part`.
- Modify the `delete_draft` union to add `ManuscriptRelease.draft_id`.

```python
AGGREGATE_TYPE = "research_manuscript"
async def build_snapshot(db, context, draft) -> dict                     # reads only; no writes
async def check_inputs(db, context, snapshot) -> CheckInputs
async def create_candidate(db, context, actor_id, data) -> tuple[ManuscriptReleaseResponse, bool]   # EDIT
async def promote(db, context, actor_id, candidate_id, data) -> tuple[ManuscriptReleaseResponse, bool]  # RELEASE
async def list_releases(db, context) -> list[ManuscriptReleaseResponse]  # derived status
async def package_bytes(db, context, release_id) -> tuple[bytes, str]    # re-hashed before return
async def verify(db, context, release_id) -> ReleaseVerification
async def graph_part(db, collection_id) -> tuple[list[Edge], set[Node]]
```

**`create_candidate` order:**
1. `resolve_project(EDIT)`.
2. `lock_aggregate_stream`.
3. `_replayed_event`.
4. Load the draft by `(draft_id, expected_content_hash)` (otherwise 409 `Draft content changed; reload`).
5. `build_snapshot`, then `evaluate`.
6. Assemble the parts and run `write_zip`.
7. `storage.put` (before the DB).
8. Insert and append.
9. **One commit.** On failure, compensating-delete the key.

An identical `snapshot_hash` already stored as a candidate for this draft returns that row (`200`, `replayed`).

**Commit:** `feat(research): candidate packaging, verified promotion and release verification (GOO-315)`

---

### Task 5: API + contracts

**Files:**
- Create `backend/src/api/research/manuscript_releases.py` (prefix `/api/v1/projects/{project_id}/manuscript-releases`) and register it.
- Create `backend/src/shared/manuscript_release_schemas.py`.
- Modify `audit_bundle.py` to add a `manuscript-releases.json` part: the rows' snapshots, checks and package hashes, but not the package bytes. It is registered before `_prisma`.
- Regenerate the OpenAPI spec and the types.

| Route | Action | Notes |
|---|---|---|
| `POST …/manuscript-releases` | EDIT | `CandidateCreate{draft_id, expected_content_hash, idempotency_key}`. Returns 201 or 200 replayed. |
| `GET …/manuscript-releases` | VIEW | Rows with derived status, checks and the package file list. |
| `POST …/manuscript-releases/{id}/promote` | RELEASE | `PromoteRequest{expected_snapshot_hash, expected_content_hash, idempotency_key}`. Returns 201, 409 failing or stale, or 403 without adjudicator or supervisor. |
| `GET …/manuscript-releases/{id}/package` | VIEW | `application/zip` with `X-Content-SHA256`. |
| `GET …/manuscript-releases/{id}/verify` | VIEW | `{package_sha256_ok, members: [...], bundle_ok, reference_mapping: [...]}` |

**oasdiff:** new operations only. Expected: no ERR.

**Tests** (`backend/tests/unit/api/test_manuscript_release_routes.py`):
- `test_promote_owner_without_role_403`
- `test_candidate_with_failing_claims_exposes_blockers`
- `test_draft_export_route_unchanged`
- `test_no_outbound_http_in_service`: an AST scan for `httpx`, `requests` and `aiohttp`.

**Commit:** `feat(research): manuscript release endpoints and bundle part (GOO-315)`

---

### Task 6: PostgreSQL proof (one test)

**Files:**
- Create `backend/tests/integration/test_manuscript_release_postgres.py` with the integration and `requires_postgres` markers. It reuses GOO-307's seed (a verified draft with supporting assessments), GOO-312's seed (one figure linked), GOO-311's seed (one synthesis link) and GOO-314's seed (one round).

**`test_candidate_verified_reproducible_and_refusals`** runs these steps in order:
1. **Candidate A** on draft v1 while one claim is unassessed:
   - `claim_support` fails with the GOO-307 `unassessed` blocker.
   - `peer_review` fails (one open comment).
   - `experiment_reproducibility` is `unknown`.
   - The package downloads, and every member hash matches `package_files`.
   - Promote gives 409 with `failing = ["claim_support", "peer_review"]`, and candidate A is unchanged.
2. **Candidate B** after the claim is assessed, the draft is promoted under GOO-307 and the comment is resolved:
   - The obligations pass.
   - Promote as the adjudicator gives `verified`, its `package_sha256` equals candidate B's, and `draft_release_id` is recorded.
   - The owner with no roles gets 403.
3. **Reproduce after reload:** in a fresh session, `verify` gives `package_sha256_ok`, `bundle_ok`, and the reference mapping keys `doc1..docN` matching the snapshot.
4. **Upstream edits:**
   - Change one `Citation.title`: `verify` on B still gives the same bytes and mapping, and the references in the package keep the old title.
   - Supersede the figure (GOO-312 successor): B reads `stale`, because the draft release is stamped through the walk. B's bytes still verify.
5. **Stale candidate:** build candidate C, then supersede a claim version, then promote C: 409 `Candidate is stale; rebuild`.
6. **Conditional methods:** a run bound to the protocol has a recorded `ProtocolDeviation` and no approved amendment, so a fresh candidate fails `method_adherence` (listing the run and deviation) and promotion refuses. A deviation whose `disposition` text reads "approved" still fails. Approving a child protocol version (`change_kind='amendment'`, parent = the deviated version) makes a rebuilt candidate pass.
7. **Concurrent promote:** two promotes of candidate B with different idempotency keys run concurrently: exactly one verified row (`UNIQUE(candidate_release_id)`), and the other caller receives it as replayed or gets 409.
8. **Retention:** `delete_draft` on v1 gives 409.
9. **Insert-only and replay:** UPDATE or DELETE raises `55000`, and `research_manuscript` replays.
10. **Ad-hoc export:** `POST …/drafts/{id}/export?format=markdown` bytes equal the pre-change baseline.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_manuscript_release_postgres.py`. Without a database, report it as **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for candidate and verified releases and their reproducibility (GOO-315)`

---

### Task 7: Frontend

**Files:**
- Create `frontend/src/types/api/manuscript-release-contract.ts`.
- Modify `projectService.ts` to add `createCandidateRelease`, `listManuscriptReleases`, `promoteManuscriptRelease`, `downloadManuscriptPackage` and `verifyManuscriptRelease`.
- Modify `frontend/src/components/research/DraftReleasePanel.tsx`: a "Manuscript releases" section has a candidate button (EDIT) and a list of releases with status.
  - The six check results show as separate rows, each with a state badge and its items.
  - Promote is shown only to adjudicators and supervisors. It is disabled while any obligation fails, and the failing list is shown.
  - Download and Verify actions; "Not authorized for external submission" is always visible.
- Tests:
  - `checks rendered separately`
  - `promote disabled with failing obligations`
  - `verified stale shown with cause`
  - `submission disclaimer visible`

**Commit:** `feat(frontend): manuscript release candidates, promotion and verification (GOO-315)`

---

## Mutation verification

Follow `docs/engineering/testing.md`. Record each check under a GOO-315 section in `docs/testing/agent-orchestration-mutation-checks.md`.

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| Fresh re-evaluation at promotion | trust the candidate's stored checks | `pytest -q backend/tests/integration/test_manuscript_release_postgres.py` | step 5: a stale candidate is promoted |
| Snapshot rebuild equality | skip the hash comparison | same | step 5 |
| Live `draft_release` same-hash rule | accept any live release for the draft | `pytest -q backend/tests/unit/services/test_manuscript_rules.py -k claim_support` | another version's release passes |
| `UNIQUE(candidate_release_id)` | drop it in a scratch migration | integration step 7 | two verified rows |
| Snapshotted references | re-read `Citation` at package time | integration step 4 | the new title appears in B's package |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_manuscript_rules.py backend/tests/unit/services/test_audit_bundle.py backend/tests/unit/services/test_release_rules.py backend/tests/unit/services/test_research_decision_ledger.py
pytest -q backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # single head e4c6a8b0d2f3
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists.** Report each of these as NOT RUN, never as passing:
- **PostgreSQL proof:** needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Methods-expert signoff** on the obligation set (which checks block verified promotion): none is available today.
- **Live journey:** needs a deployed stack with `e4c6a8b0d2f3`.

## Authenticated journey list for Linear closure

1. **Candidate:** build a candidate on a draft with one unassessed claim and one open review comment. Download it and keep `SHA256SUMS`. The failing checks are listed.
2. **Fix and verify:** assess the claim, promote the draft (GOO-307), resolve the comment, build a candidate and promote it as the adjudicator. Keep both packages.
3. **Recompute:** `sha256sum -c SHA256SUMS` on both, and the Verify action shows the mapping.
4. **Upstream edit:** edit a citation title and a figure. The verified release shows `Stale`, its package still verifies, and its references keep the snapshot values.
5. **Refusals:** promoting a stale candidate gives 409, and the owner without the role gets 403.

Record the SHA/PR, CI links, both packages and their hashes, the PostgreSQL junit, the mutation transcripts and the NOT RUN list.

## Seams

- **Upstream:** GOO-307 owns claim support and stamping. GOO-311, GOO-312 and GOO-313 supply provenance and reproducibility. GOO-314 supplies the peer-review obligations.
- **Next (GOO-316):** adds the `statements` and `venue` keys to `checks`. It makes them verified obligations when the release's snapshot binds a statement set, and adds the anonymized package variant. Candidate construction stays first, which avoids a cycle.
- **Next (GOO-317):** serializes `snapshot.references` to CSL JSON and RIS and adds them as package members for new candidates.
- **GOO-318:** deposits an exact verified release by `package_sha256`.

## Out of scope

Each item below has a `ponytail:` marker at its seam:
- DOCX or PDF rendering;
- journal-specific templates;
- external submission or deposit;
- a candidate purge;
- release diffing;
- re-signing packages.
