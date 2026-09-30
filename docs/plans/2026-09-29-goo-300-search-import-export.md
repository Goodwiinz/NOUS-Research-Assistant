# GOO-300 Search Import / Corpus Export Plan (Academic R2)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let a researcher import a bounded result file from a database that has no supported API (RIS, CSV, NBIB), keep an immutable receipt plus every original record (accepted *and* rejected), give accepted records GOO-299 report identities, record protocol-required citation chasing as receipts, and download one versioned, restriction-filtered corpus package. The package must rebuild record→report identity, literal GOO-298 requests/limits/failures and identity decisions without writing anything.

**Architecture:** Two new tables, no new ledger. `research_import_receipts` is one immutable row per imported file or citation chase. `UNIQUE(collection_id, dedup_key)` is the idempotency guard, and `resolve_project(EDIT)`'s Collection `FOR UPDATE` (`backend/src/services/research_engine/project_access.py:195-205`) serializes writers. `research_import_records` holds one row per parsed record: `raw` original text, `parsed` JSON, `status`, `rejection_reason`, and the GOO-299 link (`report_id`, `match_method`, `evidence`) on the row itself. Records are *not* `ResearchSource` rows because `research_sources.run_id` is `NOT NULL` (`backend/src/models/research_source.py:17`) and an external import has no run. Inventing a run would fabricate provenance. Identity assignment reuses the observe loop in `identity_service.observe_sources` (`backend/src/services/research_engine/identity_service.py:265-361`) and the same per-project `research_identity` stream lock. Merge and split already re-point rows by `report_id` (`identity_service.py:571-577`). Import records join that tuple, and their moves are recorded in identity events **schema 2**. Export is a pure read over the project: runs → `_search_receipts_v1` journal (`backend/src/services/research_engine/search_receipts.py:16,106`), search-step coverage, `research_sources`, import receipts/records, reports/identifiers/observations, and `replay_decisions`. It is served through the same `Response` + `Content-Disposition` download path as `/runs/{id}/export` (`backend/src/api/research_engine/runs.py:537-561`), behind `resolve_project(..., VIEW)`.

**Tech Stack:** FastAPI (`UploadFile` + `Form`, `python-multipart==0.0.32` already pinned in `backend/requirements-minimal.txt:22`), SQLAlchemy async, Alembic, stdlib `csv`/`re`/`hashlib`/`zipfile`, httpx (OpenAlex), PostgreSQL integration tests, openapi-typescript, Next.js + TanStack Query + Vitest.

---

## Decisions (read before Task 1)

