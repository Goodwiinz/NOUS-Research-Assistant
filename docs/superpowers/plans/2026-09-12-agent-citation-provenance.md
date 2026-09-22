# Agent Citation Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote successful `do_kb_retrieve` chunks into the canonical retrieved-context channel so numbered citations are grounded, streamed cumulatively, persisted in stable order, and identical after reload.

**Architecture:** Add a pure `retrieval_provenance` boundary that validates tool results, merges contexts by document UUID plus normalized-content digest, and renders the shared prompt source map. General and filtered tool nodes call it before pruning execution history; all synthesis paths consume its renderer. SSE adapters publish cumulative snapshots, persistence records one-based source positions, and the model relationship centralizes reload ordering.

**Tech Stack:** Python 3.12, FastAPI/Pydantic, LangGraph/LangChain messages, SQLAlchemy 2 async, Alembic/PostgreSQL, React/TypeScript, Zustand, Vitest, Playwright, OpenAPI/openapi-typescript.

**Spec:** `docs/superpowers/specs/2026-09-12-agent-citations-workspace-threads-design.md`

## Global Constraints

- `state["retrieved_contexts"]` remains the single downstream provenance contract.
- Deduplicate by canonical document UUID plus SHA-256 of `" ".join(content.split())`; preserve original whitespace and first-seen order.
- Retain at most 20 contexts per turn; existing contexts win.
- Promote only completed `do_kb_retrieve` chunks carrying a canonical UUID, non-empty text/title, and finite numeric score.
- Preserve optional explicit `chunk_id`, `chunk_index`, and `page_number`; never infer a locator from rank.
- Stream events are cumulative snapshots and must retain every numbered source within `MAX_PAYLOAD_BYTES` by bounding fields, not dropping entries.
- New agent citations persist one-based `source_position`; historical rows remain null and are not rewritten.
- Positioned citations sort first by position, then legacy null-position citations by `(created_at, id)`.
- Preserve the existing document-grouped display-number map: resolve raw `[Doc N]` against the canonical citation array before grouping numerals.
- Generated artifacts `backend/openapi.json` and `frontend/src/types/generated/api.d.ts` must change together.

---

### Task 1: Canonical Tool-Result Normalization and Merge

**Files:**
- Create: `backend/src/services/agent/retrieval_provenance.py`
- Modify: `backend/src/services/agent/_nodes_tools.py`
- Test: `backend/tests/unit/services/agent/test_retrieval_provenance.py`
- Test: `backend/tests/unit/services/test_agent_citation_context_regressions.py`

**Interfaces:**
- Consumes: execution dictionaries shaped as `{id, tool_name, status, result}` and existing context dictionaries.
- Produces: `contexts_from_tool_execution(execution: Mapping[str, Any]) -> list[dict[str, Any]]` and `merge_retrieved_contexts(existing: Sequence[Mapping[str, Any]], executions: Sequence[Mapping[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]`.

- [x] **Step 1: Write failing normalization tests**

```python
def test_promotes_safe_do_kb_chunks_with_exact_locators():
    execution = {
        "id": "call-1",
        "tool_name": "do_kb_retrieve",
        "status": "completed",
        "result": {
            "chunks": [{
                "document_id": str(DOC_ID),
                "title": "Paper",
                "text": "  retained whitespace\n",
                "score": 0.91,
                "score_source": "upstream",
                "metadata": {"chunk_id": "c-7", "chunk_index": 7, "page_number": 12},
            }],
        },
    }
    assert contexts_from_tool_execution(execution) == [{
        "document_id": str(DOC_ID), "title": "Paper",
        "content": "  retained whitespace\n", "score": 0.91,
        "score_source": "upstream", "chunk_id": "c-7",
        "chunk_index": 7, "page_number": 12,
        "tool_call_id": "call-1", "tool_chunk_position": 0,
        "context_origin": "tool",
    }]
```

