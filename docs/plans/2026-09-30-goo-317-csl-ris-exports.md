# GOO-317 Validated CSL JSON and RIS Reference Exports Plan (Academic R7)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add **CSL JSON** and **RIS** serializers to `BibliographyService`. Both read the canonical reference representation:
- for a release, the snapshotted `references` of an immutable GOO-315 release, never live resolution;
- for an ad-hoc draft export, the existing `_canonical_citation_records`.

The serializers preserve:
- identifiers (DOI, arXiv);
- author order and names, with no invented given/family split;
- publication type and year;
- the title and venue;
- the stable citation keys (`docN`).

Missing metadata is omitted, and the omission is reported. It is never invented. Evidence snippets never stand in for titles. Every generated file is parsed in tests by an **independent** validator:
- CSL JSON against the official CSL-data JSON Schema, using the installed `jsonschema`;
- RIS by a strict test-only reader written from the RIS tag grammar that imports nothing from `src`.

Both formats round-trip identifiers and metadata, including Unicode, corporate authors and absent dates. Markdown, LaTeX and BibTeX exports and their default MIME types stay byte-identical. DOCX is out of scope.

**Architecture:**
- **Two pure functions** in `bibliography_service.py`, `format_csl_json(records, keys)` and `format_ris(records, keys)`, plus `omissions(records)`.
- **Two new `format` values** on the existing draft export route (`csl-json`, `ris`); they return references-only attachments.
- **One release endpoint**, `GET …/manuscript-releases/{id}/references?format=`, and two new members in the GOO-315 package (`references.json`, `references.ris`) for candidates built after this change.
- **No migration.** Revision id `a8c0e2f4b6d7` was reserved for this ticket and is **not created**: nothing persists. The head stays `f6a8c0d2e4b5`.

**Tech stack:** FastAPI, `jsonschema==4.26.0` (`backend/requirements.txt:9`, already installed), the PostgreSQL integration fixture, openapi-typescript, Next.js and Vitest. No new dependency.

**Stacking (read first):** This PR stacks on **GOO-316** (`f6a8c0d2e4b5`) → GOO-315 → … → GOO-311 `a6c8e0b2d4f5`. It opens after GOO-316 merges. Its only data dependency is GOO-315's `snapshot.references`.

### Consumed names

| Consumed | Name (path:line) | Property relied on |
|---|---|---|
| Existing formatters | `BibliographyService` (`backend/src/services/research/bibliography_service.py:30`): `_generate_bibtex_key` (`:34`), `format_bibtex(citations, keys)` (`:68`), `format_ieee` (`:148`), `format_apa` (`:202`), `format_mla` (`:267`), `format_bibliography` (`:317`) | Unchanged. The new functions sit beside them. |
| Canonical records | `DraftGenerationService._canonical_citation_records` (`backend/src/services/research/draft_generation_service.py:2501-2544`), which returns `SimpleNamespace(document_title, authors: list[str] \| None, year, venue, doi, arxiv_id, abstract)` and keys `doc{citation_index}`; `_canonical_author_names` (`:2474-2498`), which flattens dicts to `"given middle family"` strings | The ad-hoc source. Its author strings are already flattened, so they are emitted as literals. |
| Release references (GOO-315) | `snapshot.references: [{key, type, title, authors, year, venue, doi, arxiv_id, source}]` (GOO-315 plan, "Snapshot content") | The immutable release source. `type` was captured at snapshot time. |
| Draft export | `DraftGenerationService.export_draft(project_id, draft_id, format, include_bibliography, bib_format)` (`:2342-2410`; its format guard is at `:2354`); route `export_draft` (`backend/src/api/research/drafts.py:502-563`, where `format: str = Query("markdown")` is a plain string, not an enum) | The default stays `markdown`. Adding values to a string query parameter is not an OpenAPI enum change. |
| `_BIBLIOGRAPHY_FORMATS` | `frozenset({"bibtex","biblatex","apa","ieee","mla"})` (`draft_generation_service.py:272`) | Unchanged. CSL and RIS are file formats, not Markdown reference styles. |
| Release package (GOO-315/316) | `manuscript_release_service.create_candidate`, the package member list, `manuscript_rules.reference_mapping`, `package_bytes`, `verify` (GOO-315 plan) | New candidates gain two members. Old packages are never rewritten. |
| Frontend | `projectService.exportDraft`/`downloadDraftExport` (`frontend/src/services/projectService.ts:645-679`, via `git show feat/goo-310-outcome-certainty:`), `DraftExportModal.tsx`, `DraftReleasePanel.tsx` | The format union widens, and the default is unchanged. |
| Roles | the drafts router's `_validate_project_ownership` (`drafts.py:97`); `ResearchAction.VIEW` for releases | Unchanged. |

