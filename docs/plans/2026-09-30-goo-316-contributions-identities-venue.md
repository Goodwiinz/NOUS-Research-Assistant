# GOO-316 Contribution Statements, Verified Identities and One Venue Profile Plan (Academic R7)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A versioned **statement set** records the release-facing facts about authorship and disclosure:
- authorship order and identity;
- CRediT contributor roles per author;
- funding, conflicts of interest, ethics, limitations, data availability, code availability and licenses.

Each author's approval is retained with attribution, and the set binds to an exact GOO-315 release because a candidate snapshot captures the set's id and hash. ORCID iDs have three explicit states:
- `authenticated`, only when an ORCID OAuth `/authenticate` flow completed for the user linked to that author, with a retained non-secret receipt;
- `unauthenticated`, an iD that was typed, imported or legacy;
- `unknown`, no iD.

Name match, registry lookup and user confirmation never produce `authenticated`. One versioned, code-defined **venue profile**, `generic-icmje-credit/1`, checks required fields, formatting and anonymization against an exact package hash. It produces actionable missing-field items, and a check against an older package hash cannot authorize a changed package. The anonymized package variant excludes every designated identity field and never contains internal reviewer or actor names. Contributions grant no permissions, project roles imply no authorship, and nothing here has deposit authority.

**Architecture:**
- **Four insert-only tables:** `manuscript_statement_sets` (versioned), `manuscript_statement_approvals`, `orcid_authentications` and `venue_checks`.
- **One pure module**, `venue_rules.py`: the CRediT vocabulary, the venue profile, anonymization and the ORCID status rule.
- **One service**, `statements_service.py`, **one ORCID OAuth callback** in `api/auth_orcid.py`, **one router**, `api/research/statements.py`, and **one ledger family**, `research_statements`.
- **GOO-315 integration:**
  - `build_snapshot` captures the current statement set (id and hash).
  - Candidate creation also writes an `anonymized` package variant.
  - `checks` gains `statements` and `venue`, which become verified obligations when the snapshot binds a statement set.

**Tech stack:** FastAPI, SQLAlchemy async, Alembic, `httpx` (`backend/requirements.txt:100`) for the ORCID token exchange, stdlib `hmac` for OAuth state, the PostgreSQL integration fixture, openapi-typescript, Next.js with TanStack Query and Vitest.

**Stacking (read first):** This PR stacks on **GOO-315** (`e4c6a8b0d2f3`) → GOO-314 → GOO-313 → GOO-312 → GOO-311 `a6c8e0b2d4f5`. It opens after GOO-315 merges.

