# GOO-290 — approved-protocol run, step provenance and export: evidence (2026-09-30b)

- Backend host: `https://dev-api.goodwiinz.tech`, image `8ad2c82` (per the dispatch; pod SHA not read, AWS session expired).
- Raw requests/responses: `2026-09-30b-live-evidence/` (`B*` steps; index in `journey.md`).

## Proven

| item | evidence | result |
|---|---|---|
| Supervisor approval of protocol `4a88e789-…` v1 (`d38ab741-…`) | `B1-04` | 200, decision `0433a6bd-c8dc-4cbc-8f1b-d5a45a4a7222`, `actor_role=supervisor`, actor `approver` |
| Owner self-approval still refused | `B1-02` | 403 |
| Run start without `protocol_version_id` | `B2-00` | 409 `approved_protocol_required` (the body must name the approved version) |
| Run start bound to the approved version | `B2-01` | 201, run `a90c3860-2ed5-4e98-9a8c-390516f16fd3`, `protocol_version_id=d38ab741-…`, `effective_plan_hash=31011c46c41161f14293c7c113ece54e571428eaf77fdb65efca2bb56244ddc0`, `conformance_status=plan_verified` |
| `content_hash` and `effective_plan_hash` recompute from the protocol version JSON | `B1-01` + script below | both equal (`71cf73b6…`, `31011c46…`) |
| Foreign user on the run | `B4-01…04` | 404 on run, export, manifest, steps |
| Export of a non-completed run | `B2-07` | 409 `run_not_completed` |

```python
from src.services.research_engine.protocol_service import canonical_hash, protocol_content
v = json.load(open("B1-01-get-protocol.json"))["response"]["versions"][0]
canonical_hash(protocol_content(v["question_version_id"], v["blueprint_id"], v["snapshot"], v["execution_plan"]))  # == v["content_hash"]
canonical_hash({"protocol_content_hash": v["content_hash"], "execution_plan": v["execution_plan"]})              # == run.effective_plan_hash
```

## Blocked by a dev defect: no step ever executes

`GET /runs/{id}/stream` returned HTTP 500 `Internal server error during tenant validation` (`B3-02`, reproduced on a second run `6bc5ccdb-1521-461d-aa44-fff52800f9db`). The run had already been claimed: it stays `running` with 0 steps and 0 tokens (`B2-02`, `B2-05`, `B3-03`), and the manifest shows the first arXiv search page started and ended ~50 ms later as `interrupted` / `InterruptedError` (`B2-06`, `B3-04`). A pause request is accepted (`B2-04`, 200) but no stream is running to honour it, and a new stream is refused with 409 because the run is `running` (`B2-03`). See D-01 in `2026-09-30b-dev-defects.md`.

No provider spend happened: the only step started was the deterministic search step, and it was interrupted before the arXiv response.

## NOT RUN

| step | reason |
|---|---|
| Wait for one step to complete; `sha256(step.input/output)` vs `input_hash/output_hash`; `effective_model`, `temperature`, `seed` on a step | No step row exists (D-01). |
| JSON export download and hash checks | Export requires `status=completed` (`B2-07` 409); a completed run needs every step, including paid LLM steps, which the dispatch capped at one step. |
| Supervisor (non-owner) export | `B4-05` returned 404 `run_not_found` for the same-org supervisor: run export is owner-scoped by design (`export_run` docstring). Recorded, not a defect. |
