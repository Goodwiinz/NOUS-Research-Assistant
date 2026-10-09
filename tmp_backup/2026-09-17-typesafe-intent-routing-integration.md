# TypeSafe intent-routing integration

Date: 2026-09-17. Status: local pilot implemented; not deployed or live-calibrated.

Implementation is isolated in `/Users/goodwiinz/development/rag-typesafe-routing`
on `codex/typesafe-intent-routing`; the primary checkout's application code is
unchanged. Routing/context fixes, the optional adapter, API/worker client lifecycle,
and an explicit offline/live replay command are implemented. Azure remains the
default, and an unset TypeSafe confidence threshold disables acceptance.

Validation: 303 focused/architecture/golden-replay tests passed (one live-dataset
test deliberately excluded); CI-pinned Ruff, Black, isort and added-file MyPy,
OpenAPI drift, and directory documentation checks passed. Cancellation was also
mutation-tested. The 20 bundled synthetic regression cases validate offline;
they are not the proposed independently reviewed holdout.

Still pending: rotated-key live checks, independent calibration/holdout review,
end-to-end development acceptance, billed-cost comparison, publication as PRs,
and separately authorized deployment. No API secrets or deployment settings were
changed. See `docs/engineering/typesafe-routing.md` in the implementation worktree
for commands and rollback instructions. The PR sections below remain the delivery
outline, not a claim that three PRs have been opened.

## Decision and scope

Integrate TypeSafe as an optional semantic intent classifier for NOUS. Keep Azure
for reasoning, tool execution planning, answer generation, and routing fallback.
Keep the existing four agent branches. Start with one Choice question; do not add
reranking, citation scoring, a new planner, or a provider framework.

