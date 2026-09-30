# GOO-299 Study Identity Plan (Academic R2)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn the per-run DOI/provider-id dedup that GOO-298 already does in memory (`backend/src/services/research_engine/discovery.py:59-109`) into durable, project-scoped (Collection) record → report → study identities with reviewed, append-only merge/split/link history, without touching any original `ResearchSource` row.

**Architecture:** Four new tables, no second ledger. `research_reports` is the canonical publication identity inside one Collection; `research_report_identifiers` is the DB-enforced "one stable identity per (collection, kind, value)"; `research_report_observations` links every `ResearchSource` row (the untouched provider snapshot — `discovery.py:73-74` already stores `provenance` per provider) to a report with `match_method` + `evidence`; `research_studies` is the FK target that several reports point at through `research_reports.study_id` + `study_link_status`. Merge/split/link decisions are events in the existing decision ledger (`backend/src/services/research_decisions/ledger.py`) under a new aggregate family `research_identity` whose aggregate is the Collection. The per-project stream row of that aggregate is the one lock that serializes imports, merges, splits and link decisions (lock order stays Workspace SHARE → Collection UPDATE → stream FOR UPDATE, `project_access.py:195-205`).

**Tech Stack:** FastAPI + SQLAlchemy async, Alembic, PostgreSQL (asyncpg) integration tests via `postgres_container` (`backend/tests/integration/conftest.py:207-233`), SQLite/aiosqlite unit tests, openapi-typescript, Next.js + TanStack Query + Vitest.

---

## Decisions (read before Task 1)

| Question | Decision | Why |
|---|---|---|
| Where does the aggregate lock live? | `research_decision_streams` row `("research_identity", collection_id)`, taken with `FOR UPDATE` through the ledger's existing `_locked_stream` (`ledger.py:162-199`), exposed as `lock_aggregate_stream`. | It already exists, already serializes ledger `seq` allocation, and one lock per project covers imports, merge, split and link in one place. `UNIQUE(collection_id, kind, value)` on identifiers stays as the DB guard underneath. `# ponytail: one lock per project; per-report locks if import throughput matters.` |
| Study link table or column? | Column on `research_reports` (`study_id`, `study_link_status`, `study_link_actor_id`, `study_link_rationale`). | "Many reports → one study" needs only a FK on the report. Prior/next links live in ledger payloads. `# ponytail: extract research_study_reports if a report ever needs two studies.` |
| Which role records link decisions? | `status="proposed"` → `ResearchAction.REVIEW` (`ResearchProjectRole.REVIEWER`); confirm / dispute / merge / split → `ResearchAction.ADJUDICATE` (`ADJUDICATOR`). Both resolved by `resolve_project` (`project_access.py:180-308`, role gate at 287-289). | Mirrors screening: reviewers propose, adjudicators resolve. Supervisor stays protocol governance (`protocol_service.py:408-409`). |
| Which identifiers equate reports? | `doi`, `pmid`, `pmcid`, `openalex`, `semantic_scholar`, and `arxiv_base` (= `arxiv` with `v\d+$` stripped). The versioned `arxiv` value is kept in `evidence` only. | Ticket: arXiv id without version is deterministic; different versions stay inspectable. |
| Inconsistent identifiers (DOI says report A, PMID says report B)? | Never merge. Attach to the first match by kind priority above, record `evidence.conflicts=[{kind,value,report_id}]`, surface in `/candidates`. | "Inconsistent identifiers remain inspectable." |
| `rag_store` sources? | No observation row. | Boundary: org-scoped document dedup is untouched; local docs already stay a separate partition (`discovery.py:76,86-91`). |
| Title/author/year? | Read-only suggestion in `GET .../candidates` (normalized-title equality + year). Never writes. | "May SUGGEST, never silently equates." |
| Governing protocol version | `research_protocols.current_approved_version_id` for the collection at action time, nullable (`backend/src/models/research_protocol.py:85`). | Ticket asks for it on every decision. |
| Backfill | Script `backend/scripts/backfill_report_identities.py --dry-run/--apply` reusing the service; not inside the migration. | Idempotent via `UNIQUE(source_id)`; runs against dev DB from a pod, like `backfill_do_kb.py`. |

