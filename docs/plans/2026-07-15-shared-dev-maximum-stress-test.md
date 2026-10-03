# Shared Dev Maximum Stress Test Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build, preflight, and run a current-contract 400-virtual-user K6 stress test against shared dev with Kubernetes monitoring, catastrophic aborts, and run-scoped cleanup.

**Architecture:** Repair `tests/load/k6-stress-testing.js` so Supabase admin setup creates confirmed test users, setup data carries their tokens to VUs, current backend routes receive the load, and teardown deletes run-tagged documents and users. A shell runner obtains credentials from the existing `rag-dev/supabase-credentials` secret without printing them, runs a pinned official K6 container, monitors readiness and the backend pod, interrupts K6 on catastrophic conditions, and stores results outside the repository.

**Tech Stack:** K6 JavaScript, Supabase Auth REST, FastAPI/OpenAPI contracts, Bash, Docker, kubectl, Python `unittest`.

---

### Task 1: Lock the Current Stress Contract with Failing Tests

**Files:**
- Create: `tests/load/test_k6_stress_contract.py`
- Test: `tests/load/k6-stress-testing.js`

**Step 1: Write the failing contract test**

Create a standard-library test that reads the K6 source and verifies the repaired contract:

```python
from pathlib import Path
import unittest


SOURCE = Path(__file__).with_name("k6-stress-testing.js").read_text()


class K6StressContractTest(unittest.TestCase):
    def test_uses_current_backend_routes(self) -> None:
        self.assertIn("/api/v1/search/", SOURCE)
        self.assertIn("/api/v1/documents", SOURCE)
        self.assertIn("/api/v1/auth/me", SOURCE)
        self.assertIn("/api/v1/files/upload", SOURCE)
        self.assertNotIn("/api/v1/users/profile", SOURCE)
        self.assertNotIn("/api/v1/documents/upload", SOURCE)

    def test_supabase_setup_data_reaches_virtual_users(self) -> None:
        self.assertIn("/auth/v1/admin/users", SOURCE)
        self.assertIn("/auth/v1/token?grant_type=password", SOURCE)
        self.assertRegex(SOURCE, r"return\s*\{[^}]*users:\s*authenticatedUsers")
        self.assertIn("data.users", SOURCE)

    def test_run_scoped_cleanup_is_present(self) -> None:
        self.assertIn("run_id", SOURCE)
        self.assertIn("http.del", SOURCE)
        self.assertIn("handleSummary", SOURCE)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run the test and verify RED**

Run:

```bash
python3 -m unittest tests/load/test_k6_stress_contract.py -v
```

Expected: failures for stale profile/upload routes, missing Supabase admin auth, missing setup tokens, missing cleanup, and missing summary output.

**Step 3: Commit the RED test**

```bash
git add tests/load/test_k6_stress_contract.py
git commit -m "test(load): define shared-dev stress contract"
```

### Task 2: Repair the K6 Maximum Profile

**Files:**
- Modify: `tests/load/k6-stress-testing.js`
- Test: `tests/load/test_k6_stress_contract.py`

**Step 1: Add required runtime configuration and profiles**

Require `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `SUPABASE_SERVICE_ROLE_KEY`. Add `RUN_ID`, `RESULTS_DIR`, and a `PREFLIGHT` switch. Keep the approved stages for the full profile and use a one-user/four-iteration profile for preflight.

```javascript
const FULL_STAGES = [
  { duration: '2m', target: 20 },
  { duration: '5m', target: 50 },
  { duration: '5m', target: 100 },
  { duration: '10m', target: 200 },
  { duration: '5m', target: 300 },
  { duration: '5m', target: 400 },
  { duration: '2m', target: 0 },
];

export const options = __ENV.PREFLIGHT === '1'
  ? { vus: 1, iterations: 4, thresholds: BASE_THRESHOLDS }
  : { stages: FULL_STAGES, thresholds: BASE_THRESHOLDS };
```

Use an aborting aggregate error threshold only for catastrophic sustained failure:

```javascript
errors: [{ threshold: 'rate<0.5', abortOnFail: true, delayAbortEval: '60s' }]
```

**Step 2: Implement Supabase admin setup**

For each timestamped test identity:

1. `POST /auth/v1/admin/users` with the service-role key and `email_confirm: true`.
2. `POST /auth/v1/token?grant_type=password` with the anon key.
3. Store `{id, email, token}` in `authenticatedUsers`.
4. Return `{runId, users: authenticatedUsers, documentIds: []}`.
5. Throw when no user authenticates so a zero-load run cannot look successful.

Do not log response bodies, keys, passwords, or tokens.

**Step 3: Use current backend operations**

Map operations exactly:

- search: `POST /api/v1/search/`;
- documents: `GET /api/v1/documents?page=...&page_size=...`;
- profile: `GET /api/v1/auth/me`;
- upload: multipart `POST /api/v1/files/upload` with required `file` and `title`, plus `tags` and `custom_metadata` containing the run ID.

Accept only documented successful statuses. Record failures by endpoint and HTTP status.

In preflight, select the operation deterministically with `__ITER % 4`; in the full run, retain the approved 60/20/15/5 distribution.

**Step 4: Implement run-scoped cleanup and summary output**

Collect uploaded document IDs in a K6 `Counter`/response-derived run registry suitable for teardown. Delete only IDs created by this run through `DELETE /api/v1/documents/{document_id}?cascade=true`. Delete only setup user IDs through Supabase admin `DELETE /auth/v1/admin/users/{id}`.

Add:

```javascript
export function handleSummary(data) {
  return {
    [`${__ENV.RESULTS_DIR || '/results'}/summary.json`]: JSON.stringify(data),
    stdout: textSummary(data, { indent: ' ', enableColors: true }),
  };
}
```

