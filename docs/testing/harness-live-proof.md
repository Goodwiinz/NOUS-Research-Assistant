# Harness live-proof runbook

**Status:** Procedure, written 2026-10-04 against `origin/develop` `e0d0fcf79`. No step below has run end to end against a live environment. Every live row stays **NOT RUN** until a dated evidence bundle under [evidence/](evidence/README.md) records it. A skipped test is NOT RUN, never PASS.

**Target:** the AWS dev lane only: backend `https://dev-api.goodwiinz.tech`, Argo CD application `nous-dev-aws`, namespace `multimodal-rag-system`, values file [`values-aws.yaml`](../../infrastructure/helm/knowledge-graph-analytics/values-aws.yaml), frontend `https://goodwiinz.tech` (Vercel). The frozen DigitalOcean `values-dev.yaml` (`rag-dev`) and the staging/production files are not targets.

Contracts: [harness bridge](../engineering/harness-bridge.md), [bridge package](../../packages/harness-bridge/README.md), [testing](../engineering/testing.md).

## What the proof shows

1. A Codex run started from NOUS chat on a paired device asks for one command approval, survives a reload, is approved once, and persists exactly one user and one assistant marker ([`harness-bridge.spec.ts`](../../tests/e2e/tests/harness-bridge.spec.ts)).
2. Codex reads project sources through the NOUS MCP read gateway.
3. Phase 2: Codex publishes a workspace file as an artifact, and the stored SHA-256 matches the local file.
4. Phase 2: a `request_action` note approved in the browser more than 15 minutes after the request still executes. Grants expire after 15 minutes, so the approval rests on the consent, not the original grant.
5. After `nous-harness disconnect` the old grant is refused (403). In Phase 2, after remediation slice A, the old CLI bearer is refused too (401).
6. Setting the flags back to `false` refuses new work with 503 while status reads and reconciliation still answer.

## Flags and scopes

| Flag | Gates | Phase 1 | Phase 2 |
| --- | --- | --- | --- |
| `HARNESS_BRIDGE_ENABLED` | New Codex chat runs (`backend/src/api/agent/harness_streaming.py`) and start dispatch (`backend/src/services/harness/delivery.py`) | `true` | `true` |
| `NOUS_MCP_ENABLED` | `/integrations/tools`, new `/integrations/actions` requests, the approved-action drain (`backend/src/services/agent/tool_actions.py`) | `true` | `true` |
| `ARTIFACTS_ENABLED` | Artifact upload and version writes (`backend/src/api/artifacts.py`) | `false` | `true` |
| `ARTIFACT_EDITING_ENABLED`, `ARTIFACT_PREVIEW_ENABLED`, `ARTIFACT_SHARING_ENABLED` | Nothing is safe to expose: `artifacts:read/edit/share` can be minted but are enforced nowhere | `false` | `false` |

All six default to `false` in `backend/src/core/config.py`. The values go in the `backend.env` list of `values-aws.yaml`. The backend, Celery worker, Celery beat and migration-job pods all render that list, and an explicit `env` entry wins over the `envFrom` secrets.

`NOUS_MCP_ENABLED` cannot separate reads from writes. The scope requested at `connect` is what separates them. **Phase 1 connects with `--tools` only.** `--publish` (`artifacts:publish`) and `--write` (`tools:write`) belong to Phase 2, which must wait until remediation slice A (CLI-bearer revocation) is merged **and** deployed. Until then `disconnect` revokes the grant but leaves the CLI bearer valid.

## Gate 0: repository evidence (no cluster)

Run from the repository root of a checkout at the SHA under test, with a local PostgreSQL 14 or later. No Docker is needed. `PY` is the main checkout's backend interpreter, because worktrees have no venv.

```sh
PY=/absolute/path/to/main/checkout/backend/.venv/bin/python
PGDIR=$(mktemp -d)
initdb -D "$PGDIR/data" -U postgres --auth=trust
pg_ctl -D "$PGDIR/data" -o "-p 55432 -k $PGDIR" -l "$PGDIR/pg.log" start
psql -h 127.0.0.1 -p 55432 -U postgres -c 'create database mig' -c 'create database orch'

# 0a. Migrations hb01..hb04, aw01/aw02, it01/it02 apply from empty.
(cd backend && env -u SUPABASE_DB_URL DATABASE_URL=postgresql://postgres@127.0.0.1:55432/mig \
  ENVIRONMENT=testing "$PY" -m alembic upgrade head 2>&1 \
  | grep -E 'hb0|aw0|it0'; "$PY" ../scripts/ci/check_alembic.py)

# 0b. Two-session SKIP LOCKED on the harness outbox.
ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch ENVIRONMENT=testing \
  PYTHONPATH=backend "$PY" -m pytest -c backend/pytest.ini -q \
  backend/tests/integration/test_harness_dispatch_postgres.py

# 0c. Unit suites and bridge package.
PYTHONPATH=backend "$PY" -m pytest -c backend/pytest.ini -q \
  backend/tests/unit/services/harness backend/tests/unit/services/artifacts
pnpm --filter @nous/harness-bridge test
pnpm --filter @nous/harness-bridge type-check

pg_ctl -D "$PGDIR/data" stop -m fast
```

