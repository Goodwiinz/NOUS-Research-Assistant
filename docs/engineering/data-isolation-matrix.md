# Data-isolation access matrix

The expected allow/deny behavior between accounts, and the test that enforces
each row. Introduced for GOO-347 (matrix + fixtures), GOO-352 (documents,
citations, search) and GOO-353 (chat reads, export revocation). Source of
the policy: `backend/src/services/threads/workspace_access.py` (module
docstring) and [backend.md](backend.md).

## Policy in one paragraph

Two different scopes, never mixed. **Documents, files, citations and document
search are organization-scoped**: any user in the document's organization can
read it, nobody outside can, and `is_public` does not cross organizations.
**Workspaces, conversations, threads and messages are membership-scoped**:
owner, active member, or `is_public` workspace (visible to every
authenticated user, cross-organization by design). Organization co-location
grants no workspace access. A soft-deleted ancestor revokes every descendant.
Denials answer 404 or an empty list (never 403), so ids cannot be probed.

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

## Matrix

Status: **pass** = enforced and passing; **xfail GOO-n** = confirmed leak,
test is `xfail(strict=True, raises=AssertionError)` and will fail loudly
(XPASS) once the fix lands, at which point remove the marker; **not covered**
= no automated check yet. A strict xfail for something other than a confirmed
reachable leak (a broken route, a latent service gap) says so in its row.

### Documents, files, citations, search (`test_document_isolation.py`)

| Row | Caller → target | Surface | Expected | Status |
| --- | --- | --- | --- | --- |
| D1 | A → `b-doc`, `b-pub-doc`, `a-old-org-doc` | `GET /api/v1/files/{id}` `/download` `/content` `/metadata`, `GET /api/v1/documents/{id}` `/status` | 404, no title/content/bytes | pass |
| D1+ | A → `a-doc` | same routes | 200; download returns the bytes | pass |
| D2 | C → `b-doc` (same org, private) | same routes | 200 (organization-shared by design) | pass |
| D3 | C → `b-doc` after soft delete | same routes + lists + search | 404 / absent | pass |
| S1 | A | `GET /api/v1/documents`, `GET /api/v1/files/?search=`, `POST /api/v1/documents/search` (incl. `page=2&size=1`) | only `a-doc`; `total == 1` | pass |
| S1+ | C | `POST /api/v1/documents/search` | finds org-b rows (positive control) | pass |
| CI1 | A → `b-cit` (foreign private) | `GET /api/v1/citations/{id}`, `POST /api/v1/citations/export` | 404 / no title or quote | pass |
| CI1+ | A → `a-cit` | `GET /api/v1/citations/{id}` | 200 | pass |
| CI2 | A → `b-pub-cit` (foreign `is_public`) | detail, list + `total`, export | 404 / absent / `total == 1` | pass (GOO-349) |
| CI3 | A → `a-old-org-cit` (A uploaded it in org-b) | detail, list + `total`, export | 404 / absent | pass (GOO-349) |
| CI4 | C → `b-cit` (same org, private) | citation reads | Today: denied (uploader-or-public rule, stricter than D2). GOO-349 decides the intended rule. | not covered |
| CI5 | any | project bibliography fallback (`citations.py` export by `project_id`) | org-guarded | not covered (GOO-349) |
| S2 | A | `POST /api/v1/search/`, `/search/hybrid`, `/api/v2/search/*`, suggestions | org/membership-scoped | pass on PostgreSQL (GOO-399): see rows S2a–S2k below |

### Search on PostgreSQL (`test_search_isolation_postgres.py`)

Document search, its suggestions and the search analytics log are
organization-scoped. Thread and message search follow workspace membership
(owner, member or `is_public`), so A and C both see `b-pub` by design. Each
leak check looks for the canary, its 8-hex tag (which survives suggestion
normalization and `ts_headline` highlighting) and the row id. Only external
services are stubbed, never an access predicate: the Neo4j entity search
returns nothing and records the scope it was given, and Cohere reranking is
off.

