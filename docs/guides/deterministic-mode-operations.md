# Deterministic Mode Operations Guide

## Purpose

Operate and verify deterministic hybrid search behavior in development and test environments.

## Quick Verification

Run deterministic backend checks:

```bash
backend/.venv/bin/pytest \
  backend/tests/api/search/test_search_deterministic_response.py \
  backend/tests/services/search/test_hybrid_deterministic_ranking.py \
  backend/tests/api/search/test_search_failure_modes.py \
  backend/tests/api/search/test_search_replay_determinism.py -v
```

Run frontend checks for deterministic metadata mapping:

```bash
pnpm --dir frontend exec vitest run src/services/__tests__/searchService.test.ts src/components/chat/shared/__tests__/messageViewModel.test.ts
pnpm --dir frontend type-check
```

## Operational Checks

For a query response, validate:

1. `deterministic_status` is present.
2. `decision_trace_id` and `trace.decision_trace_id` match.
3. `coverage` is non-null.
4. Failure modes include user-facing `suggestions`.

## Troubleshooting

- If status is always `INSUFFICIENT_EVIDENCE`, inspect `source_count` metadata in fused results.
- If status is unexpectedly `CONFLICTING_EVIDENCE`, inspect top snippets for mixed positive/negative tokens.
- If trace id differs between top-level and nested trace, check deterministic response normalization in `backend/src/api/search/search.py`.
