# Project improvement audit, 2026-11-13

Read-only audit of the whole repository at `develop` @ `8b8317c24` (2026-11-13). No application code, branch, PR, repository setting, cluster or database was changed. This is a dated record: re-verify a finding against current code before acting on it.

[Audits index](README.md) · [Current engineering contracts](../engineering/README.md)

## Method and coverage

- Static reading of the tree, plus read-only `gh` queries for Dependabot alerts, workflow runs, branch protection and rulesets.
- Nothing was run against the live cluster, AWS, or any external database. `helm`, `terraform` and `gitleaks` are not installed on the audit host, so charts and Terraform were read by hand, and the history secret scan used 16 high-signal patterns only.
- Every finding carries `path:line` on `develop`, a command with its output, or a run link. **UNVERIFIED** marks anything not confirmed.
- Uneven depth, stated so the gaps are not read as "clean":
  - Backend agent, retrieval, knowledge graph, Celery: reviewed in depth.
  - **SSE streaming and run durability** (`api/agent/streaming.py`, `execute.py`, `agent_run_service`, `agent_submission_service`): only spot-checked (event sequence allocation and the confirm claim look sound). Treat as unaudited.
  - Security: AST pass over every route for a missing auth dependency, then targeted IDOR checks. Not a line-by-line review of all handlers.
  - **Frontend and CI**: a lighter pass than the other areas (dependency, ratchet, error-boundary, accessibility heuristics; workflow triggers, pins, repository settings, run logs). Chat-store race conditions, hydration and bundle size were not analysed.
- Effort: S is under a day, M is one to three days, L is a week or more.

## Top 10, ranked by impact × likelihood

### 1. The agent has no working content retrieval in live dev, and reports it as "nothing found"

- **Evidence**
  - `backend/src/services/agent/tools_impl.py:3647-3648`: when `DO_KB_ENABLED` is off and the call is unscoped, `do_kb_retrieve` returns `{"chunks": [], "total": 0, "source": "do_kb", "reason": "disabled"}` with no `error` key, so the tool layer records a successful empty result. Live dev sets the flag to `"false"` (`infrastructure/helm/knowledge-graph-analytics/values-aws.yaml:141-144`).
  - `backend/src/services/agent/_nodes_rag.py:1706-1708`: `rag_node` returns `retrieved_contexts: []` whenever no project is active.
  - `backend/src/services/agent/tools_impl.py:3445-3451`: `search_documents` is a title/filename `ILIKE` on the whole query; its zero-hit hint (`:3485-3491`) tells the model to retry with `do_kb_retrieve`, the disabled tool.
  - `backend/src/services/search/fulltext_search_service.py:371`: the only live backend uses `plainto_tsquery`, which ANDs every lexeme of the user's sentence. Effect on hit rate is UNVERIFIED (no database), but a natural-language question needs every stem in one document.
  - `backend/src/services/agent/_nodes_rag.py:1430-1432`: `except Exception: ... return []` covers database errors and the 15s timeout; `_nodes_classify.py:339-342` replaces a raised RAG subtask with `{"retrieved_contexts": []}`. Nothing in state distinguishes "failed" from "empty".
- **Impact**: a topical question in a chat with no project gets no RAG, a title miss, then an empty "disabled" result, and the model answers that no documents exist while they are indexed. This is the code cause of the "empty retrieval" LangSmith finding.
- **Fix**: route unscoped `do_kb_retrieve` to org-scoped Postgres full-text when the KB is off (the scoped branch at `tools_impl.py:3731-3751` already does); AND-then-OR fallback in full-text search, changing the count query with it; add a `retrieval_status` field (ok / empty / error / skipped) to agent state and surface it in the prompt and trace.
- **Effort**: M

### 2. Nothing alerts on the live lane: Sentry is mis-wired, no metrics endpoint exists, alarm delivery is unconfirmed

- **Evidence**
  - `values-aws.yaml:246-247` sets `SENTRY_TRACES_SAMPLE_RATE` to `"1.0"` with no sampler dropping health checks (`backend/src/observability/sentry.py:83-97`); probes plus 20-minute synthetic traffic (`values-aws.yaml:431-434`) spend quota. This matches the 2026-11-08 quota blindness.
  - `SENTRY_DSN` is declared nowhere in the chart or values, and a missing DSN is a silent no-op (`sentry.py:74-77`). Whether it exists in Infisical is UNVERIFIED.
  - `init_sentry` is called only from `backend/src/main.py:142-144`. `backend/src/tasks/celery_app.py` has no Sentry, so worker and beat failures never reach it.
  - `/metrics` is defined only in `setup_observability` (`backend/src/observability/integration.py:247,261`), which has no caller (`grep -rn setup_observability backend/src` finds only the definition and a README). The pod `prometheus.io/*` annotations (`templates/backend-deployment.yaml:31-33`) point at nothing, and `values-aws.yaml:388-395` disables Prometheus and Grafana anyway.
  - CloudWatch alarms all route to one SNS topic (`infrastructure/terraform/monitoring.tf:176,270-290`); [`2026-10-20-eks-backup-recovery.md`](2026-10-20-eks-backup-recovery.md) (lines 111-114) records its email subscription as `PendingConfirmation`. Container Insights alarms have no dimensions (`monitoring.tf:166-198`), and the Redis memory threshold is 4 GB on a `cache.t4g.small` (`monitoring.tf:252`).
  - The only live detector is `.github/workflows/verify-dev-health.yml:15-16`: a 30-minute cron on `/health`.