Add parameterized rejections for failed status, wrong tool, title-only `search_documents`, empty text, absent/unparseable document UUID, booleans and non-finite scores, malformed chunks, and unsafe locator types. Assert rejection logs include tool name/call ID/category but never chunk text or query content.

- [x] **Step 2: Write failing merge/order/cap tests**

```python
def test_merge_deduplicates_collapsed_whitespace_but_keeps_distinct_chunks():
    merged = merge_retrieved_contexts(
        [context(DOC_ID, "alpha   beta", call="old", position=0)],
        [execution(chunks=[chunk(DOC_ID, "alpha beta"), chunk(DOC_ID, "gamma")])],
    )
    assert [item["content"] for item in merged] == ["alpha   beta", "gamma"]
    assert [item["tool_chunk_position"] for item in merged] == [0, 1]

def test_merge_keeps_first_twenty_without_renumbering():
    merged = merge_retrieved_contexts([], [execution(chunks=twenty_one_chunks())])
    assert len(merged) == 20
    assert [item["content"] for item in merged] == [f"chunk {i}" for i in range(20)]
```

- [x] **Step 3: Run the focused tests and confirm RED**

Run: `PYTHONPATH=backend /tmp/ci-venv/bin/python -m pytest -q backend/tests/unit/services/agent/test_retrieval_provenance.py backend/tests/unit/services/test_agent_citation_context_regressions.py`

Expected: collection/import failure because `retrieval_provenance` and its functions do not exist.

- [x] **Step 4: Implement the pure boundary**

```python
MAX_RETRIEVED_CONTEXTS = 20

def _dedupe_key(context: Mapping[str, Any]) -> tuple[str, str]:
    collapsed = " ".join(str(context["content"]).split())
    return str(context["document_id"]), hashlib.sha256(collapsed.encode()).hexdigest()

def contexts_from_tool_execution(execution: Mapping[str, Any]) -> list[dict[str, Any]]:
    if execution.get("tool_name") != "do_kb_retrieve" or execution.get("status") != "completed":
        return []
    result = execution.get("result")
    if not isinstance(result, Mapping) or result.get("error") or not isinstance(result.get("chunks"), list):
        return []
    return [context for position, chunk in enumerate(result["chunks"])
            if (context := _normalize_chunk(chunk, execution.get("id"), position)) is not None]

def merge_retrieved_contexts(existing, executions, *, limit=MAX_RETRIEVED_CONTEXTS):
    merged = [dict(item) for item in existing[:limit] if isinstance(item, Mapping)]
    seen = {_dedupe_key(item) for item in merged if _has_dedupe_fields(item)}
    for execution in executions:
        for context in contexts_from_tool_execution(execution):
            key = _dedupe_key(context)
            if key not in seen and len(merged) < limit:
                seen.add(key)
                merged.append(context)
    return merged
```

Use `UUID(str(value))`, `math.isfinite(float(score))`, reject `bool`, validate optional integers as non-negative integers, bound `chunk_id` to a string, and log only `tool_name`, `call_id`, and a fixed rejection category.

- [x] **Step 5: Wire both tool nodes before history pruning**

In both `tool_node` and `filtered_tool_node`, retain `batch_executions` in original call order and merge them before `tool_executions = tool_executions[-20:]`:

```python
retrieved_contexts = merge_retrieved_contexts(
    state.get("retrieved_contexts", []), batch_executions
)
return {**existing_updates, "retrieved_contexts": retrieved_contexts}
```

Do not promote cached/deduped entries a second time; existing state already owns their contexts.

- [x] **Step 6: Run focused tests and confirm GREEN**

Run the command from Step 3. Expected: all selected tests pass.

- [ ] **Step 7: Commit the normalization slice**

```bash
git add backend/src/services/agent/retrieval_provenance.py backend/src/services/agent/_nodes_tools.py backend/tests/unit/services/agent/test_retrieval_provenance.py backend/tests/unit/services/test_agent_citation_context_regressions.py
git commit -m "fix(agent): promote knowledge base tool citations"
```

