# Daily Research Brief workflow design

Date: 2026-09-27
Status: written design awaiting human review
Target branch: codex/agent-orchestration-repairs-20260925
Research basis: six independent Luna passes covering academic research-assistant work, evidence-review methods, data stewardship, research integrity, product translation, and repository gap analysis.

## Goal

Add a bounded, manually started Daily Research Brief workflow to the existing research engine. It should let a researcher define a question, search a small set of scholarly providers, screen the returned records, approve structured extraction, synthesize only approved evidence, verify the resulting claims, and export an inspectable brief.

The workflow imports the repeatable daily work of a responsible research assistant:

1. confirm the question and inclusion rules;
2. search documented sources and preserve the search record;
3. remove duplicates and screen candidates against the rules;
4. extract consistent fields from retained sources;
5. distinguish source statements from synthesis and inference;
6. verify every material claim against its supporting evidence;
7. record unresolved issues and human decisions;
8. package the result with enough provenance to reproduce or audit it.

The feature is a practical daily brief, not a claim of a systematic review. It must display its bounded coverage and never describe the result as exhaustive.

## Research and product rationale

The design follows recurring duties described by university research-assistant roles: literature discovery, bibliographic organization, data collection, structured analysis, documentation, and preparation of research summaries. It also imports the evidence-handling principles emphasized by PRISMA, the Cochrane Handbook, NIH and NSF data-management guidance, and ICMJE guidance on authorship and AI assistance.

Those sources imply four product rules:

- Keep a source and search manifest, because a conclusion without a search record cannot be audited.
- Separate screening, extraction, synthesis, and verification, because each stage can introduce a different class of error.
- Require accountable human decisions at consequential boundaries, because an AI system cannot be an author or final research decision maker.
- Label incomplete coverage, abstract-only evidence, uncertainty, and failed verification where the reader sees the result.

Primary references:

- PRISMA 2020 checklist: https://www.prisma-statement.org/prisma-2020-checklist
- Cochrane Handbook, Part 2: https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/part-2
- NIH data management and sharing guidance: https://www.niddk.nih.gov/research-funding/research-resources/data-management-sharing/guidance-writing-dms-plan
- NSF data management plans: https://www.nsf.gov/funding/data-management-plan
- ICMJE AI guidance: https://www.icmje.org/recommendations/browse/artificial-intelligence/
- ICMJE authorship guidance: https://www.icmje.org/recommendations/browse/roles-and-responsibilities/defining-the-role-of-authors-and-contributors.html
- University of Washington research-assistant duties: https://econ.washington.edu/research-assistantassociate-job-description
- Oregon State research-assistant duties: https://health.oregonstate.edu/faculty-staff/resources/ga-position-descriptions

## User outcome

A completed run produces these linked artifacts:

- a scope record with the question, inclusion and exclusion criteria, providers, limits, and confirmation;
- a search manifest with provider, query, retrieval time, limit, returned count, and warnings;
- a deduplicated screening queue with machine recommendations and human decisions;
- an extraction matrix with a human accept or reject decision for every retained record;
- a claim map from each synthesized claim to exact evidence identifiers and quotations;
- a verification result with claim-level status and coverage;
- a verified Markdown brief plus JSON and CSV exports;
- a run manifest containing contract versions, blueprint identity, model/provider settings, stage hashes, review decisions, and honest coverage limits.

A run may also produce an unverified draft after a verification failure through the hardened continuation path. That artifact must remain visibly marked as unverified and cannot receive final approval.

## Scope

### Included in version 1

- one manually started run at a time per user action;
- a question, inclusion rules, exclusion rules, and optional notes;
- one to four research-engine connectors;
- a maximum of 50 requested results per provider;
- OpenAlex and Crossref selected by default, at 25 requested results each;
- the existing search, screen, extract, synthesize, verify, and export step types;
- durable human review gates after screening, extraction, and the verified export;
- persisted run rehydration after reload or reconnect;
- Markdown, JSON, and extraction CSV downloads;
- source, claim, evidence, review, and coverage provenance;
- exact ownership checks on every run, review, resume, and export operation.

### Explicitly excluded from version 1