- **Impact**: an outage in the worker, database or graph is found by a user.
- **Fix**: sample around 0.1 with a `traces_sampler` that drops `/health*`; initialise Sentry in Celery worker and beat signals; log at ERROR when the DSN is absent; confirm the SNS subscription; add ALB 5xx, unhealthy-target and RDS-connection alarms with correct dimensions; either mount `/metrics` behind auth with a scraper, or delete the annotations and the compose-only `monitoring/` rules that query metrics the backend does not export.
- **Effort**: M

### 3. The database credential path is fragile and over-privileged

- **Evidence**
  - `templates/aws-rds-secret.yaml:33,43-48` builds `DATABASE_URL` from the rotating RDS master secret (5-minute refresh); pods consume it through `envFrom` (`values-aws.yaml:123-125`). No reloader or checksum annotation exists, and `infrastructure/kubernetes/aws-addons.md:157-161` says a manual restart is needed after each rotation. Rotation cadence is UNVERIFIED.
  - The API, workers and the migration Job all connect as the RDS master user (`values-aws.yaml:40-45`).
  - There is no tenant-level row security: `backend/src/middleware/multi_tenancy.py:13-21` says isolation is a per-query convention; Alembic only enables RLS with zero policies to lock out PostgREST roles (for example `backend/alembic/versions/rl01_secure_late_internal_tables_rls.py:45`); no `CREATE POLICY` and no tenant GUC exist.
  - `infrastructure/terraform/main.tf:484-486` sets `log_statement = all`, writing every statement, user content included, to RDS logs.
- **Impact**: after a rotation, every new connection fails until someone restarts the pods while `/health` stays green. One query missing its `organization_id` filter, or one injection, reaches every tenant with DDL rights.
- **Fix**: add a secret-hash annotation or Stakater Reloader; create a non-owner application role with a static credential; then add org policies on documents, threads, messages and agent runs with `SET LOCAL app.org_id` in `get_db` and `tool_session`.
- **Effort**: M for the role and restart, L for RLS

### 4. Search and knowledge-graph failures are returned as empty successes

- **Evidence**
  - `backend/src/services/search/hybrid_search_service.py:68`: `_raise_if_all_sources_failed` returns early if any source reports success. The graph arm reports success with Neo4j down (next bullet), so a failed or timed-out full-text arm yields an empty 200 instead of the fallback or a 503.
  - `backend/src/services/knowledge_graph/knowledge_graph_service.py`: `search_entities` `:1284-1286`, `get_entity` `:880-882`, `update_entity` `:956-958`, `delete_entity` `:989-991`, `count_entities` `:1436-1438`, `find_related_entities` `:2077-2079`, `get_neighborhood` `:2247-2249` and `get_graph_analytics` `:2869-2871` catch everything and return `[]`, `None` or `0`.
  - No driver or query timeout on Neo4j (`knowledge_graph_service.py:542-547`); the breaker counts only `ServiceUnavailable` and `SessionExpired` (`:599-603`).
  - `hybrid_search_service.py:122`: the candidate pool is capped at 20, while research-engine callers ask for 50 (`backend/src/api/research_engine/runs.py:288`).
- **Impact**: a Neo4j outage shows as 404 "Entity not found", empty lists, and a healthy `/search/health`. A hung Neo4j starves the threadpool and never opens the breaker.
- **Fix**: base the all-failed check on content-bearing sources; re-raise in the graph read methods as `get_all_entities` (`:1390-1392`) does and map to 503; add driver and per-query timeouts; fetch `max(cap, offset + limit)`.
- **Effort**: S to M (tests currently assert the empties)

### 5. Celery work is killed on every rollout and recovered late or twice

