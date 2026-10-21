# Technical Spec: Mistral Migration

> **Not implemented as of 2026-10-17: this March 2026 draft was never adopted. A search of `backend/src` finds Mistral only in two model-name prefix checks (`tasks/research_tasks.py`, `api/research_engine/runs.py`), not a provider migration. Current model and provider configuration lives in the backend code and the [engineering contracts](../engineering/README.md).**

**Status:** Draft
**Author:** Abdel
**Date:** March 2026
**Scope:** Full replacement of OpenAI/Azure OpenAI with Mistral AI

---

## 1. Overview

Replace all OpenAI GPT-4o / GPT-4o-mini usage across the NOUS backend with Mistral Medium 3 (primary) and Mistral Small 4 (lightweight). Mistral's API is OpenAI-compatible, so the migration is primarily a configuration swap + model ID change across ~39 service files. Embeddings (MiniLM-L6-v2) remain unchanged for now.

---

## 2. Current State

### Models in Use

| Role | Model | Provider | Where |
|------|-------|----------|-------|
| Agent core (tool calling, RAG) | `gpt-4o` | Azure OpenAI / OpenAI | `graph.py`, `classifier.py`, `reflection.py` |
| Agent lightweight | `gpt-4o-mini` | Azure OpenAI / OpenAI | `execute.py` model selector |
| Chat completions | `gpt-4o` | Azure OpenAI | `stream_service.py`, `draft_generation_service.py` |
| Research engine | `gpt-4o` | OpenAI | `research_engine/providers/openai_provider.py` |
| Entity extraction | `gpt-4o` | Azure OpenAI | `enhanced_document_processing_service.py` |
| Tone rewriting | `gpt-4o` | Azure OpenAI | `tone_engine_service.py` |
| Table extraction | `gpt-4o` | Azure OpenAI | `table_extraction_service.py` |
| Integrity detection | `gpt-4o` | Azure OpenAI | `integrity_detection_service.py` |
| Thread summarization | `gpt-4o-mini` | Azure OpenAI | `thread_summarization_service.py`, `thread_title_generator.py` |
| Evidence classification | `gpt-4o-mini` | Azure OpenAI | `stance_classifier.py`, `consensus_calculator.py` |
| RAG evaluation | `gpt-4o` | Azure OpenAI | `rag_evaluation_service.py`, `llm_judge_service.py` |
| Multi-agent search | `gpt-4o` | Azure OpenAI | `multi_agent_search_service.py`, `multi_agent_search_service_v2.py` |
| ArXiv processing | `gpt-4o` | Azure OpenAI | `arxiv_service.py`, `arxiv_kg_integration.py` |
| Extraction matrix | `gpt-4o` | Azure OpenAI | `extraction_matrix_service.py` |
| Embeddings | `all-MiniLM-L6-v2` | Local (sentence-transformers) | `embedding_service.py` — **NOT changing** |

### LLM Factory Pattern

The codebase uses a consistent factory pattern. Most services resolve their LLM through one of:

1. **`_build_llm()`** in `graph.py` — builds `ChatOpenAI` or `AzureChatOpenAI` based on endpoint format
2. **`AzureOpenAIService`** in `infrastructure/azure_openai_service.py` — direct OpenAI client
3. **Inline construction** — some services build their own `ChatOpenAI` instances

The key routing logic is `_is_openai_compatible()`:
```python
def _is_openai_compatible(endpoint: str) -> bool:
    return "/v1" in endpoint or "services.ai.azure.com" in endpoint
```

Since Mistral's API URL is `https://api.mistral.ai/v1`, this check already passes. The `ChatOpenAI` path will be used naturally.

### Environment Variables (Current)

```env
OPENAI_API_KEY=sk-...
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://...openai.azure.com
AZURE_OPENAI_API_VERSION=2024-02-15-preview
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o
AZURE_OPENAI_CHAT_DEPLOYMENT_NAME=gpt-4o
AZURE_OPENAI_CHAT_ENDPOINT=https://...openai.azure.com
AZURE_OPENAI_CHAT_API_KEY=...
AZURE_OPENAI_CHAT_API_VERSION=2024-06-01
AZURE_OPENAI_EMBEDDING_ENDPOINT=...
AZURE_OPENAI_EMBEDDING_API_KEY=...
AZURE_OPENAI_EMBEDDING_API_VERSION=2023-05-15
```