### Consumed names

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Release snapshot (GOO-315) | `manuscript_releases(snapshot, snapshot_hash, checks, package_files, package_sha256, stage)`, `manuscript_release_service.build_snapshot`/`create_candidate`/`promote`, `manuscript_rules.evaluate`/`VERIFIED_OBLIGATIONS`/`CHECK_KEYS` (GOO-315 plan, Tasks 1 and 4) | Candidate construction reads; verified promotion re-evaluates. |
| Package writer (GOO-308) | `audit_bundle.Part`, `write_zip(…, schema=…)` (`backend/src/services/research_engine/audit_bundle.py:64,367`; the `schema` keyword comes from GOO-315) | Deterministic member bytes. |
| Peer-review identities (GOO-314) | `peer_review_reviewers.display_name`, actor ids on `peer_review_*` (GOO-314 plan) | They are excluded from the anonymized variant. |
| Internal reviewers | Actor ids and names on `research_claim_assessments`, `draft_releases.promoted_by_id`, appraisal and screening decisions; `appraisal_service._names` (`backend/src/services/research_engine/appraisal_service.py:479`) | No name or user id from these enters any package variant. |
| Roles | `ResearchAction.EDIT/RELEASE/VIEW`, `resolve_project` (`backend/src/services/research_engine/project_access.py:28-48,186`), `ResearchProjectRole` (`backend/src/models/research_project_role.py:11-14`) | Permissions stay here and are never derived from authorship. |
| Settings | `Settings` (`backend/src/core/config.py:67`), `SECRET_KEY` (`:84`), `FRONTEND_BASE_URL` (`:137`) | New settings `ORCID_CLIENT_ID`, `ORCID_CLIENT_SECRET` and `ORCID_BASE_URL` (default `https://sandbox.orcid.org`). With no client id, the route returns 503. |
| Ledger | `lock_aggregate_stream` (`backend/src/services/research_decisions/ledger.py:1318`), `append_decision` (`:1321`), `_FAMILIES` (`:2235`), `identity_service._replayed_event` (`backend/src/services/research_engine/identity_service.py:76`) | The append is caller-owned. |
| Insert-only trigger | `prevent_research_insert_only_mutation()` | SQLSTATE `55000`. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| **Avoiding the GOO-315 cycle** | A statement set is a project-level, versioned record that exists **before** a candidate. `build_snapshot` captures the **current** set's `{statement_set_id, set_hash, approvals: [{author_key, approval_id}]}` without checking it. The `statements` and `venue` checks are evaluated on the candidate. **Verified promotion** requires them to pass for that candidate's exact package hash. A changed statement set means a new candidate. | "Candidate construction precedes these checks; verified applies them." The release still binds an exact set, because the set hash is inside `snapshot_hash`. |
| Statement set | `manuscript_statement_sets(id, collection_id, body JSONB, set_hash CHAR(64), schema VARCHAR = 'nous.statements/1', supersedes_set_id UNIQUE NULL, created_by_id, actor_role CHECK = 'editor', created_at)`, with a partial unique initial per Collection. `body` is: `{authors: [{author_key, order, display_name, affiliations: [str], email: str\|null, corresponding: bool, user_id: uuid\|null, orcid: str\|null, credit_roles: [str]}], funding: {text, grants: [{funder, award_id}]}, conflicts: str, ethics: str, limitations: str, data_availability: str, code_availability: str, licenses: {text: "SPDX", data: "SPDX"\|null, code: "SPDX"\|null}}`. Any field may be the literal `null`, meaning **explicitly missing**. The service never fills one in. | One versioned document keeps the statements together, and explicit nulls make the missing data visible to the venue check. |
| CRediT taxonomy | `CREDIT_ROLES_V1`: the 14 CRediT roles (Conceptualization, Data curation, Formal analysis, Funding acquisition, Investigation, Methodology, Project administration, Resources, Software, Supervision, Validation, Visualization, Writing – original draft, Writing – review & editing), as slugs. An unknown role gives 422. The vocabulary version is stored as `credit/1`. | It is the public NISO CRediT standard, and it stays separate from authorship order and permissions. |
| Approvals | `manuscript_statement_approvals(id, statement_set_id, author_key, set_hash, method CHECK IN ('in_app_self','recorded_attestation'), approved_by_id, attestation_note TEXT NULL, created_at)` with `UNIQUE(statement_set_id, author_key)`. `in_app_self` is allowed only when the approving user **is** the author's `user_id`. `recorded_attestation` (EDIT) records that a co-author approved off-platform and requires a note. Both keep `approved_by_id` and the timestamp, and approving binds `set_hash`. | "Statements retain approval attribution", truthfully: an attestation is never shown as self-approval. |
| Contributions vs permissions | `user_id` on an author is display-linking only. No code path in `project_access`, the role assignment routes or any `resolve_project` caller reads statement sets. Assigning a project role creates no author, and adding an author grants no role. An AST guard (Task 6) checks this. | "Contribution taxonomy, authorship and permission assignments stay separate." |
| **ORCID provenance record (OAuth NOT RUN)** | `orcid_authentications(id, user_id FK users RESTRICT, orcid CHAR(19) CHECK ~ '^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$', environment CHECK IN ('sandbox','production'), scope VARCHAR(64), name_claim VARCHAR(255) NULL, client_id VARCHAR(64), token_received_at, id_token_sha256 CHAR(64) NULL, flow_state_sha256 CHAR(64), created_at)` with `UNIQUE(user_id, orcid, token_received_at)`. These are only fields from ORCID's token response (`orcid`, `name`, `scope`) plus our own request metadata. **Never** `access_token`, `refresh_token` or `id_token` bytes (only a hash), and no secret. | "Retained non-secret provenance (actor, timestamp)". A sandbox ORCID flow fills exactly these columns, so the design is testable without production credentials. |
| ORCID flow | `GET /api/v1/auth/orcid/start` (authenticated) returns 503 `ORCID not configured` without `ORCID_CLIENT_ID`. Otherwise it redirects to `{ORCID_BASE_URL}/oauth/authorize?client_id&response_type=code&scope=/authenticate&redirect_uri&state`, where `state = base64url(user_id \|\| nonce \|\| exp) + "." + HMAC-SHA256(SECRET_KEY, …)`, valid for 10 minutes. `GET /api/v1/auth/orcid/callback` verifies `state` (the signature, expiry, and that the user equals the current session user), POSTs the code to `{ORCID_BASE_URL}/oauth/token` with `httpx`, inserts the row and redirects to `FRONTEND_BASE_URL`. Errors redirect with a safe code (`orcid_denied`, `orcid_state_invalid`, `orcid_exchange_failed`), never raw text. | The minimum OAuth that produces the record, with no new dependency. |
| ORCID status rule (pure) | `orcid_status(author, authentications) -> "authenticated" \| "unauthenticated" \| "unknown"`. `authenticated` iff `author.orcid` is set **and** `author.user_id` is set **and** an `orcid_authentications` row exists for that `user_id` with the same `orcid`. Otherwise it is `unauthenticated` when `orcid` is set, and `unknown` when it is not. The receipt (row id, environment, timestamp) is shown with `authenticated`. | "Name match, registry lookup or user confirmation never establish authenticated status; unknown stays unknown." |
| **The one venue profile** | **`generic-icmje-credit/1`**, code-defined in `venue_rules.PROFILES`. It is built from public checklists:<br>- the ICMJE *Recommendations* (authorship, disclosure of conflicts and funding, data sharing statement, ethics approval);<br>- CRediT (role taxonomy);<br>- the common IMRaD manuscript structure.<br>It is not modelled on any one journal. | No pilot venue is chosen. Journal author guides change often, differ in wording and are not machine-readable, so a journal-named profile would be a guess presented as compliance. The ICMJE and CRediT requirements are public, stable, cited by most biomedical journals and fully testable. A real journal later becomes a new profile id or version, never an edit to this one. |
| Profile rules | **Required fields:** title, ≥ 1 author, exactly one corresponding author with an email, CRediT roles for every author, `conflicts`, `funding.text`, `ethics`, `data_availability`, `licenses.text` as an SPDX id from `{CC-BY-4.0, CC-BY-SA-4.0, CC0-1.0}`, and `limitations`.<br>**Formatting:** headings `Abstract`, `Introduction`, `Methods`, `Results` and `Discussion` present in `manuscript.source.md` (case-insensitive `^#{1,3}\s`); every `[Doc N]` resolves to a `docN` snapshot reference; no reference has an empty title.<br>**Anonymization:** the anonymized variant contains none of the identity strings below. | Each failing rule gives an item `{rule, field, detail, fix}`, for example `{"rule": "required", "field": "authors[2].credit_roles", "fix": "Select at least one CRediT role for author 3"}`. "Actionable missing fields." |
| Anonymized variant | The identity fields are: author `display_name`, affiliations, email, ORCID, `funding.grants[*].award_id`, the acknowledgement section (`^#{1,3}\s*Acknowledg`), every project user's full name and email, every peer-review reviewer `display_name`, and every internal actor name or id from claims, appraisal, screening and releases. `venue_rules.anonymize(parts, identities)` drops `statements.json` identity fields (replacing them with `"[redacted]"`), removes the acknowledgement section from `manuscript.md`, and omits actor ids from `snapshot.json` and `checks.json`. A post-condition scan of **every member's bytes** for each identity string (case-insensitive, NFC-normalized) must find nothing, otherwise the build fails with `anonymization_leak:{member}`. The identified variant is unchanged. | "Anonymized output excludes designated identity fields and never leaks internal reviewers." The byte scan is the proof, so the redaction is never just trusted. |
| Variants on the release | GOO-315's candidate creation writes both variants. `package_files` becomes `{identified: [...], anonymized: [...]}`, with `package_sha256` (identified) and `anonymized_sha256` stored in `snapshot.packages` for new rows. Rows from before GOO-316 have no anonymized variant, and their venue anonymization rule is `unknown`. | No mutation of GOO-315 rows; new candidates carry the variant. |
| Venue check record | `venue_checks(id, collection_id, release_id FK manuscript_releases RESTRICT, profile_id, profile_version SMALLINT, package_sha256, anonymized_sha256 NULL, result JSONB, status CHECK IN ('pass','fail'), checked_by_id, created_at)`. The check runs on demand (EDIT) and when a candidate is created. | Bound to the exact package hash and profile version. |
| Stale checks | At verified promotion, `venue` passes iff a `venue_checks` row exists for the **same release** and **same `package_sha256`** with the current profile version and `status = 'pass'`. `statements` passes iff the snapshot binds a statement set and every author has an approval whose `set_hash` equals the bound `set_hash`. Any other check is `stale`, giving 409 with `failing: ["venue"]`. | "Stale checks cannot authorize a changed package." |
| Statements and venue as obligations | GOO-315's `VERIFIED_OBLIGATIONS` gains `statements` and `venue` only when `snapshot.statements` is non-null. Without a statement set both are `not_applicable`, so a plain verified release without a venue still works. | Opt-in by data, with no separate configuration. |
| No deposit authority | No outbound call other than the ORCID token exchange (an AST guard allowlists the one call site). | "No deposit authority." |
| Ledger family `research_statements` | `statements.versioned`, `statements.approved` and `venue.checked`. ORCID authentication is a **user identity receipt, not a project decision**, so it gets no project ledger event. The insert-only row is the receipt. | Decisions stay in the ledger, and identity provenance stays with the user. |

