# Live Agent Q&A Failure Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the five failures from the 2026-09-25 Playwright run distinguishable and reproducible, repair the two chat UI defects, and verify the Q&A rubric without rewriting the historical result.

**Architecture:** Keep the chat store as the selected-thread owner, but give the chat page hook sole responsibility for choosing the default workspace-wide thread and synchronizing its route. Let the layout persistence hook finish workspace/conversation hydration before page default selection, while preserving explicit `?new=1`, `?thread=`, and user selections. Keep the concept matcher as a fast lexical check and review answer grounding separately. Treat a committed but text-empty answer as a distinct test outcome so it fails promptly.

**Tech Stack:** Next.js 16, React 18, Zustand, ReactMarkdown, Vitest, Playwright, Node 24, `pnpm@10.18.2`.

**Spec:** `frontend/e2e/agent-qa/README.md` and its 30-case `frontend/e2e/fixtures/agent-qa.v1.json` dataset.

**Implementation status (2026-09-25):** Tasks 1–3 and the local portion of Task 4 are implemented in the worktree. Astra reviewed the plan and final code; its two Important code findings were reproduced with failing regressions and fixed. The final frontend suite passed 2,297 tests across 310 files, and offline Chromium probes passed. The historical report is unchanged. The targeted and full live Q&A reruns remain a release gate for the exact deployed build; broader backend CI is pending dependencies missing from this checkout.

## Global Constraints

- Use Node 24, `pnpm@10.18.2`, and the root `pnpm-lock.yaml`.
- Import chat state only through `@/store/chat-store`; the backend remains the only persisted-chat writer.
- Preserve explicit new-chat and deep-link navigation, sidebar selections, and stale-request guards.
- Keep the 2026-09-25 v1.2.1 Playwright report immutable at 25 passed / 5 failed; do not relabel an unasked or unrendered answer as a model error.
- A green lexical grade does not establish that every claim is supported by the supplied note.

## Evidence and Current State

The retained run is `frontend/playwright-agent-qa-report/index.html` with structured output at `frontend/test-results/agent-qa/results.json`. It ran 30 cases with no retries: 25 passed and 5 failed. These are ignored test artifacts and may contain session material; share the rendered report, not raw trace internals.

| Case                        | Observed boundary                                     | Diagnosis                                                                                                                                                                                                                                         |
| --------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `incomplete-citation`       | Answer committed; concept grade failed                | The answer said “do not infer” and “Check the original note or source”; the v1.2.1 alternatives did not include them.                                                                                                                             |
| `next-research-step`        | Answer committed; concept grade failed                | “bigger and diverse participant group” conveyed the required larger sample but missed the exact alternatives.                                                                                                                                     |
| `draft-project-description` | Answer committed; concept grade failed                | “compares” missed the matcher. The same answer also added features, usability, effectiveness, and suitability beyond the brief, so it is not a clean grounded pass.                                                                               |
| `extract-paper-year`        | Answer committed; browser waited 240 seconds for text | The DOM contained `<ol start="2022"><li></li></ol>`. “2022.” was a list marker, not selectable answer text.                                                                                                                                       |
| `citation-author-year`      | Question never submitted                              | A prior thread was visible at bare `/chat`. The layout's conversation-thread request and message request preceded the page's workspace-thread request; the layout selected a thread, and the page preserved it without putting its ID in the URL. |

Local, uncommitted edits already add v1.2.2 phrase regressions, escape a bare numeric answer before Markdown parsing, and sync a thread selected before page initialization settles. Focused and full frontend unit tests passed (2,275 tests), as did type-checking, linting, and formatting. These checks do not prove the deployed site has changed. The one-shot URL sync still needs the later-selection ordering tested and repaired. More fundamentally, `useChatPersistence` chooses the first thread of one conversation while `useChatSession` loads the workspace-wide first page; both can write `currentThreadId`. The live trace proves the layout-first ordering. The reverse ordering and the possibility of different default thread IDs need an integration test.

## Review Focus