---

## 3. Target State

### Model Mapping

| Role | Current | New | Mistral Model ID |
|------|---------|-----|-------------------|
| Agent core | `gpt-4o` | Mistral Medium 3 | `mistral-medium-latest` |
| Lightweight tasks | `gpt-4o-mini` | Mistral Small 4 | `mistral-small-latest` |
| Embeddings | `all-MiniLM-L6-v2` | **No change** | — |

### Task-to-Model Assignment

| Task | Model | Reasoning |
|------|-------|-----------|
| LangGraph agent (tool calling + RAG) | Medium 3 | Needs reliable parallel tool calling |
| Intent classification | Small 4 | Simple classification, fast response needed |
| Draft generation | Medium 3 | Quality-sensitive, long output |
| Tone rewriting | Small 4 | Mechanical transformation, speed > depth |
| Entity extraction | Small 4 | Structured output, high volume |
| Table extraction | Small 4 | Structured output, bounded context |
| Thread summarization | Small 4 | Short input/output, high frequency |
| Thread title generation | Small 4 | Trivial task |
| Evidence stance classification | Small 4 | Classification task |
| Integrity detection | Medium 3 | Needs nuanced reasoning |
| RAG evaluation / LLM judge | Medium 3 | Quality-critical assessment |
| Multi-agent search | Medium 3 | Complex reasoning + tool use |
| ArXiv feature extraction | Medium 3 | Academic content, needs depth |
| Extraction matrix | Medium 3 | Multi-document synthesis |
| Research engine | Medium 3 | Structured multi-step reasoning |
| Chat completions | Medium 3 | User-facing quality |

---

## 4. Configuration Changes

### New `.env` Variables

```env
# ── Mistral AI Configuration ──────────────────────────────
MISTRAL_API_KEY=...
MISTRAL_API_BASE=https://api.mistral.ai/v1
MISTRAL_PRIMARY_MODEL=mistral-medium-latest
MISTRAL_LIGHTWEIGHT_MODEL=mistral-small-latest

# ── Remove / Comment Out ──────────────────────────────────
# OPENAI_API_KEY=...
# AZURE_OPENAI_API_KEY=...
# AZURE_OPENAI_ENDPOINT=...
# AZURE_OPENAI_CHAT_ENDPOINT=...
# AZURE_OPENAI_CHAT_API_KEY=...
# (all AZURE_OPENAI_* vars)
```

### `config.py` Changes

```python
# Add new settings
MISTRAL_API_KEY: Optional[str] = None
MISTRAL_API_BASE: str = "https://api.mistral.ai/v1"
MISTRAL_PRIMARY_MODEL: str = "mistral-medium-latest"
MISTRAL_LIGHTWEIGHT_MODEL: str = "mistral-small-latest"

# Keep for backward compatibility during migration
OPENAI_API_KEY: Optional[str] = None  # deprecated
```

---

## 5. Migration Plan (File-by-File)

### Priority 1: Core Agent (must work first)

#### `backend/src/services/agent/graph.py`

**Change `_build_llm()`:**
```python
# Before
def _build_llm():
    endpoint = settings.AZURE_OPENAI_CHAT_ENDPOINT or settings.AZURE_OPENAI_ENDPOINT
    api_key = settings.AZURE_OPENAI_CHAT_API_KEY or settings.AZURE_OPENAI_API_KEY
    deployment = settings.AZURE_OPENAI_CHAT_DEPLOYMENT_NAME or ...
    if _is_openai_compatible(endpoint):
        return ChatOpenAI(model=deployment, api_key=api_key, base_url=endpoint, ...)

# After
def _build_llm(lightweight: bool = False):
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=settings.MISTRAL_LIGHTWEIGHT_MODEL if lightweight else settings.MISTRAL_PRIMARY_MODEL,
        api_key=settings.MISTRAL_API_KEY,
        base_url=settings.MISTRAL_API_BASE,
        temperature=0.1,
        max_tokens=2048,
    )
```

#### `backend/src/services/agent/classifier.py`