**Migration head:** new revision `f6a8c0d2e4b5_create_statements_venue.py`, with `down_revision = "e4c6a8b0d2f3"` (GOO-315). Same re-pointing rule. It creates the four tables with `_deny_data_api` and insert-only triggers. `downgrade()` refuses when rows exist.

---

### Task 1: Pure rules (`venue_rules.py`)

**Files:**
- Create `backend/src/services/research/venue_rules.py` (stdlib: `re`, `unicodedata`, `json`).
- Create `backend/tests/unit/services/test_venue_rules.py`.

```python
CREDIT_VOCABULARY = "credit/1"; CREDIT_ROLES_V1: tuple[str, ...]   # 14 slugs
PROFILE_ID = "generic-icmje-credit"; PROFILE_VERSION = 1
SPDX_TEXT_LICENSES = frozenset({"CC-BY-4.0", "CC-BY-SA-4.0", "CC0-1.0"})
def validate_statement_body(body: Mapping) -> dict           # 422 codes; never fills nulls
def set_hash(body: Mapping) -> str
def orcid_status(author: Mapping, auths: Iterable[Mapping]) -> tuple[str, dict | None]
def check_profile(snapshot: Mapping, members: Mapping[str, bytes], anonymized: Mapping[str, bytes] | None, identities: Iterable[str]) -> dict
def anonymize(members: Mapping[str, bytes], body: Mapping, identities: Iterable[str]) -> dict[str, bytes]
def scan_leaks(members: Mapping[str, bytes], identities: Iterable[str]) -> list[str]
```