- Layout and page initialization resolve in either order, with different first threads: bare `/chat` selects and routes to the workspace-wide thread; Task 1 tests both ordering and ownership.
- A sidebar selection during bare-route conversation hydration must survive `setCurrentConversation` even before the URL catches up; Task 1 tests that reset boundary with the real store.
- `?new=1` while an old thread exists: blank composer stays authoritative; Task 1 tests no route replacement.
- An explicit `?thread=` deep link outside the first sidebar page: its URL is never replaced by an older selection; Task 1 tests it.
- A bare year versus a real numbered list: year remains selectable text while `1. Read the paper` remains a list; Task 2 tests both.
- An on-topic project description with invented evaluation dimensions: lexical core is recognized, but the observed unsupported additions remain flagged and require human review; Task 3 tests and documents this limit.

---

### Task 1: Give default selection one owner and reconcile bare-chat routes

**Files:** Modify `frontend/src/hooks/chat/useChatSession.ts` and `frontend/src/hooks/useChatPersistence.ts`; test `frontend/src/hooks/chat/__tests__/useChatSession.workspaceThreads.test.tsx`, `frontend/src/hooks/__tests__/useChatPersistence.initialization.test.tsx`, and create `frontend/src/hooks/chat/__tests__/chatInitializationRoute.test.tsx` with both hooks and the real store.

**Interfaces:** `useChatPersistence().initialize()` shares a single in-flight promise across consumers; the page awaits it before loading its workspace-wide thread page, requesting error propagation for that explicit call. The layout's fire-and-forget call still reports initialization failures through its own toast. Only `useChatSession` chooses the default `currentThreadId` on bare `/chat`. The page consumes `activeThreadId`, `threadFromUrl`, `hasNewChatIntent`, `isInitializing`, and `initError` for route reconciliation. No new persisted state is introduced.

- [ ] Add a red test: finish page initialization with no selected thread, then select `thread-layout` and rerender the hook. Expect exactly one replacement to `/chat?thread=thread-layout`. The current one-shot sync must fail this test.
- [ ] Mount both hooks against the real chat store in the new integration test. Give the layout conversation `thread-old` and the workspace page `thread-new`. In the trace ordering, resolve layout threads first and verify bare `/chat` selects and routes to `thread-new`. In the reverse schedule, hold the layout request and assert that the page does not request workspace threads until layout hydration settles. Use deferred promises; do not use sleeps.
- [ ] Add tests for selection before and after page initialization, `?new=1`, an explicit `?thread=thread-deep`, an empty workspace, and one replacement for ordinary first-thread auto-selection. Keep the existing sidebar-selection and stale-workspace tests green.
- [ ] Remove the layout initializer's first-thread fallback at `frontend/src/hooks/useChatPersistence.ts` (the branch currently calling `setCurrentThread(firstThread.id)` after `setCurrentConversation`). Its `createNewChat` and explicit selection actions still select threads. Make page initialization await the hook's shared `initialize()` promise before requesting the workspace-wide page:

  ```tsx
  const { initialize: initializeChatPersistence } = useChatPersistence();
  // Inside useChatSession's initializeFromDb, before loading page-one threads:
  await initializeChatPersistence({ throwOnError: true });
  if (!ownsInitialization()) return;
  const layoutState = useChatStore.getState();
  if (!layoutState.currentWorkspaceId || !layoutState.currentConversationId) {
    throw new Error("Chat initialization did not produce a conversation");
  }
  const ws = await workspaceService.getOrCreateDefaultWorkspace();
  ```

  Verify that the page surfaces a failed layout initialization rather than treating the swallowed hook error as success. Do not treat the store's global `error` field as an initialization outcome: an unrelated failed thread operation may leave it set on a warm remount.

- [ ] Preserve a newer sidebar selection across the layout's `setCurrentConversation` reset when `/chat` is still bare. Capture the pre-reset selected thread even without an initial `?thread=` query, and restore it only when it changed during hydration, the live route has not been superseded, and `?new=1` is absent.

