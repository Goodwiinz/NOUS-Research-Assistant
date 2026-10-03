# GOO-297 — durable draft-task terminal results: evidence (2026-09-30b)

- Backend host: `https://dev-api.goodwiinz.tech`, image `8ad2c82` (per the dispatch).
- Raw requests/responses: `2026-09-30b-live-evidence/` (`C1-02`, `C3-*`, `C4-*`).

GOO-297 covers draft generation tasks (`GET /api/v1/projects/{p}/drafts/status/{task_id}`), not research-engine runs; the research run in GOO-290 never reached a step (D-01).

## Proven

| check | evidence | result |
|---|---|---|
| Completed task reports its artifact | `C4-02` | `status=completed`, `draft_id=a941ca8e-…`, `artifact_version=1`, `artifact_hash=e42fe1b5f11757885962914a28db50effb51f4587050bbeae3ab34c369bb03d6`, `state_source=database` |
| `artifact_hash` equals the saved draft content | `C4-03` | `sha256(content)` of `GET …/drafts/current` = `e42fe1b5…` (equal); draft `version=1`, `is_current=true` |
| Failed task is durable with an error code | `C1-02` | `status=failed`, `error_code=generation_error`, `state_source=database`, no artifact fields |
| Pre-GOO-297 tasks still answer | `C3-01`, `C3-02` | 200 from `state_source=cache`, `error_code=null` (tasks from 09-29/09-30 predate the table) |
| Foreign org on a task | `C3-03` | 404 |

## NOT RUN

| step | reason |
|---|---|
| Side-by-side SQL of `draft_task_results` vs `generated_drafts` | No DB/pod access (`kubectl` needs `aws login`); the API-level hash equality above stands in for it. |
| `redis-cli DEL` then re-GET (`state_source=database` after cache loss) | No Valkey access. `C4-02` already reports `state_source=database` once the task is terminal. |
| Pod kill mid-generation → `interrupted/process_lost` | Disruptive; not authorised for this session. |
| Two back-to-back tasks with distinct artifacts | One completed task only; the second (`6287c09a9769`) failed at review. |