- scheduled or background monitoring;
- a "new since last run" promise;
- living or systematic review claims;
- exhaustive database coverage;
- dual independent screening or conflict adjudication between reviewers;
- meta-analysis, statistical pooling, risk-of-bias instruments, or GRADE;
- citation-network expansion;
- paywalled full-text acquisition;
- contacting authors, participants, institutions, or publishers;
- participant recruitment, clinical, animal, or laboratory operations;
- automatic publication or external sharing;
- free-form mutation of model extraction records during review.

The current connectors accept a query and limit but do not share a reliable date or cursor contract. Version 1 therefore records retrieval time and provider limits, sets coverage.exhaustive to false, and never claims cross-run novelty.

## Existing capabilities to reuse

The repository already contains the important evidence contract and execution primitives:

- backend/src/services/research_engine/contracts.py defines strict versioned envelopes, evidence identifiers, exact quotation grounding, and typed stage outputs;
- backend/src/services/research_engine/step_executor.py implements typed screen, extract, synthesize, verify, and export execution with batching and budgets;
- backend/src/services/research_engine/verification.py and report_rendering.py provide semantic checks, coverage, and visible unverified report behavior;
- backend/src/services/research_engine/export_service.py reconstructs persisted typed output and existing provenance;
- backend/src/services/research_engine/connectors contains bounded scholarly search providers;
- backend/src/models/research_run.py already has a reproducibility_manifest suitable for the pending-review descriptor and scope confirmation;
- backend/src/api/research_engine/runs.py already supports pause, resume, persisted-stage recovery, SSE events, and verification override;
- backend/src/api/research_engine/steps.py already exposes persisted steps.

The feature must extend these units. It must not add a second evidence model, verification implementation, export truth source, or run state machine.

## Current gaps this design closes

Repository inspection found the following integration gaps:

- template listing returns metadata only, while TemplateSelector can consume steps and parameters, so selecting a template may create an empty editor;
- there is no template-detail route;
- BlueprintEditor omits template_source when it saves;
- StepCard exposes legacy analyze and filter options while omitting the valid screen, verify, and export step types;
- SourceSelector does not enforce the supported four-provider maximum;
- no durable human stage-review record or gate exists;
- the run page depends primarily on live SSE and does not reliably rebuild from persisted steps after reload;
- there is no owned research-run download endpoint or extraction CSV export;
- the UI cannot distinguish a manual pause from a review pause with the stage, hash, and decision required to continue.

## Workflow definition

Add backend/src/services/research_engine/blueprints/templates/daily_research_brief.yaml with contract_version 1 and the following ordered steps:

1. search
2. screen
3. extract
4. synthesize
5. verify
6. export

The template declares:

- template_source: daily_research_brief;
- default providers: openalex and crossref;
- default limit_per_provider: 25;
- min providers: 1;
- max providers: 4;
- hard maximum limit_per_provider: 50;
- screen.review_gate: screening;
- extract.review_gate: extraction;
- export.review_gate: final;
- export formats: markdown, json, csv;
- coverage.exhaustive: false.

The YAML schema is validated through BlueprintLoader before it can be returned or instantiated. Invalid bundled templates fail startup or the focused template-contract check rather than reaching the editor.

## Scope confirmation

Starting this template requires a scope-confirmation payload containing:

- research_question;
- inclusion_criteria as a non-empty bounded list;
- exclusion_criteria as a bounded list;
- selected provider identifiers;
- limit_per_provider;
- optional notes;
- confirmation boolean.

The API first merges the server-loaded blueprint defaults with the allowed RunCreate overrides, then validates the confirmation against that effective configuration. It canonicalizes the effective scope, computes a SHA-256 configuration hash, and stores the canonical values plus actor ID and timestamp in reproducibility_manifest.scope_confirmation. The client never supplies the hash. The run cannot enter running state without confirmation.

Extend the existing RunCreate schema with an optional typed scope_confirmation field. It is required only when the selected blueprint has template_source daily_research_brief, which preserves compatibility for every existing blueprint.

The confirmation is a record of what the researcher asked the system to do. It is not a waiver and does not turn bounded provider results into comprehensive coverage.

## Human review model

Add an append-only research_stage_reviews table and model. Each row contains:

- id: UUID primary key;
- owner_id, copied from the owning ResearchProject;
- nullable organization_id, copied from the owner when present;
- run_id;
- step_index;
- stage_type: screen, extract, or export;
- review_kind: screening, extraction, or final;
- reviewer_id;
- output_hash;
- decision: approve or decline;
- decision_payload: JSONB;
- optional note;
- created_at.