### Task 2: Shared Grounded Prompt Rendering Across Every Synthesis Path

**Files:**
- Modify: `backend/src/services/agent/retrieval_provenance.py`
- Modify: `backend/src/services/agent/_nodes_llm.py`
- Modify: `backend/src/services/agent/subgraphs/research_agent.py`
- Modify: `backend/src/services/agent/subgraphs/data_agent.py`
- Modify: `backend/src/services/agent/subgraphs/writing_agent.py`
- Modify: `backend/src/services/agent/subgraphs/_factory.py`
- Test: `backend/tests/unit/services/agent/test_retrieval_provenance.py`
- Test: `backend/tests/services/agent/test_data_subgraph_prompt.py`
- Test: `backend/tests/unit/services/test_agent_citation_context_regressions.py`

**Interfaces:**
- Consumes: canonical contexts plus the sanitized message list.
- Produces: `render_retrieval_prompt(contexts: Sequence[Mapping[str, Any]], messages: Sequence[BaseMessage]) -> str`.

- [x] **Step 1: Write failing prompt-path tests**

Exercise `llm_node`, the main `_nodes_llm.force_synthesis_node`, `research_llm_node`, `data_llm_node`, and a factory-generated specialist forced-synthesis node with fake LLMs. Assert each receives `[Doc 1]`, the malicious title/locator remains inside `wrap_untrusted` fences, and `NO_RETRIEVAL_GUIDANCE` is absent after a successful tool retrieval.

Add compaction cases where the matching `ToolMessage.additional_kwargs["compacted"]` is true, the dedupe-prefix JSON is unparseable, or chunk binding no longer proves the original evidence. Assert bounded context content is included. For an uncompacted message whose parsed chunk at `tool_chunk_position` still matches, assert the full chunk is not duplicated into the source map.

- [x] **Step 2: Run prompt tests and confirm RED**

Run: `PYTHONPATH=backend /tmp/ci-venv/bin/python -m pytest -q backend/tests/unit/services/agent/test_retrieval_provenance.py backend/tests/services/agent/test_data_subgraph_prompt.py backend/tests/unit/services/test_agent_citation_context_regressions.py`

Expected: specialist and forced-synthesis prompts omit the canonical source map.

- [x] **Step 3: Implement the renderer and evidence binding**

```python
def render_retrieval_prompt(contexts, messages) -> str:
    if not contexts:
        return NO_RETRIEVAL_GUIDANCE
    blocks = []
    for position, context in enumerate(contexts, 1):
        label = _bounded_source_label(context)
        if context.get("context_origin") != "tool" or not _tool_message_has_original_chunk(context, messages):
            label = f"{label}\nevidence: {_bounded_evidence(context)}"
        blocks.append(f"[Doc {position}]\n" + wrap_untrusted(label, "retrieved_document", 3100))
    return "Retrieved context:\n" + "\n\n".join(blocks)
```

Sanitize title, chunk ID, page number, and chunk index with `_sanitize_prompt_field`; cap title/locator fields before fencing. `_tool_message_has_original_chunk` must match `tool_call_id`, reject compacted messages, parse the exact result-chunk position, and compare canonical UUID plus collapsed-content digest.

- [x] **Step 4: Wire all model paths**

Replace `_retrieval_context_part` internals with the shared renderer and use it in both `llm_node` and the main `force_synthesis_node`; append the same retrieval prompt before sanitized history in `research_llm_node` and `data_llm_node`; append it to the factory specialist forced-synthesis system prompt. Keep RAG-origin contexts as full fenced evidence.

- [x] **Step 5: Run focused prompt tests and confirm GREEN**

Run the command from Step 2. Expected: all selected tests pass.

- [ ] **Step 6: Commit the prompt slice**