Expected: 0a prints one `Running upgrade` line for each of `hb01_integration_grants`, `hb02_harness_runs`, `hb03_bridge_delivery`, `hb04_harness_approvals`, `aw01_artifact_workspace`, `aw02_artifact_lifecycle`, `it01_integration_actions` and `it02_merge_integration_heads`, and exits 0. 0b prints `1 passed`. In hosted CI it is skipped, because no workflow sets `ORCHESTRATION_TEST_DATABASE_URL`, so the hosted result is NOT RUN. The hosted equivalent of 0a is the Test Pipeline job **Alembic Migration Check**, step **Advisory — upgrade head from empty DB**. That step is `continue-on-error`, so read its log for the eight `Running upgrade` lines; do not rely on the step colour.

Not covered here: the artifact lifecycle `SKIP LOCKED` (`backend/src/services/artifacts/lifecycle.py`) and the `tool_actions` approved-to-executing compare-and-set have SQLite unit coverage only. Record them as NOT RUN on PostgreSQL.

## Gate 1: deployment prerequisites (USER-RUN)

Read-only cluster commands need `aws login` and the EKS kubeconfig.

1. **Release pipeline delivers images again.** Release Dev has failed since 2026-10-01 at **Verify release branch protection** (`gh: Resource not accessible by personal access token`; latest run 37181835414 on 2026-10-04). The release token needs `Administration: Read`. Until this is fixed, nothing merged after `8ad2c82` reaches dev.
2. **The deployed image contains the bridge.** The image must include `271bd5c1f` (`disconnect`, #1790), `6e1452e66` (grant renewal, #1786) and `bfc33c342` (`request_action`, `it01`, #1780). The image pinned on 2026-10-04 is `8ad2c82` (2026-09-30) and contains none of the three.

   ```sh
   TAG=$(awk '/^backend:/{b=1} b&&/tag:/{gsub(/"/,"",$2);print $2;exit}' infrastructure/helm/knowledge-graph-analytics/values-aws.yaml)
   git fetch origin develop && git merge-base --is-ancestor 271bd5c1f "$TAG" && echo "image $TAG OK"
   kubectl -n multimodal-rag-system get deploy nous-dev-aws-knowledge-graph-analytics-backend \
     -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
   ```

3. **Migrations reached head on RDS.**

   ```sh
   kubectl -n multimodal-rag-system get jobs -l app.kubernetes.io/component=migrations
   kubectl -n multimodal-rag-system logs job/<newest migrate job> | grep -E 'Running upgrade .*(hb0|aw0|it0)|Will assume'
   ```

   Expected: the newest job is `Complete`. Its log shows the harness revisions on the run that first applied them; later runs show no pending upgrade.

4. **Flags are what the phase expects.** Values are plain env, never secrets:

   ```sh
   for d in nous-dev-aws-knowledge-graph-analytics-backend nous-dev-aws-celery-worker nous-dev-aws-celery-beat; do
     echo "== $d"; kubectl -n multimodal-rag-system get deploy "$d" -o yaml \
       | grep -A1 -E 'name: (HARNESS_BRIDGE_ENABLED|NOUS_MCP_ENABLED|ARTIFACTS?_[A-Z_]*ENABLED)$'
   done
   kubectl -n argocd get application nous-dev-aws -o jsonpath='{.status.sync.revision} {.status.sync.status} {.status.health.status}{"\n"}'
   ```

5. **Local device.** Node 24, `pnpm install --frozen-lockfile`, a local checkout at or after the deployed SHA, and `codex --version` printing exactly `0.153.4`. Use a disposable fixture directory and a dedicated state directory, not your everyday one:

   ```sh
   export API=https://dev-api.goodwiinz.tech/api/v1
   export STATE="$HOME/.nous/harness-proof"
   export FIXTURE="$(mktemp -d)/fixture" && mkdir -p "$FIXTURE"
   export PROJECT_ID=<project UUID of a dedicated test project>
   ```

   In the NOUS UI, create a chat thread bound to that project, then copy its id from the `?thread=` URL parameter.

## Phase 1: read-only proof (after the Phase 1 values PR is deployed)

All steps are USER-RUN.

```sh
pnpm --filter @nous/harness-bridge start connect --store "$STATE" --api "$API" \
  --project "$PROJECT_ID" --label harness-proof --tools
pnpm --filter @nous/harness-bridge start workspace add --store "$STATE" --root "$FIXTURE" --label harness-proof-fixture
jq -c '{deviceId, scopes, workspace: .workspaces[0].id}' "$STATE/connection.json"
pnpm --filter @nous/harness-bridge start run --store "$STATE"     # leave running in its own terminal
```

Expected scopes: `["harness:execute","tools:read"]`, with no `tools:write` and no `artifacts:publish`.

**P1-1: chat run and approval.** In another terminal:

```sh
read -rs NOUS_HARNESS_E2E_PASSWORD && export NOUS_HARNESS_E2E_PASSWORD
CI=1 BASE_URL=https://goodwiinz.tech NOUS_HARNESS_E2E=1 \
  NOUS_HARNESS_E2E_DEVICE_ID=<deviceId> NOUS_HARNESS_E2E_WORKSPACE_ID=<workspace> \
  NOUS_HARNESS_E2E_THREAD_ID=<thread> NOUS_HARNESS_E2E_EMAIL=<test account email> \
  PLAYWRIGHT_JSON_OUTPUT_NAME="$PWD/harness-e2e.json" \
  pnpm --dir tests/e2e exec playwright test tests/harness-bridge.spec.ts --project=chromium --retries=0 --reporter=line,json
jq -r '.. | objects | select(.type? == "live-harness-acceptance") | .description' harness-e2e.json
```

`CI=1` stops Playwright from starting a local dev server. Expected: `1 passed`, plus one annotation line `outcome=completed thread_id=… run_id=… device_id=… workspace_id=…`. A `skipped` result means a prerequisite is missing and the row stays NOT RUN.

**P1-2: MCP read.** In the same thread, with **Local Codex**, the paired computer and the fixture workspace selected, send: `Use the nous search_documents tool to search this project for "<a term from one project document>" and reply with only the returned document ids.` Record the run id (from the stream, as in P1-1) and the returned document ids. Do not record any source text.

**P1-3: disconnect, then probe.** Copy the state before disconnecting so the old credentials can be replayed once. The copy holds secrets: never print them, and delete the copy straight after.

```sh
PROBE=$(mktemp -d) && cp -Rp "$STATE"/. "$PROBE"/
pnpm --filter @nous/harness-bridge start disconnect --store "$STATE"
H=$(jq -r .credentialHandle "$PROBE/connection.json")
AT=$(jq -r .accessToken "$PROBE/$H.json"); GT=$(jq -r .grantToken "$PROBE/$H.json")
curl -s -o /dev/null -w 'grant:%{http_code}\n' -H "Authorization: Bearer $AT" -H "X-NOUS-Integration-Grant: $GT" "$API/integrations/tools"
curl -s -o /dev/null -w 'bearer:%{http_code}\n' -H "Authorization: Bearer $AT" "$API/integrations/devices"
unset AT GT H; rm -rf "$PROBE"
```

Expected: `disconnect` prints `Disconnected: NOUS revoked this device's access…` and the probe prints `grant:403`. Before slice A, `bearer:200` is the known gap: record it as **FAIL (known, slice A)**, not PASS. After slice A it must print `bearer:401`.

## Phase 2: write proof (only after slice A is merged AND deployed, and after the Phase 2 values PR)

Do not start Phase 2 until Gate 1 step 2 has been re-run with slice A's merge commit in place of `271bd5c1f`.

```sh
pnpm --filter @nous/harness-bridge start connect --store "$STATE" --api "$API" \
  --project "$PROJECT_ID" --label harness-proof --tools --publish --write
pnpm --filter @nous/harness-bridge start workspace add --store "$STATE" --root "$FIXTURE" --label harness-proof-fixture
jq -c '{deviceId, scopes, workspace: .workspaces[0].id}' "$STATE/connection.json"
pnpm --filter @nous/harness-bridge start run --store "$STATE"
```

Expected scopes: `harness:execute`, `tools:read`, `artifacts:publish` and `tools:write`.

**P2-1: publish and hash.**

```sh
printf 'harness live proof %s\n' "$(date -u +%FT%TZ)" > "$FIXTURE/proof.md"
shasum -a 256 "$FIXTURE/proof.md"
uuidgen | tr 'A-Z' 'a-z'      # publication_id
```

In the chat thread with Local Codex, send: `Call the nous artifacts_publish tool with relative_path "proof.md", title "harness live proof", publication_id "<uuid>". Then call it again with the same arguments. Reply with both results verbatim.` Expected: both results carry the same `version_id`, and their `sha256` equals the local `shasum` value. The artifact appears in the thread's artifact panel.

**P2-2: approval after the grant lifetime.**

```sh
uuidgen | tr 'A-Z' 'a-z'      # invocation_id
date -u +%FT%TZ               # requested_at
```

Send: `Call the nous request_action tool with action "create_project_note", invocation_id "<uuid>", title "harness live proof note", content "approved after grant expiry". Reply with the result verbatim.` Expected: `Not created yet. The user must approve this note in NOUS: https://goodwiinz.tech/integrations/actions/<uuid>…`. Leave `nous-harness run` running. Wait at least 16 minutes, then open the approval URL as the project owner, approve, and record `date -u +%FT%TZ` as `approved_at`. Then send: `Call the nous get_action_status tool with invocation_id "<uuid>".` Expected: `Created. …`, and the note is visible in the project notes. Record `approved_at - requested_at`; it must be at least 16 minutes.

**P2-3: disconnect, then probe.** Repeat P1-3. Expected: `grant:403` and `bearer:401`.

## Rollback and kill switch

- **Stop new work (USER APPROVAL REQUIRED):** open a PR that sets `HARNESS_BRIDGE_ENABLED`, `NOUS_MCP_ENABLED` and `ARTIFACTS_ENABLED` to `"false"` in `values-aws.yaml`, and merge it. Argo CD `nous-dev-aws` auto-syncs `develop`. Release Dev deliberately skips a rebuild when only `values-aws.yaml` changed. Never `kubectl edit/patch/set env`: self-heal reverts it and it bypasses review.
- **Expected after the flags are off:** a new Codex run is refused (`Local harness execution is disabled by server policy`). `GET $API/integrations/tools` with a live grant returns 503, and so does a new `request_action`. `get_action_status` for an existing invocation still answers. Accepted Codex runs still reconcile to a terminal state. Approved actions wait and do not execute.
- **Device side:** stop `nous-harness run` (Ctrl-C) and run `nous-harness disconnect --store "$STATE"`. A grant UUID can also be revoked through the owner API `DELETE $API/integrations/grants/{grant_id}`. Do not edit or delete `journal.sqlite`; for uncertain interrupts use `recover-interrupt` as described in the package README.
- **Schema:** the harness, artifact and integration migrations are additive, and the flags are the rollback. Do not run `alembic downgrade` on RDS.

## Evidence table

Copy this table into a new bundle `docs/testing/evidence/harness-live-proof-YYYYMMDD/README.md` and add a row for the bundle to [evidence/README.md](evidence/README.md). Never record tokens, passwords, emails, local absolute paths or source text.

| # | Check | Evidence to record | Result |
| --- | --- | --- | --- |
| 0a | Alembic head on throwaway PostgreSQL | SHA, `postgres --version`, the eight revision lines, `check_alembic.py` output | NOT RUN |
| 0b | Outbox `SKIP LOCKED`, two sessions | SHA, pytest summary line | NOT RUN |
| 0c | Unit suites and bridge package | SHA, summary lines | NOT RUN |
| G1 | Release unblocked, image contains required commits, migrate job Complete | Image tag and digest, `merge-base` output, job name | NOT RUN |
| G1-flags | Flag state per pod (Phase 1, then Phase 2) | grep output for backend, worker and beat; Argo CD revision | NOT RUN |
| P1-1 | Chat run, reload, single approval | Playwright summary; `thread_id`, `run_id`, `device_id`, `workspace_id` | NOT RUN |
| P1-2 | MCP read | `run_id`, returned document ids | NOT RUN |
| P1-3 | Disconnect, then probes | `grant:` and `bearer:` codes; bearer 200 is FAIL (known, slice A) | NOT RUN |
| P2-1 | Publish and SHA-256 | `artifact_id`, `version_id` from both calls, local and stored SHA-256 | NOT RUN |
| P2-2 | Approval after more than 15 minutes | `invocation_id`, `requested_at`, `approved_at`, delta, final state | NOT RUN |
| P2-3 | Disconnect, then probes after slice A | `grant:403`, `bearer:401` | NOT RUN |
| RB | Flags off (if rehearsed) | PR number, Argo CD revision, 503 codes, existing status 200 | NOT RUN |