Use foreign keys to the run, owner, and reviewer where repository conventions support them. Add indexes for run_id plus step_index, owner_id, reviewer_id, and organization_id. Add a uniqueness constraint on run_id, step_index, output_hash, and review_kind. Version 1 remains owner-only: organization membership by itself does not grant access to another user's ResearchProject.

Rows are immutable through the public API. A superseding decision for a newly generated stage output has a new output_hash and therefore a new row. Version 1 does not expose deletion or update endpoints.

Use the existing ResearchStep.outputs_hash after confirming that it covers the canonical persisted output envelope and contract version; repair that calculation in one place if it does not. The review route accepts the hash the reviewer saw and rejects a stale hash. This prevents a review of one output from authorizing a regenerated output.

### Screening review

The screen stage persists its original typed output, then pauses the run with review_kind screening.

The reviewer assigns each candidate one of:

- include;
- exclude, with a required reason code and optional note;
- unresolved.

Approval is allowed only when every candidate has a final include or exclude decision. The decision payload must refer only to record IDs present in the stage output, once each. It cannot add a source, change a citation, or replace evidence text.

The approved overlay, rather than mutation of the original model output, becomes the input to extraction. The manifest records the included and excluded counts and the review row ID.

### Extraction review

The extract stage persists its original typed extraction output, then pauses with review_kind extraction.

The reviewer assigns every extracted record one of:

- accept;
- reject, with a required reason;
- unresolved.

Approval is allowed only when no record remains unresolved. Only accepted records become synthesis input. Rejected records and reasons remain in the audit export. Version 1 intentionally does not allow field-by-field editing because such edits would need their own provenance and verification semantics.

If the reviewer rejects all records, the run cannot synthesize a brief. It ends with a clear no-evidence outcome and keeps the search, screening, extraction, and review artifacts.

### Final review

After verification passes, the export stage builds and persists the exact reader-facing artifacts, then the run pauses with review_kind final. The reviewer can approve or decline that exported verified brief. Approval references the export output hash, which in turn binds the verification output and report hash.

A final approval is valid only when:

- the verify stage passed under the version-1 typed contract;
- every material claim has the required evidence references;
- no required prior gate is missing or stale;
- the export being approved was derived from those exact persisted outputs and overlays.

If verification fails, the engine persists the verification output and pauses with pause_reason verification_failed. Continuing requires an explicit continue_unverified choice bound to that verification output hash; opening the stream or sending an ordinary resume is not consent. That path skips the final-approval gate, must not create a final approval row, and must preserve the unverified banner and failed checks in every rendered or downloaded form.

The export envelope must carry the verification output hash and report hash explicitly. After an approved final review, resume recognizes that export is the last step and marks the run completed without regenerating the artifact. A no-evidence terminal path also bypasses final approval, records final_status no_evidence, and exposes audit exports without presenting a research conclusion.

## Orchestration and prompt contract

ResearchEngine remains the sole workflow orchestrator. Individual stages do not discover or call one another, recursively delegate work, or infer hidden state. The blueprint order, typed envelopes, persisted hashes, and review overlays are the explicit handoff contract.

At run start, the orchestrator resolves the selected connector identifiers through the connector registry and records a bounded capability manifest. Prompts receive only the capabilities active for that run. They must not contain a hard-coded list that can drift from the registry or imply access to providers, full text, dates, cursors, or external actions that are unavailable.

Expose the same safe registry projection through a read-only capabilities endpoint. It returns canonical connector ID, display label, Daily Research Brief eligibility, availability, and supported search features such as full_text, date_filter, and cursor. It does not expose credentials, configuration values, internal URLs, or the legacy web alias. SourceSelector consumes this response instead of maintaining its own provider list, and the backend still rejects any unregistered or ineligible identifier.

Each model-backed stage receives the confirmed scope snapshot, its declared upstream typed envelope, evidence-level metadata, stable source and evidence IDs, its output schema, and the current budget. It does not receive unrelated project content or mutable browser state.

- screening may recommend inclusion or exclusion but cannot make the human decision;
- extraction must retain stable source IDs and exact quotations where the contract requires evidence;
- synthesis receives only human-accepted extraction records and may emit only claims linked to known evidence IDs;
- verification receives the claim map and source evidence, reports failures, and cannot rewrite a claim to make it pass;
- export is deterministic and model-free, consuming persisted typed outputs and review overlays.