```bash
git add backend/src/services/agent/retrieval_provenance.py backend/src/services/agent/_nodes_llm.py backend/src/services/agent/subgraphs/research_agent.py backend/src/services/agent/subgraphs/data_agent.py backend/src/services/agent/subgraphs/_factory.py backend/tests/unit/services/agent/test_retrieval_provenance.py backend/tests/services/agent/test_data_subgraph_prompt.py backend/tests/unit/services/test_agent_citation_context_regressions.py
git commit -m "fix(agent): ground every synthesis path in canonical sources"
```

### Task 3: Cumulative Bounded Citation Snapshots for Normal and Confirmation Streams

**Files:**
- Modify: `backend/src/api/agent/streaming.py`
- Test: `backend/tests/unit/api/test_agent_streaming_frame_byte_bounds.py`
- Test: `backend/tests/unit/api/test_agent_tool_citation_streaming.py`
- Test: `backend/tests/unit/api/test_agent_tool_citation_streaming_transports.py`

**Interfaces:**
- Consumes: any `on_chain_end` output containing `retrieved_contexts`.
- Produces: one `rag_context` snapshot per changed cumulative list on both stream generators.

- [x] **Step 1: Write failing stream regressions**

Drive fake graph event streams through both generators. Emit RAG, general tool-node, filtered specialist-node, and repeated wrapper outputs. Assert observed snapshots are `[[doc1], [doc1, doc2]]`, not deltas; repeated equal outputs emit nothing; a confirmation resume emits the full carried-plus-new canonical snapshot.

Extend the byte-bound test to 20 contexts containing non-ASCII title/content. Assert frame bytes fit the budget, all 20 source positions remain present and ordered, and buffered/replayed bytes equal the live frame.

- [x] **Step 2: Run stream tests and confirm RED**

Run: `PYTHONPATH=backend /tmp/ci-venv/bin/python -m pytest -q backend/tests/unit/api/test_agent_tool_citation_streaming.py backend/tests/unit/api/test_agent_tool_citation_streaming_transports.py backend/tests/unit/api/test_agent_streaming_frame_byte_bounds.py`

Expected: tool/specialist/confirmation contexts are absent and current clipping drops list entries.

- [x] **Step 3: Add a shared changed-snapshot helper**

```python
def _retrieved_context_snapshot(event: Mapping[str, Any], previous: str | None):
    if event.get("event") != "on_chain_end":
        return None, previous
    output = event.get("data", {}).get("output")
    contexts = output.get("retrieved_contexts") if isinstance(output, dict) else None
    if not isinstance(contexts, list) or not contexts:
        return None, previous
    public = [_public_stream_context(item) for item in contexts]
    fingerprint = _json.dumps(public, sort_keys=True, separators=(",", ":"))
    return (public if fingerprint != previous else None), fingerprint
```

Call it from both event loops, independent of node name. Project only public provenance fields; retain list order.

- [x] **Step 4: Preserve every context in the byte bound**

Special-case `AgentStreamEvent.RAG_CONTEXT` in `_bound_frame_payload`. Clip only per-context strings through a UTF-8-aware ladder and never slice the `contexts` list. With the maximum 20 contexts and projected fields, the smallest rung must fit; retain all one-based positions even if snippets are reduced to empty strings.

- [x] **Step 5: Run stream tests and confirm GREEN**

Run the command from Step 2. Expected: all selected tests pass.

- [ ] **Step 6: Commit the streaming slice**

```bash
git add backend/src/api/agent/streaming.py backend/tests/unit/api/test_agent_tool_citation_streaming.py backend/tests/unit/api/test_agent_tool_citation_streaming_transports.py backend/tests/unit/api/test_agent_streaming_frame_byte_bounds.py
git commit -m "fix(agent): stream cumulative citation snapshots"
```

### Task 4: Stable Citation Persistence, Migration, and Reload Contracts

**Files:**
- Create: `backend/alembic/versions/t2u3v4w5x6y7_add_citation_source_position.py`
- Modify: `backend/src/models/citation.py`
- Modify: `backend/src/models/chat_message.py`
- Modify: `backend/src/schemas/chat.py`
- Modify: `backend/src/api/threads/workspace_routes/presenters.py`
- Modify: `backend/src/api/threads/threads.py`
- Modify: `backend/src/services/agent/agent_execution_service.py`
- Test: `backend/tests/unit/models/test_citation_source_position.py`
- Test: `backend/tests/api/agent/test_persist_thread_messages.py`