| Question | Decision | Why |
|---|---|---|
| Where do imported records live? | `research_import_records`, with `report_id/match_method/evidence` on the record row. `research_report_observations` is untouched. | `ResearchSource.run_id` is NOT NULL. Changing `ReportObservationResponse.source_id/run_id` (`backend/src/schemas/research_engine.py:382-386`) to optional is a response-narrowing change that oasdiff can flag once #1752 is on `develop`. `ReportResponse` gets one additive field, `imported_records: list[ImportedRecordObservation] = []`. |
| Idempotency key | `dedup_key = "file:{sha256}:{format}:{PARSER_VERSION}"` for files. Chases use `"chase:{client idempotency_key}"`. | Identical bytes return the same receipt (`replayed=true`, HTTP 200). New bytes or a new parser version give a new receipt (201). Same bytes with a *different* declaration → 409 `import_declaration_conflict`, never a silent overwrite. |
| "Changed contents create a distinguishable version" | `lineage_key = sha256(database + "\x1f" + (query_text or ""))`, `version = max(version in lineage) + 1`, `previous_receipt_id` = latest in lineage. Computed under the Collection lock. | Distinguishable by id, hash, version and back-pointer. Nothing is overwritten. |
| Declared vs observed | `declared` JSONB is exactly the validated `ImportDeclaration`: `database`, `query_text?`, `search_date?`, `exported_at?`, `redistribution`, `notes?`. `observed` JSONB is server-set only: `imported_at`, `actor_user_id`, `filename`, `byte_size`, `sha256`, `format`, `parser_version`, `protocol_version_id`, `counts`. | This is the ticket's provenance-certainty split. A missing `query_text`/`search_date` is stored as `null` and exported as `"not_declared"`, never inferred from the file (boundary: no fabricated query or date for legacy files). |
| Bounds | File ≤ 5 MiB, ≤ 5,000 records, record raw ≤ 64 KiB, UTF-8 only (BOM stripped). Over a bound → 413/422 with nothing persisted. | "Bounded". A whole-file failure persists nothing. A per-record failure persists the record as `rejected`. |
| Whole-file vs per-record errors | Whole file: `unsupported_format`, `unsupported_encoding`, `file_too_large`, `too_many_records`, `no_records`, `csv_missing_title_column` → 422/413 `{code, message}`. Per record: `missing_title`, `malformed_line`, `unterminated_record`, `record_too_large` → row with `status='rejected'`. | Deterministic accounting: `accepted + rejected == parsed chunks`, enforced by a CHECK on the receipt. Nothing vanishes. |
| Restricted content on export | One policy dict in `corpus_export.py` (`# ponytail: static table; per-source licence metadata when a provider exposes it`): (a) receipts with `redistribution="restricted"` (the default) export no `raw` and no `abstract`; (b) `rag_store` sources export `{id, connector_type, title}` only; (c) `full_text` is stripped from every `metadata.provenance[]` snapshot (`discovery.py:81` stores `asdict(source)`, which includes `full_text`); (d) provider abstracts leave only for `openalex`/`arxiv` (CC0 metadata). Every withheld field is listed in `omissions[]`. | "Restricted content never leaves". (d) is a judgment call: flip the dict if legal says otherwise. |
| Citation chasing | Smallest honest slice: OpenAlex only. Add `OpenAlexConnector.citations(work_id, direction, max_results, search_trace)`: backward = `GET /works/{id}` `referenced_works` → `GET /works?filter=openalex_id:W1\|W2…`, forward = `GET /works?filter=cites:{id}` with cursor. No other connector does citation lookup today (`grep -rn "cited_by\|referenced_works" backend/src` is empty). | Metadata only, no PDF, capped at `MAX_CONNECTOR_RESULTS` (50, `step_executor.py:64`) and `SEARCH_TIMEOUT_SECONDS` (`discovery.py:17`). The seed needs an `openalex` or `doi` identifier; otherwise → 422 `seed_not_resolvable`. |
| "When the protocol requires it" | Read `snapshot.sources_search.citation_chasing = {"required": bool, "directions": [...]}` from the approved protocol version (`ProtocolSnapshot.sources_search`, `schemas/research_engine.py:285-287`). Coverage/export report `required`, the receipts per direction, and `missing_directions`. Nothing blocks. | The free-form `sources_search` dict already exists. We report; we never assert a chase happened. |
| Chase transaction shape | The service owns two short transactions: (1) `resolve_project(EDIT)`, load the seed, check `dedup_key`, then `rollback`. (2) Network with no locks held. (3) `resolve_project(EDIT)` again (authority rechecked after the wait, as runs.py:941-951 does), recheck `dedup_key`, insert, observe. The route commits. | Never hold Collection/stream locks across provider I/O. |
| Export scope | Project-level, `GET /research-engine/projects/{project_id}/corpus/export?format=json\|zip`, `resolve_project(VIEW)`. It covers every run of the mapped engine project, including failed and partial ones. Archived projects stay exportable. | The ticket asks to reuse the download path plus `resolve_project` VIEW. `ExportService.export` is owner-only (`export_service.py:79-104`) and run-scoped, so it isn't extended. |
| Round trip | `verify_package(bytes) -> Reconstruction` is pure. It checks `body_sha256`, rebuilds `record → final report` (following `merged_into_report_id`), re-runs the identity transition rules over the packaged events, and returns requests/limits/failures per execution. Re-importing a package into another project is **out of scope**. | "Round trip reconstructs … without changing retained history". Export performs zero writes. The integration test asserts row counts and `research_decision_streams.next_seq` are unchanged. |
| Coverage | `POST /projects/{id}/corpus/coverage` `{known: [{kind: value}] ≤ 1000}` (read-only, VIEW) returns `found`, `missing`, `recall`, `searched[]` (provider/import/chase with executed or declared dates) and `not_searched` (registry connectors never used, `connectors/registry.py:34-49`). Date window is `"no date filter (connector capability date_filter=false)"` for API providers (`registry.py:34-49`) and the declared `search_date` or `"not_declared"` for imports. It always returns `exhaustive: false` plus a fixed statement. | Names providers, windows and gaps. Never claims exhaustiveness. |
| Roles | Import and chase: `ResearchAction.EDIT` (workspace editor+). Export and coverage: `VIEW`. Merge/split of imported records: unchanged ADJUDICATE. | Imports are search execution, not a decision. Same as run creation. |

**Migration head:** the branch head is `c9d2e4f6a8b1` (`backend/alembic/versions/c9d2e4f6a8b1_create_report_identities.py:11-12`). New revision `d4e6f8a0b2c3` (≤ 32 chars, `scripts/ci/check_alembic.py:40`), with `down_revision = "c9d2e4f6a8b1"`. If #1752 is rebased under a new head, follow it.

---

### Task 1: Pure bounded parsers

**Files:**
- Create: `backend/src/services/research_engine/search_import.py`
- Create: `backend/tests/unit/services/test_search_import_parsers.py`
- Create: `backend/tests/fixtures/search_import/{valid.ris,partial.ris,valid.csv,valid.nbib}`