---

## Decisions

| Question | Decision | Why |
|---|---|---|
| **Independent validation approach** | **CSL JSON:** validate the parsed output with `jsonschema.Draft7Validator` against the **official CSL-data schema** (`csl-data.json` from `citation-style-language/schema`). It is vendored at `backend/tests/fixtures/references/csl-data.schema.json`, with its upstream release tag, commit SHA and MIT license recorded in `backend/tests/fixtures/references/README.md` at vendoring time; Task 1 fetches it once and the tests are offline. **RIS:** `backend/tests/fixtures/references/ris_reader.py`, a strict reader from the RIS tag grammar. Each line is `^[A-Z][A-Z0-9]  - ` (two spaces, a hyphen and a space; `ER  - ` may have no value). Every record starts with `TY` and ends with `ER`. A repeated `AU` keeps its order. Unknown tags are errors. It must not import `src`, and a test asserts that. | The ticket asks for an independent, standards-aware validator. The CSL schema is the standard's own artefact, and `jsonschema` is already installed. No maintained RIS validator is installed, and adding a dependency for a grammar this small is not justified. A reader written to the published grammar, kept out of the serializer's module, is independent enough to catch serializer bugs. `# ponytail: add rispy as a test-only cross-check if a second reader is ever wanted.` |
| Source of truth | Release exports read only `snapshot.references`, with no `Citation` or `Document` reads (a test checks this). Ad-hoc draft exports read `_canonical_citation_records`, the same source as today's BibTeX. | "From the canonical reference representation of the selected immutable release"; ad-hoc exports stay consistent with the existing formats. |
| Citation keys | The CSL `id` and the RIS `ID` are the record's `key` (`docN`), and the file order is the key order. | "Stable release citation keys (docN keys for drafts)." Keys reconcile with `\cite{docN}` in the LaTeX export and with the release's `reference_mapping`. |
| Author names | `authors` entries are strings, so they are emitted as CSL `{"literal": name}` and as RIS `AU  - name` verbatim, in order. Names are **never** split into family and given. A corporate author ("World Health Organization") is handled the same way. A snapshot record from GOO-315 that carries structured `{family, given}` (only if a future snapshot schema adds it) would be emitted as structured names. | "Never invented." `_canonical_author_names` has already flattened the names, so a split now would be a guess. CSL `literal` is the standard way to carry an unparsed name. |
| Type mapping | `type` → (CSL, RIS):<br>`journal_article`/`article-journal`/a venue present with a DOI → (`article-journal`, `JOUR`);<br>`conference_paper` → (`paper-conference`, `CONF`);<br>`book` → (`book`, `BOOK`);<br>`chapter` → (`chapter`, `CHAP`);<br>`report` → (`report`, `RPRT`);<br>`thesis` → (`thesis`, `THES`);<br>`dataset` → (`dataset`, `DATA`);<br>`webpage` → (`webpage`, `ELEC`);<br>`preprint`, or arXiv-only → (`article`, `UNPB`);<br>otherwise → (`document`, `GEN`), with an omission `type_unmapped:{value}`. | Every record gets a valid type. An unknown type stays visibly generic and is never guessed as a journal article. |
| Field mapping | CSL: `id, type, title, author, issued: {"date-parts": [[year]]}` (only when there is a year), `container-title` (venue), `DOI`, `archive: "arXiv"` and `archive_location: <id>` (when there is an arXiv id). RIS: `TY, ID, TI, AU*, PY` (year), `T2` (venue; `JO` too for `JOUR`), `DO`, and `AN  - arXiv:<id>`, then `ER`. `abstract` is **not** exported. | The fields the ticket names. Abstracts are a large source of encoding trouble and nobody asked for them. |
| Missing metadata | An absent field is omitted from the file and listed in `omissions(records) -> [{"key", "field", "reason": "absent"\|"type_unmapped"}]`. A record with neither title nor identifier is still emitted (it has an `id` and a type) and reported `absent:title`. A title is never filled from `snippet` or `context`: the serializer has no access to `DraftCitation.snippet`, and a test checks that a record whose citation has a snippet but no title emits no `title`/`TI`. | "Missing metadata explicit or omitted, never invented; evidence snippets never replace titles." |
| Making omissions explicit to users | The release endpoint accepts `report=true`, which returns `{"format", "records": n, "omissions": [...]}` as JSON. The download response carries `X-Reference-Omissions: <count>`, and the GOO-315 package gains `references.omissions.json`. | "Unsupported record/field cases explicit." |
| Encoding | CSL: UTF-8 JSON, `ensure_ascii=False`, sorted keys, 2-space indent and a trailing newline. RIS: UTF-8 **without** a BOM, `\r\n` line endings (the RIS convention) and NFC-normalized values. | Byte-stable output that keeps Unicode. |
| MIME and filenames | CSL: `application/vnd.citationstyles.csl+json`, `references.json`. RIS: `application/x-research-info-systems`, `references.ris`. Markdown, LaTeX zip and BibTeX keep their current types and names. | "Preserve existing formats and default MIME/download behaviour." |
| Draft export route | `format=csl-json` or `format=ris` returns the references-only attachment, and `include_bibliography`/`bib_format` are ignored for these formats. With zero citations it returns an empty array (CSL) or an empty file (RIS) with 200. Unknown formats keep today's 400 `Unsupported format`. | It extends "the draft export route formats" without touching the existing branches. |
| DOCX | **Out of scope.** No pilot has shown a DOCX requirement, and the ticket's default is out of scope. | Recorded so a reviewer does not expect it. |
| Migration | None. `a8c0e2f4b6d7` is reserved and unused, and `check_alembic.py` must still report `f6a8c0d2e4b5`. | Nothing is persisted beyond GOO-315's package members. |