**Tests (they fail on the import):**
- `test_unknown_credit_role_rejected`
- `test_explicit_null_reported_as_missing_with_fix_text`
- `test_orcid_name_match_is_not_authenticated`: same name, no row, so `unauthenticated`.
- `test_orcid_authenticated_requires_linked_user_and_matching_row`
- `test_orcid_absent_is_unknown`
- `test_profile_flags_missing_heading_and_unresolved_doc_key`
- `test_anonymize_removes_acknowledgements_and_identity_fields`
- `test_scan_leaks_catches_reviewer_name_case_and_nfc_variants`
- `test_profile_result_names_profile_and_version`

**Commit:** `feat(research): CRediT vocabulary, generic ICMJE venue profile and anonymization rules (GOO-316)`

---

### Task 2: Models + migration + settings

**Files:**
- Create `backend/src/models/manuscript_statements.py` (`ManuscriptStatementSet`, `ManuscriptStatementApproval`, `OrcidAuthentication`, `VenueCheck`) and export them.
- Create `backend/alembic/versions/f6a8c0d2e4b5_create_statements_venue.py`.
- Modify `backend/src/core/config.py` (`Settings`) to add `ORCID_CLIENT_ID: str = ""`, `ORCID_CLIENT_SECRET: str = ""` and `ORCID_BASE_URL: str = "https://sandbox.orcid.org"`.
- Modify `test_screening_queue_postgres.py` (`_REBUILT_TABLES`).

