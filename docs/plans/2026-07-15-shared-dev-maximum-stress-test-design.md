# Shared Dev Maximum Stress Test Design

## Goal

Find the practical breaking point and recovery behavior of the shared dev RAG application by running the repository's maximum 400-virtual-user profile against `https://dev-api.gen-text.app`.

This is an explicitly disruptive test. Shared dev may become slow or temporarily unavailable. The run must remain observable, abortable, attributable, and cleanable.

## Current Constraints

- Dev has one backend replica, a 1 CPU / 2 GiB limit, and backend autoscaling disabled.
- The checked-in K6 stress script targets stale authentication and profile routes.
- Supabase owns registration and login; those operations are not backend `/api/v1/auth/register` or `/api/v1/auth/login` routes.
- K6 setup data must carry authenticated identities into virtual-user execution.
- The existing teardown only logs statistics and does not remove created users or documents.
- K6 is not installed locally, so execution will use a pinned official K6 container image.

## Harness

Update the existing maximum-profile harness rather than create a parallel framework.

The harness will:

1. Resolve current Supabase authentication configuration at runtime from environment variables. Secrets and tokens must never be committed or printed.
2. Create timestamp-prefixed test identities during setup and return their access tokens as setup data.
3. Use current API routes for search, document listing, authenticated profile lookup, and document upload.
4. Tag every created document with a unique run identifier.
5. Emit endpoint-specific latency, throughput, status, and error metrics.
6. Write a machine-readable K6 summary for post-run analysis.
7. Delete run-tagged documents during teardown. Delete test identities only when an administrative cleanup credential is available; otherwise report the exact residual count without exposing credentials.

## Load Profile

Preserve the maximum repository profile:

| Stage | Duration | Target users |
|---|---:|---:|
| Warm-up | 2 minutes | 20 |
| Moderate | 5 minutes | 50 |
| High | 5 minutes | 100 |
| Stress | 10 minutes | 200 |
| Extreme | 5 minutes | 300 |
| Breaking-point attempt | 5 minutes | 400 |
| Cooldown | 2 minutes | 0 |

Operation mix:

- 60% search
- 20% document listing
- 15% authenticated profile/session lookup
- 5% document upload

## Preflight

No full run may start until all of these pass:

1. Backend health and readiness return HTTP 200.
2. One test identity can authenticate using the current Supabase flow.
3. One-VU iterations successfully exercise all four operation classes.
4. A tagged upload can be identified and deleted.
5. Kubernetes monitoring can read the backend pod, resource usage, restart count, and recent events.
6. The abort controller can terminate the K6 process.

If any preflight step fails, stop without generating load and report the concrete blocker.

## Monitoring and Abort Conditions

Run K6 and a separate monitor concurrently. Sample every five seconds:

- `/health/readiness`
- backend pod phase and restart count
- backend pod CPU and memory when metrics are available
- recent warning events

Abort the load test when any condition is met:

- readiness fails three consecutive probes;
- the backend pod restarts or is OOM-killed;
- aggregate HTTP errors exceed 50% for 60 seconds;
- request latency is dominated by 30-second timeouts for 60 seconds;
- the operator interrupts the run.

Threshold failures below these catastrophic conditions are findings, not immediate aborts, because the objective is to locate the breaking point.

## Evidence and Reporting

Capture:

- K6 summary and raw JSON metrics;
- p50, p90, p95, and p99 latency by endpoint;
- request rate, completed iterations, and error rate by status;
- highest stable user stage and first degraded stage;
- readiness failures, pod resource peaks, restarts, OOM events, and warning events;
- recovery time after ramp-down;
- cleanup counts and any residual test records.

Classify findings as observed facts. Do not infer a root cause until logs, resource data, or an isolated reproduction supports it.

## Safety Boundaries

- Target only `https://dev-api.gen-text.app` and the `rag-dev` namespace.
- Do not target production or unrelated services.
- Do not print or persist credentials, tokens, secret values, or private user data.
- Do not change deployment replicas, autoscaling, resource limits, secrets, or infrastructure during the test.
- Cleanup may delete only records bearing the unique stress-run identifier.