---

### Task 1: Validators and fixtures (write first)

**Files:**
- Create `backend/tests/fixtures/references/csl-data.schema.json` (vendored once, with the provenance in `README.md`).
- Create `backend/tests/fixtures/references/ris_reader.py` with `parse(text) -> list[dict[str, list[str]]]`. It raises `RisError` on any grammar violation.
- Create `backend/tests/fixtures/references/records_v1.json` with these cases:
  - `journal` (DOI, three ordered authors, Unicode "Zoë Ångström", venue, year);
  - `corporate` ("World Health Organization", no year);
  - `preprint` (arXiv only, no venue);
  - `untitled_with_snippet` (title null, the source citation had a snippet);
  - `unknown_type`;
  - `cjk` (a title in Japanese).
- Create `backend/tests/unit/services/test_reference_validators.py` with `test_ris_reader_rejects_bad_tag_spacing_missing_er_unknown_tag` and `test_reader_imports_nothing_from_src`.

**Commit:** `test(research): vendored CSL-data schema and an independent RIS reader (GOO-317)`

---

### Task 2: Serializers

**Files:**
- Modify `bibliography_service.py` to add `format_csl_json(records, keys) -> str`, `format_ris(records, keys) -> str`, `omissions(records) -> list[dict]` and `_TYPE_MAP`. A record is any object or mapping with `document_title|title, authors, year, venue, doi, arxiv_id, type?`. One small `_get(record, name)` accessor covers both shapes.
- Create `backend/tests/unit/services/test_reference_exports.py`.

**Tests (they fail on the import):**
- `test_csl_validates_against_official_schema_for_every_fixture`
- `test_ris_parses_with_independent_reader_for_every_fixture`
- `test_round_trip_identifiers_keys_author_order_and_unicode`: parse each output and compare `id/ID`, `DOI/DO`, the arXiv id, the author sequence, title, year and venue with the input.
- `test_corporate_author_literal_not_split`
- `test_absent_year_omits_issued_and_py_and_reports`
- `test_snippet_never_becomes_title`
- `test_unknown_type_is_document_gen_and_reported`
- `test_ris_crlf_utf8_no_bom`
- `test_existing_bibtex_ieee_apa_mla_outputs_byte_identical`: golden strings captured before the change.