**Check:** `check_alembic.py` reports single head `f6a8c0d2e4b5`, and `alembic upgrade head --sql | grep -c orcid_authentications` is > 0.
**Commit:** `feat(research): statement set, approval, ORCID receipt and venue check tables (GOO-316)`

---

### Task 3: Ledger

**Files:**
- Modify `ledger.py` to add the `research_statements` family (subject type `statement_set`).
- Modify `test_research_decision_ledger.py`.

| Event (schema 1) | Payload keys | actor_role |
|---|---|---|
| `statements.versioned` | `collection_id, statement_set_id, supersedes_set_id, set_hash, author_keys, missing_fields` | `editor` |
| `statements.approved` | `collection_id, statement_set_id, author_key, set_hash, method, approval_id` | `editor` |
| `venue.checked` | `collection_id, venue_check_id, release_id, profile_id, profile_version, package_sha256, anonymized_sha256, status, failing_rules` | `editor` |

**Replay rules:**
1. Every `collection_id` equals the `aggregate_id`.
2. One tip per Collection.
3. An approval's `set_hash` equals its set's hash, with one approval per author per set.

**Tests:**
- `test_statements_replay_rejects_approval_with_other_set_hash`
- `test_statements_replay_rejects_duplicate_author_approval`

**Commit:** `feat(research): research_statements decision family (GOO-316)`

---

### Task 4: Services + GOO-315 integration + ORCID callback

**Files:**
- Create `backend/src/services/research/statements_service.py`.
- Create `backend/src/api/auth_orcid.py`.
- Modify `manuscript_release_service.build_snapshot` (statements capture), `create_candidate` (the anonymized variant, then an automatic venue check), `promote` (the obligations), and `manuscript_rules.evaluate` (the `statements` and `venue` keys).

```python
AGGREGATE_TYPE = "research_statements"
async def version_set(db, context, actor_id, data) -> StatementSetResponse              # EDIT
async def approve(db, context, actor_id, set_id, data) -> ApprovalResponse              # EDIT; in_app_self needs actor == author.user_id
async def current_set(db, collection_id) -> tuple[Any, list[Any]] | None
async def identities(db, context) -> list[str]                                          # every designated identity string
async def run_venue_check(db, context, actor_id, release_id) -> VenueCheckResponse      # EDIT
async def record_orcid(db, user_id, token_response: Mapping, *, environment, client_id, state_hash) -> OrcidAuthentication
async def author_identity_view(db, context, set_id) -> list[AuthorIdentity]             # status + receipt
```

`record_orcid` reads only `orcid`, `name` and `scope` from the token response and drops everything else before insert. A unit test asserts that the token strings never reach the row, the logs or the response.

**Commit:** `feat(research): statements, approvals, ORCID receipts, venue checks and release obligations (GOO-316)`

---

### Task 5: API + contracts

**Files:**
- Create `backend/src/api/research/statements.py` (prefix `/api/v1/projects/{project_id}/statements`) and register it with `auth_orcid` in `main.py`.
- Add the schemas to `backend/src/shared/statements_schemas.py`.
- Add the GOO-315 route variant `GET …/manuscript-releases/{id}/package?variant=identified|anonymized`, defaulting to `identified`, so the existing behaviour is unchanged.
- Regenerate the OpenAPI spec and the types.

| Route | Action | Notes |
|---|---|---|
| `POST …/statements` | EDIT | `StatementSetCreate{body, supersedes_set_id?, idempotency_key}`. 409 on a stale tip. |
| `GET …/statements` | VIEW | The tip and history, with `missing_fields` and per-author ORCID status and receipt. |
| `POST …/statements/{set_id}/approvals` | EDIT | `ApprovalCreate{author_key, set_hash, method, attestation_note?}`. 403 for `in_app_self` by a non-author. |
| `POST /api/v1/projects/{pid}/manuscript-releases/{id}/venue-checks` | EDIT | Runs `generic-icmje-credit/1` against the stored package bytes. |
| `GET …/manuscript-releases/{id}/venue-checks` | VIEW | Results with actionable items. |
| `GET /api/v1/auth/orcid/start`, `GET /api/v1/auth/orcid/callback` | authenticated user | 503 when unconfigured. |
| `GET /api/v1/auth/orcid/authentications` | authenticated user | The current user's receipts only. |

