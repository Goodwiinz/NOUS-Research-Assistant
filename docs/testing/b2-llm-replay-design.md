# B2 — Deterministic Offline LLM Replay for the Agent Eval Golden Harness

**Status:** Design / decision-ready
**Target:** `backend/tests/eval/test_agent_regression.py::test_local_golden_case`
**Author context:** rag-clean Python backend, LangGraph agent

---

## 1. Problem & Goal

The golden regression harness (`test_local_golden_case`, 24 parametrized `LOCAL_CASES`) compiles the *real* agent graph and streams it (`compile_agent_graph().astream()`), scoring two things: the classified `intent` and the **list of tool-call names** the agent intended. Today every run requires live Azure creds (`AZURE_OPENAI_CHAT_*`) and hits the real model, so the cases are (a) **skipped entirely in CI** when creds are absent (`tests/eval/conftest.py` `pytest_collection_modifyitems` → `skip_golden`), and (b) **single-shot nondeterministic** when they do run, because the LLM picks intent/tools live. The harness that exists to catch routing regressions is therefore either green-but-blind or flaky. **Goal:** make these cases run in CI with **no live LLM creds** and **no single-shot flakiness**, while preserving the existing assertions and keeping a separate live lane that catches model-behavior drift.

---

## 2. Approach Comparison

| Approach | Reproduces `tool_calls`? | Reproduces structured output? | Creds-free replay? | Staleness / maintenance | Realism | Effort |
|---|---|---|---|---|---|---|
| **VCR / HTTP cassettes** (vcrpy/pytest-recording over the openai→httpx transport) | Yes (replays real HTTP JSON) | Yes (replays real envelopes) | **Yes at replay** — but record needs live creds | **High** — body-match on full prompt+tool-schema+model+deployment; any prompt/schema/`max_tokens` edit silently invalidates → live re-record | **Highest** — exercises real langchain parsing of real model output | **High** — 2 new deps (not installed), header/key scrubbing, still leaves tool execution live |
| **LangChain stock fake** (`FakeListChatModel`/`GenericFakeChatModel`/`FakeMessagesListChatModel`) | **No** — `bind_tools` is `BaseChatModel.bind_tools` → `NotImplementedError` | **No** — `with_structured_output` raises `NotImplementedError` (verified in venv, langchain_core 1.4.1) | Yes | Low | Low (but unusable) | **Dead end** — dies at first classifier/planner call |
| **Response cache** (`set_llm_cache` + `SQLiteCache`/`InMemoryCache`) | **No** — cache skips tool-binding calls; those dominate this agent | Partial / no | **No** — miss → real model; needs live populate + creds from scratch | High — `llm_string` includes client params; version-bump brittle; silent live re-hit on miss | Medium | Wrong primitive |
| **Hybrid: custom `BaseChatModel` fake at the factory seam + tool short-circuit** (RECOMMENDED) | **Yes** — override `bind_tools` to return a runnable whose `ainvoke` yields a scripted `AIMessage` with `tool_calls` | **Yes** — override `with_structured_output` to return canned pydantic instances | **Yes** — never touches the network at replay | **Medium, controlled** — keyed by `(case, schema, call-index)`; a miss **fails loudly** and forces re-record, never silently goes live | Medium — exercises real routing/reflection/interrupt/HITL graph wiring; does *not* prove the live model still chooses these | **Medium** — one small fake + cassette loader + conftest flip; repo already proves the seam |

---

## 3. Recommendation

**Adopt the hybrid: a small custom `BaseChatModel` fake installed at the three LLM construction seams, seeded from committed per-case JSON cassettes, paired with a creds-free guarantee on tool execution.**

Justification specific to this repo:

1. **Stock fakes are structurally disqualified.** Verified in the repo venv: `FakeMessagesListChatModel(...).with_structured_output(IntentClassification)` and `.bind_tools(...)` both raise `NotImplementedError`. The agent's two hard dependencies — `with_structured_output` (classifier `IntentClassification`, planner `ComplexityCheck`/`AgentPlan`, reflection `ReflectionResult`) and `bind_tools` (`_nodes_llm`, research/writing/data subgraphs) — are exactly what the stock fakes cannot do. So "use the library fake" collapses into "write a custom fake."
2. **The cache is the wrong primitive** — it still needs creds on a miss/populate and explicitly skips tool-binding calls, which are the calls this agent lives on.
3. **VCR works but is the wrong fit for a per-PR gate** — it is not installed (2 new deps), its body-match is brittle to the repo's frequent prompt/`max_tokens` churn, it can leak the Azure key into committed cassettes, and it leaves tool execution live. Reserve it for an optional higher-fidelity lane if ever needed.
4. **The repo already proves the hybrid seam.** `tests/integration/agent/conftest.py` (`compile_graph_with_mocks`) and `test_agent_graph_partial.py` patch exactly `graph._build_llm` + `llm_factory.build_synthesis_llm` + `llm_factory.build_lightweight_llm`, with a house `MockChatModel` whose `bind_tools` returns `self` and `ainvoke` pops scripted responses. We reuse that pattern, parametrized per golden case.
5. **The harness scores only `intent` + tool-call *names*** (`_extract_tool_calls`, interrupt payloads) — never tool *results* or prose. So the LLM seam is sufficient for *assertions*; tool execution only needs to be *prevented from going live*, not faithfully reproduced.

We use **scripted-per-call** outputs (not VCR-style prompt-hash matching) as the primary mechanism, because it is deterministic, creds-free, and decouples the gate from prompt-whitespace edits — at the explicit cost that this validates **routing/wiring**, not that the live model still emits these decisions. That cost is bought back by the weekly live sweep (§6, §8).

---

## 4. Implementation Plan — the Exact Seam

### 4.1 The three construction seams (the B2 premise's "two factories" is wrong)

All LLM construction must be intercepted at **three** points, not two:

1. `src.services.agent.llm_factory.build_lightweight_llm` — classifier, planner complexity/plan, reflection, compactor, memory_store.
2. `src.services.agent.llm_factory.build_synthesis_llm` — post-tool prose synthesis, general-intent fast path.
3. `src.services.agent.graph._build_llm` — the **primary reasoning model** with its own `graph._LLM_CACHE`, reached for non-general intents via `_nodes_llm.py` and by the writing/data subgraphs and `tools_impl`. Patching `_build_chat_llm` alone does **not** cover this.

`build_lightweight_llm`/`build_synthesis_llm` are called via **lazy in-function imports** in classifier/`_nodes_llm`/subgraphs/memory_store, so patching the `llm_factory` source attribute reaches them. But `reflection.py`, `planner.py`, and `compactor.py` do **module-top `from ... import build_lightweight_llm`**, binding a local name — so they must *also* be patched at their own module attributes:

- `src.services.agent.reflection.build_lightweight_llm`
- `src.services.agent.planner.build_lightweight_llm`
- `src.services.agent.compactor.build_lightweight_llm`

`graph._build_llm`'s callers (`_nodes_llm`, writing/data agents) all lazy-import `graph`, so a single patch on `graph._build_llm` covers them.

### 4.2 Cache handling (mandatory, or a real client leaks)

Built instances are memoized in several places that survive across tests in-process. Before each case, in an autouse fixture:

- `llm_factory.reset_llm_caches()` — clears `_LIGHTWEIGHT_LLM_CACHE` / `_SYNTHESIS_LLM_CACHE`.
- `graph._LLM_CACHE.clear()`.
- Reset the result-caching singletons to `None`: `classifier._CLASSIFIER_LLM`, `reflection._REFLECTION_LLM`, `compactor._COMPACTOR_LLM`.

Because we patch the *builders* (not post-build) and reset the singletons, the singletons will re-call the patched builder and harmlessly cache the *fake*. (`reset_llm_caches()` alone is insufficient — it does not touch `graph._LLM_CACHE` or the three singletons; this is the classic green-locally/flaky-in-CI ordering hazard.)

### 4.3 The fake model

