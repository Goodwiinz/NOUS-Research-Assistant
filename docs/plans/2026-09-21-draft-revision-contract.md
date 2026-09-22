# Draft revision and citation integrity contract

Status: implemented on `codex/fix-draft-revision-contract`; not deployed
Target branch: `develop`
Scope: NOUS writing agent, generated drafts, structured draft citations, and
artifact-version status reporting

## Outcome

Revision requests must never be routed through the create-only
`create_draft(themes=...)` path. A revision must load a durable base version on
the server, apply the requested change, validate the result, and only then
persist and promote a new version. A failed or non-compliant revision must
leave the current pointer unchanged.

## Live reproduction

Project: `dbb488c4-f814-4cf3-82d3-25bc3afac94a`
Thread: `c3f90c9d-1b54-4cff-8f55-275f35d79460`

1. v1 was generated from the attached *Attention Is All You Need* PDF.
   Generation saw an abbreviated source context and incorrectly claimed that
   benchmark figures in the abstract were unavailable.
2. A grounded chat correction retrieved the abstract and correctly identified
   28.4 BLEU, 41.8 BLEU, and 3.5 days on eight GPUs.
3. A request to revise the saved draft created v2. Its prose was corrected, but
   `citation_count` remained zero because the generated text used prose
   references rather than canonical `[Doc N]` markers.
4. A citation-only request loaded v2, then called `create_draft`. v3 became the
   current draft even though it was only a 134-word refusal asking the user to
   provide v2. The single citation belonged to the refusal, not the review.
5. The execution plan stopped after 2/6 steps. Verification, comparison, and
   final reporting never ran, while the UI retained a pending/stopped status.

v1 and v2 remain stored, so this is a current-pointer/content-integrity
regression rather than irreversible data loss.

## Audit findings

### P0 — revision is modeled as creation

- `create_draft` accepts only `themes`, `project_id`, and `style`; it has no
  base draft or revision contract.
- The writing driver explicitly says that `create_draft` does not accept a
  draft body.
- The routing evaluation nevertheless requires `create_draft` for the
  `revise-draft` case.
- Passing revision instructions as themes creates a fresh synthesis from
  project sources. The previously loaded draft is not an input to generation.

Impact: an edit request can replace the current artifact with unrelated or
refusal content while appearing to create a valid next version.

### P0 — invalid output is promoted before semantic validation

Draft generation calculates the next version, clears `is_current` on every
existing version, inserts the generated output, and marks it current. The only
content checks are that the LLM returned non-empty text and that the database
insert succeeded.

Impact: refusals, requests for missing input, short error prose, and uncited
outputs become authoritative current drafts.

### P1 — source evidence is truncated before synthesis

Each document contributes at most 500 characters from `content_summary` or
`content_text`. The benchmark sentence in the reproduced abstract falls beyond
that boundary. Other source-reading tools expose more evidence, which explains
why chat could correct the draft while draft generation could not.

Impact: the draft pipeline can claim evidence is absent when the same project
source contains it.

### P1 — citation metadata counts syntax, not supported claims

Structured citations are created only from literal `[Doc N]` markers. Plain
text such as `Abstract, PDF p. 1` produces `citation_count = 0`. Conversely, a
refusal containing one `[Doc 1]` marker produces `citation_count = 1`.

Impact: the count indicates marker presence, not whether the artifact is a
valid grounded draft. The renderer can only make citations clickable when the
content marker and `DraftCitation.citation_index` agree.

### P1 — persisted citation indices can disagree with visible markers

Citation extraction keeps distinct documents in first-appearance order, but
persistence assigns `citation_index` by enumerating that reduced list. A draft
that cites only `[Doc 2]` therefore writes a citation row with index `1`.

Impact: the artifact shows a `[Doc 2]` marker that has no matching structured
citation row, so its source chip cannot resolve even though the document was
recognized during extraction.

### P1 — document numbering is not deterministic across calls

The project-document query has no explicit ordering even though prompt labels
and citation extraction treat list position as `[Doc N]` identity.

Impact: creation and a later revision can bind the same visible marker to
different documents without any project-content change.

### P1 — revision can rebind historical citation markers

Deterministic ordering fixes new calls, but an existing base version already
has its own durable marker-to-document mapping in `DraftCitation`. Rebuilding
the revision context only from the project's current document order can make a
preserved `[Doc N]` point at a different source.

Impact: a citation-only edit can leave the prose and marker text unchanged
while silently changing the cited document.

### P1 — a stale default revision can replace a newer current version

The model call runs outside a database transaction. Without a shared project
lock and post-model current-version check, another create/revise operation can
promote a new version while the first revision is generating; the stale
revision can then promote itself over that newer result.