**oasdiff:** new operations, plus a new optional query parameter on the package route. Expected: no ERR.

**Tests** (`backend/tests/unit/api/test_statements_routes.py`):
- `test_orcid_start_503_without_client_id`
- `test_orcid_callback_rejects_forged_or_expired_state`
- `test_orcid_callback_stores_no_token`: the exchange is mocked.
- `test_in_app_self_approval_by_non_author_403`
- `test_package_default_variant_unchanged`

**Commit:** `feat(research): statement, approval, venue-check and ORCID endpoints (GOO-316)`

---

### Task 6: PostgreSQL proof (one test) + structural guard

**Files:**
- Create `backend/tests/integration/test_statements_venue_postgres.py` with the integration and `requires_postgres` markers. It reuses the GOO-315 seed (a verified-ready draft with a peer-review round and an internal adjudicator with a distinctive name).
- Create `backend/tests/unit/architecture/test_statements_boundary.py`, an AST scan with three checks:
  - **(a)** `project_access.py` and the role-assignment route never import `manuscript_statements` or `statements_service`.
  - **(b)** `statements_service` and `auth_orcid` contain exactly one outbound `httpx` call site (the token POST).
  - **(c)** No service builds an `OrcidAuthentication(` except `record_orcid`.

**`test_statements_identity_venue_anonymization_and_stale_checks`** runs these steps in order:
1. **Statement set v1** with three authors:
   - A is linked to user U and has an ORCID.
   - B has an ORCID typed in and no user.
   - C has no ORCID.
   - `limitations` is null and `ethics` is null.
   - `missing_fields` lists both.
2. **ORCID:** call `record_orcid` for U with a sandbox-shaped token response (`orcid`, `name`, `scope`, `access_token`, `refresh_token`).
   - A shows `authenticated` with the receipt.
   - B is `unauthenticated` even though its name equals an existing ORCID record's `name_claim` (the name match is ignored).
   - C is `unknown`.
   - Neither token string appears anywhere in the database (scan the `orcid_authentications` columns and `research_decision_events.payload`).
3. **Approvals:**
   - A approves `in_app_self` as U.
   - B and C are recorded as `recorded_attestation` by an editor with notes.
   - U trying `in_app_self` for B gets 403.
4. **Permission separation:**
   - Being listed as an author gives U no decision role: `resolve_project(ADJUDICATE)` for U gives 403 until an explicit assignment exists.
   - Assigning U the reviewer role adds no author.
   - Deleting U's role leaves the statement set unchanged.
5. **Candidate 1:**
   - The snapshot binds set v1.
   - The venue check `fail`s with items for `limitations`, `ethics` and the missing `Discussion` heading, each with a fix text.
   - Both variants download.
   - The **anonymized** variant's bytes contain none of A, B and C's names, affiliations, emails or ORCIDs, the reviewer `display_name`, the adjudicator's name, or any user id.
   - The identified variant contains the author names.
6. **Fix:** set v2 fills the nulls, and the draft gains the heading. Approvals on v1 do not carry over, so `statements` fails until all three re-approve v2. Candidate 2's venue check passes, and promotion succeeds (verified).
7. **Stale check:** the candidate 2 venue check row is replayed against candidate 3, built after a draft edit with a different package hash. Promoting candidate 3 gives 409 with `failing ⊇ ["venue"]` until a new check runs on candidate 3's hash. Candidate 2's verified row is unchanged.
8. **Legacy:** a GOO-315 candidate built before this migration has `statements`/`venue` = `not_applicable` and promotes under the GOO-315 obligations only.
9. **Insert-only and replay:** UPDATE or DELETE raises `55000` on all four tables, and `research_statements` replays.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_statements_venue_postgres.py`. Without a database, report it as **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for statements, ORCID states, venue checks and anonymization (GOO-316)`

---

### Task 7: Frontend

