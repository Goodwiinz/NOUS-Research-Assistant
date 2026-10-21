# Archived frontend documents

One-off migration notes, completion reports and early architecture proposals
that used to sit in `frontend/` and `frontend/docs/`. They were archived on
2026-10-17 because they describe a finished Vite-to-Next.js migration or an
early design that no longer matches the app. They are historical records, not
live contracts, and their paths, commands and library versions are outdated.

For current behavior read [`frontend/README.md`](../../../frontend/README.md),
[`frontend/AGENTS.md`](../../../frontend/AGENTS.md) and
[`docs/engineering/frontend.md`](../../engineering/frontend.md). The live E2E
suite is documented in [`tests/e2e/README.md`](../../../tests/e2e/README.md).

[Archive home](../README.md)

## Next.js migration (Vite to App Router)

| File | What it was |
| ---- | ----------- |
| [QUICK_START.md](QUICK_START.md) | Getting-started note written right after the migration. |
| [NEXTJS_MIGRATION_GUIDE.md](NEXTJS_MIGRATION_GUIDE.md) | How the app moved from Vite to Next.js and the routing patterns used. |
| [MIGRATION_SUMMARY.md](MIGRATION_SUMMARY.md) | Short overview of the migration. |
| [OPTION2_COMPLETE.md](OPTION2_COMPLETE.md) | Report on the "Option 2" full Next.js directory structure. |
| [RESTRUCTURE_PLAN.md](RESTRUCTURE_PLAN.md) | Plan for the directory reorganization. |
| [FINAL_SUMMARY.md](FINAL_SUMMARY.md) | Final summary of the migration work. |
| [TESTING_CHECKLIST.md](TESTING_CHECKLIST.md) | Manual smoke checklist for verifying the migration. |
| [FIX_API_ERROR.md](FIX_API_ERROR.md) | Troubleshooting note for an old document-fetch API error. |

## E2E testing (early framework)

| File | What it was |
| ---- | ----------- |
| [E2E_TESTING_DOCUMENTATION.md](E2E_TESTING_DOCUMENTATION.md) | Write-up of the first Playwright E2E setup. |
| [E2E_TESTING_COMPLETION_SUMMARY.md](E2E_TESTING_COMPLETION_SUMMARY.md) | Completion report for that setup. |

## Chat, graph and service layers

| File | What it was |
| ---- | ----------- |
| [LLM_CHAT_INTEGRATION_GUIDE.md](LLM_CHAT_INTEGRATION_GUIDE.md) | Integration guide for early chat UI components. |
| [LLM_CHAT_COMPLETION_SUMMARY.md](LLM_CHAT_COMPLETION_SUMMARY.md) | Completion report for the chat UI enhancement. |
| [MIGRATION_GUIDE_GRAPH_ARCHITECTURE.md](MIGRATION_GUIDE_GRAPH_ARCHITECTURE.md) | Note on moving graph algorithms out of the frontend. |
| [PHASE3_CHECKLIST.md](PHASE3_CHECKLIST.md) | Checklist for an early frontend service and store layer. |

## Architecture and type-safety proposals

| File | What it was |
| ---- | ----------- |
| [FRONTEND_ARCHITECTURE.md](FRONTEND_ARCHITECTURE.md) | Early frontend architecture proposal for the evaluation and analytics UI. |
| [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md) | Summary of that architecture proposal. |
| [TYPESCRIPT_FIXES_SUMMARY.md](TYPESCRIPT_FIXES_SUMMARY.md) | Report of one round of TypeScript fixes. |
| [TYPESCRIPT_IMPROVEMENTS.md](TYPESCRIPT_IMPROVEMENTS.md) | Past type-safety improvement proposal. |
| [TYPE_SAFETY_QUICK_REFERENCE.md](TYPE_SAFETY_QUICK_REFERENCE.md) | Quick reference for that type-safety effort. |