```python
PARSER_VERSION = "nous.search-import.v1"
FORMATS = ("ris", "csv", "nbib")
MAX_BYTES, MAX_RECORDS, MAX_RECORD_BYTES = 5 * 1024 * 1024, 5_000, 64 * 1024

class ImportFormatError(ValueError):          # whole file → 413/422, nothing persisted
    def __init__(self, code: str, status: int = 422): ...

@dataclass(frozen=True)
class ParsedRecord:
    index: int; raw: str; parsed: dict[str, Any]; rejection_reason: str | None

def parse(fmt: str, data: bytes) -> list[ParsedRecord]: ...
```

- **RIS:** tag line `^([A-Z][A-Z0-9])  - ?(.*)$`. A record runs from `TY` to `ER`. Mapping: `TI/T1`→title, `AU/A1`→authors, `PY/Y1/DA`→year (first 4 digits only), `AB/N2`→abstract, `DO`→doi, `UR`→url, `JO/T2`→venue. `AN` is kept in `parsed.fields` only; it is never mapped to `pmid` (it is database-specific).
- **NBIB (MEDLINE):** a record is separated by a blank line. `^([A-Z]{2,4})\s*- (.*)` plus 6-space continuation lines. `PMID`→pmid, `PMC`→pmcid, `TI`, `AU/FAU`, `DP`→year, `AB`, and `LID/AID … [doi]`→doi.
- **CSV:** `csv.DictReader`, headers matched case-insensitively against `title, authors, year, doi, pmid, pmcid, arxiv, abstract, venue, url`. Missing `title` column → `csv_missing_title_column`. Unknown columns stay in `raw` only.

Identifiers are normalized by `extract_identifiers_from_mapping` (`discovery.py:32-61`). An invalid DOI is dropped and noted in `parsed.dropped_identifiers`; it does not reject the record. `parsed["identifiers"]` uses the same shape `observe_sources` reads (`identity_service.py:313`).

**Step 1: Failing tests:**
- `test_ris_partial_file_accounting_is_deterministic`: 3 valid + 1 without `TI` + 1 unterminated → `[accepted×3, missing_title, unterminated_record]`, indexes 0..4, and the same output twice.
- `test_rejected_record_keeps_original_raw`.
- `test_unknown_format_and_non_utf8_raise_explicit_codes` (`unsupported_format`, `unsupported_encoding`).
- `test_too_many_records_and_file_too_large_raise` (413).
- `test_nbib_maps_pmid_pmcid_doi`.
- `test_csv_without_title_column_raises`.
- `test_no_query_or_date_inferred_from_file`: the parser output has no `query`/`search_date` keys.

**Step 2:** `pytest -q backend/tests/unit/services/test_search_import_parsers.py` → `ModuleNotFoundError`.
**Step 3:** Implement (~150 lines, stdlib only).
**Step 4:** Green. Then `mypy --ignore-missing-imports --follow-imports=silent backend/src/services/research_engine/search_import.py`, which the added-file gate requires (`docs/engineering/backend.md` Ratchets).
**Step 5: Commit** `feat(research): bounded RIS/CSV/NBIB search-result parsers (GOO-300)`

---

### Task 2: Models + migration

**Files:**
- Create: `backend/src/models/research_import.py` (`ResearchImportReceipt`, `ResearchImportRecord`). Plain `Base`, never deleted, same style as `backend/src/models/research_report.py:23-51`.
- Modify: `backend/src/models/__init__.py` (export)
- Create: `backend/alembic/versions/d4e6f8a0b2c3_create_search_imports.py`
- Modify: `backend/tests/integration/test_academic_wave_migrations.py` (add a round-trip test next to `test_report_identity_migration_upgrade_downgrade_round_trip`, `:478-500`)
- Modify: `backend/tests/unit/ci/test_daily_research_brief_migration.py:75` only if its ancestry assertion lists heads

```python
class ResearchImportReceipt(Base):      # research_import_receipts
    id, collection_id FK collections RESTRICT,
    kind String(16) CHECK IN ('file_import','citation_chase'),
    dedup_key String(160) NOT NULL, lineage_key String(64) NOT NULL,
    version Integer NOT NULL CHECK (version >= 1),
    previous_receipt_id FK research_import_receipts RESTRICT NULL,
    declared JSONB NOT NULL, observed JSONB NOT NULL,
    parsed_count, accepted_count, rejected_count Integer NOT NULL,
    actor_user_id FK users RESTRICT, created_at
    UNIQUE(collection_id, dedup_key)                         # idempotency guard
    CHECK (accepted_count + rejected_count = parsed_count)   # nothing vanishes
    INDEX(collection_id), INDEX(collection_id, lineage_key)

class ResearchImportRecord(Base):       # research_import_records
    id, collection_id, receipt_id FK RESTRICT, record_index Integer,
    status String(16) CHECK IN ('accepted','rejected'),
    raw Text NOT NULL, parsed JSONB NOT NULL default '{}',
    rejection_reason String(64) NULL,
    report_id FK research_reports RESTRICT NULL, match_method String(32) NULL, evidence JSONB NULL,
    created_at
    UNIQUE(receipt_id, record_index)
    CHECK ((status = 'rejected') = (rejection_reason IS NOT NULL))
    CHECK (status = 'accepted' OR report_id IS NULL)
    INDEX(collection_id), INDEX(report_id)
```