A single small class in the eval package, e.g. `backend/tests/eval/_replay_llm.py`:

```python
class GoldenReplayLLM(BaseChatModel):
    """Deterministic, scripted chat model for golden replay.

    Seeded per GoldenCase from a committed cassette. Advances a per-call
    cursor so reflection/tool loops terminate. Honors both
    with_structured_output() and bind_tools() by returning bound runnables
    that replay the recorded decision for the current call index.
    """
    cassette: GoldenCassette          # loaded JSON for this case
    _cursor: int = 0                  # advanced per ainvoke

    @property
    def _llm_type(self) -> str: return "golden-replay"

    def with_structured_output(self, schema, *, method=None, **kw):
        # Return a runnable whose ainvoke yields the canned pydantic instance
        # for `schema` at the current call slot (IntentClassification,
        # ComplexityCheck, AgentPlan, ReflectionResult).
        return _StructuredReplay(self, schema)

    def bind_tools(self, tools, **kw):
        # Return a runnable whose ainvoke yields the scripted AIMessage
        # (carrying tool_calls=[{name,args,id}, ...]) for the current slot,
        # then a terminal tool-less AIMessage to end the loop.
        return _ToolReplay(self, tools)

    async def _agenerate(self, messages, stop=None, **kw):
        # Plain-text path (synthesis with no bind_tools): return next content.
        ...
```

**Structured-output round-trip:** we override `with_structured_output` directly to *return the recorded pydantic object* rather than relying on `BaseChatModel`'s default (which internally calls `bind_tools([schema], tool_choice="any")` + `PydanticToolsParser`). This sidesteps the `NotImplementedError` path entirely and guarantees `planner.py`'s `method="function_calling"` variant works identically to the JSON variant — the fake just hands back `IntentClassification(intent=..., confidence=...)` etc. Each schema → recorded payload mapping lives in the cassette.

**Tool-call round-trip:** `bind_tools(...).ainvoke(...)` returns an `AIMessage(content=..., tool_calls=[{ "name": ..., "args": ..., "id": ... }])`. `_extract_tool_calls` reads `msg.tool_calls`, so the names flow straight into scoring. The fake's tool_call `id`s are fixed strings (the LLM normally supplies them; the fake controls them deterministically).

**Termination:** the cursor advances per `ainvoke`; when the script for a case is exhausted the fake returns a **tool-less `AIMessage`** (safe default) rather than raising `IndexError`, so an unexpected extra LLM hop after a topology change degrades gracefully instead of erroring. An exhausted *structured* slot fails loudly (forces re-record).

### 4.4 Wiring (per-case, autouse for the golden module)

```python
@pytest.fixture(autouse=True)
def _golden_replay(monkeypatch, request):
    if not _replay_enabled():            # AGENT_GOLDEN_REPLAY=1 (or always in CI lane)
        return
    case = _case_for(request)            # the GoldenCase being parametrized
    fake = GoldenReplayLLM(cassette=load_cassette(case.name))
    # reset caches/singletons (4.2)
    llm_factory.reset_llm_caches()
    graph._LLM_CACHE.clear()
    monkeypatch.setattr(classifier, "_CLASSIFIER_LLM", None, raising=False)
    monkeypatch.setattr(reflection, "_REFLECTION_LLM", None, raising=False)
    monkeypatch.setattr(compactor, "_COMPACTOR_LLM", None, raising=False)
    # three construction seams + three module-top rebinds
    for tgt in (
        (llm_factory, "build_lightweight_llm"),
        (llm_factory, "build_synthesis_llm"),
        (graph, "_build_llm"),
        (reflection, "build_lightweight_llm"),
        (planner, "build_lightweight_llm"),
        (compactor, "build_lightweight_llm"),
    ):
        monkeypatch.setattr(tgt[0], tgt[1], lambda *a, **k: fake)
    # non-LLM determinism guard (§7)
    monkeypatch.delenv("AGENT_LEDGER_DIR", raising=False)
```

No fake Azure env vars are needed: because all three builders are patched, `_build_chat_llm` / `_build_llm`'s `RuntimeError` guards on missing endpoint/key are never reached.