**Interfaces:**
- Produces: nullable `Citation.source_position`, ordered `ChatMessage.citations`, and optional `CitationResponse.source_position`.

- [x] **Step 1: Write failing migration/model/serialization tests**

Assert migration revision `t2u3v4w5x6y7` revises current head `s1t2u3v4w5x6`, adds nullable integer `citations.source_position`, and downgrade drops only that column. Build mixed positioned and legacy citation rows; assert `[2, 1, None-old, None-new]` serializes as `[1, 2, None-old, None-new]`. Assert persistence stores positions 1..N plus chunk/page locators in the same transaction.

- [x] **Step 2: Run persistence tests and confirm RED**

Run: `PYTHONPATH=backend /tmp/ci-venv/bin/python -m pytest -q backend/tests/unit/models/test_citation_source_position.py backend/tests/api/agent/test_persist_thread_messages.py`

Expected: `source_position` is absent.

- [x] **Step 3: Add the additive migration and model field**

```python
revision = "t2u3v4w5x6y7"
down_revision = "s1t2u3v4w5x6"

def upgrade() -> None:
    op.add_column("citations", sa.Column("source_position", sa.Integer(), nullable=True))

def downgrade() -> None:
    op.drop_column("citations", "source_position")
```

Add `source_position = Column(Integer, nullable=True)` and relationship ordering equivalent to `source_position ASC NULLS LAST, created_at ASC, id ASC` so every eager-load/serializer path shares it.

- [x] **Step 4: Persist and expose full locator data**

Enumerate canonical contexts one-based in `_persist_assistant_message`; pass `source_position`, `chunk_id`, `chunk_index`, and `page_number`. Add optional `source_position` to `CitationResponse`, `_citation_to_response`, `Citation.to_dict`, and `Citation.to_frontend_format`.

- [x] **Step 5: Run persistence tests and migration checks**

Run:

```bash
TEST_PG_ASYNC_URL=postgresql+asyncpg:///issue1622_verify_20260912 PYTHONPATH=backend /tmp/ci-venv/bin/python -m pytest -q backend/tests/unit/models/test_citation_source_position.py backend/tests/api/agent/test_persist_thread_messages.py
PYTHONPATH=backend /tmp/ci-venv/bin/python -m pytest -q backend/tests/unit/ci/test_migration_check_workflow.py
```

Also execute the actual migration module against the isolated PostgreSQL database: upgrade, verify a legacy row retains a null position, downgrade, and re-upgrade. Verify the ORM relationship orders positioned rows before legacy rows.

Expected: all selected tests pass.

- [ ] **Step 6: Commit the persistence slice**

```bash
git add backend/alembic/versions/t2u3v4w5x6y7_add_citation_source_position.py backend/src/models/citation.py backend/src/models/chat_message.py backend/src/schemas/chat.py backend/src/api/threads/workspace_routes/presenters.py backend/src/api/threads/threads.py backend/src/services/agent/agent_execution_service.py backend/tests/unit/models/test_citation_source_position.py backend/tests/api/agent/test_persist_thread_messages.py
git commit -m "fix(citations): preserve source numbering across reloads"
```

### Task 5: Frontend Snapshot Replacement and Citation Adapter Fidelity

**Files:**
- Modify: `frontend/src/types/workspace.ts`
- Modify: `frontend/src/utils/citationNormalizer.ts`
- Modify: `frontend/src/utils/citationParser.ts`
- Modify: `frontend/src/hooks/chat/useChatStreaming.ts`
- Test: `frontend/src/hooks/__tests__/useChatStreaming.citations.test.ts`
- Test: `frontend/src/hooks/__tests__/useChatStreaming.confirmToolSteps.test.tsx`
- Test: `frontend/src/utils/__tests__/citationNormalizer.test.ts`
- Test: `frontend/src/components/chat/aui/__tests__/AuiMessage.test.tsx`