**Change `_build_classifier_llm()`:**
```python
# Uses lightweight model — intent classification is a simple task
def _build_classifier_llm():
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=settings.MISTRAL_LIGHTWEIGHT_MODEL,
        api_key=settings.MISTRAL_API_KEY,
        base_url=settings.MISTRAL_API_BASE,
        temperature=0.0,
        max_tokens=256,
    )
```

#### `backend/src/services/agent/reflection.py`

Same pattern as classifier — use `MISTRAL_LIGHTWEIGHT_MODEL`.

#### `backend/src/api/agent/execute.py`

**Update model enum:**
```python
# Before
model: Literal["gpt-4o", "gpt-4o-mini"] = Field(default="gpt-4o")

# After
model: Literal["mistral-medium-latest", "mistral-small-latest"] = Field(
    default="mistral-medium-latest"
)
```

### Priority 2: Research & Writing Services

| File | Change |
|------|--------|
| `services/research/draft_generation_service.py` | Swap to `MISTRAL_PRIMARY_MODEL` |
| `services/research/tone_engine_service.py` | Swap to `MISTRAL_LIGHTWEIGHT_MODEL` |
| `services/research/extraction_matrix_service.py` | Swap to `MISTRAL_PRIMARY_MODEL` |
| `services/research_engine/providers/openai_provider.py` | Rename → `mistral_provider.py`, update client |
| `services/threads/stream_service.py` | Swap LLM construction |
| `services/threads/thread_summarization_service.py` | Swap to `MISTRAL_LIGHTWEIGHT_MODEL` |
| `services/threads/thread_title_generator.py` | Swap to `MISTRAL_LIGHTWEIGHT_MODEL` |

### Priority 3: Document Processing & Search

| File | Change |
|------|--------|
| `services/documents/enhanced_document_processing_service.py` | Swap to `MISTRAL_LIGHTWEIGHT_MODEL` |
| `services/documents/integrity_detection_service.py` | Swap to `MISTRAL_PRIMARY_MODEL` |
| `services/processing/table_extraction_service.py` | Swap to `MISTRAL_LIGHTWEIGHT_MODEL` |
| `services/search/multi_agent_search_service.py` | Swap to `MISTRAL_PRIMARY_MODEL` |
| `services/search/multi_agent_search_service_v2.py` | Swap to `MISTRAL_PRIMARY_MODEL` |
| `services/evidence/stance_classifier.py` | Swap to `MISTRAL_LIGHTWEIGHT_MODEL` |
| `services/evidence/consensus_calculator.py` | Swap to `MISTRAL_LIGHTWEIGHT_MODEL` |

### Priority 4: ArXiv & Evaluation

| File | Change |
|------|--------|
| `services/arxiv/arxiv_service.py` | Swap to `MISTRAL_PRIMARY_MODEL` |
| `services/arxiv/arxiv_kg_integration.py` | Swap to `MISTRAL_PRIMARY_MODEL` |
| `services/evaluation/rag_evaluation_service.py` | Swap to `MISTRAL_PRIMARY_MODEL` |
| `services/evaluation/llm_judge_service.py` | Swap to `MISTRAL_PRIMARY_MODEL` |
| `services/evaluation/advanced_rag_evaluator.py` | Swap to `MISTRAL_PRIMARY_MODEL` |

### Priority 5: Infrastructure

| File | Change |
|------|--------|
| `services/infrastructure/azure_openai_service.py` | Rewrite as `mistral_service.py` or generic `llm_service.py` |
| `services/infrastructure/llm_response_cache.py` | No change needed (model-agnostic) |
| `services/ingestion/kaggle_llm_bulk_ingestion.py` | Swap LLM construction |

---

## 6. API Compatibility Notes

### What works out of the box

Mistral's API is OpenAI-compatible at `https://api.mistral.ai/v1`. LangChain's `ChatOpenAI` class works directly with `base_url` pointed to Mistral.

```python
# This just works
from langchain_openai import ChatOpenAI
llm = ChatOpenAI(
    model="mistral-medium-latest",
    api_key="your-mistral-key",
    base_url="https://api.mistral.ai/v1",
)
```

### Tool calling

Mistral Medium 3 supports native function calling with the same schema format as OpenAI. LangChain's `.bind_tools()` works unchanged. Key differences:

- Mistral supports **parallel tool calls** (same as GPT-4o)
- Tool call IDs use the same format
- `tool_choice` parameter is supported (`auto`, `any`, `none`)
- **No `strict` mode** — Mistral doesn't have OpenAI's "strict" JSON schema enforcement. Outputs are still well-formed, but no guaranteed schema adherence

### Structured output

- JSON mode works via `response_format={"type": "json_object"}` — same as OpenAI
- Mistral does not support OpenAI's `response_format: { type: "json_schema", ... }` strict schema mode

### Streaming

- SSE streaming works identically — same `delta` format in chunks
- The agent's SSE streaming endpoint (`/stream`) should work unchanged

### Context window differences

| Model | Context Window |
|-------|---------------|
| GPT-4o | 128K tokens |
| Mistral Medium 3 | 131K tokens |
| GPT-4o-mini | 128K tokens |
| Mistral Small 4 | 256K tokens |

No truncation concerns — Mistral has equal or larger context than GPT-4o.

### Rate limits

Mistral's default rate limits (La Plateforme):
- 5 requests/second
- 2M tokens/minute

The agent's existing semaphore (max 3 concurrent tool calls) + 30s timeout keeps us well within these limits.

---

## 7. Cost Analysis

### Per-Token Pricing

| Model | Input (per M tokens) | Output (per M tokens) |
|-------|---------------------|-----------------------|
| GPT-4o | $2.50 | $10.00 |
| Mistral Medium 3 | $0.40 | $2.00 |
| GPT-4o-mini | $0.15 | $0.60 |
| Mistral Small 4 | $0.15 | $0.60 |

### Savings Estimate

Assuming moderate usage (a single developer workflow):

| Task | Monthly Tokens (est.) | GPT-4o Cost | Mistral Cost | Savings |
|------|-----------------------|-------------|--------------|---------|
| Agent conversations (in+out) | 2M in / 1M out | $15.00 | $2.80 | 81% |
| Draft generation | 500K in / 2M out | $21.25 | $4.20 | 80% |
| Entity extraction | 1M in / 200K out | $4.50 | $0.27 | 94% |
| Thread summarization | 500K in / 100K out | $1.85 | $0.14 | 92% |
| ArXiv processing | 1M in / 500K out | $7.50 | $1.40 | 81% |
| Evidence classification | 300K in / 50K out | $1.25 | $0.08 | 94% |
| Search + evaluation | 500K in / 300K out | $4.25 | $0.80 | 81% |
| **Total** | | **$55.60/mo** | **$9.69/mo** | **~83%** |

At scale (10 active users), estimated savings: **$459/month → $97/month** (~79% reduction).

### Break-even analysis

There is no migration cost beyond development time. Mistral is cheaper at every volume level. The Small 4 pricing matches GPT-4o-mini exactly ($0.15/$0.60), so lightweight tasks cost the same. The savings come entirely from replacing GPT-4o with Medium 3 on the heavy tasks.

---

## 8. Testing Strategy

### Phase 1: Smoke Test

1. Set new `.env` variables
2. Start backend with `uvicorn src.main:app --reload`
3. Hit `/api/v1/agent/health` — verify agent initializes
4. Send a simple message via `/api/v1/agent/stream` — verify SSE streaming works
5. Trigger a tool call (e.g., "search for documents about X") — verify tool calling works

### Phase 2: Subgraph Validation

Test each subgraph independently:

| Subgraph | Test Query | Expected Behavior |
|----------|-----------|-------------------|
| Research | "Search arXiv for attention mechanisms" | Tool call → arXiv search → results |
| Writing | "Create a draft summarizing my documents" | HITL interrupt → confirm → draft generated |
| Data | "Extract entities from document X" | Tool call → entity extraction → KG result |
| General | "What documents do I have?" | Tool call → document list |

### Phase 3: Regression

Run existing test suite:
```bash
# Unit tests (mock LLM, verify request format)
pytest tests/unit/ -m unit -k "agent" -v

# Integration tests (real API calls)
pytest tests/integration/ -m integration -k "agent" -v

# Contract tests (verify API response shapes)
pytest tests/contract/ -v
```

### Phase 4: Quality Comparison

Use the existing evaluation framework:
```bash
# Run RAG evaluation with Mistral
pytest tests/ -m deepeval -v

# Compare with baseline (save GPT-4o results first)
POST /api/v1/evaluation/comparisons
```