Impact: concurrent work can be lost from the current pointer even though both
individual writes satisfy the database uniqueness constraints.

### P1 — an asynchronous write is followed by impossible same-turn steps

`create_draft` returns `pending`, and the writing node deliberately ends the
turn immediately. The planner still produces downstream verification,
comparison, and reporting steps.

Impact: the visible plan overpromises work that the executor cannot perform,
and progress remains incomplete after the artifact appears.

### P1 — generation status is process-local

Draft status and task cancellation use a module-level dictionary and
fire-and-forget asyncio tasks. A restart loses status; another backend process
cannot observe or cancel the task.

Impact: polling can return “not found” or stale state in a multi-process
deployment even when a draft was persisted.

### P2 — version history lacks an explicit restore/current operation

Deleting a current version promotes the latest remaining version, but there is
no non-destructive operation to select an older valid version as current.

Impact: recovery from a bad version requires deletion or direct database work.

## Required behavior

### Creation

`create_draft` remains create-only.

- It must synthesize from project documents, never from a prior draft.
- The document context builder must use a bounded multi-document budget large
  enough to include substantive source evidence, not a fixed 500-character
  prefix.
- A sourced literature review must contain at least one resolved `[Doc N]`
  citation before it can be persisted.
- An output that fails validation must mark the task failed and must not change
  the current draft.
- A create request is terminal for the current agent turn once it returns
  `pending`; the plan must not promise post-generation verification in that
  turn.

### Revision

Add a distinct destructive writing tool:

```text
revise_draft(
  instructions: str,
  project_id?: UUID,
  base_version?: int,
  mode: "revise" | "citations_only" = "revise"
)
```

Contract:

- The server loads the selected base version and project documents. Draft
  content is never supplied by the model as a tool argument.
- `base_version` defaults to the current version. Supplying a version revises
  that exact durable version even if a later version is current.
- Existing base markers keep their persisted `DraftCitation` document mapping;
  current project ordering must never rebind them. If the mapped source is no
  longer available to the project, fail instead of silently substituting one.
- The tool returns a completed result with draft ID, version, word count,
  citation count, base version, and a bounded change summary. It must not return
  `pending` for the initial implementation.
- The tool is destructive, slow, and non-retryable at the outer agent layer.
- A tool receipt prevents checkpoint replay from creating a duplicate version.
- Project-query invalidation and user-facing tool labels treat it as a project
  mutation.

Revision validation:

- All `[Doc N]` indices must resolve to project documents.
- `citations_only` must add at least one resolved citation and must preserve the
  base text after citation markers are removed. Any other prose or structural
  change rejects the result.
- `revise` must reject empty output, refusal/error placeholders, unresolved
  citation markers, and an output that unexpectedly collapses the base draft.
- The new version and its `DraftCitation` rows are written in one transaction.
  The current pointer changes only after validation and successful persistence.
- Creation and revision serialize version allocation on the project row. A
  default-current revision must verify after locking that its selected base is
  still current; an explicit `base_version` intentionally remains eligible.
- On any validation, model, or persistence failure, the base/current versions
  remain unchanged and the tool returns a safe error.

### Structured citation contract

- Canonical inline syntax remains `[Doc N]`; the existing renderer and API
  already depend on it.
- Document numbering must be stable for the generation/revision call and must
  use deterministic project-document ordering, then be persisted through
  matching `DraftCitation` rows.
- A visible `[Doc N]` marker must persist `citation_index = N`; never renumber
  the distinct cited documents by encounter order.
- `citation_count` equals the number of distinct resolved document indices,
  not the number of marker occurrences.
- Human-readable location text such as `Abstract, PDF p. 1` may remain in prose
  but does not replace `[Doc N]`.

### Status and plans

Immediate patch:

- Mark `create_draft` as terminal-after-pending in both the writing instructions
  and the separate planner prompt; when it is required, it must be the final
  planned step with no same-turn dependents.
- If the model nevertheless batches a pending creation with read tools, report
  the already-completed read results through a tool-free synthesis pass; do not
  discard them or permit another tool call.
- `revise_draft` is synchronous and returns its final persisted result, allowing
  the assistant to report the version and change summary truthfully.

Follow-up durability patch:

- Move generation task state out of the process-local dictionary to the
  existing durable job infrastructure or a dedicated database-backed record.
- Polling, cancellation, and background-job UI must read the same durable
  source of truth.
- This durability migration is deliberately separate from the revision fix.

### Recovery

Add a non-destructive current-version selector in a follow-up:

```text
set_current_draft(project_id, version)
```

It must project-scope the lookup and atomically switch the unique current
pointer. Until this exists, the UI must not imply that opening an older version
restores it.