### 4.5 Conftest gate flip

In `tests/eval/conftest.py`, the `skip_golden` branch must **not** skip when replay is active. Add a replay condition so that under `AGENT_GOLDEN_REPLAY=1` (or the dedicated marker) the golden cases **run creds-free** and only skip in the *non-replay, no-creds* combination. Otherwise the new CI job is vacuously green — the exact green-but-blind failure mode the harness comments warn against.

---

## 5. Fixture / Cassette Format

One JSON file per case under `backend/tests/eval/cassettes/<case.name>.json`. Keyed by **call slot** (ordinal) and **kind** (`structured` with `schema`, `tool`, or `text`), not by raw prompt hash — so prompt-whitespace edits don't invalidate it, but a topology change (new slot) fails loudly.

**Example — `do_kb_retrieve` case** (`DO_KB_CASES`, expected_intent `research`, expected_tools `["do_kb_retrieve"]`):

```json
{
  "case": "do_kb_retrieve_basic",
  "expected_intent": "research",
  "expected_tools": ["do_kb_retrieve"],
  "calls": [
    {
      "slot": 0,
      "kind": "structured",
      "schema": "IntentClassification",
      "payload": { "intent": "research", "confidence": 0.95, "reasoning": "kb lookup" }
    },
    {
      "slot": 1,
      "kind": "tool",
      "message": {
        "content": "",
        "tool_calls": [
          { "name": "do_kb_retrieve", "args": { "query": "vector db indexing" }, "id": "call_golden_0" }
        ]
      }
    },
    {
      "slot": 2,
      "kind": "text",
      "message": { "content": "Here is what the knowledge base says.", "tool_calls": [] }
    }
  ]
}
```

**Example — greeting case** (`expected_tools=()`, intent-only):

```json
{
  "case": "greeting_hello",
  "expected_intent": "general",
  "expected_tools": [],
  "calls": [
    { "slot": 0, "kind": "structured", "schema": "IntentClassification",
      "payload": { "intent": "general", "confidence": 0.98, "reasoning": "greeting" } },
    { "slot": 1, "kind": "text", "message": { "content": "Hi! How can I help?", "tool_calls": [] } }
  ]
}
```

For greeting/ack cases the classifier keyword fast-path (`classify_intent_with_fallback`, keyword confidence ≥ 0.7) may resolve intent **without** an LLM call — so slot 0 must be *optional*: the fake must not assert it was consumed. The loader marks structured slots `optional: true` where the keyword path can satisfy them.

---

## 6. Recording Workflow — Deliberate Update vs Accidental Drift

**Who/when:** Cassettes are recorded by the maintainer **only when intentionally changing** a prompt, model deployment, classifier/planner/reflection schema, or adding a golden case — never on the PR pipeline. Recording needs real creds + live infra, so it runs locally or via `workflow_dispatch` on the creds-bearing self-hosted runner (the `agent-eval.yml` runner).

**Tool:** Add `backend/tests/eval/record_golden_cassette.py` (sibling to the existing `upload_golden.py`). It iterates `LOCAL_CASES`, runs `_run_agent` against the **live** model+infra, and captures, per call slot, the structured payloads and tool-call AIMessages the replay seam needs, writing `cassettes/<case.name>.json`.

**Anti-masking (mandatory):** `record_golden_cassette` must **assert `intent_match`/`tool_subset_match` against the committed `golden_examples` expectations while recording**, and **refuse to write** a cassette whose live decision violates `expected_intent`/`expected_tools`. This turns re-record into *live validation*, not blind capture. Without it, B2 degenerates into a tautology (replay replays itself and always passes), and a re-record could silently absorb an unintended regression introduced in the same PR.

**Review path:** cassettes are committed in the **same PR** as the prompt/schema diff, so the reviewer sees the prompt change and the decision change together.