Prompt templates state the stage role, allowed inputs, forbidden actions, evidence rules, uncertainty behavior, and exact output contract. They prohibit fabricated citations, unsupported full-text claims, external outreach, and claims of exhaustive or newly updated coverage. Production logs record prompt-template version and hash, never prompt content.

Contract tests compare the step enum, executor dispatch, blueprint validation, connector registry, API schemas, generated frontend types, StepCard options, and bundled-template steps. A mismatch fails CI. This is the mechanism that keeps every agent-facing stage and tool-facing connector aware of the same supported capability graph.

## Run state and engine behavior

Keep the existing pending, running, paused, completed, and failed run states. Review is a typed reason for paused rather than a new top-level status.

RunResponse gains bounded optional pause_reason, review_kind, step_index, and output_hash fields derived from the manifest. They contain no research content and let a reload distinguish user pause, review_required, and verification_failed without relying on a missed SSE frame.

When a gated stage completes, the engine must perform the persistence and state transition atomically:

1. persist the typed ResearchStep output;
2. compute its canonical output hash;
3. write pending_review to reproducibility_manifest with run ID, step index, stage type, review kind, output hash, and created time;
4. transition the run to paused;
5. emit a run_paused SSE event containing pause_reason: review_required and the same non-sensitive descriptor; the database status and manifest remain durable if the event is missed;
6. stop before invoking the next stage.

A review approval does not itself execute the next stage. It appends the review row and atomically marks pending_review as approved with the review ID. The descriptor remains available across a lost response or browser reload. The existing resume route validates it, moves the reference into manifest review history, consumes pending_review, and then advances from the persisted stage boundary or completes the run when the approved export was the last step.

POST resume must refuse continuation when the current gated output does not have a matching approved review. Manual pause and review pause remain distinguishable. Double submission is idempotent for the same canonical decision; a different decision for the same unique gate returns conflict rather than rewriting history.

On resume and cold rehydration, the engine loads persisted step envelopes and applies approved review overlays at the boundary between stages. Original ResearchStep outputs remain unchanged.

## API design

### Templates

Add:

GET /api/v1/research-engine/blueprints/templates/{slug}

The response contains the validated full template: metadata, contract version, parameters, steps, defaults, and constraints. Unknown slugs return 404. The existing template-list route remains a small metadata collection and may include a detail URL.

Blueprint creation from a template persists template_source and the concrete server-expanded steps. A bundled template slug is trusted only when the server loaded and validated it; the server does not accept arbitrary client steps under a known template_source. The editor may change declared user parameters, but it cannot remove or reorder the required Daily Research Brief stages or review gates. A custom step topology clears template_source and becomes a normal custom blueprint. Later bundled-template edits do not silently alter an existing blueprint.

### Capabilities

Add:

GET /api/v1/research-engine/capabilities

The response is the safe connector-registry projection used by template validation, the setup UI, and the runtime capability manifest. Alias entries are normalized to their canonical provider and cannot be selected twice. Availability is descriptive at request time; the run still records actual provider success or failure.

### Reviews

Add:

GET /api/v1/research-engine/runs/{run_id}/reviews/pending

This returns either no pending review or the owned run's descriptor plus the bounded stage output needed to render the review UI, any previously accepted decision for that exact hash, and validation vocabulary. It never returns another tenant's run.

Add:

POST /api/v1/research-engine/runs/{run_id}/reviews/{step_index}

The request includes review_kind, output_hash, decision, decision_payload, and optional note. The server validates ownership, run state, step identity, contract version, exact item set, allowed decisions, required reasons, and hash freshness. It inserts the append-only review row and returns the accepted review plus whether the request was an idempotent replay.

The route cannot create new sources, claims, evidence, or citations. It can only select or reject identifiers present in the persisted stage output.

### Resume

Keep:

POST /api/v1/research-engine/runs/{run_id}/resume

Add a bounded optional RunResumeRequest. Ordinary review continuation needs no special flag and still requires a matching approved row. Continuing after verification failure requires continue_unverified true plus the exact failed verification output hash; the server records actor, time, and hash in the manifest. Add the gate check before changing state. If a pending review has no matching approved row, or a verification override is absent or stale, return 409 with a stable error code and the content-free pause descriptor.

### Exports

Add:

GET /api/v1/research-engine/runs/{run_id}/export?format=markdown|json|csv

