# TypeSafe routing pilot

The optional TypeSafe router chooses an existing agent branch. It does not replace
Azure generation, grant tools, authorize actions, or solve cross-branch workflows.
Default: `AGENT_INTENT_PROVIDER=azure`. No deployment is enabled by this change.

## Local configuration

Supply a **rotated** `TYPESAFE_API_KEY` through your environment or secret manager.
Do not reuse the key pasted into chat, put it in a shell command, or commit it.

```dotenv
AGENT_INTENT_PROVIDER=typesafe
TYPESAFE_MODEL=jev-1.13.0
TYPESAFE_TIMEOUT_SECONDS=1.0
```

`TYPESAFE_MIN_CONFIDENCE` must also be set to a threshold calibrated on reviewed
data before the router accepts TypeSafe results. Leave it **unset**, not empty,
until then. With no threshold/key/client, classification goes directly to Azure
without a TypeSafe request. Azure credentials are still required for the agent.

The FastAPI lifespan and Celery worker manage pooled clients. Explicit scripts
must call `start_typesafe_client()` and `await close_typesafe_client()` on their
own event loop, as the replay command does. Settings changes require restarting
the affected process; this is not a hot-reload feature.

## Offline and live replay

From the checkout root, using the project's Python environment:

```sh
PYTHONPATH=backend python backend/tests/eval/replay_intent_routing.py
```

This validates 20 synthetic regression cases with **zero network calls**. They
derive from previously seen experiments, not an independent holdout. Multiple
routes can be acceptable; tool coverage is not task completion. The unsupported
Python-plus-bibliography case is reported separately.

Only an explicit `--run-live` makes paid API calls:

```sh
PYTHONPATH=backend python backend/tests/eval/replay_intent_routing.py --provider azure --run-live
PYTHONPATH=backend python backend/tests/eval/replay_intent_routing.py --provider typesafe --run-live
PYTHONPATH=backend python backend/tests/eval/replay_intent_routing.py --provider cascade --run-live
```

- `azure`: corrected deterministic eligibility, Azure semantics, existing final fallback.
- `typesafe`: raw validated Choice, probabilities, confidence, model, and usage;
  no acceptance threshold or Azure fallback. Useful for calibration.
- `cascade`: actual live routing policy, requiring an explicit threshold.

Use `--cases /path/to/reviewed.json` for a separate reviewed calibration/holdout
dataset. JSONL output excludes queries/context and raw exception bodies. Errors
remain rows in the denominator; do not overwrite them with successful reruns.
Retain the source revision, settings, model, dataset provenance, and account-billed
cost separately. Keep benchmark results internal unless publication is permitted.

## Behavior and diagnostics

Free-form requests reach semantic classification instead of phrase/keyword early
returns. Empty input and context-free bare greetings remain local. Keywords are
the final fallback if the semantic provider fails or is insufficiently confident.
This increases semantic-call volume relative to the old heuristics; measure it.

Both providers receive the same bounded input: query (at most 8,000 characters
plus a truncation marker), previous assistant text (400 plus an ellipsis), page hints, and the last
eight tool outcomes. Tool arguments and result bodies are omitted. Nested
`metadata.paper_id` / `paper_title` are understood when supplied, but the normal
chat UI currently sends project/thread context, not active-paper selection. No UI
or public request-schema change is included. Page metadata never grants access.

The Choice rubric uses current registry subgraph memberships for specialists and
intent membership for general. It includes an internal `unresolved` option that
falls back to Azure; it does not create another graph branch. Provider confidence
scales are not compared. Failures, invalid responses and uncertainty fall back
once to Azure, within a shared 25-second classification deadline. Caller
cancellation propagates. No TypeSafe retry loop is added.

Existing source/confidence/token metrics distinguish TypeSafe. Structured logs
record model, rubric version, choice/confidence, duration and fallback category,
without payloads or secrets. TypeSafe source `llm` after fallback means Azure made
the final decision; inspect `classifier_outcome` for the TypeSafe failure reason.

## Deployment gate and rollback

Cancellation mutation check (2026-09-17): replacing the re-raise in
`backend/src/services/agent/typesafe_classifier.py:170` with `return None` made
the following test fail with `DID NOT RAISE CancelledError`. Restoring the guard
made the test pass; a cancelled request must not start Azure fallback.

```sh
PYTHONPATH=backend python -m pytest -q -o addopts='' backend/tests/unit/services/test_typesafe_classifier.py -k test_cancellation_does_not_invoke_azure
```

Before enabling a shared instance: independently review a new holdout, calibrate
and freeze the threshold, verify end-to-end tasks and approvals, compare p95 turn
latency and billed cost, and exercise missing-key/timeout rollback. A mocked test
or successful routing-only API call does not meet this gate.

Use existing Infisical secret-folder mappings and backend/worker `envFrom` in the
deployment chart; verify both runtimes receive the rotated key. No secret store,
Helm values, shared environment, or production traffic is changed in this pilot.
Restore `AGENT_INTENT_PROVIDER=azure` and roll/restart affected processes to revert
provider selection while keeping the routing/context fixes.

References checked 2026-09-17: [API](https://docs.typesafe.ai/api.md),
[Choice](https://docs.typesafe.ai/primitives/choice.md),
[confidence](https://docs.typesafe.ai/confidence.md),
[models](https://docs.typesafe.ai/models.md). The TypeSafe skill guided the bounded
Choice design, no-match handling, and separation of judgment from execution.