**Interfaces:**
- Consumes: cumulative `rag_context` arrays with optional source positions and locators.
- Produces: live and committed `Citation[]` arrays in canonical order; existing `numberCitations` display grouping remains unchanged.

- [x] **Step 1: Write failing frontend regressions**

Simulate carried `[Doc 1]`, then resume snapshot `[Doc 1, Doc 2]`. Assert the store and optimistic committed message contain two citations, not three. Assert nested confirmation and failure retry carry the latest full snapshot. Assert `normalizeCitation` preserves `source_position`, `chunk_id`, `chunk_index`, and `page_number` aliases. Render distinct chunks from one document and verify raw markers resolve correctly while both display the shared document numeral.

- [x] **Step 2: Run frontend tests and confirm RED**

Run: `pnpm --dir frontend test -- --run src/hooks/__tests__/useChatStreaming.citations.test.ts src/hooks/__tests__/useChatStreaming.confirmToolSteps.test.tsx src/utils/__tests__/citationNormalizer.test.ts src/components/chat/aui/__tests__/AuiMessage.test.tsx`

Expected: carried-plus-resume concatenation duplicates the first citation and locator fields are dropped.

- [x] **Step 3: Replace confirmation snapshots instead of concatenating**

Maintain a single `confirmCitations` variable initialized from `pendingConfirmation.citations`; each `onRagContext(contexts)` replaces it. Use that same variable for live store state, optimistic message creation, nested confirmation, and retry state. Remove all `[...carriedCitations, ...resumeCitations]` expressions.

- [x] **Step 4: Preserve optional fields through frontend adapters**

Add snake/camel aliases in `Citation`, `CitationCreate`, and normalized `Citation`:

```typescript
sourcePosition: number | undefined;
chunkId: string | undefined;
chunkIndex: number | undefined;
pageNumber: number | undefined;
```

Do not change `CitationChips.numberCitations` or raw marker lookup.

- [x] **Step 5: Run frontend tests and confirm GREEN**

Run the command from Step 2. Expected: all selected tests pass.

- [ ] **Step 6: Commit the frontend slice**

```bash
git add frontend/src/types/workspace.ts frontend/src/utils/citationNormalizer.ts frontend/src/utils/citationParser.ts frontend/src/hooks/chat/useChatStreaming.ts frontend/src/hooks/__tests__/useChatStreaming.citations.test.ts frontend/src/hooks/__tests__/useChatStreaming.confirmToolSteps.test.tsx frontend/src/utils/__tests__/citationNormalizer.test.ts frontend/src/components/chat/aui/__tests__/AuiMessage.test.tsx
git commit -m "fix(chat): replace cumulative citation snapshots"
```

### Task 6: Generated Contracts and Deterministic Browser Regression

**Files:**
- Modify: `backend/openapi.json`
- Modify: `frontend/src/types/generated/api.d.ts`
- Create: `frontend/app/visual-test/agent-citations/page.tsx`
- Create: `frontend/app/visual-test/agent-citations/AgentCitationsVisualFixture.tsx`
- Create: `frontend/playwright.agent-citations.config.ts`
- Create: `frontend/e2e/visual/agent-citations-workspace.spec.ts`

**Interfaces:**
- Produces: generated optional `source_position` and a fixture-gated browser route exercising actual citation renderers with seeded SSE/reload shapes.

- [x] **Step 1: Regenerate contracts**

```bash
PYTHONPATH=backend /tmp/ci-venv/bin/python scripts/ci/generate_openapi.py
pnpm --dir frontend generate:api-types
```

- [x] **Step 2: Add the fixture-gated deterministic page and Playwright flow**