- **Evidence**
  - `templates/celery-worker-deployment.yaml:29-37` sets no `terminationGracePeriodSeconds` (default 30s) against task limits of 300/600s (`backend/src/tasks/celery_app.py:220-221`) and 900/960s (`backend/src/tasks/arxiv_paper_tasks.py:12-13`). No `visibility_timeout` is set (`celery_app.py:71-74`). All tiers run single-replica on a SPOT node group (`values-aws.yaml:58,339,371`; `infrastructure/terraform/main.tf:250`).
  - Worker-lost redelivery is skipped and acked: `celery_app.py:92`, `backend/src/tasks/replay_guard.py:164-168`, `backend/src/tasks/processing_tasks.py:438-447`. The job is marked FAILED by the sweeper about 30 minutes later.
  - The lost-job reconciler treats "queued behind a backlog" as "never dispatched": the task id is never stored (`backend/src/services/documents/file_service.py:713`; `backend/src/tasks/reconcile_jobs.py:84-87,99-114`), so a backlog over 10 minutes gets duplicate messages.
  - Graph job endpoints publish before commit (`backend/src/api/search/knowledge_graph.py:822-829,886-894`). The race was not reproduced.
  - `kg_extract_entities_job` loops documents with a 300s per-document timeout inside a 300s soft limit (`processing_tasks.py:1063,1108-1128`).
  - Blocking `.delay()` in async handlers (`backend/src/api/agent/execute.py:433`, `backend/src/services/threads/chat_service.py:514,669`).
- **Impact**: each deploy or spot reclaim loses in-flight ingestion; users see documents stuck, then failed.
- **Fix**: worker grace period of at least 660s and an explicit `visibility_timeout`; `self.retry(countdown=...)` on a "running" skip; persist the task id at enqueue; use the after-commit helper in `backend/src/tasks/enqueue.py`; fan graph extraction out per document; `asyncio.to_thread` for publishes.
- **Effort**: S for the chart values, M for the rest

### 6. The public repository runs with GitHub's free protections off, and a leaked key is still in history

- **Evidence**
  - `gh api repos/goodwiins/NOUS-Research-Assistant --jq .security_and_analysis` reports `secret_scanning`, `secret_scanning_push_protection` and `dependabot_security_updates` all `disabled`; visibility is `public`.
  - `gh api .../rulesets/23197379`: the active ruleset "develop required checks" has `"rules": []`. Classic protection on `develop` requires only the `Release Gate` check, no review, and `enforce_admins` is false. `can_approve_pull_request_reviews` is true, `allowed_actions` is `all`, and `sha_pinning_required` is false although all 76 `uses:` references are already SHA-pinned.
  - `config/obsidian-mcp-config.json:11` held a literal 64-character API key from `3945f0bd2` (2026-03-23) until `7ef8e9cb9` (2026-10-07); the commit is reachable from `develop`. Rotation status is UNVERIFIED.
  - `.github/workflows/secret-scan.yml:47,61,65` scans only the PR or push range, never full history.
- **Impact**: a pushed secret is not blocked or flagged; the Obsidian key is recoverable by anyone who clones.
- **Fix**: rotate the Obsidian key; enable secret scanning, push protection and Dependabot security updates; put the required checks in the ruleset (or delete the empty one); require SHA pinning; add a scheduled full-history `gitleaks git .` job.
- **Effort**: S

### 7. The weekly agent eval is red for the wrong reason, and tests a configuration dev does not run

