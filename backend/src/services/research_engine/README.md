# Research engine services

Backend of the structured research engine behind `/api/v1/research-engine/...` (the largest path group in `backend/openapi.json`, served by `backend/src/api/research_engine/`). It runs versioned blueprints as durable runs and steps, and layers the evidence-bearing review stages on top: protocol, search, screening, acquisition, appraisal, evidence tables, synthesis, reruns and scheduled updates. Many modules cite their GOO ticket in the docstring.

## Pattern

Each stage is a pair: a pure `*_rules.py` module (no database, no FastAPI, deterministic) and a `*_service.py` module that owns persistence and one transaction boundary per method. Architecture tests under `backend/tests/unit/architecture/` (for example the appraisal, evidence, experiment, rerun, review-update, search-update and synthesis boundary tests) keep routers from committing or importing across resources.

## Layout

| Area | Files |
| --- | --- |
| Engine and runs | `engine.py`, `step_executor.py`, `run_lifecycle.py`, `run_conformance.py`, `blueprints/` (loader and templates), `providers/` (Claude, OpenAI, Ollama LLM providers), `contracts.py` |
| Access and scope | `project_access.py` (canonical Collection access, built on `services/threads/workspace_access.py`), `scope.py` |
| Protocol and review | `protocol_service.py`, `protocol_methods.py`, `review_service.py`, `review_update_service.py`, `review_update_rules.py` |
| Discovery and corpus | `discovery.py`, `connectors/` (arXiv, Crossref, OpenAlex, PubMed, Semantic Scholar, RAG store), `search_import.py`, `search_receipts.py`, `corpus_service.py`, `corpus_export.py`, `identity_service.py`, `report_identity.py` |
| Screening and acquisition | `screening_service.py`, `screening_rules.py`, `acquisition_service.py`, `prisma.py`, `prisma_service.py` |
| Appraisal, evidence, synthesis | `appraisal_service.py`, `appraisal_rules.py`, `evidence_service.py`, `evidence_rules.py`, `synthesis_service.py`, `synthesis_rules.py` |
| Manifests and reruns | `experiment_service.py`, `manifest_rules.py`, `rerun_service.py`, `rerun_rules.py`, `search_update_service.py`, `search_update_rules.py` |
| Output | `report_rendering.py`, `export_service.py`, `audit_bundle.py`, `journey.py`, `verification.py`, `observability.py`, `prompt_batches.py`, `prompt_context.py`, `source_persistence.py` |

## Rules

- Resolve every project through `project_access.py`; project (Collection) ownership is `Workspace.owner_id`, and a client-supplied project id must be verified before use.
- Reviews and receipts are append-only and bound to exact hashes; do not add update paths that rewrite them. Add a superseding version instead.
- Log content-safely (`observability.py`); never put source text or prompts in logs.
- Tasks that drive runs live in `backend/src/tasks/research_tasks.py` and `research_run_tasks.py`; scheduled search updates run from `search_update_tasks.py` and are a no-op unless `SEARCH_UPDATES_ENABLED`.