The migration creates both tables and calls `_deny_data_api` for each (copy `c9d2e4f6a8b1_create_report_identities.py:20-32`). The downgrade drops them in reverse order.

**Verify:** `(cd backend && python ../scripts/ci/check_alembic.py)` → `single head 'd4e6f8a0b2c3'`. `alembic upgrade head --sql` renders offline (no Docker). `pytest -q backend/tests/unit/architecture`.
**Commit** `feat(research): import receipt and record tables (GOO-300)`

---

### Task 3: Identity plumbing for imported records

**Files:**
- Modify: `backend/src/services/research_engine/identity_service.py`
- Modify: `backend/src/services/research_decisions/ledger.py:52-73` (schema-2 keys), `:236-298` (validate the new list)
- Modify: `backend/src/schemas/research_engine.py:389-431` (`ReportResponse.imported_records`, `ReportSplitRequest.import_record_ids`)
- Modify: `backend/tests/unit/services/test_research_decision_ledger.py`, `backend/tests/unit/services/test_research_identity_service.py`

Changes, each as small as the existing code allows:
1. Extract the body of `observe_sources` after the lock (`identity_service.py:276-338`: load observed set, build identifier index, `assign_report`, create report/identifiers) into `_assign(db, collection_id, items: list[tuple[title, raw_ids]]) -> list[ReportAssignment-with-id]`. `observe_sources` keeps its signature and behavior, so the existing tests stay green.
2. Add `observe_import_records(db, *, collection_id, records: Sequence[ResearchImportRecord])`. It takes `_lock` and runs `_assign` over accepted records only. It sets `report_id/match_method/evidence` *before* the insert flush, so the CHECK in Task 2 holds.
3. `merge_reports`: add `ResearchImportRecord` to the re-point tuple at `:571`. Select `moved_import_record_ids` the same way `moved_source_ids` is selected (`:553-563`). Append with `event_schema_version=2`.
4. `split_report`: `ReportSplitRequest` gets `import_record_ids: List[UUID] = []`, and `source_ids` drops `min_length=1` for a `model_validator` "at least one of source_ids/import_record_ids". Relaxing a request constraint is additive. `moving`/`staying` include import records (title from `parsed["title"]`). Payload v2 carries `moved_import_record_ids`.
5. Ledger: add `("identity.report_merged", 2)` and `("identity.report_split", 2)` as the v1 keys `| {"moved_import_record_ids"}`. `_validate_identity_payload` runs `_validated_uuid_list` on it when present. Transitions are unchanged, and v1 events replay as before.
6. `_responses` (`:196-257`) fills `imported_records=[{import_record_id, receipt_id, match_method, evidence}]` with one extra query.

**Tests first:**
- `test_identity_merge_v2_requires_import_ids_key`
- `test_v1_merge_events_still_replay`
- `test_observe_import_records_attaches_by_identifier_and_skips_rejected`
- `test_split_can_move_only_import_records`
- `test_merge_moves_import_records_and_records_them`

**Run:**
```sh
pytest -q backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/services/test_research_identity_service.py backend/tests/unit/services/test_report_identity_matching.py
```
**Commit** `feat(research): imported records join report identity (GOO-300)`

---

### Task 4: Import service

**Files:**
- Create: `backend/src/services/research_engine/corpus_service.py`
- Modify: `backend/src/schemas/research_engine.py` (append after `IdentityEventResponse`, `:433`)
- Create: `backend/tests/unit/services/test_corpus_service.py` (SQLite; pattern `backend/tests/unit/services/test_research_identity_service.py`)

```python
async def import_file(db, context: ProjectContext, actor_user_id, declaration: ImportDeclaration,
                      *, fmt: str, filename: str, data: bytes) -> tuple[ImportReceiptResponse, bool]
async def list_receipts(db, *, collection_id) -> list[ImportReceiptResponse]
async def get_receipt(db, *, collection_id, receipt_id) -> ImportReceiptDetail  # + records, rejections first
```

