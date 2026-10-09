# Data-isolation access matrix

The expected allow/deny behavior between accounts, and the test that enforces
each row. Introduced for GOO-347 (matrix + fixtures), GOO-352 (documents,
citations, search), GOO-353 (chat reads, export revocation) and GOO-400
(project bibliography fallback, PDF export). Source of
the policy: `backend/src/services/threads/workspace_access.py` (module
docstring) and [backend.md](backend.md).

## Policy in one paragraph

Two different scopes, never mixed. **Documents, files, citations and document
search are organization-scoped**: any user in the document's organization can
read it, nobody outside can, and `is_public` does not cross organizations. A
document's `is_public` flag is a label and a list/search filter only: it grants
nothing across organizations and hides nothing inside one, so a colleague's
private document and its citations read the same as a public one (see the
GOO-410 decision below). Uploader-only rules apply to mutations (delete,
metadata edit, reprocess), never to reads.
**Workspaces, conversations, threads and messages are membership-scoped**:
owner, active member, or `is_public` workspace (visible to every
authenticated user, cross-organization by design). Organization co-location
grants no workspace access. A soft-deleted ancestor revokes every descendant.
Denials answer 404 or an empty list (never 403), so ids cannot be probed.

### Decision (GOO-410, 2026-10-08)

Documents are **organization-shared**. The evidence: `backend/AGENTS.md`
("Document, content-hash deduplication, and search-suggestion access is
organization scoped"); the policy paragraph above; the
`backend/src/services/threads/workspace_access.py` module docstring; row D2
(same-org colleague C reads B's private document: 200 by design); and the
2026-10-07 security audit, which rejected candidate A2 (same-org read of a
private document through `/citations/extract`) because org-wide read is the
platform norm. GOO-410 / GOO-398 removed the contradicting uploader-or-public
rule from the citation reads (`_document_is_accessible` /
`_document_access_clause` in `backend/src/api/research/citations.py`) and from
adding a document to a project and from the unused `can_access_document`
dependency (`backend/src/core/dependencies.py`), deleted the never-firing
private checks in `backend/src/api/documents/documents.py`, and scoped the
project bibliography (`GET /api/v1/projects/{id}/bibliography`) to the
caller's organization like the other project reads (rows CI4, CI6 and
PD1-PD4).

Evidence revision: rows CI4, CI6 and PD1-PD4 were marked pass from local runs
of `backend/tests/integration/two_account` on branch commit `5d958e51d`
(base `develop` `9c90ed8d3`). They were re-run green after `develop`
(including GOO-400's CI5/X4 rows) was merged in at `235725cc1`: the
two-account and citation suites gave 164 passed. The producer of record is
the PR's Integration Tests job on its final head.

Making documents private to their uploader would be a separate product
change, not a fix. It would need the read boundary changed on the documents
list and detail routes (`GET /api/v1/documents` and its `/{id}` reads:
detail, `/entities`, `/status`, `/figures`, `/tables`, `/integrity-score`),
the files read and download routes (`GET /api/v1/files/`,
`/{id}`, `/{id}/download`, `/{id}/content`, `/{id}/metadata`), and search
(`POST /api/v1/documents/search`, `/api/v1/search/*`, `/api/v2/search/*`,
suggestions), followed by the citation, project and agent-tool reads that
mirror them.

## Accounts and seed

Fixtures: `backend/tests/integration/two_account/conftest.py`. Fresh
in-memory database per test, so cleanup is `drop_all` and idempotent. Every
id is `uuid5(NS, name)` and every private string is `canary(name)`
(`ISO-CANARY-<NAME>-<hash>`), so ids and canaries are reproducible and a
failure names the leaked key. Clients authenticate with a real HS256 token
through `MultiTenancyMiddleware` and the real `get_current_user`; only
`get_db` is overridden, with a fresh session per request.

| Account | Organization | Role in the matrix |
| --- | --- | --- |
| A | org-a | Caller under test. Owns `a-doc`, `a-cit`, private workspace `a`. Uploaded `a-old-org-doc` while in org-b. |
| B | org-b | Owns `b-doc` (private), `b-pub-doc` (`is_public`), their citations, private workspace `b`, public workspace `b-pub`. |
| C | org-b | B's colleague: same organization, not a member of workspace `b` until a test invites them. |
| D | none | PostgreSQL seed only: an active user with no organization (missing-organization caller). |

The PostgreSQL search suite (GOO-399) uses the `pg_*` fixtures in the same
`conftest.py`: the same seed plus D and a second org-a document `a-doc-2`, in
a throwaway schema of the database named by `TWO_ACCOUNT_PG_TEST_DATABASE_URL`
(the suite skips without it). The schema is every ORM table plus the
production Alembic revision that adds thread and message `search_vector`
columns and triggers; document vectors are built by the production ingest
helper. `get_db_sync` and `database.SessionLocal` also point at that schema.

CI5 adds project `b-proj`, a Collection in workspace `b` (fixture
`bib_project` in `test_document_isolation.py`). It links `c-proj-doc` (org-b,
uploaded by C, no citation), `a-proj-doc` (org-a, `is_public`, citation
`a-proj-cit`) and soft-deleted `c-gone-doc` (org-b, uploaded by C, citation
`c-gone-cit`). Every linked document and citation carries DOI and arXiv
canaries. No link is a same-org document uploaded by someone else, so the rows
hold under both the uploader-or-public citation rule and an org-shared one.

## Matrix

Status: **pass** = enforced and passing; **xfail GOO-n** = confirmed leak,
test is `xfail(strict=True, raises=AssertionError)` and will fail loudly
(XPASS) once the fix lands, at which point remove the marker; **pending CI**
= the check exists and passed locally, but the CI step that produces the
evidence has not yet passed on the PR head (flip it to pass, citing that run,
once it has); **not covered** = no automated check yet.

### Documents, files, citations, search (`test_document_isolation.py`)

| Row | Caller → target | Surface | Expected | Status |
| --- | --- | --- | --- | --- |
| D1 | A → `b-doc`, `b-pub-doc`, `a-old-org-doc` | `GET /api/v1/files/{id}` `/download` `/content` `/metadata`, `GET /api/v1/documents/{id}` `/status` | 404, no title/content/bytes | pass |
| D1+ | A → `a-doc` | same routes | 200; download returns the bytes | pass |
| D2 | C → `b-doc` (same org, private) | same routes | 200 (organization-shared by design) | pass |
| D3 | C → `b-doc` after soft delete | same routes + lists + search | 404 / absent | pass |
| S1 | A | `GET /api/v1/documents`, `GET /api/v1/files/?search=`, `POST /api/v1/documents/search` (incl. `page=2&size=1`) | only `a-doc`; `total == 1` | pass |
| S1+ | C | `POST /api/v1/documents/search` | finds org-b rows (positive control) | pass |
| CI1 | A → `b-cit` (foreign private) | `GET /api/v1/citations/{id}`, list + `total`, `POST /api/v1/citations/export` | 404 / absent / export 404, no title or quote | pass |
| CI1+ | A → `a-cit` | `GET /api/v1/citations/{id}` | 200 | pass |
| CI2 | A → `b-pub-cit` (foreign `is_public`) | detail, list + `total`, export | 404 / absent / `total == 1` | pass (GOO-349) |
| CI3 | A → `a-old-org-cit` (A uploaded it in org-b) | detail, list + `total`, export | 404 / absent | pass (GOO-349) |
| CI4 | C → `b-cit` (same org, private) | detail, list + `total`, export | 200 with the quote / all three org-b citations listed, `total == 3` / exported (organization-shared, like D2) | pass (GOO-398) |
| CI5 | C (invited member of `b`) → `b-proj`; A (org-a, also invited); C after B removes them | `POST /api/v1/citations/export` with `project_id` (document-metadata fallback), bibtex/ieee/apa/mla | C: only `c-proj-doc`, one BibTeX entry, no title, quote, DOI or arXiv id of `a-proj-doc`/`c-gone-doc` or their citations; with only those two links left, 404. A and removed C: 404, no canary | pass (GOO-400) |
| CI6 | B, C → `b-cit` after `b-doc` is soft-deleted | detail, list + `total`, export | 404 / absent, `total == 2` / export 404 | pass (GOO-398) |
| S2 | A | `POST /api/v1/search/`, `/search/hybrid`, `/api/v2/search/*`, suggestions | org/membership-scoped | pending CI (GOO-399): rows S2a–S2m below passed locally on PostgreSQL 14; they flip to pass when the Integration Tests step "Run two-account PostgreSQL search isolation tests" passes on the PR head |

### Search on PostgreSQL (`test_search_isolation_postgres.py`)

Document search, its suggestions, reindex and the search analytics log are
organization-scoped. Thread and message search follow workspace membership
(owner, member or `is_public`), so A and C both see `b-pub` by design. Each
leak check looks for the canary, its 8-hex tag (which survives suggestion
normalization and `ts_headline` highlighting) and the row id. Only external
services are stubbed, never an access predicate: the Neo4j entity search
returns the entities a test sets (none by default) whatever its scope, and
records the scope it was given; Cohere reranking is off.

| Row | Caller → target | Surface | Expected | Status |
| --- | --- | --- | --- | --- |
| S2a | A → org-b documents (incl. `a-old-org-doc`) | `POST /api/v1/search/` (fulltext, hybrid, semantic), `POST /api/v1/search/hybrid`, incl. one-result pages past the end and the title suggestions in the response | only `a-doc`, `a-doc-2`; `total_results == 2`; no org-b title, snippet, id or suggestion | pending CI |
| S2a+ | C → org-b | S2a routes | all three org-b documents, `total_results == 3` (positive control) | pending CI |
| S2b | A | `POST /api/v1/search/` fulltext and hybrid with filters `document_ids` (org-b ids), `organization_id` (org-b), `is_public`, `uploaded_by_user_id` | filters narrow A's own documents only | pending CI |
| S2c | A | `POST /api/v1/search/` `knowledge_graph` | Neo4j scope is A's organization and A's document ids | pending CI |
| S2d | A; C after `b-doc` is soft-deleted | hybrid routes of S2a, with the graph stub returning an entity whose source is `b-doc` (a stale organization stamp) | a graph hit only corroborates a document the full-text arm returned: `b-doc` and the entity name never reach a result, total or suggestion | pending CI |
| S2e | A; C | `GET /api/v1/search/suggestions` (incl. B's exact title) | A: no org-b title; C: gets it (positive control) | pending CI |
| S2f | A; C | `GET /api/v1/search/analytics` after B searches | A: no B query, `total_searches == 0`; C: sees it | pending CI |
| S2g | A → org-b documents; C → `b-doc` | `POST /api/v1/search/documents/{id}/reindex` | A: 404, no title; C: 200 with the title (positive control) | pending CI |
| S2h | C → `b-doc` after soft delete | S2a routes, suggestions, reindex | absent / 404; `total_results == 2` | pending CI |
| S2i | A (other org), C (same org, nonmember) → workspace `b` | `POST`/`GET /api/v2/search/threads` and `/messages`, `GET /combined`; one-result pages; filters `workspace_id`, `conversation_id`, `thread_id`, author `user_id` | no `b` title, snippet or id; totals count only own rows + `b-pub` | pending CI |
| S2i+ | B owner; C invited, then membership soft-deleted | S2i routes | finds `b`; C loses it after removal | pending CI |
| S2j | B after soft-deleting thread, conversation or workspace `b` | S2i routes | absent, even for the owner | pending CI |
| S2k | A, C → workspace `b`; B; C invited, then removed | `GET /api/v2/search/suggestions` (incl. B's exact title and `workspace_id` = `b`) | A, C: only titles they can read; B and invited C: `b`'s title; removed C: none | pending CI |
| S2l | D (no organization) | every route above | 401 from the tenancy gate before any search | pending CI |
| S2m | service layer, organization `None`, `""`, `"None"` or non-UUID | full-text search, hybrid search (incl. its graph arm), title suggestions | nothing returned; search runs no SQL (GOO-351); the graph arm never queries Neo4j without an organization; suggestions run no SQL for `None` or `""` and rely on the `uuid` column for the rest | pending CI |

GOO-399 also fixed three defects this suite found. None was a cross-organization
leak reachable through these routes:

- `GET /api/v2/search/suggestions` answered 500 on PostgreSQL for every
  caller: `SELECT DISTINCT t.title ... ORDER BY t.last_message_at` is invalid
  there (row S2k).
- With no organization, the hybrid graph arm called Neo4j with an empty
  entity scope, which spans every tenant. The search routes cannot reach this
  because the tenancy gate rejects org-less callers, but the agent's legacy
  hybrid fallback (`_nodes_rag._legacy_hybrid_search_fallback`) passes `None`
  when its organization id is empty (row S2m).
- Hybrid `suggestions` were built from raw graph entity names, including
  entities whose document fusion had dropped (row S2d).

Not covered here: the other `/api/v1/search` routes (`/indexes`,
`/indexes/rebuild`, `/health`, the API-key `/authenticated/*` routes, and the
placeholders that return no data), `GET /api/v2/search/health`,
`/api/v1/search-quality/*`, `/api/v1/knowledge-graph/*`, and the Neo4j entity
scope itself, which needs a graph database.

### Project documents and bibliography (`test_project_document_isolation.py`)

The file seeds `a-project` in workspace `a` and `b-project` in workspace `b`.
Workspace membership may cross organizations, so a project can hold a
foreign-org document: an org-a editor of `b` can link an org-a document
through `POST /api/v2/collections/{id}/documents`, which accepts any
document of the caller's own organization.

| Row | Caller → target | Surface | Expected | Status |
| --- | --- | --- | --- | --- |
| PD1 | A → `b-doc`, `b-pub-doc`, `a-old-org-doc` | `POST /api/v1/projects/{a-project}/documents` | 404, no title/content, no link row | pass (GOO-410) |
| PD1+ | C (editor of `b`) → `b-doc` (same org, private) | `POST /api/v1/projects/{b-project}/documents` | 201, linked (organization-shared, like D2) | pass (GOO-410) |
| PD2 | B, C → `b-doc` after soft delete | same route on `b-project` | 404, no link row | pass (GOO-410) |
| PD3 | B → `b-project` holding `b-doc` and A's org-a `a-doc` (linked by A as an editor of `b`) | `GET /api/v1/projects/{id}/bibliography`, citation rows | `b-cit` only, `citation_count == 1`, no `a-cit`/`a-doc` text; A gets 404 | pass (GOO-410) |
| PD4 | B → same project with `b-cit` soft-deleted, then with `b-doc` soft-deleted | same route, document-metadata fallback | `b-doc` metadata only, then nothing; never `a-doc` | pass (GOO-410) |

### Chat and exports (`test_chat_isolation.py`)

Read surfaces checked per row: `GET /api/v2/workspaces/{ws}` (+ `/conversations`,
`/threads`, nested conversation, nested thread messages),
`/api/v2/conversations/{id}` (+ `/threads`), `/api/v2/threads/{id}`
(+ `/messages`, `/messages/{id}`, `/context`), `/api/v2/messages/{id}`, the
workspace list, and the owner-only `GET /api/v1/agent/threads/{id}/messages`.

| Row | Caller → target | Expected | Status |
| --- | --- | --- | --- |
| W1 | B → own workspace `b` | 200 with content | pass |
| W2 | A (other org) → `b` | 404 / empty, no canary | pass |
| W3 | C (same org, nonmember) → `b` | 404 / empty, no canary | pass |
| W4 | C invited as member → `b` | 200 with content (agent history stays owner-only) | pass |
| W5 | A → public workspace `b-pub` | 200 with content | pass |
| W6 | C member, after soft-deleting workspace / conversation / thread | 404 on thread, message, context | pass |
| W7 | A, member then removed by B via `DELETE .../members/{id}` | all reads denied, including a B-only message added after removal | pass |
| X1 | A (other org) → B's thread | single, stream and batch export in markdown/json/html: 404 or absent from the ZIP | pass |
| X2 | B → own thread after its conversation is soft-deleted | export 404 | pass |
| X3 | A, removed member who created a thread in `b` | single + stream export (md/json/html), preview, batch ZIP + non-ZIP: no B-only message, 404 | pass (GOO-348) |
| X4 | B → own thread; A (other org); A as member-creator before and after removal; B after soft-deleting workspace / conversation / thread | single + stream PDF export: B, and A before removal, get `%PDF-` bytes whose text holds the message; every denial is 404 with no PDF bytes and no canary | pass (GOO-400) |
| X5 | B → own thread | export returns content (positive control) | pass |

Note: thread export is creator-based, so a current member who did not create
a thread cannot export it, and neither can a reader of a public workspace.
That is stricter than reads. Since GOO-348 the creator must also still have
current workspace access, so a creator removed from the workspace cannot export.

X4 renders real PDFs, so it needs WeasyPrint's native pango libraries. The
Integration Tests job installs them (`libpango-1.0-0 libpangoft2-1.0-0
libharfbuzz-subset0 fonts-dejavu-core`, as `backend/docker/Dockerfile.prod`
does) and sets `PDF_EXPORT_TEST_REQUIRE_RENDERER=1`, so a missing renderer
fails X4 there. Anywhere else X4 skips and names the reason.

### Browser account switching (GOO-354)

`tests/e2e/tests/account-switch-isolation.spec.ts` runs five Chromium smoke
cases. Each signs in as seeded account A, switches to B in the same browser,
and checks that an A canary never reaches B. Agent, project, and search
responses are synthetic (intercepted); sign-in uses real credentials.

| Row | Switch | Checks | Guard | Status |
| --- | --- | --- | --- | --- |
| BS1 | same tab | chat transcript; the selected thread and canary in `localStorage` | client store clearing, GOO-350 (PR #1854) | pass |
| BS2 | same tab | agent panel | client store clearing, GOO-350 | pass |
| BS3 | same tab | `/research` never renders A's project, checked by a MutationObserver that sees a one-render flash | `useProjectStore` reset in `clearUserScopedClientState` | pending CI |
| BS4 | second tab signs A out and B in; `/search` stays mounted | A's earlier answer and a held A response never show for B | `AuthProvider` `key: accountRevision` remount; the account abort signal | pending CI |
| BS5 | A's chat stream is rejected, then B signs in | the canary is absent from the page, `localStorage`, and the `sessionStorage` chat recovery draft | `observeIdentity` discards a draft owned by another account | pending CI |

BS5 found a leak: B's chat route never reopens A's thread, so nothing consumed
or removed A's staged prompt, and it stayed in B's `sessionStorage` for up to
15 minutes. The unit counterpart is
`frontend/src/store/__tests__/auth-account-isolation.test.ts` ("chat
draft"). Each pending row lists, beside its test, the mutation that must turn
it red. Chat request lifetime handling is PR #1776. Real two-JWT Data API
probes and a deployed browser run are still pending; a passing source or PR
test does not prove deployed isolation.

## Commands

```sh
# What CI runs (Integration Tests job; the files carry the integration marker)
pytest backend/tests/integration/two_account -c backend/pytest.ini -m integration

# PostgreSQL search rows (S2*). Point the variable at a disposable database;
# each run creates and drops its own schema. CI runs this as its own step
# against the job's postgres service and fails if any test skips.
TWO_ACCOUNT_PG_TEST_DATABASE_URL=postgresql://user@/disposable_db?host=/tmp \
  pytest backend/tests/integration/two_account/test_search_isolation_postgres.py \
  -c backend/pytest.ini -m integration

# See the current leaks behind the xfail rows
pytest backend/tests/integration/two_account -c backend/pytest.ini --runxfail

# X4 locally on macOS with Homebrew pango (without the library path it skips)
DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib PDF_EXPORT_TEST_REQUIRE_RENDERER=1 \
  pytest backend/tests/integration/two_account -c backend/pytest.ini -m integration -k pdf
```