The TypeSafe skill informed this design: bounded state, capability-aware choices,
an explicit no-match outcome, and confidence calibrated against actual behavior.
The documented [Choice primitive](https://docs.typesafe.ai/primitives/choice.md)
fits this single routing decision.

Local investigation found routing-policy and context-plumbing problems before
the provider call. A provider-only replacement would leave those defects intact.
The previous synthetic experiment is useful for regression cases, not evidence
of production accuracy, task completion, or cost savings.

## Intended flow

```text
Chat request + bounded conversation/page/tool context
  -> shared context normalization
  -> existing eligible greeting fast path / empty-input handling
  -> semantic intent classification
       provider=azure: corrected existing Azure classifier
       provider=typesafe: one Choice request
         accepted route ----------------------------+
         uncertain / no-match / unavailable -> Azure |
  -> existing routing node <-------------------------+
  -> research | writing | knowledge_graph | general
  -> existing tool bindings, access checks, approvals, and generation
```

Classification remains concurrent with retrieval and memory in preprocessing.
There is no TypeSafe call for a request already handled by the greeting fast path.
The existing fast-path enablement policy does not change.

## PR 1 — Repair routing eligibility and context, using Azure

This PR must be useful without a TypeSafe account.

1. Remove broad action-phrase and keyword-confidence early returns for free-form
   natural-language requests. They currently ignore negation, quotations, and the
   distinction between a source and a requested action. Keep empty-input handling
   and genuinely context-free greetings. Retain keywords as last-resort fallback
   evidence, not as proof that a model call is unnecessary. Do not build a regex
   negation parser or keep adding phrase exceptions.
2. Remove the fewer-than-eight-words shortcut. Short follow-ups such as “make it
   shorter” and “try again” need their conversation and tool context.
3. Normalize page metadata once for both providers. Trace the real
   `PageContextRequest` producer and `_page_context_to_dict()` consumers; test the
   request-adapter path, not only direct classifier calls. Preserve the existing
   public shape where possible. Do not claim active-paper support until a real
   producer supplies the required context. Client-provided IDs remain hints,
   never evidence of document access.
4. Preserve relevant outcomes from the most recent tool-call batch, matching each
   result by call ID. Do not assume `tool_calls[0]` describes a retry. Bound this
   context and preserve existing cancellation, approval, and retry restrictions;
   recognizing a retry does not authorize executing it.
5. Correct the Azure rubric against actual tool bindings. Specialized branches
   use subgraph membership; general uses intent membership. Reuse `TOOL_REGISTRY`
   and its existing metadata for a small capability-contract test. Do not make
   general a universal tool superset.

Main edit points:

- `backend/src/services/agent/classifier.py`
- `backend/src/services/agent/_prompts.py`
- `backend/src/services/agent/_nodes_classify.py`
- `backend/src/services/agent/agent_execution_service.py`

Required regressions: bibliography export mentioning the knowledge base; entity
extraction with paraphrased wording; negated/quoted actions; contextual short
turns; active-document metadata through the real adapter; retry after a mixed
success/failure tool batch; and external-database listing.

Exit: deterministic policy/context tests pass, a corrected Azure baseline is
recorded, and specialist-tool availability is checked separately from preferred
intent labels. Ordinary natural-language requests will make more semantic calls;
measure that cost and latency explicitly.

## PR 2 — Add the optional TypeSafe adapter

### Request and result

Add a small `backend/src/services/agent/typesafe_classifier.py` using the already
installed `httpx`. Reuse existing Pydantic validation and `ClassificationResult`;
add `typesafe` to its source values. No additional SDK or dependency is needed.

Use the documented [HTTP endpoint](https://docs.typesafe.ai/api.md),
`POST https://api.typesafe.ai/v1/systemone`, with server-side Bearer authentication.
Send one named Choice question, `intent`, over:

| Choice | Routing meaning |
| --- | --- |
| `research` | Evidence retrieval, paper ingestion, project research, and Python execution supported by the research branch. |
| `writing` | Saved drafts, bibliography export, and supported document-writing workflows. |
| `knowledge_graph` | Entity extraction, graph exploration, and entity paths. |
| `general` | Conversation and tasks supported by the general branch, including external-database discovery. Not a specialist-tool superset. |
| `unresolved` | Insufficient context or no single available branch can support the requested workflow. |

These summaries guide the rubric; the current registry is authoritative. Supply
both providers with the same capability descriptions and normalized context.
`unresolved` is internal abstention, not a fifth graph branch: hand it to Azure
with the no-match reason. This changes the old playground's four-choice rubric,
so its old thresholds/results cannot be reused as acceptance evidence.

State contains only the current request, bounded preceding assistant context,
relevant page/document hints, and bounded recent tool outcomes. Keep policy in
instructions and untrusted content in state. Exclude full documents, unrelated
history, credentials, and unnecessary identifiers. Use existing access checks
before adding server-resolved document data.

Validate answer type, allowed choice, complete option keys, finite probabilities
and confidence in range, and distribution consistency with a documented rounding
tolerance. Reject malformed responses. Map accepted answers into the existing
result type with a short code-authored reason; Jev need not generate an explanation.

### Configuration and failure behavior

Add to `backend/src/core/config.py`:

| Setting | Initial behavior |
| --- | --- |
| `AGENT_INTENT_PROVIDER` | `azure` by default; `typesafe` is opt-in. |
| `TYPESAFE_API_KEY` | Optional server-only secret; never a frontend variable. |
| `TYPESAFE_MODEL` | `jev-latest` for development; verify an available pinned model before production, and record the returned model. |
| `TYPESAFE_TIMEOUT_SECONDS` | Proposed initial total request budget: 1 second, verified under deployment load. |
| `TYPESAFE_MIN_CONFIDENCE` | Unset until calibration; unset means no TypeSafe decisions are accepted into live routing. |

Own one pooled asynchronous client per application process using the existing
lifespan pattern in `backend/src/main.py`; close it on shutdown and allow test
transport injection.

- On missing credentials, timeout, transport/status error, invalid response,
  `unresolved`, or below-threshold confidence: invoke the Azure semantic path once,
  without re-entering the routing wrapper. Do not send it back through the broken
  shortcuts. Preserve the existing final fallback if Azure also fails.
- Give TypeSafe and Azure one shared classifier deadline, currently 25 seconds;
  Azure receives the remaining budget. No TypeSafe retries in the interactive
  path and no additional Azure retry loop. Propagate caller cancellation without
  invoking fallback. Never use fallback to bypass a downstream safety refusal.
- Keep TypeSafe acceptance separate from existing Azure/keyword confidence
  comparisons. Its [confidence describes distribution concentration](https://docs.typesafe.ai/confidence.md),
  not a probability of completing the task correctly.
- Use the current Infisical-to-Kubernetes pattern for deployment credentials;
  verify the secret-folder mapping and backend `envFrom` together. Rotate the key
  previously pasted into chat before deployment; do not copy it into the repo.

Reuse existing observability for provider/source, selected intent, duration,
fallback reason, resolved model, rubric version, and usage. Keep provider-specific
confidence distinguishable. Do not log prompts, tool content, API keys, or raw
error bodies; retain only redacted diagnostic fields. Avoid request IDs as metric
labels and do not build a new dashboard for this pilot.

Exit: default Azure behavior passes regression tests; mocked TypeSafe success,
malformed output, auth failure, throttling/overload, timeout, low confidence,
abstention, fallback deadline, cancellation, and client shutdown are covered.
TypeSafe still does not receive production traffic.

## PR 3 — Replay evaluation and controlled rollout

1. Move only reviewed, sanitized synthetic cases and the minimum replay harness
   from the temporary experiment into the repo's existing evaluation/test layout.
   Do not commit credentials, raw private traces, or local benchmark outputs.
   Keep the initial failed responses counted as failures; reruns are separate.
2. Freeze the rubric, model, and thresholds after a calibration set. Then compare
   corrected Azure, TypeSafe alone, and the actual TypeSafe-plus-Azure cascade on
   a fresh independently reviewed holdout. Allow multiple valid routes when their
   capabilities satisfy the task. Report unsupported cross-branch tasks separately.
3. Start with approximately 200 reviewed cases spanning normal requests,
   paraphrases, context-dependent follow-ups, adversarial metadata, and provider
   failures. This is a pilot target, not a statistical accuracy guarantee. Use
   synthetic or explicitly approved, minimized data; replay is offline initially,
   not a new background shadow service on production requests.
4. Run representative end-to-end tasks in development. Verify actual retrieval,
   saved drafts, bibliography exports, and graph results under existing approvals.
   Routing to a branch with the right tools is necessary, not proof of completion.
5. Enable TypeSafe only on a dedicated development instance, then a limited
   deployment cohort using existing rollout controls after separate deployment
   authorization. Expand only after the following gates pass.

Proposed promotion gates, to freeze before the holdout:

- All required routing/context regressions pass; no access, approval, retry, or
  cancellation regression.
- No reduction in successful feasible tasks versus corrected Azure on the
  reviewed holdout and development acceptance tasks; inspect each disagreement.
- At least 20% lower p95 semantic-classification latency for the actual cascade,
  including fallback; no p95 end-to-end turn-latency regression.
- Provider/validation error rate below 1% during the development cohort. Track
  low-confidence abstentions separately; assess their fallback cost and latency.
- Actual account pricing and usage establish acceptable cost per completed task,
  counting increased semantic-call volume and both providers on fallback.

Rollback: restore `AGENT_INTENT_PROVIDER=azure` through the existing configuration
rollout and verify running instances. This does not require reverting PR 1 or
removing the adapter. An individual TypeSafe failure already falls back to Azure.
Do not describe a configuration change as instantaneous unless hot reload exists.

## Verification and delivery

Implement in an isolated `codex/` worktree to preserve the dirty primary checkout.
Extend existing classifier tests in
`backend/tests/services/agent/test_classifier_intent.py` and
`backend/tests/unit/services/test_agent_classifier.py`; add one focused adapter
test module and reuse existing request/tool-binding tests where possible.

For each implementation PR, run the smallest relevant pytest subset, CI-pinned
changed-file Ruff/Black/isort, added-file MyPy, and `git diff --check`. Run the
repository's `scripts/ci/run_local_ci.sh` before pushing. If a public request shape
changes, regenerate/check OpenAPI and affected frontend types. Validate Helm
rendering if secret wiring changes. Report local checks, hosted exact-head CI,
deployment, and live acceptance separately.

Deferred: multi-branch orchestration, reranking, citation checks, new tool grants,
UI changes, a generic provider abstraction, and production shadow infrastructure.
Add them only for a demonstrated need. In particular, a request requiring both
Python and bibliography export is not solved by assigning it a confident intent;
the current single-branch execution limitation remains outside this pilot.
