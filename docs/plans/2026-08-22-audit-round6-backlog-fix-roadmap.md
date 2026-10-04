# Audit Round-6 Backlog Fix Roadmap

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` /
> `superpowers:subagent-driven-development` to implement PR-by-PR.

**Goal:** Land all 112 open audit findings (R6 41 + R2 backlog 41 + R5 remainder 30) as ten reviewable PRs, severity-ordered.

**Architecture:** Cluster findings by subsystem into dependency-ordered PRs. Migration baseline PR closes the create_all/alembic split before any PR needing schema changes. Every PR re-verifies stale rows against HEAD before fixing.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, PostgreSQL, Neo4j, Redis, Celery, Next.js/React Query, Helm.

---

## Decisions (user-approved defaults)

- R6-H3 strategy: **baseline migration + guards** (alembic becomes authoritative).
- R5-L19 ResearchEvidence: **delete** tables/routes (zero writers).
- R6-L5 search analytics mocks: **real persistence** (feedback table added by PR 3).
- Dead code defaults to delete (precedent #1498/#1507): R6-H6 KG viz, R2-L16, R2-L24.

## Gate mechanics

GitHub Actions unavailable (P0-plan note). Exit gate per PR:

```sh
scripts/ci/run_local_ci.sh --base origin/develop   # add --frontend for frontend PRs
```

Paste summary into PR body. PR-specific manual checks listed below.

## PR sequence

| # | PR | Findings | Key work | Depends |
|---|----|----------|----------|---------|
| 1 | Tenant leaks + config gates | R6-H2, H4, H5 · R5-M7, M8 · R6-L2, L10 | Cache key org/user/system_prompt-hash; citation graph project_id property; ENVIRONMENT via Settings only; KG delete/analytics org scope; title-enrichment + message-citation scoping | — |
| 2 | Migration baseline + alignment | R6-H3, M8, M9, M10, L11, L12 · R2-L19=R6-M13 | Guarded baseline migration (~49 tables); stamp script; Citation model unique alignment; guard DDL cluster; downgrade fix; beat placeholder org | 1 |
| 3 | Search API worker health | R6-H1, M1, M2, L3, L4, L5 · R2-M15–M18, L20, L21, L25, L26 | run_in_executor wrap; benchmark→Celery; text()+#-comment fix; rerank pagination; real feedback persistence | 2 |
| 4 | Frontend truth-telling | R6-H7, H6, M14–M18, L13, L14, L17, L18 | Surface swallowed errors; idempotent-only retry; abort tokens; poll resilience; tsconfig hole; delete dead KG viz | — |
| 5 | KG service integrity | R5-M1, M3, M4, M5, L1–L5 · R2-M5, M6, L1 | Neo4j off loop; paper-title entity pre-create; exact-name resolution; breaker reset; deterministic pagination | — |
| 6 | Upload/quota/embedding | R2-M1, M2, M7, M10, M14, L4, L10–L13 · R6-M3, M4, L1, L6 | Atomic quota + CHECK; streaming upload; mime sniffing; tracker state→Redis; embedding singleton; semantic-cache fix-or-delete | 2 |
| 7 | Agent runtime cluster | R2-M3, M8, M11–M13, L2, L3, L5, L7–L9, L14, L16, L17, L24 · R5-M17–M20, M23, L16 | Summarize lock + recent-window; draft cancel; GeneratedDraft uniques; research-run atomicity + sweeper + context rehydration; extraction→Celery; eval FK ondelete | 2 |
| 8 | Threads/workspaces/misc | R5-M10, M11, L7–L11, L15, L19, L17(R6) | Context route shape/authz; feedback order; soft-delete counts; workspace quotas; add_member 400; delete ResearchEvidence | — |
| 9 | Infra/Helm | R5-M30–M32, M34 | Synthetic retention; staging bump chain; PDB; prod values | — |
| 10 | Low sweep | R2-L22, L23 · R6-L7–L9, L15, L16, L19–L24 | Bounds, escaping, server-side filters, virtualization | parents |

## Standing rules

1. Re-verify every row against HEAD before fixing (R2 rows are 08-17 vintage); mark refuted if gone.
2. Claim IDs in `docs/audits/agent/agent-audit-roundN.md` + `~/.audit-ledgers` mirror before branching.
3. Ledger row update on PR open + merge.
4. PR 2 manual gate: fresh-scratch `alembic upgrade head` green + stamp script on dev snapshot.