`import_file` never commits. It assumes the caller already ran `resolve_project(EDIT)`, which holds the Collection row lock.
1. `sha = sha256(data)` and `dedup_key`. Look up `(collection_id, dedup_key)`. If a receipt exists and its `declared == declaration.model_dump(mode="json")`, return `(receipt, False)`; if the declarations differ → 409 `import_declaration_conflict`. **This lookup is the guard under test (Task 8).**
2. `records = search_import.parse(fmt, data)`. `ImportFormatError` → `HTTPException(status, {"code", "message"})` with a fixed message per code, never the exception text.
3. Compute the lineage version/previous under the same lock. Insert the receipt. `observed` gets `protocol_version_id` via `identity_service._protocol_version_id` (`identity_service.py:99-112`; make it public as `current_protocol_version_id`) and `filename` trimmed to basename[:255].
4. Build a `ResearchImportRecord` per `ParsedRecord`, then `observe_import_records`, then `db.add_all` + flush. The counts come from `records`, and the CHECK proves them.

Schemas:
- `ImportDeclaration{database: str(1..200), query_text: str(1..20_000)|None, search_date: date|None, exported_at: datetime|None, redistribution: Literal["restricted","allowed"]="restricted", notes: str(..2000)|None}`
- `ImportReceiptResponse{id, kind, version, previous_receipt_id, declared, observed, parsed_count, accepted_count, rejected_count, replayed: bool, created_at}`
- `ImportRecordResponse{id, record_index, status, rejection_reason, parsed, report_id}`. `raw` is omitted for restricted receipts, same policy as export.
- `ImportReceiptDetail(ImportReceiptResponse){records: list[ImportRecordResponse]}`

**Tests:**
- `test_identical_bytes_return_same_receipt`
- `test_changed_bytes_create_version_2_with_back_pointer`
- `test_same_bytes_new_declaration_is_409`
- `test_parser_version_bump_creates_new_receipt` (monkeypatch `PARSER_VERSION`)
- `test_malformed_file_persists_nothing`
- `test_counts_match_rows`

**Commit** `feat(research): idempotent external search-result import receipts (GOO-300)`

---

### Task 5: OpenAlex citation chase receipts

**Files:**
- Modify: `backend/src/services/research_engine/connectors/openalex_connector.py`. Extract `_work_document(work)` from `:54-88` (search output unchanged), then add `citations(...)` with the same `begin_request`/`record_page` trace calls as `:42-100`.
- Modify: `backend/src/services/research_engine/corpus_service.py` (`chase_citations`)
- Create: `backend/tests/unit/services/test_openalex_citations.py` (`httpx.MockTransport`, pattern `backend/tests/unit/services/test_paper_provider_http.py:24`)

`chase_citations(db, *, project_id, user_id, data: CitationChaseRequest, connector=None)`:
- `CitationChaseRequest{seed_report_id, direction: Literal["backward","forward"], max_results: int(1..MAX_CONNECTOR_RESULTS)=MAX_CONNECTOR_RESULTS, idempotency_key: str(1..240)}`.
- Seed = a live report of this collection (a foreign or merged report gets 404, via `_live_reports`, `identity_service.py:156-193`) with an `openalex` identifier, else `doi` (`/works/doi:{doi}`). Without either → 422 `seed_not_resolvable`.
- Stored receipt: `kind='citation_chase'`, `declared={seed_report_id, direction, requested_limit}`, `observed={provider:"openalex", started_at, trace receipt via SearchTrace.as_receipt (connectors/base.py:238-280) incl. literal redacted requests, completion, error_type, protocol_version_id, protocol_requires}`.
- Each resulting work is an accepted record with `raw=json.dumps(work)`, `redistribution="allowed"` (OpenAlex metadata is CC0). A provider error with zero works still stores the receipt with `completion="failed"`: a failed chase is evidence, not nothing.
- Uses the three-phase transaction from the Decisions table.

**Tests:**
- `test_backward_fetches_referenced_ids_in_bounded_batches`
- `test_forward_uses_cites_filter_and_stops_at_limit`
- `test_no_pdf_url_is_fetched` (the transport asserts that every host is `api.openalex.org`)
- `test_seed_without_ids_is_422`
- `test_failed_provider_still_records_receipt`

**Commit** `feat(research): OpenAlex citation-chase receipts (GOO-300)`

---

### Task 6: Corpus export, verify, coverage

**Files:**
- Create: `backend/src/services/research_engine/corpus_export.py`
- Create: `backend/tests/unit/services/test_corpus_export.py`

```python
PACKAGE_SCHEMA = "nous.academic.corpus-export.v1"
MAX_EXPORT_RECORDS = 50_000          # over → 413 export_too_large, never truncated
async def build_package(db, context: ProjectContext) -> dict      # read-only
def render(package: dict, fmt: Literal["json","zip"]) -> tuple[bytes, str, str]  # bytes, media type, filename
def verify_package(data: bytes) -> Reconstruction                 # pure
async def coverage(db, context, known: list[dict[str, str]]) -> CoverageResponse
```