**Files:**
- Create `frontend/src/types/api/statements-contract.ts`.
- Modify `projectService.ts` to add statements, approvals, venue checks and the package variant download.
- Create `frontend/src/components/research/ManuscriptStatementsPanel.tsx` and mount it in `DraftReleasePanel` above "Manuscript releases".
  - The author table has order, name, affiliations, corresponding, CRediT multi-select and ORCID. ORCID shows the status badge `Authenticated` (with the receipt date and environment), `Unauthenticated` or `Unknown`, plus a "Verify with ORCID" link to `/auth/orcid/start` that is shown only for the current user's own author row.
  - Statement fields show an explicit "Missing" state.
  - Per-author approval status.
- Modify the release list: venue check results with profile id and version, a list of actionable items, and both download variants.
- Tests:
  - `orcid badge never authenticated without receipt`
  - `missing fields listed with fix text`
  - `verify link only on own author row`
  - `anonymized download offered`

**Commit:** `feat(frontend): statements, ORCID identity states and venue checks (GOO-316)`

---

## Mutation verification

Follow `docs/engineering/testing.md`. Record each check under a GOO-316 section in `docs/testing/agent-orchestration-mutation-checks.md`.

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| ORCID status needs a receipt | treat any `orcid` as authenticated | `pytest -q backend/tests/unit/services/test_venue_rules.py -k orcid` | B shows authenticated |
| Token stripping in `record_orcid` | store the whole response | `pytest -q backend/tests/integration/test_statements_venue_postgres.py` | step 2: a token found |
| Leak scan post-condition | skip `scan_leaks` and remove the reviewer redaction | same | step 5: the reviewer name is in the anonymized bytes |
| Venue check bound to the package hash | accept any check for the project | same | step 7: candidate 3 promotes |
| Approval `set_hash` binding | accept approvals on any set | same | step 6: v1 approvals carry over |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_venue_rules.py backend/tests/unit/services/test_manuscript_rules.py backend/tests/unit/services/test_research_decision_ledger.py
pytest -q backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # single head f6a8c0d2e4b5
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists.** Report each of these as NOT RUN, never as passing:
- **PostgreSQL proof:** needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **ORCID OAuth, sandbox or production:** needs registered ORCID app credentials (`ORCID_CLIENT_ID`/`SECRET`) and a sandbox ORCID account. The record shape is designed so that a sandbox flow fills it unchanged.
- **Venue-profile expert review** (that the ICMJE/CRediT checklist mapping is correct): none is available today.
- **Live journey:** needs a deployed stack with `f6a8c0d2e4b5`.

## Authenticated journey list for Linear closure

1. **Statements:** enter three authors with CRediT roles and leave `limitations` empty. The panel shows Missing.
2. **ORCID:** with sandbox credentials, "Verify with ORCID" on your own author row shows Authenticated with a receipt. This step is NOT RUN without credentials. A typed iD for a co-author shows Unauthenticated, and a blank one shows Unknown.
3. **Approve:** self-approve your row, and record attestations for the others.
4. **Candidate and venue:** build a candidate. The venue check fails with actionable items. Download both variants, and grep the anonymized zip for every author and reviewer name: no hits.
5. **Fix and promote:** fill the fields, re-approve, build a candidate (the check passes) and promote as the adjudicator.
6. **Stale:** edit the draft and build a new candidate. Promotion refuses until a new venue check runs.
7. **Separation:** an author with no project role cannot act, and a reviewer role adds no authorship.

Record the SHA/PR, CI links, both package variants, the grep transcript, the PostgreSQL junit, the mutation transcripts and the NOT RUN list.

## Seams

- **Upstream (GOO-315):** adds two check keys, a package variant and snapshot capture. GOO-315's semantics for releases without statements are unchanged.
- **GOO-317:** reference exports come from the snapshot and contain no author-of-manuscript identity, so they are safe in both variants.
- **GOO-318:** a deposit uses the identified package of a verified release and a separate approval. Nothing here authorizes it.

## Out of scope

Each item below has a `ponytail:` marker at its seam:
- journal-specific profiles;
- ORCID record writes (works or affiliations) and ORCID member API scopes;
- institution or ROR identifiers;
- automated CRediT suggestion;
- e-signature for approvals;
- a DOCX title page.
