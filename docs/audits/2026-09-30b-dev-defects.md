# Dev defects observed — 2026-09-30b live evidence

Backend `https://dev-api.goodwiinz.tech`, image `8ad2c82` (per the dispatch). Evidence files are in `2026-09-30b-live-evidence/`.

## D-01 (high): research-run SSE stream returns 500 and leaves the run stuck in `running`

- **Endpoint:** `GET /api/v1/research-engine/runs/{run_id}/stream`
- **Status/body:** `500 {"error": {"message": "Internal server error during tenant validation", "status_code": 500, "type": "internal_error"}}` (`B3-02`). Reproduced on two runs: `a90c3860-2ed5-4e98-9a8c-390516f16fd3` and `6bc5ccdb-1521-461d-aa44-fff52800f9db`.
- **Effect:** the run is claimed (`pending → running`) and the search step starts; the first arXiv page is recorded ~50 ms later as `page_status=interrupted`, `error_type=InterruptedError` (`B2-06`, `B3-04`). The run then stays `running` with 0 steps and 0 tokens (`B2-02`, `B2-05`, `B3-03`), a new stream gets 409 `Run is in 'running' state and cannot be streamed` (`B2-03`), and `pause` returns 200 but has nothing to act on (`B2-04`). No research run can execute on dev, so GOO-290 step provenance, GOO-299 observe-from-sources and GOO-301 AI suggestions cannot be collected.
- **Likely cause (code reading only, logs not available):** the message comes from the catch-all in `backend/src/middleware/multi_tenancy.py` (`MultiTenancyMiddleware.dispatch`). That middleware opens `AsyncSessionLocal()` and sets `request.state.db`; `get_db` (`backend/src/core/database.py`) reuses `request.state.db` when present. For a `StreamingResponse`, `call_next` returns once headers are ready, the middleware's `async with` then closes the session while the stream generator is still using it, the resulting exception is caught and turned into this 500, and the generator is cancelled. Its `CancelledError` cleanup (`recover_run(PAUSED)`) runs on the closed session, so the run is never moved out of `running`. Not confirmed from pod logs (`kubectl` needs `aws login`).

## D-02 (medium): multi-document drafts still cannot persist

- **Endpoint:** `POST /api/v1/projects/{p}/drafts` (12 `document_ids`) → task `6287c09a9769` `failed`, `current_step = "Error: Citation review did not provide grounded evidence"`, `error_code = generation_error` (`C1-02`).
- **Review:** `f9e5d9ce-92da-4197-b1e5-b468075a5a8f`, blocked; exact/minor verdicts for docs 4, 5 and 10 have `location = "source excerpt"`, which `_require_passing_citation_review` treats as missing evidence (`C1-03`). Four `major` verdicts would block as well. Unchanged in effect from 2026-09-30 despite #1768/#1772; a single-document draft does save (`C4-02`).

## D-03 (low, PRISMA semantics): full text can be requested for a title/abstract-excluded report

- **Endpoint:** `POST /api/v1/research-engine/projects/{p}/fulltext/requests` for r1 `4b755e8c-…` after its title/abstract adjudication to `exclude` → 201 (`I1-04`).
- **Effect:** `reports_sought` (4) exceeds `records_screened − records_excluded` (3) in the PRISMA flow (`M2-10`), and no reconciliation check catches it. The implementation matches the GOO-303 plan's definition of `sought`, so this is a spec gap rather than a regression.

## D-04 (low, plan vs implementation): restricted-import titles appear in the corpus export

- **Endpoint:** `GET /api/v1/research-engine/projects/{p}/corpus/export?format=zip` (`E4-01`).
- **Observed:** titles of records from `redistribution=restricted` receipts are exported (`imports[].records[].parsed.title`, `identities.reports[].title_snapshot`). `raw`, `abstract` and `fields` are withheld and listed in `omissions`. The GOO-300 plan's journey step 8 expects a restricted title token to be absent; the code comment and `RESTRICTED_PARSED_FIELDS = ("abstract", "fields")` say titles are allowed. One of the two needs to change.

## Other observations (not defects)

- Workspace membership accepts users from another organization (`A1-01`, `D2-04`: 201), but research-engine routes and drafts return 404 to them (`A1-03`, `A1-04`, `D2-07`, `D3-04`), and role assignment refuses them with 422 (`A1-02`). A cross-org "collaborator" can therefore never collaborate; the UI may want to stop offering it.
- Run export is owner-scoped: a same-org SUPERVISOR who can `GET` the run gets 404 `run_not_found` on `/export` (`B4-05`, `B4-06`).
- Two older project documents have no `checksum_sha256` and cannot be used as retrieved full text (`M1-03`, `M2-01`: 422 `Document has no content hash yet`).