**Drift detection (two independent backstops):**
1. **Staleness guard** — a fast, creds-free unit test asserting every `LOCAL_CASES.name` has a cassette file (mirrors the null-reference guards at `test_agent_regression.py:215-220`). A newly added case with no cassette fails immediately.
2. **Weekly live sweep** — `agent-eval.yml` runs `pytest -m golden` **live** so cassette-vs-reality drift surfaces as a red weekly build. **Fix the existing latent gap:** `agent-eval.yml`'s run step currently uses `-m langsmith`, which does **not** select the `golden`-marked local goldens, so today there is no live backstop for them. Add/scope `-m golden` to that job.

---

## 7. Non-LLM Determinism

**For the *current* golden set, the minimum set of non-LLM backends that must be faked is empty** — but this is fragile and must be locked in, not relied on by accident:

- **Empty `RunnableConfig`.** `_run_agent` calls `compile_agent_graph()` (no checkpointer, no store) and `astream(initial_state, stream_mode="updates")` with **no config**. So everywhere `config.get("configurable", {})` is `{}` → `current_user=None`, `db=None`. This is the contract the replay must preserve.
- **`do_kb_retrieve`** (`DO_KB_CASES`, the only non-destructive expected tool) is *not* destructive, so it would reach `_tool_do_kb_retrieve` — but that returns `{"error": "Authentication required"}` immediately when `current_user is None`. No DO KB / Qdrant call fires.
- **`ingest_arxiv_papers`** (`ARXIV_ID_CASES`) is destructive → `interrupt()` fires **before** execution; the harness harvests the name from the interrupt payload. No network.
- **All other cases** assert `expected_tools=()`; the fake simply emits no tool_call, so nothing executes.
- **rag_node / memory nodes** short-circuit on `current_user is None` (`{"retrieved_contexts":[]}` / `{"user_memories":[]}`). No backend touched.

**The one real non-LLM side effect to neutralize:** `memory_save_node` writes an iteration-ledger file if `AGENT_LEDGER_DIR` is set **and** `thread_id` is non-empty. The fixture must **unset `AGENT_LEDGER_DIR`** (done in §4.4) and the harness should keep `thread_id` empty. Low risk, but a stray-file side effect otherwise.

**Defensive guards to add now (so the suite cannot silently go live later):**
- Add a belt-and-suspenders short-circuit for `do_kb_retrieve` behind the replay flag (patch `_tool_do_kb_retrieve` to return a canned `ToolMessage`), so the case stays creds-free even if the `current_user==None` auth gate is ever removed.
- Optionally patch `classify_intent_llm` to raise under the replay flag for cases whose `expected_intent` is keyword-deterministic, asserting `source == "keyword"` — so a keyword-table edit can't silently push a case into the LLM path.

**Defer:** faking tool *results* (asserting tool output content rather than names) — that would require faking arxiv (`search_arxiv`, no auth gate, hits network), Neo4j tools (no `current_user` gate), and the DO KB/hybrid-search client, multiplying the fake surface. **Keep golden assertions at intent + tool-name granularity.** Time/randomness need no handling: the only `time`/`now` sources land in `tool_executions`/plan metadata, which is never scored; `add_messages` ordering and `preprocessing_node`'s `gather`+zip merge are deterministic; tool-call `id`s come from the (now fake) LLM.

---

## 8. CI Wiring

**Two lanes, clearly separated:**

1. **Per-PR deterministic gate — new blocking job `golden-eval`** (creds-free):
   - Selector: `pytest -m golden -c backend/pytest.ini backend/tests/eval/test_agent_regression.py -v --tb=short`. `-m golden` selects exactly the 24 `LOCAL_CASES` parametrizations (only `test_local_golden_case` carries `golden`); it does **not** pull the `langsmith` dataset test, and does **not** require dropping `slow`. Do **not** use `-m slow` (pulls perf benchmarks) or `-m "golden and slow"` (redundant).
   - Env: `AGENT_GOLDEN_REPLAY=1`, **no** Azure creds, `AGENT_LEDGER_DIR` unset.
   - **Not** `continue-on-error` — this is the determinism gate B2 buys; a missing/stale cassette must **fail**, not skip.
   - **Verify which file actually gates PRs** before adding the job: the workflows README says PR-gating is `.depot/workflows/test-pipeline.yml` (Depot CI), not the GitHub-Actions `test-pipeline.yml`. Add `golden-eval` to whichever file is the real gate.
   - The fast unit job (`-m "unit or not (integration or e2e or slow)"`) already excludes `golden` via the `slow` marker — leave it; `golden-eval` is a distinct required check.