**Commit:** `feat(research): CSL JSON and RIS serializers with explicit omissions (GOO-317)`

---

### Task 3: Draft export route formats

**Files:**
- Modify `export_draft` (`draft_generation_service.py:2354`), whose guard becomes `("markdown","latex","csl-json","ris")`. The new branch returns `{"format", "filename", "content", "mime_type", "omissions"}` from `_canonical_citation_records`.
- Modify the route (`drafts.py:529-563`) to add one branch returning `Response(content, media_type, Content-Disposition, X-Reference-Omissions)`. The Markdown and LaTeX branches are untouched.
- Create `backend/tests/unit/api/test_draft_export_formats.py`:
  - `test_markdown_and_latex_bytes_unchanged`: golden.
  - `test_csl_and_ris_attachments_mime_and_filename`
  - `test_unknown_format_still_400`

**Commit:** `feat(drafts): CSL JSON and RIS draft reference exports (GOO-317)`

---

### Task 4: Release references endpoint + package members

**Files:**
- Modify `backend/src/api/research/manuscript_releases.py` to add `GET …/manuscript-releases/{id}/references?format=bibtex|csl-json|ris&report=false` (VIEW). It reads `snapshot.references` only.
- Modify `manuscript_release_service.create_candidate` to add the members `references.json`, `references.ris` and `references.omissions.json` for new candidates. `manuscript_rules.reference_mapping` extends to check that each `docN` maps to the same record in all three reference files.
- Tests:
  - `test_release_references_read_snapshot_only`: patch `Citation` and `Document` loads to raise.
  - `test_reference_mapping_reconciles_bib_csl_ris`

**Commit:** `feat(research): release reference exports and package members (GOO-317)`

---

### Task 5: Contracts

**Files:**
- Regenerate `backend/openapi.json` and `frontend/src/types/generated/api.d.ts`.

**oasdiff:** one new operation. The draft export's `format` is a free string, so nothing changes in its schema. Expected: no ERR.

**Check:** `(cd backend && python ../scripts/ci/check_alembic.py)` still reports single head `f6a8c0d2e4b5`, and no file named `a8c0e2f4b6d7_*` exists.
**Commit:** `chore(api): regenerate contracts for reference exports (GOO-317)`

---

### Task 6: PostgreSQL proof (one test)

**Files:**
- Create `backend/tests/integration/test_reference_exports_postgres.py` with the integration and `requires_postgres` markers. It reuses the GOO-315 seed and loads `records_v1.json` as `Citation`/`DraftCitation` rows on a draft.

**`test_release_and_draft_reference_exports_reconcile_and_stay_immutable`** runs these steps in order:
1. **Build a candidate.** Download `references?format=csl-json` and `ris`.
   - Both validate (the schema and the independent reader).
   - Keys `doc1..docN` equal the snapshot keys in order.
   - Every identifier round-trips.
   - `report=true` lists `absent:year` for `corporate`, `absent:title` for `untitled_with_snippet` and `type_unmapped` for `unknown_type`.
2. **Package:** `references.json`, `references.ris` and `references.omissions.json` exist, and `verify` gives `reference_mapping` consistent across BibTeX, CSL and RIS.
3. **Immutability:** update `Citation.document_title` and `doi` for `journal`. The release downloads are byte-identical to step 1. A fresh ad-hoc draft export (`POST …/drafts/{id}/export?format=csl-json`) shows the new values.
4. **Back-compat:** the draft export with `format=markdown` and `format=latex` gives bytes equal to the baseline captured before Task 3. BibTeX inside the LaTeX zip is unchanged.
5. **Tenancy:** a foreign user gets 404 on the release references, and the drafts router's existing ownership check rejects a foreign draft export.

**Run:** `RESEARCH_DECISION_DATABASE_URL=... pytest -q backend/tests/integration/test_reference_exports_postgres.py`. Without a database, report it as **NOT RUN**.
**Commit:** `test(research): PostgreSQL proof for release and draft reference exports (GOO-317)`

---

### Task 7: Frontend

