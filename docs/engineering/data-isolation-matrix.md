# Data-isolation access matrix

The expected allow/deny behavior between accounts, and the test that enforces
each row. Introduced for GOO-347 (matrix + fixtures), GOO-352 (documents,
citations, search) and GOO-353 (chat reads, export revocation). Source of
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
caller's organization like the other project reads.

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

## Matrix

Status: **pass** = enforced and passing; **xfail GOO-n** = confirmed leak,
test is `xfail(strict=True, raises=AssertionError)` and will fail loudly
(XPASS) once the fix lands, at which point remove the marker; **not covered**
= no automated check yet.

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
| CI5 | any | project bibliography fallback (`citations.py` export by `project_id`) | org-guarded | not covered (GOO-349) |
| CI6 | B, C → `b-cit` after `b-doc` is soft-deleted | detail, list + `total`, export | 404 / absent, `total == 2` / export 404 | pass (GOO-398) |
| S2 | A | `POST /api/v1/search/`, `/search/hybrid`, `/api/v2/search/*`, suggestions | org/membership-scoped | not covered: PostgreSQL full-text only (SQLite cannot run them); GOO-351 owns the fail-closed service guard |

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

# See the current leaks behind the xfail rows
pytest backend/tests/integration/two_account -c backend/pytest.ini --runxfail
```