## Implementation plan

### PR 1 — prevent corruption and support real revisions

1. Add `revise_draft` to the agent tool registry, executor dispatch, writing
   subgraph, destructive/slow/no-retry policies, side-effect receipts, labels,
   project-query invalidation, completed-write fabrication guard, and trajectory
   evaluator allowlist.
2. Add a synchronous revision method to `DraftGenerationService` that loads the
   base version server-side, builds bounded source context, calls the configured
   model, validates output, extracts structured citations, and persists a new
   version transactionally.
3. Factor document-context construction so create and revise share the same
   bounded evidence budget and deterministic numbering. Preserve
   tenant/project scoping.
4. Add a pre-persistence quality gate to `create_draft`: at least one resolved
   citation and no unresolved citation markers. Fail without changing current.
5. Update `AGENTS_writing.md` so create and revise are disjoint and pending
   creation is terminal for the turn.
6. Change the `revise-draft` routing evaluation to require `revise_draft` and
   forbid `create_draft` for that case.

### PR 2 — durable status and honest progress

1. Persist generation state in the durable job store/database.
2. Expose one scoped status read used by the API, chat background jobs, and
   cancellation.
3. Reconcile plan progress with terminal tool results instead of leaving
   completed or stopped writes at `0/N`.
4. Add restart and multi-worker tests.

### PR 3 — recovery ergonomics

1. Add atomic `set_current_draft` service/API support.
2. Add a version-history restore action in the artifact UI.
3. Preserve the previous current version on authorization or transaction
   failure.

## PR 1 test matrix

Service tests:

- revising explicit v2 while v3 is current uses v2 as the base;
- citation-only output preserves prose and adds resolved markers;
- citation-only prose mutation is rejected and persists nothing;
- unresolved/out-of-range markers are rejected;
- a refusal or collapsed revision is rejected;
- model failure leaves the current pointer unchanged;
- persistence failure leaves the current pointer unchanged;
- citation rows and `citation_count` agree;
- a draft that cites only `[Doc 2]` persists citation index `2`, not `1`;
- source context includes evidence beyond character 500 while respecting the
  total context budget;
- repeated project-document reads produce the same `[Doc N]` ordering;
- existing base `[Doc N]` markers retain their stored document identity;
- a stale default-current revision is rejected, while an explicit historical
  base revision remains allowed;
- create output with no resolved citations fails without promotion.

Agent tests:

- “revise/update the saved draft” selects `revise_draft`, not `create_draft`;
- “create a new literature review” still selects `create_draft`;
- revision is HITL-gated, slow, no-outer-retry, and receipt-protected;
- completed revision reports the actual returned version/counts;
- pending creation reports only pending status and does not claim later plan
  steps completed.

Frontend tests:

- `revise_draft` uses the correct active/completed label;
- completion invalidates the project draft list, current draft, artifact, and
  citations queries;
- vN metadata and citation chips render from the persisted revision.

Acceptance test:

1. Start with a valid v2 and a deliberately bad current v3.
2. Ask NOUS to add structured citations to v2 without changing its prose.
3. Confirm the HITL action.
4. Verify the returned final version is completed, not pending.
5. Open the artifact and verify:
   - the base prose is unchanged except for `[Doc N]` insertions;
   - the benchmark claims cite *Attention Is All You Need*;
   - `citation_count >= 1`;
   - the citation chip opens the attached document;
   - the new version is current;
   - v2 and v3 remain available in history;
   - no refusal/error artifact was created.

## Validation commands

Run the smallest focused tests first, then the repository's exact changed-file
gates:

```bash
backend/.venv/bin/python -m pytest \
  backend/tests/unit/services/test_draft_revision_service.py \
  backend/tests/services/agent/test_writing_grounded_context.py \
  backend/tests/services/agent/test_writing_subgraph_tools.py \
  backend/tests/unit/agent/test_tool_receipts.py \
  backend/tests/eval/test_eval_harness.py \
  -c backend/pytest.ini -q --no-cov

pnpm --dir frontend test -- \
  src/components/context-rail/__tests__/toolLabels.test.ts \
  src/components/chat/artifact-panel/__tests__/ArtifactPanel.test.tsx

scripts/ci/run_local_ci.sh --frontend
git diff --check
```

Run CodeRabbit against the isolated branch after local tests. Treat its output
as review input; fix Critical and Warning findings, then rerun focused tests.

## Non-goals for PR 1

- Replacing the draft renderer or `[Doc N]` syntax.
- Deleting bad historical versions automatically.
- Reworking general chat citations.
- Migrating every background job to a new queue.
- Adding collaborative/manual rich-text editing.