Compose the real chat message/citation rendering surface only when `NEXT_PUBLIC_VISUAL_TEST_FIXTURES=1`. Seed five canonical chunks across cumulative tool-call snapshots, including two chunks from one document and page/chunk locators. The Chromium test asserts live raw markers resolve, the grouped source list stays stable, then switches to the reloaded response and asserts identical titles/locators/order.

- [x] **Step 3: Run the browser regression**

Run: `CI=1 NEXT_PUBLIC_VISUAL_TEST_FIXTURES=1 pnpm --dir frontend exec playwright test --config=playwright.agent-citations.config.ts`

Expected: Chromium passes without external LLM, KB, auth, or network dependencies.

- [x] **Step 4: Run contract and focused quality gates**

```bash
PYTHONPATH=backend /tmp/ci-venv/bin/python scripts/ci/generate_openapi.py --check
sha256sum backend/openapi.json frontend/src/types/generated/api.d.ts
PYTHONPATH=backend /tmp/ci-venv/bin/python scripts/ci/generate_openapi.py
pnpm --dir frontend generate:api-types
sha256sum backend/openapi.json frontend/src/types/generated/api.d.ts  # hashes unchanged
# Raw ESLint reports the existing full-tree debt (exit 1); the ratchet below passes.
pnpm --dir frontend exec eslint app src --format json --output-file /tmp/issue1622-eslint.9G4WYx.json
node scripts/ci/check_frontend_quality.mjs --report /tmp/issue1622-eslint.9G4WYx.json --base origin/develop
pnpm --dir frontend quality:exclusions
pnpm --dir frontend type-check

for file in \
  backend/alembic/versions/t2u3v4w5x6y7_add_citation_source_position.py \
  backend/src/services/agent/retrieval_provenance.py \
  backend/tests/unit/api/test_agent_thread_resolution_transports.py \
  backend/tests/unit/api/test_agent_tool_citation_streaming.py \
  backend/tests/unit/api/test_agent_tool_citation_streaming_transports.py \
  backend/tests/unit/models/test_citation_source_position.py \
  backend/tests/unit/services/agent/test_retrieval_provenance.py \
  backend/tests/unit/services/threads/test_workspace_thread_listing.py; do
  /tmp/issue1622-lint.Ixbeu1/bin/mypy --ignore-missing-imports --follow-imports=silent "$file"
done
```

`check:api-types` is intentionally not recorded as a passing command in this uncommitted worktree because that script asserts a clean generated-artifact diff. Instead, both generators were rerun and the before/after SHA-256 hashes matched. The CI-equivalent lint-only MyPy environment above passed all eight added Python files individually.

- [x] **Step 5: Run citation regression suites**

```bash
PYTHONPATH=backend /tmp/ci-venv/bin/python -m pytest -q backend/tests/unit/services/agent/test_retrieval_provenance.py backend/tests/unit/services/test_agent_citation_context_regressions.py backend/tests/unit/api/test_agent_tool_citation_streaming.py backend/tests/unit/api/test_agent_tool_citation_streaming_transports.py backend/tests/unit/api/test_agent_streaming_frame_byte_bounds.py backend/tests/unit/models/test_citation_source_position.py backend/tests/api/agent/test_persist_thread_messages.py backend/tests/unit/architecture
pnpm --dir frontend test -- --run src/hooks/__tests__/useChatStreaming.citations.test.ts src/hooks/__tests__/useChatStreaming.confirmToolSteps.test.tsx src/utils/__tests__/citationNormalizer.test.ts src/components/chat/aui/__tests__/AuiMessage.test.tsx
```

- [ ] **Step 6: Commit generated artifacts and browser coverage**

```bash
git add backend/openapi.json frontend/src/types/generated/api.d.ts frontend/app/visual-test/agent-citations/page.tsx frontend/app/visual-test/agent-citations/AgentCitationsVisualFixture.tsx frontend/playwright.agent-citations.config.ts frontend/e2e/visual/agent-citations-workspace.spec.ts
git commit -m "test(chat): cover live and reloaded citation provenance"
```