**Migration head:** at plan time `python scripts/ci/check_alembic.py` reports single head `merge_daily_harness_20260928` (`backend/alembic/versions/merge_daily_brief_harness_heads.py:10`). If PR #1747 has merged, head is `b7c4e1d9a2f6`; rebase and set `down_revision` accordingly before Task 2. Revision ids ≤ 32 chars (`scripts/ci/check_alembic.py:40`).

---

### Task 1: Extract the pure, explainable matcher

**Files:**
- Create: `backend/src/services/research_engine/report_identity.py`
- Modify: `backend/src/services/research_engine/discovery.py:20` (rename `_identifiers` → `extract_identifiers`, keep `_identifiers = extract_identifiers` alias so `test_paper_discovery.py` keeps passing)
- Create: `backend/tests/unit/services/test_report_identity_matching.py`
- Create: `backend/tests/fixtures/report_identity_gold.json`

**Step 1: Failing tests**

```python
from src.services.research_engine.report_identity import (
    IDENTITY_KINDS, assign_report, report_identifiers,
)

def test_arxiv_versions_share_base_but_keep_version_in_evidence():
    index = {("arxiv_base", "2301.00001"): "r1"}
    hit = assign_report(index, {"arxiv": "2301.00001v2"})
    assert hit.report_key == "r1" and hit.match_method == "arxiv_base"
    assert hit.evidence["matched"] == {"kind": "arxiv_base", "value": "2301.00001"}
    assert hit.evidence["observed"]["arxiv"] == "2301.00001v2"

def test_conflicting_identifiers_attach_by_priority_and_record_conflict():
    index = {("doi", "10.1/a"): "r1", ("pmid", "99"): "r2"}
    hit = assign_report(index, {"doi": "10.1/a", "pmid": "99"})
    assert hit.report_key == "r1"
    assert hit.evidence["conflicts"] == [{"kind": "pmid", "value": "99", "report_key": "r2"}]

def test_title_alone_never_matches():
    assert assign_report({}, {"title": "Attention Is All You Need"}).report_key is None

def test_gold_fixture_precision_and_recall():
    gold = json.loads(Path("backend/tests/fixtures/report_identity_gold.json").read_text())
    clusters = cluster_records(gold["records"])          # pure, in-memory
    pairs = predicted_pairs(clusters); truth = gold_pairs(gold)
    precision = len(pairs & truth) / len(pairs); recall = len(pairs & truth) / len(truth)
    assert precision == 1.0                              # zero false merges
    assert recall >= gold["min_recall"]                  # identifier-only ceiling, e.g. 0.85
    for a, b in gold["known_false_merges"]:
        assert clusters[a] != clusters[b]
```

Gold fixture: ~20 records covering DOI-only, PMID+DOI bridge, arXiv v1/v2 (same base → same gold report), same title different DOI (known false merge), preprint vs published with different DOI and no shared id (known false merge; counts against recall by design), `rag_store` (excluded).

**Step 2: Run, expect ImportError**

`pytest -q backend/tests/unit/services/test_report_identity_matching.py` → `ImportError: cannot import name 'assign_report'`.

**Step 3: Minimal implementation**

```python
IDENTITY_KINDS = ("doi", "pmid", "pmcid", "arxiv_base", "openalex", "semantic_scholar")  # priority order

def report_identifiers(raw: Mapping[str, str]) -> dict[str, str]:
    ids = dict(extract_identifiers_from_mapping(raw))      # reuse discovery normalization
    if "arxiv" in ids:
        ids["arxiv_base"] = re.sub(r"v\d+$", "", ids["arxiv"])
    return ids

@dataclass(frozen=True)
class ReportAssignment:
    report_key: Any | None; match_method: str; evidence: dict[str, Any]

def assign_report(index: Mapping[tuple[str, str], Any], raw_ids: Mapping[str, str]) -> ReportAssignment:
    ids = report_identifiers(raw_ids)
    hits = [(k, ids[k], index[(k, ids[k])]) for k in IDENTITY_KINDS if (k, ids.get(k)) in index]
    if not hits:
        return ReportAssignment(None, "new", {"observed": ids, "conflicts": []})
    kind, value, key = hits[0]
    conflicts = [{"kind": k, "value": v, "report_key": r} for k, v, r in hits[1:] if r != key]
    return ReportAssignment(key, kind, {"observed": ids, "matched": {"kind": kind, "value": value}, "conflicts": conflicts})
```

