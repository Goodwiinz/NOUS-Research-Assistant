# PR 1661 Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three security review findings on PR #1661 without changing unrelated research behavior.

**Architecture:** Keep an active research stream claimed until it acknowledges a pause and persist any completed step before releasing that claim. Configure the shared Redis limiter to fail closed only for paid work. Carry the ArXiv job's monotonic deadline to a cancellable async OpenAI request with an SDK timeout and retries disabled. Existing fail-open rate-limit callers and non-ArXiv model callers keep their current behavior.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async sessions, Redis, OpenAI Python SDK, pytest.

**Spec:** `docs/superpowers/plans/2026-09-17-high-security-findings-remediation.md` plus the three PR #1661 review comments supplied for this change.

## Global Constraints

- Keep the run's persisted step index and token total as the source of truth for resume.
- Keep the organization-wide paid-work limit at five starts per 60 seconds.
- Bound the provider request by the existing monotonic ArXiv background deadline.
- Preserve default fail-open behavior for existing general-purpose `RateLimiter` users.
- Add a failing regression test before each production change.

## Review Focus

- A pause arriving on `step_complete` persists the step and token count before `run_paused` is emitted.
- A pause arriving before completion still stops without persisting an unfinished step.
- A pause request does not make an in-flight run claimable by a second stream.
- Redis failures deny expensive work while general-purpose limiter callers retain their configured policy.
- GPT-5 and older deployment request branches both forward the provider timeout.
- An exhausted ArXiv deadline raises cancellation instead of falling through to template generation.

---

### Task 1: Persist a completed step before pausing

**Files:**
- Modify: `backend/tests/unit/api/test_research_engine_stream.py`
- Modify: `backend/src/api/research_engine/runs.py`

**Interfaces:**
- Consumes: `WorkflowEngine.run()` events and `ResearchRun.status` refreshed from the database.
- Produces: a committed `ResearchStep` and updated `run.total_tokens` before the pause event.

- [ ] **Step 1: Write the failing test**

Extend the external-pause test so the pause becomes visible while handling a `step_complete` event and assert that the added `ResearchStep` has `token_count == 10`, `run.total_tokens == 10`, and the emitted step event precedes `run_paused`.

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `pytest -q --confcutdir=backend/tests/unit/api backend/tests/unit/api/test_research_engine_stream.py::TestStreamEndpointSuccess::test_stream_persists_completed_step_before_honoring_external_pause`

Expected: FAIL because the current pause branch exits before `db.add(...)` and token accounting.

- [ ] **Step 3: Implement the minimal ordering change**

Record the pause request without publishing the run as streamable. Capture it after `db.refresh(run)`. Keep the immediate pause path for events other than `step_complete`; process and commit `step_complete`, acknowledge the paused state, emit it, then emit `run_paused` and stop before requesting another engine event.

- [ ] **Step 4: Run the focused test and surrounding stream tests**

Run the focused test above, followed by the directly invocable async stream tests in the same module.

### Task 2: Fail closed for paid-work Redis errors

**Files:**
- Create: `backend/tests/unit/services/test_expensive_work_admission.py`
- Modify: `backend/src/shared/utils.py`
- Modify: `backend/src/services/expensive_work_admission.py`

**Interfaces:**
- Consumes: `RateLimiter(redis_url, fail_open=...)`.
- Produces: `admit_expensive_work(...) -> False` when Redis cannot make the shared decision.

- [ ] **Step 1: Write the failing tests**

Use a fake Redis client whose `incr` raises. Assert a default `RateLimiter` still allows the request and a `RateLimiter(..., fail_open=False)` denies it. Assert the expensive-work singleton is configured with `fail_open is False`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest -q --confcutdir=backend/tests/unit/services backend/tests/unit/services/test_expensive_work_admission.py`

Expected: FAIL because `RateLimiter` has no failure-policy parameter and the expensive limiter is fail open.

- [ ] **Step 3: Implement the policy**

Add a keyword-only `fail_open: bool = True` constructor argument, return that value on Redis exceptions, and instantiate the paid-work limiter with `fail_open=False`.

- [ ] **Step 4: Run the focused tests**

Run the new service test file and existing rate-limiter call-site tests that do not require external services.

### Task 3: Apply the ArXiv deadline to the provider request

**Files:**
- Modify: `backend/tests/unit/services/test_stream_chat_completion.py`
- Create: `backend/tests/unit/services/arxiv/test_dataset_deadline.py`
- Modify: `backend/src/services/infrastructure/azure_openai_service.py`
- Modify: `backend/src/api/arxiv/core.py`
- Modify: `backend/src/services/arxiv/arxiv_service.py`

**Interfaces:**
- Consumes: an optional monotonic `deadline: float` on ArXiv dataset creation.
- Produces: OpenAI request option `timeout=<remaining seconds>` and `asyncio.TimeoutError` when no budget remains.

- [ ] **Step 1: Write failing provider and propagation tests**

Assert `chat_completion(..., timeout=7.5)` forwards `timeout=7.5` to both SDK request branches. In the ArXiv service test, patch `time.monotonic()` and assert question generation passes the hand-derived remaining duration; assert an expired deadline raises `asyncio.TimeoutError` instead of using fallback templates.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest -q --confcutdir=backend/tests/unit/services backend/tests/unit/services/test_stream_chat_completion.py backend/tests/unit/services/arxiv/test_dataset_deadline.py`

Expected: FAIL because the service signatures do not accept or forward a deadline/timeout.

- [ ] **Step 3: Implement deadline propagation**

Add optional `timeout` to `AzureOpenAIService.chat_completion`. Deadline-bearing calls use the async SDK client with both an SDK timeout and `asyncio.wait_for`, with retries disabled so cancellation stops the in-flight request. Pass the existing effective deadline through `_create_arxiv_dataset_work`, `create_evaluation_dataset`, and `_generate_questions_for_paper`; compute remaining time immediately before each provider request and re-raise `asyncio.TimeoutError` rather than converting it to template fallback.

- [ ] **Step 4: Run focused and static verification**

Run the two focused service test files, Ruff on changed source, Black/isort checks on changed Python files, compile checks, and inspect `git diff --check` plus the final diff.