- [ ] Move bare-route reconciliation into a post-initialization effect keyed by the selected thread, and remove the local one-shot reconciliation and its `selectionUrlUpdated` return field. Use the live browser URL as a second intent guard so a pending navigation to `?new=1` or `?thread=` is not overwritten:

  ```tsx
  useEffect(() => {
    if (
      !isAuthenticated ||
      isInitializing ||
      initError ||
      hasNewChatIntent ||
      threadFromUrl ||
      !activeThreadId
    )
      return;
    const liveParams = new URLSearchParams(window.location.search);
    if (liveParams.get("new") === "1" || liveParams.has("thread")) return;
    routerRef.current.replace(getSelectedThreadUrl(activeThreadId));
  }, [
    activeThreadId,
    hasNewChatIntent,
    initError,
    isAuthenticated,
    isInitializing,
    threadFromUrl,
  ]);
  ```

- [ ] Remove the earlier bare-route `replace` calls that would duplicate this effect, while retaining the explicit unavailable-deep-link fallback. Keep the tests' one-navigation assertion. Verify the context rail reads the same selected thread as the chat page after hydration.
- [ ] Adapt isolated `useChatSession` test scaffolding to stub only the persistence-initialization boundary; keep the real hooks/store in the integration test. Run focused chat hook and auth-recovery tests. Verify that the route sync, hydration barrier, and pre-reset selection preservation each fail their relevant regression when neutralized, then restore them and rerun green, as required by `docs/engineering/testing.md`.

### Task 2: Render numeric answers as text and fail promptly on empty committed output

**Files:** Review the existing local edit in `frontend/src/components/chat/ChatMarkdown.tsx`; test `frontend/src/components/chat/__tests__/CitationRenderer.render.test.tsx`; modify `frontend/e2e/agent-qa/live-agent-qa.spec.ts` and add a focused offline browser test of committed-answer observation.

**Interfaces:** `ChatMarkdown({content})` must preserve visible text for `/^\s*\d+\.\s*$/` without changing normal ordered lists. The Playwright case consumes committed `[data-role="assistant"] [data-quotable]` content and attaches its raw text before grading.

- [ ] Keep the bare-year regression (`2022.` has text content and no `<ol>`), and the real-list regression (`1. Read the paper` remains an `<ol><li>`). Add a whitespace variant if the renderer's exact match changes.
- [ ] Replace the Playwright `toHaveText(/\S/, {timeout: 240000})` wait with a wait for a committed quotable element, then read its text. A committed empty answer should be attached and fail immediately:

  ```ts
  await expect(committedAnswer).toHaveCount(1, { timeout: turnTimeout });
  const actualAnswer = (await committedAnswer.textContent())?.trim() ?? "";
  await test.info().attach("agent-qa-answer", {
    body: JSON.stringify({ id: testCase.id, actualAnswer }),
    contentType: "application/json",
  });
  expect(actualAnswer, "committed answer has no selectable text").toMatch(/\S/);
  ```

- [ ] Keep the existing stop-agent, approval-dialog, and error checks. Confirm the empty-ordered-list fixture produces an immediate `rendered-empty` failure and a normal answer reaches grading.
- [ ] Give the offline test a controlled DOM with a committed empty ordered list, a normal committed answer, and a submitted-but-never-committed turn. Check each observed phase without authentication or model calls.
- [ ] Run the renderer and agent-Q&A focused tests, then Playwright's `--list` mode to verify all 30 case IDs remain present without making model calls.

### Task 3: Preserve grading integrity and report the right failure classes

**Files:** Review the current edits in `frontend/e2e/fixtures/agent-qa.v1.json`, `frontend/src/test/agent-qa/__tests__/agentQaParaphrases.test.ts`, and `frontend/e2e/agent-qa/README.md`; modify `frontend/e2e/agent-qa/live-agent-qa.spec.ts` only for diagnostic attachments.

**Interfaces:** `gradeAgentAnswer(answer, criteria)` remains a lexical concept check. The live test records `not-sent`, `sent`, `rendered-empty`, or `graded` across the complete browser flow; a `graded` case separately records pass/fail. Do not treat a browser-stage failure as a wrong model answer.