| Row | Caller → target | Surface | Expected | Status |
| --- | --- | --- | --- | --- |
| S2a | A → org-b documents (incl. `a-old-org-doc`) | `POST /api/v1/search/` (fulltext, hybrid, semantic), `POST /api/v1/search/hybrid`, incl. one-result pages past the end and the title suggestions in the response | only `a-doc`, `a-doc-2`; `total_results == 2`; no org-b title, snippet, id or suggestion | pass |
| S2a+ | C → org-b | S2a routes | all three org-b documents, `total_results == 3` (positive control) | pass |
| S2b | A | `POST /api/v1/search/` fulltext and hybrid with filters `document_ids` (org-b ids), `organization_id` (org-b), `is_public`, `uploaded_by_user_id` | filters narrow A's own documents only | pass |
| S2c | A | `POST /api/v1/search/` `knowledge_graph` | Neo4j scope is A's organization and A's document ids | pass |
| S2d | A; C | `GET /api/v1/search/suggestions` (incl. B's exact title) | A: no org-b title; C: gets it (positive control) | pass |
| S2e | A; C | `GET /api/v1/search/analytics` after B searches | A: no B query, `total_searches == 0`; C: sees it | pass |
| S2f | C → `b-doc` after soft delete | S2a routes + suggestions | absent; `total_results == 2` | pass |
| S2g | A (other org), C (same org, nonmember) → workspace `b` | `POST`/`GET /api/v2/search/threads` and `/messages`, `GET /combined`; one-result pages; filters `workspace_id`, `conversation_id`, `thread_id`, author `user_id` | no `b` title, snippet or id; totals count only own rows + `b-pub` | pass |
| S2g+ | B owner; C invited, then membership soft-deleted | S2g routes | finds `b`; C loses it after removal | pass |
| S2h | B after soft-deleting thread, conversation or workspace `b` | S2g routes | absent, even for the owner | pass |
| S2i | A, C → workspace `b` | `GET /api/v2/search/suggestions` | no `b` thread title | partial: the route answers 500 on PostgreSQL for every caller (`SELECT DISTINCT t.title ... ORDER BY t.last_message_at`), so only the error body is checked and the positive control is `xfail(strict=True)`. Broken route, not a leak; needs a follow-up fix |
| S2j | D (no organization) | every route above | 401 from the tenancy gate before any search | pass |
| S2k | service layer, organization `None`, `""`, `"None"` or non-UUID | full-text search, its suggestions, hybrid search | nothing returned (GOO-351) | pass; latent gap `xfail(strict=True)`: with `None` the hybrid knowledge-graph arm still calls Neo4j with an empty entity scope (cross-tenant). Unreachable through these routes (the gate 401s org-less callers and routes pass `str(organization_id)`); the agent's legacy hybrid fallback (`_nodes_rag._legacy_hybrid_search_fallback`) passes `None` when its organization id is empty |

Not covered here: `POST /api/v1/search/authenticated/hybrid` (API-key auth;
the route rejects keys without an organization) and the Neo4j entity scope
itself, which needs a graph database.

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
| X4 | any | PDF export | not covered: needs a PDF renderer in CI |
| X5 | B → own thread | export returns content (positive control) | pass |

Note: thread export is creator-based, so a current member who did not create
a thread cannot export it, and neither can a reader of a public workspace.
That is stricter than reads. Since GOO-348 the creator must also still have
current workspace access, so a creator removed from the workspace cannot export.

### Browser account switching (GOO-354)

`tests/e2e/tests/account-switch-isolation.spec.ts`: sign in as one seeded
account, put a canary message in the chat transcript (stream intercepted),
sign out, sign in as another account in the same browser, and assert the
canary never renders and no previous-account thread id survives in
`localStorage`. It is opt-in (`E2E_ACCOUNT_SWITCH=1`) until it has been run
once against the CI stack; client-store clearing itself is GOO-350
(PR #1776). Deployed Data API grants remain GOO-285.

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
```
