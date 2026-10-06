# GOO-303 — full-text acquisition and derived PRISMA 2020 flow: evidence (2026-09-30b)

- Backend host: `https://dev-api.goodwiinz.tech`, image `8ad2c82` (per the dispatch).
- Raw requests/responses: `2026-09-30b-live-evidence/` (`I*`, `J*`, `K*`, `M*`; index in `journey.md`).
- Reports: r1 `4b755e8c-…` (title/abstract adjudicated **exclude**), r2 `5d6149b8-…`, r3 `ec92bcc8-…`, r4 `a80f493a-…` (title/abstract include). Protocol v2 `94d8590a-…`.

## Proven

| check | evidence | result |
|---|---|---|
| Request full text ×3 | `I1-01` | 201 ×3, `state=pending`; requests r2 `8f03b28f-…`, r3 `cdd09024-…`, r4 `a7832d2f-…` |
| Replay / new key | `I1-02`, `I1-03` | 200 same `request_id` / 409 `Full text already requested` |
| Foreign org request / PRISMA | `I1-05`, `K4-01` | 404, 404 |
| r3 unavailable with reason and date | `I2-01` | 201, `state=unavailable`, attempt `136fc61a-…` |
| Full-text queue gate on r3 | `J2-01` | 409 `Full text not retrieved` |
| r2 retrieved with a project document | `I2-02` | 201, `document_content_hash=78ae5bb1f37b0919869b93c4e7c7f73803b52a85773e806b36fca34a9e822981`; `sha256` of the file bytes from `GET /api/v1/files/{id}/download` (`I2-10`, 1750 B) is the same value |
| Replay the retrieved attempt | `I2-03`, `K2-01` | 200; PRISMA `body_sha256` unchanged (`K2-02`) |
| Attempt on an unknown document id | `I2-05` | 404 `Document not found` (random UUID; no cross-org document was available to try) |
| Stale `previous_attempt_id` | `M1-01`, `M1-02` | 409 `Attempt is stale; reload acquisition state` |
| Retry r3 after unavailable | `M2-02` | 201, chain `unavailable → retrieved` (`M2-11`) |
| Full-text dual screening of r2 | `J2-04…06` | P2 exclude `wrong study design`, P3 include → conflict → adjudicator exclude `wrong study design` (200) |
| r4 agreement include | `J2-07/08` | `agreement/include` |
| PRISMA JSON twice | `K1-01`, `K1-02` | same `body_sha256 9a5ada19…` |
| PRISMA changes after one more attempt | `K3-01`, `K3-02` | `body_sha256` → `29296703…` |
| Final flow | `M2-10`, `M2-12` (Markdown) | `body_sha256 2fa99f5b4af52110821ceef0145d4c99f68ba2317150c4bd3ee52bc7936d2801`; all three `checks` true; `amendments` = `acquisition.retrieved` (unavailable→retrieved), `full_text.adjudicated`, `title_abstract.adjudicated`, `title_abstract.reopened`, `title_abstract.adjudicated` |
| Archived project | `K4-02`, `K4-03` | GET `/prisma` 200; POST request 409 `Project is not writable` |

### Reconciliation of the final counts with the persisted rows

No DB access, so each PRISMA count was recomputed from the rows the API returned in this session (import receipts `E1-07`, reports `E2-01` minus the merge in `E3-10`, the governing title/abstract resolutions `H3-01`/`H4-02`/`H6-02`/`H7-06`, full-text states `M2-11`, full-text resolutions `J2-06`/`J2-08`):

| count | recomputed | PRISMA | result |
|---|---|---|---|
| records_identified | 12 | 12 | PASS |
| import_rejected | 4 | 4 | PASS |
| unique_reports | 8 | 8 | PASS |
| duplicates_removed | 4 | 4 | PASS |
| records_screened | 4 | 4 | PASS |
| records_excluded | 1 | 1 | PASS |
| records_awaiting_screening | 4 | 4 | PASS |
| reports_sought | 4 | 4 | PASS |
| reports_not_retrieved | 1 | 1 | PASS |
| reports_awaiting_retrieval | 0 | 0 | PASS |
| reports_assessed | 2 | 2 | PASS |
| included_reports | 1 | 1 | PASS |

`included_studies = 1`: r4 has no confirmed study link, so it counts as its own study.

### Observation: a title/abstract-excluded report can be sought

`I1-04` requested full text for r1, which was adjudicated **exclude** at title/abstract, and got 201. The flow then shows `records_screened 4 − records_excluded 1 = 3` but `reports_sought 4`. The plan defines `sought` as every requested report and does not gate requests on inclusion, so the numbers reconcile with the rows; in PRISMA 2020 terms, though, "reports sought" should come from records that passed screening. Filed as D-03.

## NOT RUN

| step | reason |
|---|---|
| Raw SQL per count on the dev DB | No pod/DB access (AWS session expired); API-row reconciliation above instead. |
| `document_content_hash` vs `SELECT checksum_sha256` | Same; compared with the sha256 of the downloaded file bytes instead. |
| Upload a new PDF for the retry | Used existing hashed project documents; two older documents (`0d73d115-…`, `03be7c34-…`) were refused with 422 `Document has no content hash yet` (`M1-03`, `M2-01`). |
| Role-less viewer POST → 404 | No role-less viewer in the owner org. |
| Reopen and re-resolve the full-text adjudication | Reopen/re-resolve was exercised at title/abstract (GOO-302, `H7-*`). |
| Browser screenshots | API-only session. |
