# TypeSafe intent-routing integration

Date: 2026-09-17
Status: local pilot implemented; not deployed or live-calibrated.

## Decision

Add TypeSafe Jev as an opt-in semantic classifier for the agent's intent
routing boundary. Azure remains the default provider. TypeSafe is used only
when explicitly enabled, with a short timeout and a conservative fallback to
Azure and then the existing deterministic classifier.

The classifier returns one of the existing agent intents plus `unresolved`.
It receives bounded request state, recent turn context, active paper metadata,
and recent tool outcomes without tool arguments or raw results.

## Implementation

1. Add TypeSafe settings, a pooled HTTP client, lifecycle hooks for the API and
   Celery worker paths, and server-side secret handling.
2. Add a typed Choice request with an explicit rubric, no-match outcome,
   finite-probability validation, confidence handling, and cancellation
   propagation.
3. Replace unconditional deterministic shortcuts with a narrow bare-conversation
   shortcut; contextual requests use the configured provider cascade.
4. Add an offline replay harness with 20 synthetic routing cases, unit tests,
   architecture coverage, and an engineering runbook.

## Validation and rollout

- 303 focused, architecture, integration, and golden-replay tests passed;
  the live external dataset remains deliberately excluded.
- CI-pinned Ruff, Black, isort, added-file MyPy, OpenAPI, documentation, and
  `git diff --check` gates passed.
- Offline replay validated all 20 cases with zero network calls.
- Keep `AGENT_INTENT_PROVIDER=azure` until a rotated TypeSafe key is supplied,
  representative live replay is reviewed, and confidence/latency/cost metrics
  are accepted.
- Roll back by setting the provider to `azure`; no data migration is required.

See `docs/engineering/typesafe-routing.md` for configuration, replay,
observability, cancellation, and deployment guidance.