`cluster_records` = fold `assign_report` over records with an in-memory index (same loop the service will run). No similarity code here.

**Step 4: Run tests** — new file green; `pytest -q backend/tests/unit/services/test_paper_discovery.py` still green (25 tests).

**Step 5: Commit** `feat(research): pure explainable report identity matcher (GOO-299)`

---

### Task 2: Models + migration

**Files:**
- Create: `backend/src/models/research_report.py` (`ResearchStudy`, `ResearchReport`, `ResearchReportIdentifier`, `ResearchReportObservation`)
- Modify: `backend/src/models/__init__.py` (export)
- Create: `backend/alembic/versions/c9d2e4f6a8b1_create_report_identities.py`
- Modify: `backend/tests/integration/test_academic_wave_migrations.py` (add the new revision to whatever list it asserts)

Model rules (copy `research_decision.py` style: plain `Base`, no `BaseModel` soft-delete — identity rows are never deleted, `research_decision.py:23,50`):

```python
class ResearchStudy(Base):            # research_studies
    id, collection_id FK collections RESTRICT, label String(500), created_at

class ResearchReport(Base):           # research_reports
    id, collection_id FK RESTRICT, title_snapshot String(500),
    merged_into_report_id FK research_reports.id RESTRICT NULL,   # loser → winner, never deleted
    study_id FK research_studies RESTRICT NULL,
    study_link_status String(16) NULL  CHECK IN ('proposed','confirmed','disputed'),
    study_link_actor_id FK users RESTRICT NULL, study_link_rationale Text NULL,
    created_at
    CHECK ((study_id IS NULL) = (study_link_status IS NULL))

class ResearchReportIdentifier(Base): # research_report_identifiers
    id, collection_id, report_id FK RESTRICT, kind String(32), value String(512), created_at
    UNIQUE(collection_id, kind, value)        # the stable-identity guard
    CHECK (kind IN IDENTITY_KINDS)

class ResearchReportObservation(Base):# research_report_observations (append-only)
    id, collection_id, report_id FK RESTRICT, source_id FK research_sources.id RESTRICT,
    match_method String(32), evidence JSONB NOT NULL default '{}', created_at
    UNIQUE(source_id)                          # repeated import = no-op
```

Migration: `down_revision = "merge_daily_harness_20260928"` (or `b7c4e1d9a2f6`, see header), create the four tables + indexes on `(collection_id)` and `(report_id)`, call `_deny_data_api` for each (copy `a3c5e7f901b2_create_research_decision_ledger.py:16-31`).

**Step 1: Failing check** — `(cd backend && python ../scripts/ci/check_alembic.py)` must still print `single head`; `test_academic_wave_migrations.py` red until the revision is listed.
**Step 2: Write models + migration.** `alembic upgrade head --sql` offline renders CREATE TABLE ×4 (no Docker; see gotchas).
**Step 3: Verify** — `check_alembic.py` → `single head 'c9d2e4f6a8b1'`; `pytest -q backend/tests/unit/architecture`.
**Step 4: Commit** `feat(research): report/study identity tables (GOO-299)`

---

### Task 3: Ledger vocabulary — `research_identity` family

**Files:**
- Modify: `backend/src/services/research_decisions/ledger.py` (lines 26-44 registry, 77-111 `_validate_event`, 296-363 `replay_decisions`, 373-411 transitions)
- Modify: `backend/src/services/research_decisions/__init__.py` (export `lock_aggregate_stream`)
- Modify: `backend/tests/unit/services/test_research_decision_ledger.py`