- [ ] Run the captured-answer regression tests: both grounded paraphrases pass; a brief-faithful project description using “compares” passes; the captured expanded description remains flagged. Inspect the complete answers, not only `grade.passed`.
- [ ] Add a per-case `phase` attachment in `finally` around the complete browser flow so failures before submission retain `phase: 'not-sent'`. Leave the phase at `sent` while awaiting or validating committed output; set `rendered-empty` for committed empty text; set `graded` only after grading returns. Include pass/fail separately. Use the existing page URL and case ID; do not attach cookies, authorization headers, or storage state:

  ```ts
  let phase: "not-sent" | "sent" | "rendered-empty" | "graded" = "not-sent";
  try {
    // begins before page.goto('/chat')
    await page.getByRole("button", { name: /^Send/ }).click();
    phase = "sent";
    await expect(committedAnswer).toHaveCount(1, { timeout: turnTimeout });
    const actualAnswer = (await committedAnswer.textContent())?.trim() ?? "";
    if (!actualAnswer) phase = "rendered-empty";
    expect(actualAnswer, "committed answer has no selectable text").toMatch(
      /\S/,
    );
    // Run stop/approval/error checks, then grade and set phase = "graded".
  } finally {
    await test.info().attach("agent-qa-phase", {
      body: JSON.stringify({ id: testCase.id, phase, threadUrl: page.url() }),
      contentType: "application/json",
    });
  }
  ```

- [ ] Document that v1.2.2 is a rubric revision over unchanged prompts, and that project-scope checks only catch observed wording. Report human grounding review alongside the automated score; do not silently recalculate the v1.2.1 historical 25/30.
- [ ] Check a brief-faithful answer that negates an extra topic (for example, “the brief does not specify features or usability”). A substring blacklist can flag that valid caveat; document this false-positive limit or distinguish review flags from hard failures.
- [ ] Run the dataset and paraphrase unit tests, and inspect all case IDs and criteria in the Playwright `--list` output.

### Task 4: Verify the exact build and rerun the live cases

**Files:** Configure explicit per-run Playwright JSON, HTML, and test-output paths. Retain the original `frontend/playwright-agent-qa-report/index.html` and `frontend/test-results/agent-qa/results.json` as immutable v1.2.1 evidence. Require a unique `AGENT_QA_RUN_LABEL`; validate directory collisions in the Playwright coordinator before output cleanup, while allowing its workers to reload the configuration after creating their artifact folders.

**Interfaces:** The production check must exercise the build containing Tasks 1–3. A local unit pass against this worktree is not evidence that `goodwiinz.tech` has that build.

- [ ] Run `pnpm install --frozen-lockfile`, `pnpm --dir frontend type-check`, `pnpm --dir frontend test`, and `scripts/ci/run_local_ci.sh --base origin/develop --frontend` with the repo-pinned toolchain. `origin/develop` was verified locally when this plan was written.
- [ ] Record the target URL and deployed commit/build provenance. On a browser-accessible build containing these exact changes, run Playwright for `incomplete-citation|next-research-step|draft-project-description|extract-paper-year|citation-author-year` using distinct targeted JSON, HTML, and test-output paths. Expect the two grounded paraphrases to pass, the year to produce selectable text, and citation-author-year to submit. Inspect the project description for unsupported additions even when its required concepts match.
- [ ] Run all 30 live cases once on that same build, retaining traces on failure and no retries in distinct full-run JSON, HTML, and test-output paths. Report totals by `not-sent`, `sent` (uncommitted or validation failure), `rendered-empty`, `graded-pass`, and `graded-fail`, plus any grounding findings from human review.
- [ ] If any target case still fails, follow its attached phase and trace to the failing boundary before changing another layer. Do not alter the rubric merely to raise the aggregate score.

## Self-Review

The plan covers each of the five observed failures and the later-selection ordering that the current local fix misses. The route tests protect explicit navigation, the renderer tests protect real lists, and the grading tests separate topicality from support. No backend API or persisted-chat changes are planned. The production rerun remains a release gate because this worktree's passing tests cannot establish live behavior.