The package is `{"schema", "exported_at", "body_sha256", "body": {...}}`. `body` uses canonical JSON (`sort_keys`, stable `ORDER BY created_at, id` everywhere), so two exports with no writes in between have the same `body_sha256`:
- `project`: `{collection_id, name}`, `protocol`: `{current_approved_version_id, content_hash, citation_chasing}`.
- `searches[]`, one per run of `context.engine` (blueprint → runs): `{run_id, run_status, conformance_status, protocol_version_id, effective_plan_hash, strategies, executions}`. These are copied verbatim from `reproducibility_manifest["_search_receipts_v1"]`, and each search step's `output.coverage` is included, holding `providers{status, completion, requested_limit, returned, error_type, pages[].request}`, `partial` and `exhaustive:false` (`discovery.py:237-245`). This covers literal requests, limits and failures.
- `sources[]`: `research_sources` rows with `metadata.identifiers` + `provenance` (with `full_text` stripped). The restriction policy applies.
- `imports[]`: receipts plus records (`raw`/`abstract` per policy), including rejected records with their reasons.
- `identities`: `reports[]` (with `merged_into_report_id`, study link), `identifiers[]`, `observations[]`, `studies[]`, `decisions[]` = `identity_service.history` output, which is replay-validated.
- `counts`: per table, per receipt `{parsed, accepted, rejected}`, per provider `{returned, limit}`.
- `omissions[]`: `{scope, id, fields, reason}` for every withheld field, plus a `not_declared` entry for each missing `query_text`/`search_date`.
- `failures[]`: every execution with `status != "ok"`, every chase with `completion="failed"`.
- `coverage`: the `coverage()` output with `known=[]`.

The zip format holds `corpus.json`, `README.txt` (schema id and the fixed non-exhaustiveness statement) and nothing else (stdlib `zipfile`, as `backend/src/api/research/drafts.py:428-437` does). `# ponytail: RIS/CSV corpus renderings when a reviewer tool needs them.`