Today `_validate_event` hard-codes the protocol family (`ledger.py:95-104`, `111`) and `replay_decisions` always requires a subject version hash and calls `_validate_protocol_transitions` (`ledger.py:354-360`). Refactor into a per-aggregate registry; protocol behaviour must not change (existing unit + `backend/tests/integration/test_research_decision_ledger.py` stay green).

```python
@dataclass(frozen=True)
class _Family:
    subject_type: str
    payload_keys: dict[tuple[str, int], frozenset[str]]
    validate_payload: Callable[[str, Mapping[str, Any], UUID, UUID | None, UUID], None]
    validate_transitions: Callable[[Sequence[ResearchDecisionEvent], UUID], None]
    requires_subject_version: bool

_FAMILIES = {"research_protocol": _Family(... existing code ...),
             "research_identity": _Family("research_report", _IDENTITY_KEYS, _validate_identity_payload,
                                          _validate_identity_transitions, requires_subject_version=False)}
```

Identity vocabulary (schema 1), aggregate_id = collection_id, subject_id = report id, `subject_version_id=None`, `subject_hash` = `decision_request_fingerprint(payload)` so replay verifies it from the stored payload instead of a live row (reports change after later merges):

| event_type | payload keys |
|---|---|
| `identity.report_merged` | `collection_id, surviving_report_id, merged_report_ids, moved_source_ids, moved_identifiers, protocol_version_id` |
| `identity.report_split` | `collection_id, source_report_id, new_report_id, moved_source_ids, moved_identifiers, protocol_version_id` |
| `identity.study_linked` | `collection_id, report_id, study_id, status, prior_study_id, prior_status, match_evidence, protocol_version_id` |

`_validate_identity_payload`: all ids are UUID strings, `collection_id == aggregate_id`, `surviving_report_id ∉ merged_report_ids`, `merged_report_ids` non-empty, `status ∈ {proposed, confirmed, disputed}`, `moved_*` are lists.
`_validate_identity_transitions` (replay rule): per report, `prior_study_id/prior_status` of a `study_linked` must equal the `study_id/status` of the last `study_linked` for that report (or `None`); a report named in `merged_report_ids` can appear later only as `source_report_id` of a split is **not** allowed — merged reports are terminal. Replay for `requires_subject_version=False` compares `subject_hash` to `decision_request_fingerprint(payload)` instead of calling `subject_version_hash`.

**Step 1: Failing tests** in the unit ledger file: `test_identity_merge_rejects_self_merge`, `test_identity_link_rejects_unknown_status`, `test_identity_events_need_no_subject_version`, plus a replay test that `prior_study_id` mismatch raises `DecisionReplayError("contradictory study link")`.
**Step 2:** `pytest -q backend/tests/unit/services/test_research_decision_ledger.py` → `unsupported decision event 'identity.report_merged'`.
**Step 3:** Implement registry; add `lock_aggregate_stream = _locked_stream` (public name, same function).
**Step 4:** Unit file green; `pytest -q backend/tests/unit/services/test_research_protocol_service.py` green.
**Step 5: Commit** `feat(research): identity events in the decision ledger (GOO-299)`

---

### Task 4: Identity service (observe, merge, split, link, history)

**Files:**
- Create: `backend/src/services/research_engine/identity_service.py`
- Modify: `backend/src/schemas/research_engine.py` (append after `ProtocolApprovalResponse`, line 366)

Public surface (never commits — caller owns the transaction, like `ledger.py:7`):

```python
async def observe_sources(db, *, collection_id: UUID, sources: Sequence[ResearchSource]) -> list[ResearchReportObservation]
async def list_reports(db, *, collection_id) -> list[ReportResponse]
async def candidates(db, *, collection_id, report_id) -> ReportCandidatesResponse
async def link_study(db, context: ProjectContext, report_id, actor_user_id, data: StudyLinkRequest) -> ReportResponse
async def merge_reports(db, context, actor_user_id, data: ReportMergeRequest) -> ReportResponse
async def split_report(db, context, report_id, actor_user_id, data: ReportSplitRequest) -> ReportResponse
async def history(db, *, collection_id) -> list[IdentityEventResponse]   # replay_decisions
```