**Files:**
- Modify `frontend/src/services/projectService.ts:645-679`: widen the `format` union to `'markdown' | 'latex' | 'csl-json' | 'ris'` (the default stays `'markdown'`), with filenames `references.json`/`references.ris`. Add `downloadReleaseReferences(projectId, releaseId, format)`.
- Modify `frontend/src/components/research/DraftExportModal.tsx` to add a "Reference file" group (CSL JSON, RIS). The existing choices and defaults are unchanged.
- Modify `DraftReleasePanel.tsx` to add per-release reference downloads plus the omissions count.
- Tests:
  - `default export format still markdown`
  - `csl and ris options call the right format`
  - `release reference download uses release endpoint`

**Commit:** `feat(frontend): CSL JSON and RIS reference downloads (GOO-317)`

---

## Mutation verification

Follow `docs/engineering/testing.md`. Record each check under a GOO-317 section in `docs/testing/agent-orchestration-mutation-checks.md`.

| Guard | Neutralize | Focused command | Must fail with |
|---|---|---|---|
| No name splitting | split on the last space into family/given | `pytest -q backend/tests/unit/services/test_reference_exports.py -k corporate` | the corporate author is split |
| Omit-not-invent dates | emit `PY  - n.d.` or a year of 0 | same `-k absent_year` | the reader or schema sees an invented date |
| Snippet exclusion | fall back to the snippet for a title | same `-k snippet` | `TI` present |
| Snapshot-only release source | read the live `Citation` | `pytest -q backend/tests/integration/test_reference_exports_postgres.py` | step 3: the release bytes change |
| RIS line grammar | emit a single space before `-` | `pytest -q backend/tests/unit/services/test_reference_exports.py -k ris` | `RisError` |

After each check, restore the guard, confirm `git diff` on that source file is empty, and rerun until green.

## Verification (before PR)

```sh
ruff check backend/src && black --check <changed> && isort --check-only <changed>
mypy --ignore-missing-imports --follow-imports=silent <added .py files>
pytest -q backend/tests/unit/services/test_reference_validators.py backend/tests/unit/services/test_reference_exports.py
pytest -q backend/tests/unit/api/test_draft_export_formats.py backend/tests/unit/api backend/tests/unit/architecture
(cd backend && python ../scripts/ci/check_alembic.py)      # still single head f6a8c0d2e4b5
python scripts/ci/generate_openapi.py && pnpm --dir frontend generate:api-types
git diff --exit-code -- backend/openapi.json frontend/src/types/generated/api.d.ts
pnpm --dir frontend exec vitest run src/components/research src/services
scripts/ci/run_local_ci.sh --base origin/develop --frontend
```

**NOT RUN unless the resource exists.** Report each of these as NOT RUN, never as passing:
- **PostgreSQL proof:** needs `postgres_container` or `RESEARCH_DECISION_DATABASE_URL`.
- **Import into a reference manager** (Zotero, EndNote) as a manual check: needs a desktop app. It is optional evidence and never a substitute for the schema and reader checks.
- **Vendoring the CSL schema:** needs network once (Task 1). After that the tests are offline.
- **Live journey:** needs a deployed stack with the GOO-315/316 migrations.

## Authenticated journey list for Linear closure

1. **Draft export:** export a draft as CSL JSON and as RIS. Validate offline with the vendored schema and `ris_reader.py`, and keep the files and transcripts.
2. **Release export:** download the references of a verified release in all three formats. The keys match `\cite{docN}` in the LaTeX package.
3. **Immutability:** edit a citation and re-download the release references (identical), then the draft export (updated).
4. **Omissions:** the report lists the absent fields for the corporate-author record and the untitled record.
5. **Back-compat:** the Markdown and LaTeX downloads are unchanged (compare with a pre-deploy copy).

Record the SHA/PR, CI links, generated files, validator outputs, the PostgreSQL junit, the mutation transcripts and the NOT RUN list.

## Seams

- **Upstream (GOO-315):** the only source for release exports is `snapshot.references`. If GOO-315's snapshot schema later adds structured names, the serializers emit structured CSL names, still with no invention.
- **GOO-318:** a deposit can include `references.json` from the verified package. Nothing here talks to a repository.

## Out of scope

Each item below has a `ponytail:` marker at its seam:
- DOCX;
- CSL style rendering (citeproc formatting into a bibliography);
- EndNote XML;
- abstracts and keywords in exports;
- name parsing;
- importing CSL or RIS back into projects.