The server checks ownership and rebuilds the artifact from persisted step envelopes, approved overlays, and the manifest. It does not trust client-supplied report content.

- markdown returns the reader-facing brief and provenance appendix;
- json returns the complete versioned run artifact;
- csv returns the approved extraction matrix, including stable source ID, bibliographic fields, extracted fields, evidence level, human decision, and rejection reason when applicable.

Exports are available only for terminal completed runs. An unverified completed draft is downloadable only with its unverified status intact. Use safe deterministic filenames and content disposition. Bound exported cell values and neutralize spreadsheet formula prefixes in CSV.

## Frontend behavior

### Template selection and blueprint editing

TemplateSelector fetches full template detail before applying it. BlueprintEditor persists template_source. StepCard exposes only the step types accepted by the current backend contract, including screen, extract, synthesize, verify, and export. SourceSelector renders the capabilities response, enforces one to four eligible canonical providers, and shows the per-provider limit.

The Daily Research Brief setup uses plain labels:

- Research question
- What to include
- What to exclude
- Sources
- Results per source
- Confirm scope

It displays: "This is a bounded brief from the selected sources. It is not an exhaustive or systematic review."

### Review workspace

RunView renders a review panel when pause_reason is review_required.

For screening it shows citation metadata, abstract availability, machine recommendation, and include, exclude, or unresolved controls. For extraction it shows each original extraction record and accept, reject, or unresolved controls. For final review it shows the verified brief, claim coverage, failed or uncertain checks, and the approval action.

The browser holds draft review choices locally until submission. Server validation remains authoritative. The UI disables approval while unresolved items remain and requires a reason for exclusions and rejections.

### Reload and reconnect

On mount, the run page fetches the run and listSteps, sorts persisted steps by step_index, and builds the complete visible state before attaching SSE for newer events. Step events are merged idempotently by persisted step identity, while run status is reconciled with the latest run response, so reconnect does not duplicate stages.

A reload during a review pause fetches the pending review and restores the server-accepted state. Unsaved local choices are not claimed to be durable.

### Results and downloads

A completed verified run shows:

- coverage and provider counts;
- included and excluded source counts;
- accepted and rejected extraction counts;
- verified claim coverage;
- unresolved limitations;
- download actions for Markdown, JSON, and CSV.

A no-evidence run shows why no brief was produced and still offers the audit JSON and extraction CSV when applicable.

## Artifact and provenance contract

The JSON export is the canonical portable artifact. It includes:

- artifact_version;
- run and blueprint identifiers;
- template_source and template contract version;
- scope confirmation and configuration hash;
- provider search manifests and warnings;
- deduplication information;
- original typed stage envelopes and hashes;
- review overlays and review identifiers;
- claim and evidence maps;
- verification results and coverage;
- final status: verified, unverified, or no_evidence;
- model/provider configuration already allowed by the reproducibility manifest;
- generation and review timestamps;
- limitations and exhaustive: false.

The Markdown brief presents the research question, concise findings, claim-linked citations, uncertainty, method and coverage, limitations, and a provenance appendix. It must not hide failed checks behind a generic success heading.

The CSV is a convenience view. The JSON artifact remains the full audit record.

## Evidence-level display

Each source or extracted record exposes the strongest evidence basis available to the engine, using a small explicit vocabulary such as full_text, abstract, metadata_only, or workspace_document. This is descriptive provenance, not a quality score.

Synthesis and verification receive this field. The report identifies conclusions supported only by abstracts or metadata. A metadata-only record cannot supply an exact evidentiary quotation and therefore cannot independently support a material claim under the existing typed evidence contract.

## Error and edge-case handling

- Provider timeout or partial failure: retain successful provider results, record the failed provider and error class, and lower visible coverage. If every provider fails, fail before screening.
- Duplicate results: preserve provider occurrence metadata while screening one canonical record.
- Missing abstract or text: retain bibliographic metadata for screening, mark evidence level, and prevent unsupported quotation claims.
- Empty screen inclusion: finish as no_evidence after the screening audit rather than calling later model stages.
- All extractions rejected: finish as no_evidence and retain the rejection audit.
- Stale review tab: reject the stale output hash with 409 and return the current pending descriptor.
- Double review submission: return the original accepted row for an identical decision; conflict on a different decision.
- Browser disconnect: persisted step, run status, and pending-review descriptor remain the truth; SSE is only a notification channel.
- Review declined: keep the run paused with a recorded decline; the user may revise the blueprint and start a new run. Version 1 does not mutate prior stage output in place.
- Verification failure: leave the run paused until an exact-hash continue_unverified request is accepted; then export and complete the visibly unverified draft without a final approval gate.
- Export reconstruction failure: return a stable server error, keep the completed run unchanged, and log identifiers without research content.