- **Evidence**
  - `gh run list --workflow agent-eval.yml`: failure on 7 of the last 8 scheduled runs. The workflow's own comment says 9 of 10 weeks (`.github/workflows/agent-eval.yml` "Run regression sweep" step).
  - The Redis errors in the three latest runs are logged cache warnings, not test failures. The failing assertions are agent behaviour: [run 42642270609](https://github.com/goodwiins/NOUS-Research-Assistant/actions/runs/42642270609) `intent mismatch: expected=writing actual=general` (1 failed, 25 passed); [run 41465224637](https://github.com/goodwiins/NOUS-Research-Assistant/actions/runs/41465224637) `tool_subset_match=0`; [run 40321269334](https://github.com/goodwiins/NOUS-Research-Assistant/actions/runs/40321269334) `tool sequence mismatch: expected=('ingest_arxiv_papers',) actual=['list_projects']`.
  - So #2105 (own Redis) removes log noise but not the cause; the `--reruns 1` added with it is what will turn most weeks green, and it can hide a probabilistic regression. #2105 has not run on schedule yet.
  - `agent-eval.yml` sets `DO_KB_ENABLED: "true"` while live dev runs `"false"`, and `evals/agent-kb-retrieval-v1/tests/verify.py:252` requires `source == "do_kb"`.
  - Code causes of the routing drift: an LLM classifier timeout or provider error silently falls back to keywords (`backend/src/services/agent/classifier.py:555-582`), a single-keyword result is demoted to general (`:586-608`), and the lanes bind different tools (`backend/src/services/agent/tools.py:528-548`). The title-to-arXiv chain exists only in the writing protocol (`backend/src/services/agent/subgraphs/AGENTS_writing.md:16,78,88`); the shared rules say to stop on an empty title lookup (`backend/src/services/agent/_prompts.py:278-284`).
- **Impact**: the eval cannot gate a regression, and item 1 is invisible to it because it exercises the KB path.
- **Fix**: run the eval with the live flag set (or both); record classification source in state and trace; make general a superset of specialist tools or re-route on an unbound tool request; add a "try `search_arxiv` by title" step to the shared rules and the zero-hit hint; track rerun counts over time.
- **Effort**: M

### 8. Dependency debt: a critical JWT library with no fix, and Dependabot PRs that cannot resolve

- **Evidence**
  - 42 open Dependabot alerts (8 critical, 13 high, 17 medium, 4 low), down from about 74. Six of the criticals are one advisory, GHSA-3qf3-8w2g-rqmx in `python-jose` 3.5.0, repeated across six requirements files, with no patched version. The other two are `tinypool` (dev-only, via vitest).
  - `python-jose` is the main auth path: `backend/src/core/security.py:24`, `backend/src/core/websocket_auth.py:19`, `backend/src/websocket/auth.py:16`, `backend/src/services/security/user_management.py:18`. Every `jwt.decode` pins `algorithms` (`security.py:339,364,388`), which is the advisory's stated precondition for safety, so it is not directly exploitable today. `PyJWT` is already a dependency (`backend/requirements.txt:138`).
  - #2111 and #2107 fail with `ResolutionImpossible`: `opentelemetry-instrumentation-redis 0.66b2` needs `opentelemetry-instrumentation==0.66b2` while fastapi and sqlalchemy instrumentation stay on `0.66b1` (`backend/requirements.txt:87-96`). `.github/dependabot.yml` has no group for `opentelemetry-*`.
  - #2083 is blocked by `@assistant-ui/react` API removals; `useThread` is used in `frontend/src/components/chat/aui/AuiMessage.tsx`.
  - Eight requirements files, with test packages in the production one (`backend/requirements.txt:133-137`) and at least eleven packages nothing imports (for example `flower`, `passlib`, `tabula-py`, `sse-starlette`).
- **Fix**: migrate the five import sites to PyJWT and drop `python-jose`; add an `opentelemetry` group (pattern `opentelemetry-*`) ahead of the catch-all pip groups; collapse to one production and one dev requirements file; bump `vitest` to clear `tinypool`.
- **Effort**: M

### 9. Trigger.dev path: user tokens in a third party's run records, and a deploy workflow that cannot fire

- **Evidence**
  - `frontend/app/api/trigger/agent/execute/route.ts:44` puts `session.access_token` in the task payload, consumed at `src/trigger/agent/execute-agent.ts:97`.
  - `src/trigger/_lib/backend-client.ts:95-97` sends the Supabase service-role key as the backend bearer; the default `BACKEND_API_URL` is plain http (`:4`). The backend rejects it (`backend/src/core/security.py:233,365` require `sub` and `aud=authenticated`), so those tasks 401.
  - `.github/workflows/trigger-deploy.yml:5` fires only on pushes to `main`. `gh api repos/.../branches/main` returns 404 and the workflow has no runs. It also pins Node 20 (`:24`) against `.nvmrc` 24.
  - `frontend/app/api/trigger/agent/runs/[runId]/route.ts:32` interpolates an unvalidated `runId` into a URL sent with `TRIGGER_SECRET_KEY`.
- **Impact**: a replayable bearer token sits in Trigger.dev run history; the deployed Trigger tasks are whatever was last deployed by hand.
- **Fix**: mint a short-lived single-purpose backend token for the task; give Trigger tasks a dedicated service credential; point the workflow at `develop` (or decide it is retired and delete it); validate `runId` against `run_[a-z0-9]+`.
- **Effort**: M

### 10. Dead code and dead weight that slow CI and hide what is live

- **Evidence**
  - CI still boots Qdrant: `.github/workflows/test-pipeline.yml:1359` (`up -d postgres redis neo4j qdrant gotrue auth-proxy`), health probe at `:1385`; `qdrant-client==1.19.1` ships in the production image (`backend/requirements.txt:53`) with zero imports in `backend/src`.
  - 27 of 750 backend modules (about 16,100 lines) have no importer, led by `models/ab_testing_models.py` (1300), `models/qa_system_models.py` (1244) and `api/quality/ab_testing.py` (1142; its router is commented out at `backend/src/api/quality/__init__.py:12`). A further 16 modules (about 7,000 lines) are imported only by tests.
  - Three standalone FastAPI apps the live chart never runs: `backend/src/websocket/` (4,337 lines), `backend/src/services/infrastructure/api_gateway.py`, `backend/src/services/security/user_management.py`. The first has an admin API on a bare `HTTPBearer()` that never validates the token (`backend/src/websocket/websocket_api.py:25,118,163,216`). Whether it runs anywhere is UNVERIFIED.
  - Fourteen backend Dockerfiles; workflows reference two. The other twelve use `python:3.14-slim` while CI and production use 3.11.
  - `git ls-files -z scripts/data .playwright-mcp | xargs -0 du -ck` totals 175,636 KB: 21 arXiv PDFs and 63 screenshots with no references.
  - Embeddings computed then discarded on the bulk path (`backend/src/services/ingestion/kaggle_llm_bulk_ingestion.py:419-424`), with `enable_embeddings` defaulting to true (`backend/src/api/arxiv/arxiv_llm_bulk.py:65`).
  - Frontend: `frontend/src/stores/analyticsStore.new.ts` (630 lines) and `enhancedGraphStore.ts` (863 lines) have no importers.
- **Fix**: remove the Qdrant service, probe, env and requirement; delete unimported modules in batches with their tests; delete the standalone apps and the twelve Dockerfiles; `git rm` the PDFs and screenshots and ignore the paths; drop the discarded embedding call.
- **Effort**: M in total, each piece S

## Remaining findings by area

### Backend correctness

| Finding | Evidence | Fix | Effort |
| --- | --- | --- | --- |
| Nested 3s timeout in the KB primary read: outer and inner use the same value, so the outer also covers session open, ownership check and rerank, and a 2.5s retrieve is discarded when rerank passes 3s. Dormant in dev (flag off). | `backend/src/services/agent/_nodes_rag.py:1093-1103`, `:1201-1206`; `backend/src/core/config.py:372` | One deadline; pass the remaining budget inward; return un-reranked chunks on rerank timeout | S |
| Project-scoped graph reads filter on the creator document, so an entity first extracted from document A is missing from views of document B | `backend/src/services/knowledge_graph/knowledge_graph_service.py:175-177` vs `:764-774` | Scope on `e.document_ids` membership | M |
| Graph search adapter ignores `offset` and fabricates `total` | `knowledge_graph_service.py:3098-3101`, `:3142-3143` | Add `SKIP` and a count query | M |
| Combined thread and message search omits the legacy agent-thread exclusion, giving duplicate hits | `backend/src/services/threads/thread_message_search_service.py:524-557` vs `:753,792,832,872` | Add the predicate to both CTEs | S |
| Bulkhead: no `try/finally` between semaphore acquire and counter update. Not reachable today (every lock body is await-free), not covered by a cancellation test, and the class has no callers in `backend/src` | `backend/src/core/resilience.py:274-294`; `backend/tests/resilience/test_core_resilience.py:405` | Delete the unused class, or add the `finally` and a cancel test | S |
| Benchmark default still includes `SearchType.VECTOR`, which the API rejects with 400. Benchmark behaviour UNVERIFIED | `backend/src/services/search/search_quality_service.py:523`; `backend/src/api/search/search.py:224-234` | Drop the value | S |
| 55 backend modules exceed 1000 lines (`tools_impl.py` 5807, `api/agent/streaming.py` 4839); 837 function-level `src.` imports in 181 files; 193 settings fields, 9 never read, and 106 `os.getenv` calls bypass Settings | `wc -l`; AST scan | Split along tool families; fold env reads into Settings | L |
| Layer inversions: a service imports API schemas; a task imports a private helper from a router | `backend/src/services/evidence/consensus_calculator.py:12`; `backend/src/tasks/search_update_tasks.py:16` | Move the symbols into services | S |

### Security

| Finding | Evidence | Fix | Effort |
| --- | --- | --- | --- |
| Blind SSRF by DNS rebinding in report webhook delivery (POST only, https only, response not returned) | `backend/src/services/analytics/report_service.py:690`, `:711`, `:720` | Connect to the validated IP | S |
| Anonymous arXiv proxy spends the shared arXiv quota | `backend/src/api/arxiv/core.py:148-151`; `backend/src/middleware/multi_tenancy.py:95` | Require auth | S |
| `/openapi.json` served unauthenticated outside DEBUG (schema is already public in the repo) | `backend/src/main.py:496-497` | Set `openapi_url=None` outside DEBUG | S |
| Live CORS allows localhost origins with credentials | `values-aws.yaml:225`; `backend/src/main.py:542` | Remove localhost from the deployed value | S |
| Dead unauthenticated `/admin/performance-test` waiting to be wired | `backend/src/observability/integration.py:277` | Delete | S |
| Infrastructure identifiers in a public repo: AWS account id, secret ARN and RDS hostname; a droplet IP with an SSH username | `values-aws.yaml:43-45`; `docs/deployment/TODO-CI-SETUP.md:50` | Move to non-committed values; scrub the doc | S |
| Frontend Vercel install uses `--no-frozen-lockfile` | `frontend/vercel.json:4` | Use `--frozen-lockfile` | S |
| Hard-coded weak Grafana admin password. Deployment UNVERIFIED | `deployment/monitoring/grafana-configmap.yaml:31,34` | Delete with the dead chart | S |
| EKS API endpoint public with no CIDR allowlist; cluster-admin role trusts the account root. Applied state UNVERIFIED | `infrastructure/terraform/main.tf:190-191`; `infrastructure/terraform/admin.tf:16-19` | Restrict CIDRs | S |

Checked and found sound: every main-app route requires a bearer (`backend/src/middleware/multi_tenancy.py:202-216`); agent tools verify project ownership and every mutating tool carries the interrupt tag; document and file queries filter `organization_id`; no unsafe SQL interpolation; frontend has a nonce CSP and checks run ownership; `.gitleaksignore` allow-lists only synthetic markers; no workflow uses `pull_request_target`.

### Tests and CI

| Finding | Evidence | Fix | Effort |
| --- | --- | --- | --- |
| The self-hosted runner `do-droplet-runner` is persistent and registered on a public repo. Today only `agent-eval.yml` targets it, on `schedule` and `workflow_dispatch`, so fork code cannot reach it; fork PR approval is `all_external_contributors`. One future `pull_request` job on `[self-hosted]` would expose it and the eval secrets | `.github/workflows/agent-eval.yml:22`; `gh api .../actions/runners`; `gh api .../actions/permissions/fork-pr-contributor-approval` | Restrict the runner group to selected workflows, or move the eval to a hosted runner now that Redis is a service container | S |
| Coverage is advisory everywhere: all Codecov statuses are `informational: true`; `fail_under = 60` exists only in `backend/.coveragerc:72` | `codecov.yml:32,36,65` | Turn on the diff-coverage status for new code | S |
| Test Pipeline has 8 `continue-on-error: true` and 57 `\|\| true` | `grep -c` on `.github/workflows/test-pipeline.yml` | Review each; keep only where a comment justifies it | M |
| Most of root `tests/` is never run: `tests/contract` (about 10,000 lines), `tests/integration` (11,800), `tests/api_contract` (8,000), `tests/performance`, `tests/security`. Root `pyproject.toml:32-38` sets `--cov=src` where root `src/` is TypeScript | workflow grep; `pyproject.toml:32-38` | Move the live parts under `backend/tests`; delete the rest | M |
| 120 `skip`/`xfail` markers in Python tests and 10 skipped frontend or E2E tests | `grep -rE "pytest\.mark\.(skip\|xfail)\|pytest\.skip\("` | Triage; delete or fix | M |
| Critical path: 39 to 50 minutes on `develop` before #2104, 21 minutes on the one run after it. One sample | `gh run list --workflow test-pipeline.yml --branch develop` | Re-measure after a week | S |

### Infrastructure and operations

| Finding | Evidence | Fix | Effort |
| --- | --- | --- | --- |
| PDBs protect nothing (`maxUnavailable: 1` with one replica); Neo4j has no PDB; single NAT and single-node ElastiCache | `values-aws.yaml:409-411`; `templates/neo4j-statefulset.yaml:11`; `infrastructure/terraform/main.tf:150,560-561` | Add an on-demand base node and two API replicas, or record it as accepted dev risk | S to M |
| `wait-for-postgres` init container is unconditional, uses a mutable `postgres:15-alpine` tag, loops forever, and receives the whole DB secret | `templates/backend-deployment.yaml:51-73` | Pin by digest or mirror to ECR; add a deadline; pass host and port only | S |
| Neo4j: no securityContext, mutable tag, APOC downloaded at every start, credentials secret created by hand outside GitOps | `templates/neo4j-statefulset.yaml:21-52`; `values-aws.yaml:316-323` | Bake APOC into a pinned image; manage the secret through ESO | M |
| NetworkPolicy off; the template is DigitalOcean-shaped and would black-hole the ALB if enabled as-is | `values-aws.yaml:403-408`; `templates/networkpolicy.yaml:22-25,75-92` | Write a Neo4j ingress policy; rework the backend policy for the VPC CNI | M |
| Worker, beat, migration Job and synthetic CronJob set no container securityContext; beat liveness is `pgrep`; worker has no readiness probe | `templates/celery-worker-deployment.yaml:38`; `templates/celery-beat-deployment.yaml:39,71-80` | Apply the backend container's settings to all | S |
| Argo CD: every app tracks `develop` with prune and selfHeal, and the AppProject allows `kind: "*"` plus ClusterRole and CRD, so any merge can grant cluster-admin. `bootstrap.sh` still applies the DOKS root app | `infrastructure/argocd/project.yaml:33-48,51-78`; `infrastructure/argocd/bootstrap.sh:40,55,73` | Narrow the whitelists; delete `dev.yaml`, `root.yaml`, `bootstrap.sh` | S to M |
| Migration Job name hashes all of `backend.env`, so any flag flip reruns Alembic; `backoffLimit: 0` blocks the rollout on a transient blip | `templates/backend-migrations-job.yaml:11-19`; `templates/_helpers.tpl:360-394` | Hash image and DB settings only; alert on a failed Job | S |
| Terraform: empty `backend "s3" {}` so locking and encryption are CLI-only; defaults target us-west-2 and production; ECR tags mutable, and keep-last-20 can expire the running digest | `infrastructure/terraform/main.tf:35-43`; `variables.tf:14,25,74`; `ecr.tf:7,16,23-42` | Commit the backend config; fix defaults; immutable tags | M |
| No RDS restore drill is recorded (the Neo4j one is) | [`2026-10-20-eks-backup-recovery.md`](2026-10-20-eks-backup-recovery.md) | Run and record a point-in-time restore | S to M |
| Retired DigitalOcean secrets are still synced into the live namespace "for rollback" | `infrastructure/helm/knowledge-graph-analytics/values.yaml:37-45`; `values-aws.yaml:33-39` | Override `infisical.secrets` once rollback is closed | S |
| external-dns annotations name Cloudflare while DNS is hand-managed, so an ALB replacement breaks `dev-api` until someone edits DNS | `values-aws.yaml:88-96` | Manage the records, or remove the annotations and document the manual step | S |
| Production image keeps build tools in the final stage on an unpinned `python:3.11-slim` | `backend/docker/Dockerfile.prod:7,11-35,86` | Multi-stage; pin by digest | S |

### Frontend

Lighter pass; see coverage note.

| Finding | Evidence | Fix | Effort |
| --- | --- | --- | --- |
| No route-level `error.tsx` or `loading.tsx` under `frontend/app` (44 pages); only `global-error.tsx` and `not-found.tsx` exist, so a render error in one page replaces the whole shell | `find frontend/app -name error.tsx -o -name loading.tsx` returns nothing | Add `error.tsx` at the `(dashboard)` segment first | S |
| 15 production paths are excluded from type-check, including `AppLayout.tsx`, `Header.tsx`, `Sidebar.tsx` and two stores | `frontend/quality-baseline.json` `tsconfigProductionExclusions` | Earn them back one by one, layout first | M |
| `ignoreBuildErrors` is on for every non-production `NODE_ENV` | `frontend/next.config.js:32` | Remove once the exclusions shrink | S |
| 186 `: any` and 33 `as unknown as` in production code, against the documented rule | `grep -rE` over `frontend/src` and `frontend/app`, tests and generated files excluded | Ratchet in `quality-baseline.json` | M |
| Two store directories (`src/store`, `src/stores`) holding about 8,500 lines across 20 Zustand stores, while the contract names the chat store as the single exception to TanStack Query | `ls frontend/src/store frontend/src/stores` | Merge the directories; audit each store for server data | M |
| 4 of 35 icon-only buttons show no accessible name within four lines (heuristic, UNVERIFIED per instance); the chat transcript has no `aria-live` region of its own (the input and sidebar do) | `grep -rn 'size="icon"' -A4`; `grep -rn aria-live frontend/src/components/chat` | Label the buttons; add a polite live region for the streaming message | S |
| God files: `useChatStreaming.ts` 3776 lines, `agentChatStore.ts` 1797, `projects/[id]/page.tsx` 1442 | `wc -l` | Split by concern | L |
| `frontend/.env.example` carries 11 `REACT_APP_*` names in a Next.js app; `analytics/index.ts:205` reads a `localStorage` key nothing writes | `frontend/.env.example`; `frontend/src/services/analytics/index.ts:205` | Clean both | S |

Positive: lists are virtualised (`VirtualizedMessageList.tsx`, `VirtualizedConversationList.tsx`), the query cache is cleared on account change (`frontend/src/stores/authStore.ts:147`), 20 files honour reduced motion, and no empty `catch` blocks were found.

### Developer experience and documentation

| Finding | Evidence | Fix | Effort |
| --- | --- | --- | --- |
| Lint pins disagree in five places: CI and `run_local_ci.sh` use `ruff==0.15.15 black==26.5.1 isort==5.13.2 mypy==1.7.1`; `requirements.dev.txt` has ruff 0.17.1 and black 26.10.1; `Dockerfile.lint` has ruff 0.1.8 and black 23.12.1; pre-commit pins nothing | `.github/workflows/test-pipeline.yml:157`; `backend/requirements.dev.txt:11-13`; `backend/docker/Dockerfile.lint:30-33`; `.pre-commit-config.yaml` | One constraints file used by all | S |
| Two local CI runners; root `run-ci.sh` is Docker-based, describes `npm ci`, and is used only by an unreferenced test | `run-ci.sh:66`; `scripts/ci/stages.sh:9` | Delete it with `stages.sh` and `tests/ci/` | S |
| Root `Makefile` is Docker-only and mostly broken, as `CLAUDE.md` already warns | `Makefile` | Reduce to the docs targets plus wrappers over `run_local_ci.sh` | S |
| README setup contradicts "no Docker" and has no backend venv steps; it uses the `Goodwiinz` owner; `package.json:36` has a placeholder repository URL | `README.md:9,124,147,180` | Rewrite the setup section | S |
| Old repo name, functional: `preflight.py` default feeds `repo_slug`; the ruleset migration script default; the ARC runner manifest; 18 eval `task.toml` source pins that work only through GitHub's redirect (whether the harness clones that field is UNVERIFIED) | `scripts/nous/preflight.py:46`; `scripts/ci/migrate-develop-protection-to-ruleset.sh:21`; `infrastructure/arc/runner-deployment.yaml:32,70`; `evals/agent-kb-retrieval-v1/task.toml:2` | One replace pass plus `tests/unit/scripts/test_nous_run_cli.py:29` and `docs/engineering/nous-loop.md:81` | S |
| 53 files hardcode a personal `/Users/.../RAG_system` path, one in production source | `backend/src/api/arxiv/arxiv_local_simple.py:20` | Delete the scripts; fix or delete the module (it has no importer) | S |
| Docs contradict the architecture test: "no route module calls `.commit()`" while the test allows it and the routes commit; outside that package 108 `db.commit()` calls sit in 33 API files | `docs/engineering/backend.md:31`; `backend/tests/unit/architecture/test_workspace_boundaries.py:92-99` | Correct the docs to what is enforced | S |
| Docs recommend `alembic revision --autogenerate` | `docs/CODEMAPS/DATA.md:37,118`; `docs/CODEMAPS/BACKEND.md:80`; `backend/alembic/README.md:16` | Replace with the hand-written revision workflow | S |
| Chart path: `docs/CODEMAPS/INFRA.md:35` correctly marks `deployment/helm/rag-system/` as legacy, but that chart, `deployment/monitoring/`, `infrastructure/kubernetes/manifests/` and the DOKS values still sit beside the live chart `infrastructure/helm/knowledge-graph-analytics` | `infrastructure/argocd/applications/aws-dev.yaml:22` | Delete or move to `docs/archive` | S |
| Three schema sources: `database/` (62 files, 32,000 lines; compose files mount a `database/init.sql` that does not exist), Alembic (128 revisions), and `supabase/migrations` creating application tables that RDS does not use. Live Supabase contents UNVERIFIED | `ls database/init.sql` fails; `values-aws.yaml:139` | Archive `database/`; document Supabase as auth only | M |
| Stale docs outside dated records: Qdrant in 198 files, `npm` commands in 24, DigitalOcean deployment in 19, `RAG_system` in 73; `frontend/.github/workflows/e2e-tests.yml` is never executed and starts Qdrant | `git grep -l` | Delete or archive by directory | M |
| `SECURITY.md` has no reporting channel | `SECURITY.md` | Add a contact or enable private vulnerability reporting | S |
| DigitalOcean KB is dormant, not dead: 2,256 lines, 15 settings, referenced from 24 modules, off in dev | `backend/src/services/do_kb`; `backend/src/core/config.py:356-357` | Decide: remove, or keep and say so | L to remove |
| Unused top-level directories: `security/` (17,700 lines), `feature-flags/`, `specs/`, `notebooks/`, `memory/`, `.Jules/` and `.jules/` (case-colliding), `monitoring/` (13,000 lines, compose-only) | `git ls-files` | Archive or delete | S |

## Already fixed since 2026-11-03

| Lead | Status | Evidence |
| --- | --- | --- |
| API key in `config/obsidian-mcp-config.json` | Fixed in the tree, still in history | `config/obsidian-mcp-config.json:11` now reads `${OBSIDIAN_API_KEY}`; see top-10 item 6 |
| Test databases exposed at 138.197.36.44 | Not substantiated | The IP appears in one tracked file (`docs/deployment/TODO-CI-SETUP.md:50`, an SSH command with a username and no secret). `git log --all -S` finds no database URL or password beside it, in tree or history |
| Frontend `next@16.2.11` advisory | Fixed | `frontend/package.json:63` and `pnpm-lock.yaml:7481` resolve `next@16.4.5` |
| `GraphHealthMonitor` basePath without a leading slash | Fixed | `frontend/src/components/entities/GraphHealthMonitor.tsx:134-135` calls `'/knowledge-graph/health'`; every `basePath` under `frontend/src/services` starts with `/` |
| Project-scope filter gap in retrieval | Fixed in the agent paths | Entry check `backend/src/services/agent/agent_execution_service.py:1104-1191`; tools call `_verify_project_ownership` (`backend/src/services/agent/tool_helpers.py:256-272`); full-text fallback fails closed (`_nodes_rag.py:1364-1386`). Access is owner or live workspace member, by design. `search_documents` stays org-wide by design |
| Bulkhead permit leak on cancellation | Not reachable in current code | See Backend correctness table |
| Qdrant code paths in `backend/src` | Mostly removed | No `QDRANT_*` settings remain in `backend/src/core/config.py`; leftovers are in top-10 item 10 |
| Argo CD apps naming the old repository | Fixed | `infrastructure/argocd/applications/aws-dev.yaml:20` uses `goodwiins/NOUS-Research-Assistant` |
| `opencode.yml` open to any commenter | Fixed | The job requires `OWNER`, `MEMBER` or `COLLABORATOR` author association and the action is SHA-pinned |

Still present from the same lead list: the nested 3s timeout (Backend correctness table), the LangSmith findings (top-10 items 1 and 7), the `agent-eval.yml` reds and Dependabot failures (items 7 and 8), and the post-transfer leftovers (Developer experience table).
