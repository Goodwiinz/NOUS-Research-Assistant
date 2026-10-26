# NOUS Project Status

> **Archived 2026-10-17: historical project status snapshot from 2026-03-23. It is not current status and no script or workflow reads it.**

_Last updated: 2026-03-23_

---

## Linear Issue Summary

**Total tracked issues (first 100):** 100+ (paginated — more exist)

### Status Breakdown (sample of 100)
| Status | Count |
|--------|-------|
| Done | 79 |
| Backlog | 18 |
| In Progress | 1 |
| Canceled | 2 |

### Priority Breakdown (open issues)
| Priority | Count |
|----------|-------|
| Urgent | 14 |
| High | 31 |
| Medium | 29 |
| Low | 14 |

---

## Recent Activity (2026-03-23)

**11 new Competitive Gap issues added to Backlog today**, all focused on closing the gap with K-Dense:

### 🔴 Urgent (4 issues)
- **GOO-187** — Add sandboxed code execution environment
- **GOO-188** — Build external database connector architecture
- **GOO-189** — Add modular scientific file format parser registry
- **GOO-190** — Extend agent autonomous execution depth

### 🟠 High (4 issues)
- **GOO-191** — Add publication-ready output generation
- **GOO-192** — Build domain-specific vertical specializations
- **GOO-193** — Evolve to hierarchical multi-agent architecture
- **GOO-194** — Enable ML model training in agent workflows

### 🟡 Medium (3 issues)
- **GOO-195** — Document security controls for SOC 2 / HIPAA compliance
- **GOO-196** — Build public use case gallery with research sessions
- **GOO-197** — Add R language support in code execution sandbox

---

## GitHub Branch Activity

> GitHub CLI (`gh`) was not available in the execution environment during the last sync.
> Branch activity could not be pulled. Ensure `gh` is installed and authenticated.

---

## Blockers / Items Needing Attention

1. **`gh` CLI missing** — Install and authenticate `gh` in the scheduled task environment to enable GitHub activity syncing.
2. **Obsidian vault not mounted** — The iCloud Obsidian path is not accessible from the VM. Notes are being written to the workspace folder (`RAG_system/daily-logs/`, `projects/`, `memory/`) as a fallback.
3. **GOO-187 (Urgent)** — Sandboxed code execution is the top capability gap vs. K-Dense. No branch or In Progress status yet.
4. **GOO-190 (Urgent)** — Agent loop depth (currently capped at 8) is a significant limitation for complex multi-hour research tasks.

---

## Architecture Notes

Current stack: Next.js 15 + FastAPI + PostgreSQL + Qdrant + Neo4j + Redis + LangGraph

Agent system: Intent-routing StateGraph with 4 subgraphs (research, writing, data, general), 12 tools, 30s timeout, max 3 concurrent via semaphore. Human-in-the-loop via `interrupt()` for destructive operations.