## Security and privacy

The existing owned ResearchProject join remains the authoritative access boundary for every new route. The stored organization is an audit snapshot and a signal for anomaly checks; it does not replace owner authorization or lock an owner out if their organization later changes. Organization membership alone grants no access. Cross-user and cross-organization unknown or inaccessible IDs use the repository's non-enumerating behavior.

Review validation uses server-loaded persisted output and canonical hashes. The client cannot approve unseen content, insert evidence, or authorize a different regenerated stage. Transactions protect review insertion and every pending-review state transition against races.

Logs and metrics may include run ID, organization ID, step index, review kind, status, counts, duration, and error category. They must not include the research question, criteria, abstracts, extracted content, quotations, prompts, model responses, review notes, or exported report body.

CSV export neutralizes leading equals, plus, minus, tab, and at-sign characters where spreadsheet applications may interpret formulas. Download filenames contain no research question.

No connector gains new credentials or network scope through this feature.

## Observability

Add counters and timings for:

- daily brief runs started and terminal outcomes;
- provider success, partial failure, and returned counts;
- candidates before and after deduplication;
- review pauses, approvals, declines, and stale-hash conflicts by review kind;
- time waiting for review;
- accepted and rejected extraction counts;
- verification pass, fail, and override;
- export generation by format and failure class;
- reload rehydration and SSE reconnect errors.

Operational dashboards must use identifiers and aggregates only.

## Testing strategy

### Contract and service tests

- bundled daily_research_brief validates and contains the exact ordered steps;
- template list remains metadata-only and detail returns the full template;
- the capabilities response matches the canonical connector registry, hides aliases and secrets, and drives server validation;
- blueprint creation persists template_source and expanded steps;
- invalid step types, more than four providers, and limits above 50 fail validation;
- scope confirmation canonicalization and hash are deterministic;
- each gated stage persists output before pause and records the exact descriptor;
- screening decisions must cover the exact candidate set and require exclusion reasons;
- extraction decisions must cover the exact record set and require rejection reasons;
- all-rejected and no-inclusion paths produce no_evidence without later model calls;
- final approval is impossible after failed verification or an override;
- an ordinary resume or direct stream cannot override a failed verification;
- continue_unverified is accepted only for the current failed verification hash and is audited;
- review replay is idempotent, conflicting replay fails, and stale hash fails;
- cross-user and cross-organization review, resume, pending, and export calls fail closed;
- resume refuses every missing or stale required gate;
- rehydration applies overlays without changing original step output;
- Markdown and JSON exports preserve verification status and exact evidence links;
- CSV contains approved and rejected extraction audit rows and neutralizes formulas;
- exports reconstructed twice from the same persisted state are byte-stable except explicitly documented timestamp fields.

### API and migration tests

- Alembic upgrade and downgrade for research_stage_reviews;
- uniqueness and foreign-key behavior;
- OpenAPI includes capabilities, template detail, pending review, review submission, and export routes;
- generated frontend API types match the checked-in OpenAPI snapshot;
- 404 and 409 errors use stable codes without content leakage;
- completed-only export enforcement;
- partial-provider and abstract-only fixtures retain accurate coverage labels.

### Frontend tests

- selecting Daily Research Brief loads real steps and defaults;
- saving keeps template_source;
- StepCard offers only valid current step types;
- SourceSelector uses server capabilities, blocks aliases or unavailable choices, a fifth provider, and an excessive limit;
- scope confirmation is required before start;
- persisted steps render after reload without an SSE replay;
- persisted and streamed stages merge without duplicates;
- screening and extraction approval remain disabled with unresolved records;
- required reasons are enforced;
- stale review responses refresh current state;
- a reload reconstructs user, review, and verification-failure pause reasons from RunResponse;
- verified, unverified, and no-evidence results render distinct labels;
- all download actions use the owned export endpoint.

### Integrated lifecycle tests

Run a real local browser-to-worker lifecycle with controlled connector and model fixtures:

1. create a blueprint from Daily Research Brief;
2. confirm scope and start;
3. receive search and screening output;
4. reload during the screening pause;
5. approve screening and resume;
6. reload during extraction review;
7. approve and reject extraction records, then resume;
8. reach verification and final review;
9. approve, complete, and download all formats;
10. verify artifact hashes, evidence links, and review references.

Add separate paths for provider partial failure, no evidence, verification failure, unverified override, reconnect, stale review, duplicate submission, and cross-tenant denial.

Use current production model configurations for a bounded evaluation set after deterministic tests pass. Score evidence citation correctness, unsupported-claim rate, gate compliance, coverage-label accuracy, and export reconstruction.

## Performance and reliability checks

Before ship, measure:

- provider fan-out and bounded result latency at one, two, and four providers;
- screen and extraction batch latency at 50 results per provider;
- memory and serialized payload sizes at the maximum supported run;
- review-page render time for the maximum candidate count;
- cold reload and persisted-step hydration;
- export generation size and latency;
- pause/resume concurrency and duplicate review submissions;
- a soak of repeated full lifecycles with reconnects.

Set acceptance thresholds in the implementation plan from a measured baseline. Do not invent thresholds in this design document.

## Rollout and compatibility

The feature is additive and can ship behind the repository's existing feature-control mechanism if one applies to research engine pages.

Existing blueprints and runs without review_gate parameters retain their current behavior. Existing runs without scope_confirmation or research_stage_reviews remain readable and exportable through their legacy contracts but cannot be represented as an approved Daily Research Brief.

The new review model and routes deploy before the frontend enables the template. Regenerated OpenAPI and frontend types ship in the same change. Rollback disables template selection first, then rolls back application code while leaving the additive review table intact until the normal migration rollback window.

## Ship evidence

This feature is not ready to ship until all of the following evidence is attached to the candidate commit:

- integrated local CI completes successfully;
- remote required checks run against the exact candidate SHA;
- authenticated security scanning and fresh dependency-profile installs pass;
- the real browser-to-worker lifecycle passes, including reload and resume at every gate;
- current-model evaluations meet the plan's evidence-quality thresholds;
- maximum-bound performance and soak checks pass;
- deployment configuration and feature exposure are verified;
- rollback is rehearsed or demonstrated with the candidate artifacts;
- the observed branch rules require the checks the release process depends on.

These items directly close the outstanding release gaps recorded in docs/testing/agent-orchestration-repair-verification.md. A passing focused test suite alone is not a ship decision.

## Files expected to change during implementation

The implementation plan should confirm exact locations, but expected areas are:

- backend/src/services/research_engine/blueprints/templates/daily_research_brief.yaml
- backend/src/services/research_engine/blueprints/loader.py
- backend/src/services/research_engine/contracts.py
- backend/src/services/research_engine/connectors/registry.py
- backend/src/services/research_engine/engine.py
- backend/src/services/research_engine/step_executor.py
- backend/src/services/research_engine/export_service.py
- backend/src/services/research_engine/report_rendering.py
- backend/src/api/research_engine/blueprints.py
- backend/src/api/research_engine/__init__.py or a focused capabilities route
- backend/src/api/research_engine/runs.py
- backend/src/api/research_engine/steps.py or a focused reviews route
- backend/src/schemas/research_engine.py
- backend/src/models/research_stage_review.py
- backend/src/models/__init__.py
- one Alembic migration
- focused backend unit and integration tests
- backend/openapi.json and generated frontend types
- frontend/src/components/research-engine/TemplateSelector.tsx
- frontend/src/components/research-engine/BlueprintEditor.tsx
- frontend/src/components/research-engine/StepCard.tsx
- frontend/src/components/research-engine/SourceSelector.tsx
- frontend/src/components/research-engine/RunView.tsx
- frontend/src/store/research-engine-store.ts
- research-engine service client and frontend tests
- browser lifecycle tests and release evidence documentation

## Acceptance criteria

The feature is complete when a researcher can start the bundled Daily Research Brief, confirm a bounded scope, make durable screening and extraction decisions, resume safely after each review, approve only a successfully verified result, reload at any point without losing server state, and download provenance-complete Markdown, JSON, and CSV artifacts.

At every surface the system must tell the truth about provider bounds, evidence level, human decisions, verification status, and incomplete coverage. No successful UI state may imply that a bounded daily brief is exhaustive, systematic, or newly updated since a prior run.