`observe_sources` (called from the import transaction, Task 5, and the backfill, Task 7):
1. `await lock_aggregate_stream(db, collection_id=..., aggregate_type="research_identity", aggregate_id=collection_id)` — takes the project identity lock.
2. Load the collection's identifier index `{(kind, value): report_id}` (one query, `merged_into_report_id IS NULL` reports only).
3. For each non-`rag_store` source: `assign_report(index, source.metadata_["identifiers"])` (`discovery.py:75` stores them). `report_key is None` → insert `ResearchReport(title_snapshot=source.title[:500])` + one `ResearchReportIdentifier` per identity kind, add them to the in-memory index. Else: insert missing identifiers for the matched report (identifier set only grows; conflicts are **not** inserted).
4. `pg_insert(ResearchReportObservation).on_conflict_do_nothing(index_elements=["source_id"])` with `match_method` + `evidence` → repeated import is a no-op, nothing discarded.

`merge_reports`: lock → load surviving + merged reports `WHERE collection_id = context.collection.id AND merged_into_report_id IS NULL` (any missing → 404 "Report not found"; a foreign-project id looks identical to a missing one) → re-point `research_report_identifiers.report_id` and `research_report_observations.report_id` to the survivor, set `merged_into_report_id` on losers, if a loser had a study link and the survivor has none, carry it → `append_decision(... event_type="identity.report_merged", actor_role="adjudicator", idempotency_key=data.idempotency_key, request_fingerprint=decision_request_fingerprint({...ids, actor, body}))`. Second concurrent merge naming an already-merged report fails with 409 "Report already merged" *after* waiting on the lock — no partial rewiring.

`split_report`: lock → observations by `source_ids` must belong to `report_id` (else 404) → new report with `title_snapshot` of first moved source → move those observations; move identifiers whose value appears only in moved sources' `evidence.observed`; append `identity.report_split`.