Compare: answer relevancy, faithfulness, context precision, latency.

---

## 9. Future: Embedding Migration

**Not in scope for this migration.** Documented here for planning.

### Current
- Model: `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions)
- Provider: Local (sentence-transformers library)
- Storage: Qdrant collections

### Potential Upgrade
- Model: `mistral-embed` (1024 dimensions)
- Provider: Mistral API ($0.10 per M tokens)
- Better semantic quality for academic/research content

### Migration Cost
- Re-index all Qdrant collections (full re-embedding of all documents)
- Update Qdrant collection schemas (384 → 1024 dimensions)
- Update `embedding_service.py` to call Mistral API instead of local model
- Downtime: depends on collection size, estimate 30min–2hrs for re-indexing
- API cost: ~$5–20 depending on corpus size

### When to do this
After the LLM migration is stable and validated. Can be a separate PR.

---

## 10. References

- [Mistral Medium 3 Announcement](https://mistral.ai/news/mistral-medium-3)
- [Mistral Small 4 Announcement](https://mistral.ai/news/mistral-small-4)
- [Mistral API Pricing](https://mistral.ai/pricing)
- [Mistral Function Calling Docs](https://docs.mistral.ai/capabilities/function_calling)
- [Mistral API Reference](https://docs.mistral.ai/api/)
- [LangChain ChatOpenAI + Mistral](https://python.langchain.com/docs/integrations/chat/mistralai/)

---

## Appendix: All Affected Files (39)

### Agent System (5 files)
- `backend/src/services/agent/graph.py` — main LLM factory `_build_llm()`
- `backend/src/services/agent/classifier.py` — intent classifier LLM
- `backend/src/services/agent/reflection.py` — reflection LLM
- `backend/src/services/agent/planner.py` — agent planner
- `backend/src/services/agent/compactor.py` — message compaction

### API Layer (1 file)
- `backend/src/api/agent/execute.py` — model enum `Literal["gpt-4o", "gpt-4o-mini"]`

### Research Services (5 files)
- `backend/src/services/research/draft_generation_service.py`
- `backend/src/services/research/tone_engine_service.py`
- `backend/src/services/research/extraction_matrix_service.py`
- `backend/src/services/research_engine/providers/openai_provider.py`
- `backend/src/services/threads/stream_service.py`

### Thread Services (2 files)
- `backend/src/services/threads/thread_summarization_service.py`
- `backend/src/services/threads/thread_title_generator.py`

### Document Processing (3 files)
- `backend/src/services/documents/enhanced_document_processing_service.py`
- `backend/src/services/documents/integrity_detection_service.py`
- `backend/src/services/processing/table_extraction_service.py`

### Search Services (2 files)
- `backend/src/services/search/multi_agent_search_service.py`
- `backend/src/services/search/multi_agent_search_service_v2.py`

### Evidence Services (2 files)
- `backend/src/services/evidence/stance_classifier.py`
- `backend/src/services/evidence/consensus_calculator.py`

### ArXiv Services (2 files)
- `backend/src/services/arxiv/arxiv_service.py`
- `backend/src/services/arxiv/arxiv_kg_integration.py`

### Evaluation Services (3 files)
- `backend/src/services/evaluation/rag_evaluation_service.py`
- `backend/src/services/evaluation/llm_judge_service.py`
- `backend/src/services/evaluation/advanced_rag_evaluator.py`

### Infrastructure (3 files)
- `backend/src/services/infrastructure/azure_openai_service.py`
- `backend/src/services/infrastructure/llm_response_cache.py`
- `backend/src/services/ingestion/kaggle_llm_bulk_ingestion.py`

### Configuration (2 files)
- `backend/src/core/config.py`
- `.env`

### Other (9 files)
- `backend/src/services/agent/memory_store.py`
- `backend/src/services/documents/document_upload_service.py`
- `backend/src/services/processing/processing_service.py`
- `backend/src/services/processing/multimodal_processing_service.py`
- `backend/src/services/search/bm25_service.py`
- `backend/src/services/search/vector_search_service.py`
- `backend/src/services/search/hybrid_vector_search_service.py`
- `backend/src/services/embedding/embedding_service.py`
- `backend/src/services/evaluation/__init__.py`
