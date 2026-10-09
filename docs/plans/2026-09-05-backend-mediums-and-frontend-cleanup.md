# Backend mediums (#1610) and frontend cleanup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the five medium findings from `~/.audit-ledgers/rag/agent-chat-backend-2026-09-05.md` (issue #1610) and three frontend leftovers (Inter never applied, dead chat components, artifact-sheet Tab trap) as small independent PRs off `origin/develop`.

**Architecture:** One PR per task, own branch and worktree under `.worktrees/`. Backend fixes are local to the cited functions; one new table only where a durable receipt is genuinely required (Task 5). Frontend tasks are deletions and one-liners. Decisions are made here; executors do not re-decide.

**Tech Stack:** FastAPI + SQLAlchemy async + Alembic + LangGraph (backend, `backend/.venv` Python 3.12, pytest); Next.js 15 + Tailwind v4 + vitest (frontend, pnpm).

---

## Ground rules for every task

- Repo `/Users/goodwiinz/development/RAG_system`. Never `git checkout`/`pull`/`reset`/`stash` in the main checkout. Per task: `git fetch origin develop && git worktree add /Users/goodwiinz/development/RAG_system/.worktrees/<branch> -b <branch> origin/develop`. Work only in the worktree.
- Stage path-explicitly. Commit trailer (exact two lines):
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01DgdZGWxkRah4zDk1Ku4FCZ`
- PR body: what changed, ledger IDs, `Refs #1610` (backend) or the finding source (frontend), gates run with verbatim results, what is not verified; end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`, blank line, the session URL.
- **Backend gates** (run from the worktree root, using the MAIN checkout's venv `/Users/goodwiinz/development/RAG_system/backend/.venv/bin/`): the CI "Lint Backend" gate runs `ruff check`, `black --check`, `isort --check-only` on CHANGED files and `mypy --ignore-missing-imports --follow-imports=silent <file>` on ADDED files (see `.github/workflows/test-pipeline.yml:~100-120`; reproduce exactly). Run `backend/.venv/bin/pytest <touched test files> -q` from `backend/`. Do not run the full suite.
- **Frontend gates** in `<worktree>/frontend`: `pnpm exec tsc --noEmit -p tsconfig.json`; `pnpm exec eslint <touched>` (touched files must have ZERO errors, the CI changed-file gate blocks otherwise; pre-existing warnings fine); `pnpm exec prettier --write <touched>`; `pnpm exec vitest run <touched tests>`. Never `npm run validate`. If `node_modules` is missing in the worktree, symlink `/Users/goodwiinz/development/RAG_system/frontend/node_modules` and remove the symlink before committing.
- Tests: write the failing test first for every behaviour change; one small test per task, mirroring the nearest existing test's fixtures/mocks.
- Ledger `~/.audit-ledgers/rag/agent-chat-backend-2026-09-05.md`: before starting a backend task set its rows `claimed` / owner `sess:fix-b8-mediums`; on PR open set `pr` + `#NNNN`. After each backend PR, one-line comment on issue #1610 listing IDs + PR, ending with `Reviewed by Claude Fable 5.1 (claude-fable-5-1) · session https://claude.ai/code/session_01DgdZGWxkRah4zDk1Ku4FCZ`.
- Copy: sentence case, no em dashes.

---

## Backend

### Task 1: Project access is membership, not ownership (branch `fix/agent-project-access-membership`)

Closes B8-T1.

**Files:**
- Modify: `backend/src/services/agent/_nodes_rag.py` (`_user_owns_project` ~147-205; the hybrid-fallback scope query ~595-607)
- Read: `backend/src/services/threads/workspace_access.py:57-73` (`user_can_access_workspace`: owner OR `WorkspaceMember`)
- Test: nearest existing test for `_user_owns_project` / `_nodes_rag` (grep `backend/tests` for `_user_owns_project`, `_nodes_rag`); add a case there or create `backend/tests/unit/agent/test_nodes_rag_project_access.py`.

**Steps:**
1. Failing test: a user who is a `WorkspaceMember` (not owner) of the workspace owning the collection → `_user_owns_project` returns `True`; a non-member returns `False`; DB error still returns `None`. Run → FAIL.
2. Replace the `Workspace.owner_id == user_uuid` predicate with an OR over owner and membership. Do it in SQL, not by loading the workspace: `or_(Workspace.owner_id == user_uuid, exists().where(WorkspaceMember.workspace_id == Workspace.id, WorkspaceMember.user_id == user_uuid))` (check the `WorkspaceMember` model for the exact column names and any `is_active`/`removed_at` flag; mirror what `user_can_access_workspace` checks). Rename nothing; add a docstring line saying it now matches `user_can_access_workspace` semantics.
3. Apply the same predicate to the hybrid-fallback scope query at ~595-607 so a member gets the project scope instead of `[]`.
4. Test → PASS. Gates. Commit `fix(agent): grant project RAG scope to workspace members, not only owners (B8-T1)`. PR.

### Task 2: extract_entities error honesty (branch `fix/agent-extract-entities-error`)

Closes B8-S2, B8-S3.

**Files:**
- Modify: `backend/src/services/agent/tools_impl.py` (`_tool_extract_entities` ~2700-2750)
- Modify: `backend/src/services/agent/error_recovery.py` (~180-190 `classify_error_from_payload`)
- Test: nearest `tests/unit/agent/test_tools_impl*.py` and `test_error_recovery*.py` (grep).

**Steps:**
1. Failing tests: (a) `_tool_extract_entities` with a result of zero entities and `error=None` returns a payload with NO `error` key and `total_entities == 0`; (b) with `result.error == "truncated"` on a non-empty result, the payload carries `"error": "truncated"` AND the entities; (c) `classify_error_from_payload({"error": None}, "extract_entities")` does not raise. Run → FAIL.
2. Zero-entity branch: only include `"error": result.error` when truthy. Success branch: include `"partial_error": result.error` when truthy (not `error`, so the classifier does not count a partial as a failure; document why in one comment).
3. `classify_error_from_payload`: `error_msg = payload.get("error") or ""` and `str(error_msg)` before `.lower()`.
4. Tests → PASS. Gates. Commit `fix(agent): honest extract_entities payloads, nil-safe error classifier (B8-S2 B8-S3)`. PR.

### Task 3: forget_memory must not report a failed search as completed (branch `fix/agent-forget-memory-honesty`)

Closes B8-S1.

**Files:**
- Modify: `backend/src/services/agent/memory.py` (`delete_memory_by_query` ~280-292 swallowed `asearch` exception)
- Modify: `backend/src/services/agent/tools_impl.py` (`_tool_forget_memory` ~3430-3442)
- Test: existing memory tests (grep `delete_memory_by_query` in `backend/tests`).

**Steps:**
1. Failing test: when `asearch` raises, `delete_memory_by_query` raises (or returns a result object with `error` set, whichever the current return type supports; prefer raising a `MemoryStoreError` if such an exception class exists, else re-raise the original) and `_tool_forget_memory` returns `tool_error_payload("forget_memory", exc)` rather than `{"status": "completed", "deleted": 0}`. Run → FAIL.
2. Remove the swallow at memory.py ~286-288 (keep the log line, then `raise`). In `_tool_forget_memory` wrap the call in the same `try/except Exception` → `tool_error_payload` pattern the other tools use (see `_tool_find_entity_paths` for the shape).
3. Test → PASS. Gates. Commit `fix(agent): forget_memory reports search failures instead of completed (B8-S1)`. PR.

### Task 4: REST message create idempotency (branch `fix/chat-message-create-idempotency`)

Closes B8-I2.

**Files:**
- Modify: `backend/src/schemas/chat.py` (`ChatMessageCreate` ~310-317): add `client_message_id: Optional[str] = Field(default=None, max_length=128)` (check what type/length `models/chat_message.py` uses for the column and match it).
- Modify: `backend/src/services/chat_service.py` (`create_message` ~687-745): pass `client_message_id` through to the model; before insert, if `client_message_id` is set and `role == "user"`, look up an existing row by `(thread_id, client_message_id)` and return it WITHOUT bumping counters. Read how `jobs._persist_assistant_message` does its idempotent insert (`ON CONFLICT` on the partial index `uq_chat_messages_thread_client_msg_user`, see `models/chat_message.py:53-58`) and reuse that exact pattern for the user role rather than a select-then-insert race.
- Modify: the REST route that calls `create_message` (grep `create_message(` in `backend/src/api`) only if it needs to forward the field.
- Test: nearest `test_chat_service*.py`.

**Steps:**
1. Failing test: two `create_message` calls with the same `thread_id` + `client_message_id` create ONE row and the thread's message counter increments once. Run → FAIL.
2. Implement per above. Do not touch the assistant path.
3. Test → PASS. Gates (ruff/black/isort on changed files). Commit `fix(chat): honour client_message_id on REST message create (B8-I2)`. PR. Note in PR body that the frontend does not yet send the field; that is a follow-up.

### Task 5: Durable tool receipts against checkpoint replay (branch `fix/agent-tool-receipts`)

Closes B8-I1.

**Files:**
- Create: `backend/src/models/agent_tool_receipt.py` — table `agent_tool_receipts`: `tool_call_id` (String(128), PK), `thread_id` (String/UUID matching how thread ids are stored on `agent_runs`, indexed), `tool_name` (String(64)), `created_at` (timestamptz default now). Register it wherever models are imported for Alembic autogenerate (grep `from src.models` in `backend/alembic/env.py` or `src/models/__init__.py`).
- Create: Alembic migration `backend/alembic/versions/<newid>_add_agent_tool_receipts.py` with a `to_regclass` guard like the recent migrations (copy the pattern from `z8a9b0c1d2e3_add_chat_messages_plan_token_usage.py`). Verify offline: `backend/.venv/bin/alembic upgrade head --sql` from `backend/` renders without error (never run it against a DB; see `alembic heads` must remain a single head).
- Modify: `backend/src/services/agent/_nodes_tools.py` (`tool_node` ~560-640 around `fresh_calls` / `_execute_single_tool`).
- Test: `backend/tests/unit/agent/test_nodes_tools*.py` (grep) — add a case with a fake session/repo.

**Design (do not widen):** a fixed set `SIDE_EFFECT_TOOLS = {"create_project", "create_project_note", "create_draft", "ingest_arxiv_papers", "execute_code"}` (confirm the exact registered tool names in `tools_impl.py`/the registry). Before dispatching a fresh call whose name is in the set, `SELECT 1 FROM agent_tool_receipts WHERE tool_call_id = :id`; if present, skip execution and emit a `ToolMessage` with `{"status": "skipped", "reason": "already_executed", "tool_call_id": id}` (classifier must not count it as an error: check `classify_error_from_payload` keys on `error` presence, so no `error` key). After a successful execution of a set member, `INSERT ... ON CONFLICT DO NOTHING` the receipt in its own short session (use the same session factory `_execute_single_tool` or its callers already use; grep `SessionLocal`/`get_async_session` in the file). Failures to write the receipt log a warning and do not fail the tool. No receipt for non-side-effect tools.

**Steps:**
1. Failing test: a `tool_call_id` with an existing receipt is not executed and yields the skipped payload; a fresh id executes and writes a receipt. Run → FAIL.
2. Implement model + migration + node change.
3. Test → PASS. `alembic upgrade head --sql` renders; `alembic heads` prints one head. Gates (mypy on the two ADDED files with `disallow_untyped_defs` expectations: annotate everything). Commit `feat(agent): durable per-tool-call receipts to prevent side-effect replay (B8-I1)`. PR. PR body states the replay window was never reproduced live (ledger note) and that the migration adds one small table.

---

## Frontend

### Task 6: Inter actually applied (branch `fix/nous-font-ui-token`)

**Files:**
- Modify: `frontend/app/nous-tokens.css` (`--nous-font-ui` and `--nous-font-heading` lines ~85-88)
- Read: `frontend/app/layout.tsx` (how `Inter` is loaded via `next/font` and which CSS variable it exposes, likely `--font-inter`; mirror exactly what PR #1601 did for `--nous-font-body` → `var(--font-serif)`).

**Steps:**
1. Change both `--nous-font-heading` and `--nous-font-ui` from the literal `'Inter', ...` to `var(--font-inter), -apple-system, BlinkMacSystemFont, sans-serif` (use the real variable name from layout.tsx). Keep the fallback stack.
2. `grep -rn "'Inter'" frontend/src frontend/app --include=*.tsx --include=*.css` for other literal uses in the app shell; change only token definitions, not per-component strings, unless a component literally sets `fontFamily: 'Inter'` (then point it at `var(--nous-font-ui)`).
3. Gates (tsc, prettier). No test needed. Commit `fix(theme): resolve --nous-font-ui and --nous-font-heading to the loaded Inter face`. PR body: app-wide repaint of UI type from system font to Inter; not browser-verified.

### Task 7: Delete dead chat components (branch `chore/chat-delete-dead-components`)

**Files (delete):** `frontend/src/components/chat/{RAGToggle,CitationPanel,CitationPreview,ChatSettings,ChatAnalytics,ModelLoadingProgress}.tsx`, and `SearchComposer` + `ui/agent-plan` if they resolve to files that are likewise unrendered (verify each with `grep -rn "<Name" frontend/src frontend/app` returning only the barrel/export and their own tests).
- Modify: `frontend/src/components/chat/index.ts` (remove the exports), any `__tests__` that test only a deleted component (delete those tests too), any story/mock referencing them.

**Steps:**
1. For each candidate run the JSX-usage grep; delete only those with zero renders outside their own file/test. Record the grep result per file in the PR body.
2. Delete files + exports + their tests. `pnpm exec tsc --noEmit` must pass (unresolved imports will show up here). `pnpm exec vitest run src/components/chat` must pass.
3. Commit `chore(chat): delete unrendered components exported from the chat barrel`. PR.

### Task 8: Trap Tab inside the artifact sheet (branch `fix/chat-artifact-sheet-focus-trap`)

Closes the A12 residual from #1608.

**Files:**
- Modify: `frontend/src/components/chat/artifact-panel/ArtifactPanel.tsx` (the `isSheet` branch: `sheetRef`, `role="dialog"`, keydown handling)
- Test: `frontend/src/components/chat/artifact-panel/__tests__/` (extend the existing ArtifactPanel test).

**Steps:**
1. Failing test: with `isSheet` true (mock `matchMedia` to report below md, see how #1608's test does it), pressing Tab on the last focusable element inside the sheet moves focus to the first, and Shift+Tab on the first moves to the last. Run → FAIL.
2. Add a `onKeyDown` on the `<aside>` active only when `isSheet`: on `Tab`, compute focusables via `querySelectorAll('a[href],button:not([disabled]),input,textarea,select,[tabindex]:not([tabindex="-1"])')` inside `sheetRef.current`, wrap at the ends, `preventDefault`. Escape already closes (from #1608). ~15 lines, no dependency.
3. Test → PASS. Gates. Commit `fix(chat): trap Tab inside the artifact bottom sheet (A12)`. PR.

---

## Out of scope

- R6-H3 (initial Alembic migration is `pass`): needs a real from-empty migration for ~49 tables; separate planned effort, not a fix PR.
- Frontend sending `client_message_id` on REST create (follow-up to Task 4).
- Browser verification of any of this; each PR body says so.