If K6 cannot share upload IDs with teardown, cleanup must instead list documents using a setup user's token, filter by exact `run_id`, and delete only matches.

**Step 5: Run the contract test and verify GREEN**

Run:

```bash
python3 -m unittest tests/load/test_k6_stress_contract.py -v
```

Expected: all tests pass.

**Step 6: Validate K6 syntax with a pinned official image**

Run:

```bash
docker run --rm -v "$PWD/tests/load:/scripts:ro" grafana/k6:<pinned-tag> inspect /scripts/k6-stress-testing.js
```

Expected: K6 prints the full and preflight execution configuration without a JavaScript/import error.

**Step 7: Commit**

```bash
git add tests/load/k6-stress-testing.js tests/load/test_k6_stress_contract.py
git commit -m "test(load): repair maximum shared-dev stress profile"
```

### Task 3: Add the Secure Monitored Runner

**Files:**
- Create: `tests/load/run-shared-dev-max.sh`
- Modify: `tests/load/README.md`
- Test: `tests/load/test_k6_stress_contract.py`

**Step 1: Extend the failing test for runner safety**

Assert the runner:

- targets only `https://dev-api.gen-text.app` and namespace `rag-dev`;
- reads the five named Supabase keys from `supabase-credentials`;
- never echoes secret variables;
- checks `/health` and `/health/readiness` before K6;
- records initial backend restart count;
- interrupts K6 after three readiness failures or any restart-count increase;
- stores results beneath `/tmp/rag-stress/<run-id>`;
- uses traps to stop monitoring and K6.

Run the test and confirm it fails because the runner is absent.

**Step 2: Implement the runner**

Create a strict Bash script (`set -euo pipefail`) that:

1. validates the Kubernetes context and read access;
2. obtains Supabase values with `kubectl get secret ... -o jsonpath=... | base64 --decode` into shell variables without printing them;
3. verifies both health endpoints;
4. records the backend pod UID and restart count;
5. launches the pinned K6 container in the background;
6. samples readiness, pod identity/restarts, `kubectl top`, and warning events every five seconds into the results directory;
7. sends `SIGINT` to K6 when catastrophic conditions occur;
8. waits for cooldown recovery and writes final health/restart evidence;
9. unsets secret variables on exit.

The runner accepts `--preflight` and `--full`. It must refuse `--full` unless a successful preflight marker for the same commit and target is present.

**Step 3: Document exact commands and risk**

Update `tests/load/README.md` with:

```bash
tests/load/run-shared-dev-max.sh --preflight
tests/load/run-shared-dev-max.sh --full
```

Document the 34-minute duration, 400-user maximum, shared-dev outage risk, abort conditions, result location, and cleanup behavior.

**Step 4: Verify runner and tests**

Run:

```bash
bash -n tests/load/run-shared-dev-max.sh
python3 -m unittest tests/load/test_k6_stress_contract.py -v
git diff --check
```

Expected: shell syntax succeeds, all tests pass, and no whitespace errors are reported.

**Step 5: Commit**

```bash
git add tests/load/run-shared-dev-max.sh tests/load/README.md tests/load/test_k6_stress_contract.py
git commit -m "test(load): add monitored shared-dev stress runner"
```

### Task 4: Run the One-User Preflight

**Files:**
- No repository changes expected
- Runtime results: `/tmp/rag-stress/<run-id>/`

**Step 1: Capture initial state**

Run single-request health/readiness probes and capture backend pod identity, restarts, and resource usage.

**Step 2: Run preflight**

```bash
tests/load/run-shared-dev-max.sh --preflight
```

Expected: one authenticated user completes search, document list, profile, and upload operations; the tagged document and user are deleted; readiness stays green; a preflight marker is written.

**Step 3: Inspect all failures before proceeding**

If any operation fails, use `superpowers:systematic-debugging` to trace the route, auth, request body, or cleanup error. Do not start the full run until preflight is fully green.

### Task 5: Run and Observe the Full 400-User Profile

**Files:**
- No repository changes expected during load
- Runtime results: `/tmp/rag-stress/<run-id>/`

**Step 1: Start the full monitored run**

```bash
tests/load/run-shared-dev-max.sh --full
```

Expected duration: approximately 34 minutes unless a catastrophic abort fires.

**Step 2: Preserve evidence without intervening**

Do not change replicas, limits, KEDA, secrets, or application code during the run. Record stage transitions, first threshold breach, peak resource use, readiness state, restarts, and recovery.

**Step 3: Verify cleanup and recovery**

After K6 exits, verify:

- readiness returns 200 for three consecutive probes;
- backend pod identity/restarts are recorded;
- run-tagged document count is zero or explicitly reported;
- test-user cleanup count equals setup-user count or residual IDs are recorded securely;
- result files are complete.

### Task 6: Analyze Results and Report Concrete Findings

**Files:**
- Create: `docs/reports/2026-07-15-shared-dev-maximum-stress-test.md`

**Step 1: Parse metrics**

Extract p50/p90/p95/p99, request rate, total requests, error rate, endpoint/status failures, completed iterations, highest stable stage, first degraded stage, resource peaks, restarts/OOMs, and recovery time.

**Step 2: Correlate observed failures**

Use timestamps to correlate K6 failures with readiness, pod metrics, events, and backend logs. State root causes only where evidence crosses those boundaries.

**Step 3: Write the report**

Rank observed failures by operational severity and include exact evidence paths. Separate observed facts from hypotheses. Include cleanup status and any shared-dev residual impact.

**Step 4: Verify and commit the report**

```bash
git diff --check
git add docs/reports/2026-07-15-shared-dev-maximum-stress-test.md
git commit -m "docs: report shared-dev maximum stress results"
```