`verify_package` recomputes `body_sha256` and rejects a mismatch. It maps each observation/import record to its final report by following `merged_into_report_id`. It re-runs `_validate_identity_transitions` (`ledger.py:573-617`) over `decisions` (import that function; it's pure), and returns `{record_to_report, executions: {id: {requests, limit, completion, error_type}}, rejected: {...}}`.

**Unit tests:**
- `test_restricted_raw_and_full_text_never_serialized`: search the rendered bytes for sentinel strings seeded into restricted `raw`, `rag_store` provenance `full_text` and a pubmed abstract.
- `test_body_hash_stable`
- `test_verify_rejects_tampered_body`
- `test_coverage_never_claims_exhaustive_and_names_unsearched_connectors`
- `test_legacy_import_without_query_reports_not_declared`
- `test_export_too_large_is_413`

**Commit** `feat(research): versioned reconstructable corpus export (GOO-300)`

---

### Task 7: Routes + OpenAPI/TypeScript

**Files:**
- Create: `backend/src/api/research_engine/corpus.py`. Transport only; one `commit` per mutating route, as `identities.py:1-6` describes.
- Modify: `backend/src/api/research_engine/__init__.py`, `backend/src/main.py:80-88,671` (register with `prefix="/api/v1"`)
- Create: `backend/tests/unit/api/test_research_corpus_routes.py` (pattern `backend/tests/unit/api/test_research_identity_routes.py`)
- Regenerate: `backend/openapi.json`, `frontend/src/types/generated/api.d.ts`

| Method + path | Action | Notes |
|---|---|---|
| `POST /research-engine/projects/{project_id}/imports` (multipart `file`, `format`, `declaration` JSON form field) | EDIT | 201 new, 200 replayed. The body is read with `await file.read(MAX_BYTES + 1)` → 413 if longer, so the whole upload is never read unbounded. |
| `GET /research-engine/projects/{project_id}/imports` | VIEW | list |
| `GET /research-engine/projects/{project_id}/imports/{receipt_id}` | VIEW | records + rejections. A foreign receipt → 404 "Import not found". |
| `POST /research-engine/projects/{project_id}/citation-chases` | EDIT | the service owns the two phases, the route commits |
| `GET /research-engine/projects/{project_id}/corpus/export?format=json\|zip` | VIEW | `Response(content, media_type, Content-Disposition)`, same as `runs.py:557-561` |
| `POST /research-engine/projects/{project_id}/corpus/coverage` | VIEW | read-only |

Error bodies are fixed `{code, message}` (`docs/engineering/gotchas.md` API). Don't name the query param `format` if it shadows an import in the module; `runs.py:540` already uses `format` safely.

**Route tests:**
- foreign project → 404
- viewer import → 404 (EDIT)
- archived import → 409, archived export → 200
- replay → 200 with `replayed=true`
- the export sets an `attachment` Content-Disposition, and the zip unpacks to `corpus.json`

**Then:** `python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types && python scripts/ci/generate_openapi.py --check`. Commit both generated files with the router. The changes are additive (the relaxed `ReportSplitRequest.source_ids` min is a loosening), so the `api-breaking-approved` label isn't expected. Check the `oasdiff` changelog in the PR.
**Commit** `feat(research): search import, citation chase and corpus export API (GOO-300)`

---

### Task 8: One real PostgreSQL integration test

**Files:**
- Create: `backend/tests/integration/test_search_import_postgres.py` (marker `integration`)

The fixture `corpus_factory` copies `identity_factory` (`backend/tests/integration/test_report_identity_postgres.py:84-110`): drop the four identity tables and the two new tables, then run `c9d2e4f6a8b1.upgrade` followed by `d4e6f8a0b2c3.upgrade`, so both migrations are proven. `_seed` is imported from that module (`:113-185`: O owner, R reviewer, A adjudicator, V role-less viewer, F foreign org, run). The seed also makes O the editor path by being owner.

1. `test_import_commit_reopen_and_reimport`
   - O imports `partial.ris` → 201 with `3/2` accepted/rejected. **In a new session**, the receipt and 5 record rows reload, every accepted record has a `report_id`, and each rejected record has its reason and original `raw`.
   - The same bytes again return the same id with `replayed=True`, and the row counts are unchanged.
   - Edited bytes → `version=2`, `previous_receipt_id=first`.
   - The same bytes with another `query_text` → 409.
   - A record sharing a DOI with an existing `ResearchSource` observation attaches to that report.
2. `test_concurrent_duplicate_import_yields_one_receipt`: two sessions each `resolve_project(EDIT)` and then `import_file` on identical bytes via `asyncio.gather`. Session 2 blocks on the Collection lock (prove it with `_wait_until_blocked`, `test_report_identity_postgres.py:463-472`). The result is exactly 1 receipt, both calls return its id, one call reports `replayed=True`, and there are 5 records rather than 10.
3. `test_downloaded_package_matches_persisted_rows`: seed a run manifest with `_search_receipts_v1` (one ok execution, one `timed_out`), matching `ResearchSource` rows (openalex, plus `rag_store` with a `full_text` sentinel in provenance), a restricted import with a sentinel in `raw`, and A merging two reports through `identity_service.merge_reports`.
   - Snapshot the table counts and `next_seq`. Call the export route function for `json` and `zip`, then parse the bodies.
   - `verify_package` passes. `record_to_report` equals the DB after merge resolution. `decisions` equals `history()`. The executions' requests/limits/failures equal the manifest. No sentinel appears in the bytes. The counts equal the rows. Counts and `next_seq` are unchanged afterwards. A second export has the same `body_sha256`.
4. `test_tenancy`:
   - F import/export → 404; V import → 404 but V export → 200.
   - A revoked member (`WorkspaceMember.is_deleted=True`) → 404.
   - An archived collection makes import → 409 while export → 200.
   - A soft-deleted workspace → 404.
   - A foreign project's receipt id → 404.

**Mutation verification** (per `docs/engineering/testing.md`), recorded in the test docstring:
- Guard: the `dedup_key` lookup in `corpus_service.import_file` (fill in file:line at implementation). Comment it out, and `-k concurrent_duplicate` fails with `IntegrityError … uq_research_import_receipt_dedup` (the second writer hits the unique constraint after the lock releases). Restore, `git diff --exit-code backend/src/services/research_engine/corpus_service.py`, rerun green.
- Guard: the `full_text` strip in `corpus_export` fails `-k downloaded_package` on the sentinel assertion. Restore, rerun.

**Run:** `pytest -q backend/tests/integration/test_search_import_postgres.py`. It needs testcontainers or `RESEARCH_DECISION_DATABASE_URL`. Locally, report `NOT RUN` if neither is available; the CI Integration job runs it.
**Commit** `test(research): PostgreSQL proof for import idempotency and corpus export (GOO-300)`

---

### Task 9: Frontend corpus panel (Workflow tab)

**Files:**
- Modify: `frontend/src/services/researchEngineService.ts`. Add aliases from `components['schemas'][...]` for `ImportDeclaration`, `ImportReceiptResponse`, `ImportReceiptDetail` and `CoverageResponse`. Add `importSearchResults` (`api.upload`/FormData, `frontend/src/services/api-client.ts:451`), `listImports`, `getImport` and `downloadCorpus(projectId, format)` via `api.download` (`api-client.ts:585`, same as `downloadRunExport` at `researchEngineService.ts:180-186`).
- Create: `frontend/src/components/research-engine/CorpusPanel.tsx`
- Modify: `frontend/src/components/research-engine/ProjectWorkflow.tsx:149`. Render `<CorpusPanel projectId={project.id} readOnly={archived} />` after `ReportIdentityPanel`.
- Create: `frontend/src/components/research-engine/__tests__/CorpusPanel.test.tsx`. Also modify `__tests__/ProjectWorkflow.test.tsx` (mock the panel).

The panel is one `<section>`:
- An import form with file, format `<select>` (RIS/CSV/NBIB), database, query text, search date `<input type="date">`, and a redistribution `<select>` defaulting to restricted. Labels say "declared by you".
- A receipts table showing version, database, declared vs observed dates, and accepted/rejected counts, with a `<details>` listing rejections (index, reason, raw when allowed).
- Two buttons: "Download corpus (JSON)" and "(ZIP)".
- The text "Coverage is not exhaustive" next to the download buttons. Errors render with `role="alert"`. Citation chase and coverage stay API-only. `# ponytail: UI when reviewers ask.`

**Test:** it renders receipts, shows rejection reasons, a replayed import shows the "already imported" notice, and download calls `downloadCorpus(projectId, 'zip')`.

**Run:**
```sh
pnpm --dir frontend exec vitest run src/components/research-engine/__tests__/CorpusPanel.test.tsx src/components/research-engine/__tests__/ProjectWorkflow.test.tsx
pnpm --dir frontend type-check
```
**Commit** `feat(frontend): corpus import/export panel (GOO-300)`

---

### Task 10: Gates + PR

1. `scripts/ci/run_local_ci.sh --base origin/develop --frontend` — ruff, black and isort on changed files, mypy on the added `search_import.py`, `corpus_service.py`, `corpus_export.py`, `research_import.py`, `corpus.py`, then `check_alembic.py`, OpenAPI drift, unit suites and `lint:changed`.
2. `pytest -q backend/tests/unit/services/test_search_import_parsers.py backend/tests/unit/services/test_corpus_service.py backend/tests/unit/services/test_corpus_export.py backend/tests/unit/services/test_openalex_citations.py backend/tests/unit/services/test_research_decision_ledger.py backend/tests/unit/services/test_research_identity_service.py backend/tests/unit/services/test_paper_discovery.py backend/tests/unit/api/test_research_corpus_routes.py backend/tests/unit/architecture` → green.
3. Open the PR from `feat/goo-300-search-import-export` with base `feat/goo-299-study-identity` (#1752), and retarget it to `develop` after #1752 merges. The description includes both mutation transcripts and the oasdiff changelog (additive only).

---

## Authenticated journey list (Linear closure evidence)

Collect on `rag-dev` after deploy, as `allocs16@gmail.com`, on a project with an approved protocol whose `sources_search.citation_chasing.required=true`. This is blocked until a backend origin is reachable (`docs/plans/2026-09-29-academic-r0-r1-closure.md` hard blocker).

1. **Migration:** `kubectl -n rag-dev exec deploy/backend -- alembic current` → `d4e6f8a0b2c3 (head)`.
2. **Import:** in the Workflow tab, upload `partial.ris` with database "Embase (Ovid)", a query and a search date → 201. The screenshot shows 3 accepted / 2 rejected with the reasons expanded.
3. **Reload:** refresh the browser. The same receipt and counts show, and `GET .../imports/{id}` JSON has `declared` and `observed` separated.
4. **Idempotency:** re-upload the identical file → 200 `replayed:true`, same id. Upload an edited file → 201 `version:2`, `previous_receipt_id` set. Upload the identical file with a changed query → 409.
5. **Identity:** `GET .../reports` shows the imported record in `imported_records` of the report it shares a DOI with. A merge as the adjudicator shows up in `GET .../reports/history` as `identity.report_merged` schema 2 with `moved_import_record_ids`.
6. **Citation chase:** `POST .../citation-chases` backward on a seed with an OpenAlex id → 201, then forward → 201. The receipt shows literal requests, the limit and completion.
7. **Coverage:** `POST .../corpus/coverage` with 5 known DOIs → `found/missing/recall`, `searched[]` with dates, `not_searched[]`, `exhaustive:false`.
8. **Download:** Download corpus (ZIP) → run `python -c "from src.services.research_engine.corpus_export import verify_package; ..."` on the file → OK. Record its `body_sha256`, and grep for a restricted-record title token from the raw text to show it is absent.
9. **Denials:**
   - A foreign-org user GETs the export → 404.
   - A viewer POSTs an import → 404.
   - An archived project gets import → 409 and export → 200.
10. **CI:** the Integration job URL where `test_search_import_postgres.py` passed at the merge SHA, plus the PostgreSQL version from the testcontainer log and the mutation transcripts in the PR.

## Out of scope (say so in the PR)

- Re-importing an exported package into another project.
- RIS/CSV renderings of the corpus.
- Citation chasing via Crossref, S2 or PubMed.
- A UI for chase and coverage.
- Per-source licence discovery.
- Any claim that a search or chase is exhaustive.