`link_study`: lock → report (404 if merged/foreign) → `study_id is None` ⇒ create `ResearchStudy(label=report.title_snapshot)` → set the four link columns → append `identity.study_linked` with `prior_study_id/prior_status` from the row before the write, `match_evidence` = `candidates()` output for that report (so a reviewer's evidence is retained), `actor_role` = `"reviewer"` for proposed else `"adjudicator"`.

`protocol_version_id` for every event: `select(ResearchProtocol.current_approved_version_id).where(collection_id == ...)` first row or `None`.

Idempotency: same key + same fingerprint ⇒ `AppendDecisionResult.replayed=True` ⇒ return current state without re-applying; different content ⇒ `DecisionIdempotencyConflict` → 409 (pattern `protocol_service.py:531-`).

Schemas: `ReportObservationResponse{source_id, run_id, match_method, evidence}`, `ReportResponse{id, title_snapshot, identifiers: dict, study_id, study_link_status, study_link_rationale, merged_into_report_id, observations: list}`, `ReportCandidatesResponse{report_id, suggested: list[{report_id, reason: "title_year"}], conflicts: list}`, `StudyLinkRequest{study_id: UUID|None, status: Literal[...], rationale: str(1..10_000), idempotency_key: str(1..240)}`, `ReportMergeRequest{surviving_report_id, merged_report_ids: list[UUID] (min 1), rationale, idempotency_key}`, `ReportSplitRequest{source_ids: list[UUID] (min 1), rationale, idempotency_key}`, `IdentityEventResponse{seq, event_type, actor_user_id, actor_role, reason, payload, occurred_at}`.

**Step 1: Failing test** — write the PostgreSQL test file of Task 8 first (it is the spec); until Task 6 it fails at import.
**Step 2:** Implement; `ruff check backend/src` clean; `mypy --ignore-missing-imports --follow-imports=silent backend/src/services/research_engine/identity_service.py` clean (added-file gate, `docs/engineering/backend.md` Ratchets).
**Step 3: Commit** `feat(research): report identity service (GOO-299)`

---

### Task 5: Observe at import time, in the step-completion transaction

**Files:**
- Modify: `backend/src/services/research_engine/run_lifecycle.py:169-176` (add `collection_id: UUID | None = None`), after `:298-299` add `if collection_id is not None and source_rows: await self.session.flush(); await observe_sources(self.session, collection_id=collection_id, sources=source_rows)`
- Modify: `backend/src/api/research_engine/runs.py:982-987` (pass `collection_id=canonical_project_id`; `resolve_project(..., EDIT)` at `:945-951` already took Workspace SHARE + Collection UPDATE, so the stream lock inside `observe_sources` follows the documented order)
- Modify: `backend/tests/unit/services/test_research_run_lifecycle.py` (one test: `collection_id=None` ⇒ no observation call; monkeypatch `observe_sources` to assert it receives the same rows in the same session)

`ResearchSource.id` is uuid5-stable per run/step/strategy (`discovery.py:128-133`, `test_paper_discovery.py:223`), so a resumed step re-inserts the same `source_id` and `UNIQUE(source_id)` makes the observation a no-op — this is the "repeated import" guarantee; the "concurrent import" guarantee is the stream lock.

**Run:** `pytest -q backend/tests/unit/services/test_research_run_lifecycle.py backend/tests/unit/services/test_paper_discovery.py` → green.
**Commit** `feat(research): record report observations on search-step completion (GOO-299)`

---

### Task 6: API routes + OpenAPI/TypeScript

**Files:**
- Create: `backend/src/api/research_engine/identities.py` (`router = APIRouter(prefix="/research-engine", tags=["research-engine-identities"])`, transport only — pattern `reviews.py:26-93`)
- Modify: `backend/src/api/research_engine/__init__.py`, `backend/src/main.py:85,667` (register with `prefix="/api/v1"`)
- Regenerate: `backend/openapi.json`, `frontend/src/types/generated/api.d.ts`

| Method + path | `resolve_project` action | Service |
|---|---|---|
| `GET /projects/{project_id}/reports` | VIEW | `list_reports` |
| `GET /projects/{project_id}/reports/history` | VIEW | `history` |
| `GET /projects/{project_id}/reports/{report_id}/candidates` | VIEW | `candidates` |
| `POST /projects/{project_id}/reports/{report_id}/study-link` | REVIEW if `status=="proposed"` else ADJUDICATE | `link_study` |
| `POST /projects/{project_id}/reports/merge` | ADJUDICATE | `merge_reports` |
| `POST /projects/{project_id}/reports/{report_id}/split` | ADJUDICATE | `split_report` |

Every handler: `context = await resolve_project(db, project_id, current_user.id, action)` then service then `await db.commit()` happens in the service caller — keep the one boundary in the route's service call wrapper exactly as `protocols.py` does for approvals; routers never call `.commit()` (`backend/tests/unit/architecture/test_workspace_boundaries.py` only guards workspace routes, but follow it anyway). `DecisionIdempotencyConflict` → 409; missing/foreign report → 404 with the fixed message "Report not found"; archived → 409 from `resolve_project` (`project_access.py:232-235`).

**Steps:** write `backend/tests/unit/api/test_research_identity_routes.py` (FastAPI `TestClient` with `resolve_project` + service monkeypatched; asserts 404 on foreign report, 403 when role missing, status→action mapping) → red → implement → green → `python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types` → `python scripts/ci/generate_openapi.py --check` prints nothing and exits 0 → commit both generated files with the router: `feat(research): report identity API (GOO-299)`. Additive only, so no `api-breaking-approved` label.

---

### Task 7: Backfill script

**Files:**
- Create: `backend/scripts/backfill_report_identities.py`
- Create: `backend/tests/unit/scripts/test_backfill_report_identities.py` (SQLite, pattern `backend/tests/unit/services/test_research_review_service.py:204-216`)

Join `ResearchSource.run_id → ResearchRun.blueprint_id → ResearchBlueprint.project_id → ResearchProject.collection_id` (`research_blueprint.py:17`, `research_project.py:20-25`), group by collection, skip sources with `collection_id IS NULL` (legacy engine projects → reported as `mapping_required`, count only), and per collection call `observe_sources` in one transaction. `--dry-run` prints `{collections, sources, would_create_reports, skipped_rag_store, mapping_required}`; `--apply` commits per collection; rerun is a no-op (assert in test: second apply creates 0 rows). Ordering is `ResearchSource.created_at, id` so the result is deterministic.

**Commit** `chore(research): backfill report identities from existing sources (GOO-299)`

---

### Task 8: One real PostgreSQL integration test

**Files:**
- Create: `backend/tests/integration/test_report_identity_postgres.py` (marker `integration`)

Fixture `identity_engine`: copy `decision_engine` (`backend/tests/integration/test_research_decision_ledger.py:47-72`) — own schema, `Base.metadata.create_all`, then drop the four new tables and run `c9d2e4f6a8b1.upgrade` through `Operations(MigrationContext.configure(...))` so the migration itself is proven. Seed with `seed_canonical_project_scope` (`backend/tests/integration/research_engine_postgres_support.py:84-140`) plus a `ResearchProject`/blueprint/run so `research_sources.run_id` resolves; assign REVIEWER to user R, ADJUDICATOR to user A, a VIEWER member V with no role, and a foreign-org user F.

Tests (each `@pytest.mark.asyncio`):
1. `test_constraints` — duplicate `(collection, kind, value)` raises `IntegrityError`; deleting a `research_sources` row referenced by an observation raises (RESTRICT); deleting a report with observations raises.
2. `test_repeated_and_concurrent_imports_are_idempotent` — `observe_sources` twice with the same rows ⇒ 1 report, 2 identifiers, 2 observations both times; then two sessions `asyncio.gather` importing two *different* source rows sharing one DOI ⇒ exactly 1 report, 2 observations, no `IntegrityError`.
3. `test_link_reload_history` — R proposes (role reviewer), A confirms, new session `list_reports` shows `confirmed` and `history()` replays 2 events with correct `prior_study_id/prior_status`; V calling confirm ⇒ 403; F ⇒ 404; archived collection (`UPDATE collections SET research_status='archived'`) ⇒ 409.
4. `test_concurrent_merges_serialize_without_partial_rewiring` — reports r1, r2, r3. Session 1 begins `merge_reports(surviving=r1, merged=[r2])` and pauses after the lock (inject an `asyncio.Event` via monkeypatched `append_decision`); session 2 starts `merge_reports(surviving=r3, merged=[r2])`; `_wait_until_blocked` (`test_research_authorization_concurrency.py:64-71`) proves a real lock wait; release; session 1 commits; session 2 raises 409 "Report already merged"; assert every r2 observation/identifier points at r1 and `r3` has none.

**Mutation verification** (docs/engineering/testing.md procedure), recorded in the test docstring:
- Guard: `identity_service.merge_reports` → the `lock_aggregate_stream(...)` call (file:line filled in at implementation). Comment it out → `pytest -q backend/tests/integration/test_report_identity_postgres.py -k concurrent_merges` fails with `assert r3_observations == []` (r2's rows split between r1 and r3). Restore, `git diff --exit-code backend/src/services/research_engine/identity_service.py`, rerun green.
- Guard: `on_conflict_do_nothing(index_elements=["source_id"])` in `observe_sources` → replace with plain insert → `-k idempotent` fails with `IntegrityError ... uq_research_report_observation_source`. Restore, rerun.

**Run:** `pytest -q backend/tests/integration/test_report_identity_postgres.py` (needs Docker testcontainers or `RESEARCH_DECISION_DATABASE_URL`; locally report `NOT RUN` if neither, CI Integration job runs it).
**Commit** `test(research): PostgreSQL proof for report identity guards (GOO-299)`

---

### Task 9: Frontend panel (Workflow tab)

**Files:**
- Modify: `frontend/src/services/researchEngineService.ts` (append aliases from `components['schemas']['ReportResponse' | 'StudyLinkRequest' | 'ReportMergeRequest' | 'IdentityEventResponse']`, pattern lines 1-20, and 6 thin `api.get/post` functions, pattern `:82-93`)
- Create: `frontend/src/components/research-engine/ReportIdentityPanel.tsx`
- Modify: `frontend/src/components/research-engine/ProjectWorkflow.tsx` (render `<ReportIdentityPanel projectId={project.id} />` after `<ProtocolPanel …/>`)
- Create: `frontend/src/components/research-engine/__tests__/ReportIdentityPanel.test.tsx`
- Modify: `frontend/src/components/research-engine/__tests__/ProjectWorkflow.test.tsx:22-33` (add `vi.mock('../ReportIdentityPanel')` and the new service fns to the service mock)

Panel = one `<section>` in the same style as `ProjectWorkflow.tsx:189-274`: a table (title snapshot, identifiers, observation count, study status), a per-row "Evidence" disclosure (`<details>`) listing observations' `match_method` + `evidence.conflicts` and that report's history events, and two actions: **Confirm / Dispute** (prompts rationale, `POST study-link` with `idempotency_key = crypto.randomUUID()`) and **Merge into…** (`<select>` of other reports + rationale). Split is API-only. `# ponytail: split UI when a reviewer asks for it.` Errors: `role="alert"` with the API detail. Queries: `useQuery(['research-reports', projectId])`, invalidate on mutation success.

Test: renders rows from mocked `listReports`, shows `conflicts` in evidence, confirm calls `linkStudy(projectId, reportId, {status:'confirmed', …})` and invalidates.

**Run:** `pnpm --dir frontend exec vitest run src/components/research-engine/__tests__/ReportIdentityPanel.test.tsx src/components/research-engine/__tests__/ProjectWorkflow.test.tsx`; `pnpm --dir frontend type-check`; `scripts/ci/run_local_ci.sh --frontend` before push (lint:changed ratchet).
**Commit** `feat(frontend): report identity panel in project workflow (GOO-299)`

---

### Task 10: Gates + PR

1. `scripts/ci/run_local_ci.sh --base origin/develop --frontend` — all blocking gates green (`ruff`, black/isort on changed files, mypy on added files, `check_alembic.py`, OpenAPI drift, unit suites).
2. `pytest -q backend/tests/unit/services/test_paper_discovery.py backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/services/test_research_run_lifecycle.py backend/tests/unit/architecture` → green.
3. PR to `develop` from `feat/goo-299-study-identity`, description lists the mutation-verification transcript for both guards, `oasdiff` changelog should show only added paths.

---

## Live evidence for Linear closure

Collect after deploy to `rag-dev` (blocked until a backend origin is reachable; see `docs/plans/2026-09-29-academic-r0-r1-closure.md` hard blocker):

1. **Migration** — `kubectl -n rag-dev exec deploy/backend -- alembic current` shows `c9d2e4f6a8b1 (head)`.
2. **Backfill** — `python backend/scripts/backfill_report_identities.py --dry-run` then `--apply` output from a pod; second `--apply` shows 0 new rows; `SELECT count(*) FROM research_sources` unchanged before/after (originals preserved).
3. **Reviewer journey** (authenticated as `allocs16@gmail.com`, project with REVIEWER + ADJUDICATOR roles): screenshot of the panel with evidence disclosure open; `POST .../study-link` (proposed) 200; confirm 200; browser reload shows `confirmed`; `GET .../reports/history` JSON with 2 events incl. `prior_study_id`, `protocol_version_id`, `actor_role`.
4. **Denials** — same report id from a user in another org → 404 body; VIEWER-only member confirm → 403 `adjudicator role required`; archived project merge → 409.
5. **Idempotency** — replay the confirm request with the same `idempotency_key` → 200 same state; with a changed rationale → 409.
6. **CI** — Integration job URL where `test_report_identity_postgres.py` passed at the merge SHA, plus the mutation-verification transcript pasted in the PR.
7. **Gold fixture** — `pytest -q backend/tests/unit/services/test_report_identity_matching.py -k gold -v` output showing precision 1.0 / recall value.