2. **Weekly live sweep — `agent-eval.yml`** (real creds, self-hosted runner):
   - Keep as the cron reality check. **Add `-m golden` live** (today's `-m langsmith` does not select the local goldens — the latent gap from §6).
   - This is where model-behavior drift is caught; the PR gate validates routing/wiring only.

**Marker strategy:** reuse the existing registered `golden` marker (`pytest.ini`, `--strict-markers`). Distinguish *lanes by env flag* (`AGENT_GOLDEN_REPLAY`) and *conftest gate logic*, not by a new marker — avoids touching `--strict-markers` registration and keeps the live sweep selecting the same cases. The `slow` marker stays on `golden` so the fast job keeps excluding it.

---

## 9. Rollout Phases, Risks, Open Questions

### Phases
1. **Seam + fake (no behavior change):** land `GoldenReplayLLM`, the cache-reset fixture, and the six monkeypatch targets behind `AGENT_GOLDEN_REPLAY` (default off). Existing creds-based runs unaffected.
2. **Record cassettes:** run `record_golden_cassette.py` live for all 24 cases with the anti-masking assertion; commit `cassettes/`.
3. **Flip the gate:** relax `tests/eval/conftest.py` `skip_golden` under replay; add the staleness guard test.
4. **Wire CI:** add the creds-free `golden-eval` job to the real PR-gating file; fix `agent-eval.yml` to run `-m golden` live weekly.
5. **Defensive guards:** add the `do_kb_retrieve` tool short-circuit and the optional keyword-source assertion.

### Risks
- **Partial-patch leakage** (highest): patching only `llm_factory.*` silently misses `reflection`/`planner`/`compactor` (module-top imports) and `graph._build_llm` (primary reasoning + its own cache) → those cases still hit Azure and stay flaky. Mitigation: the full six-target patch list (§4.1) + cache resets (§4.2), both already proven by existing tests.
- **Tautology / regression-masking:** re-record absorbs an unintended regression. Mitigation: mandatory live-validation-on-record (§6) + human review of cassette diff + weekly live sweep.
- **Couples the gate to call order, not prompt content:** the gate validates wiring, not that the live model still emits these decisions. Mitigation: the weekly live lane is the model-drift backstop; document this scope explicitly so nobody mistakes the PR gate for behavior validation.
- **Vacuous-green if the gate isn't flipped:** wiring the job before relaxing `skip_golden` yields a permanently-skipped (green) non-gate. Mitigation: phase 3 before phase 4; staleness guard catches uncovered cases.
- **Structured-output subtlety:** the easiest thing to get wrong; the fake must return real pydantic instances and must satisfy planner's `method="function_calling"` variant identically. Mitigation: override `with_structured_output` directly (don't lean on the default bind_tools-based path).

### Open Questions
1. **Exact per-case LLM call count / order** — I did not enumerate the full call sequence for every case (e.g. whether reflection runs on empty-tool general cases, whether planner is skipped for non-`project` page_context). This must be traced while scripting; the recorder captures it empirically, which is the safest source of truth. Marked the main implementation unknown.
2. **Which workflow file actually gates PRs** — Depot (`.depot/workflows/test-pipeline.yml`) vs GitHub Actions (`test-pipeline.yml`). Confirm before adding `golden-eval`.
3. **Keyword fast-path coverage** — confirm which `expected_intent` cases resolve via keywords (no LLM call) so their structured slots are marked optional, and decide whether to enforce `source=="keyword"` for those.
4. **`agent-eval.yml` marker fix scope** — confirm adding `-m golden` to the weekly job doesn't double-run or conflict with the existing `-m langsmith` step (likely two separate steps).
