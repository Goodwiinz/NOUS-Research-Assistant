# GOO-299 — report identity, merge and study link: evidence (2026-09-30b)

- Backend host: `https://dev-api.goodwiinz.tech`, image `8ad2c82` (per the dispatch).
- Raw requests/responses: `2026-09-30b-live-evidence/` (`E2-*`, `E3-*`; index in `journey.md`).
- Reports come from the GOO-300 imports in this session (the research run produced no sources, see D-01).

## Proven

| check | evidence | result |
|---|---|---|
| Imported records observed into reports | `E2-01` | 9 reports; the two eval records sharing DOI `10.5555/eval-0930b-alpha` land on one report `5d6149b8-…` (`match_method` `new` + `doi`); receipt v2 records re-attach to v1 reports by DOI |
| Candidate evidence | `E3-01` | report `0642117c-…` ("Record three", no identifiers) suggests `284229e8-…` with reason `title_year` |
| Reviewer cannot merge | `E3-02` | 403 `adjudicator role required` |
| Adjudicator merge | `E3-03` | 200; history event `identity.report_merged`, `actor_role=adjudicator`, `moved_import_record_ids=["159b34ed-…"]`, `protocol_version_id=d38ab741-…` (`E3-10`) |
| Reviewer proposes a study link | `E3-04` | 200, `study_link_status=proposed`, study `206bafb0-5826-449c-973e-b43d66bec1fc` |
| Reviewer cannot confirm | `E3-05` | 403 `adjudicator role required` |
| Adjudicator confirms | `E3-06` | 200, `confirmed` |
| Idempotent replay / changed body | `E3-07`, `E3-08` | 200 same state / 409 `Idempotency conflict` |
| History | `E3-10` | 3 events in order: `report_merged`, `study_linked` (reviewer, `prior_study_id=null`, `status=proposed`), `study_linked` (adjudicator, `prior_study_id=206bafb0-…`, `status=confirmed`); each carries `protocol_version_id` and `actor_role` |
| Foreign org on the same report | `E3-09` | 404 |

The history events have no `schema_version` field in the response (the GOO-300 plan's "schema 2" wording could not be checked from the API).

## NOT RUN

| step | reason |
|---|---|
| Observe from research-run sources | Runs cannot execute on dev (D-01), so no `research_sources` exist for the project. |
| `alembic current`, backfill `--dry-run/--apply` | No pod access (AWS session expired). |
| Archived-project merge → 409 | Not attempted; the archived collection has no reports. |
| Browser screenshot of the panel | API-only session. |
