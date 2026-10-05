# Harness live-proof runbook

**Status:** Procedure, written 2026-10-04 against `origin/develop` `e0d0fcf79` and amended 2026-10-05 against `be0edb2f3` for the merged #1863 and #1866, and again on 2026-10-05 on `feat/plan06-slice5` for Plan 06 (same-project continuation, [plan](../plans/2026-10-05-same-project-harness-continuation.md)): Phase 3, Gate 0a and 0d, flags, Gate 1 step 2 and the P2-1 gap. No step below has run end to end against a live environment. Every live row stays **NOT RUN** until a dated evidence bundle under [evidence/](evidence/README.md) records it. A skipped test is NOT RUN, never PASS.

**Target:** the AWS dev lane only: backend `https://dev-api.goodwiinz.tech`, Argo CD application `nous-dev-aws`, namespace `multimodal-rag-system`, values file [`values-aws.yaml`](../../infrastructure/helm/knowledge-graph-analytics/values-aws.yaml), frontend `https://goodwiinz.tech` (Vercel). The frozen DigitalOcean `values-dev.yaml` (`rag-dev`) and the staging/production files are not targets.

Contracts: [harness bridge](../engineering/harness-bridge.md), [bridge package](../../packages/harness-bridge/README.md), [testing](../engineering/testing.md).

## What the proof shows

1. A Codex run started from NOUS chat on a paired device asks for one command approval, survives a reload, is approved once, and persists exactly one user and one assistant marker ([`harness-bridge.spec.ts`](../../tests/e2e/tests/harness-bridge.spec.ts)).
2. Codex reads project sources through the NOUS MCP read gateway.
3. Phase 2: Codex publishes a workspace file as an artifact, and the stored SHA-256 matches the local file.
4. Phase 2: a `request_action` note approved in the browser more than 15 minutes after the request still executes. Grants expire after 15 minutes, so the approval rests on the consent, not the original grant.
5. After `nous-harness disconnect`, credentials that answered 200 just before are refused. Remediation slice A (#1863, merged as `613fe13bf`: `disconnect` also ends the user's CLI logins) is not in the image pinned on dev (`8ad2c82`) yet. Without it in the deployed image the old grant answers 403 and the old CLI bearer still answers 200 (the known gap). With it both answer 401, because the revoked bearer is checked before the grant.
6. Setting the flags back to `false` refuses new work with 503 while status reads and reconciliation still answer.
7. Phase 3: a second, standalone Codex session on another device continues the first one's work in the same project and chat. A fresh browser finds the published files on the project Files tab and the handoff card in the chat without knowing any id. The second session reads handoff v1, publishes a new version of one file without changing the first version, and saves handoff v2 on parent 1.

## Flags and scopes

| Flag | Gates | Phase 1 | Phase 2 | Phase 3 |
| --- | --- | --- | --- | --- |
| `HARNESS_BRIDGE_ENABLED` | New Codex chat runs (`backend/src/api/agent/harness_streaming.py`) and start dispatch (`backend/src/services/harness/delivery.py`) | `true` | `true` | not used (standalone Codex via `mcp install`); leave as Phase 2 left it |
| `NOUS_MCP_ENABLED` | `/integrations/tools`, new `/integrations/actions` requests, the approved-action drain (`backend/src/services/agent/tool_actions.py`), new handoff saves (`POST /integrations/handoffs`; handoff reads stay ungated) | `true` | `true` | `true` |
| `ARTIFACTS_ENABLED` | Artifact upload and version writes (`backend/src/api/artifacts.py`); project and thread artifact reads stay ungated | `false` | `true` | `true` |
| `ARTIFACT_EDITING_ENABLED`, `ARTIFACT_PREVIEW_ENABLED`, `ARTIFACT_SHARING_ENABLED` | Inert on develop (defined in `backend/src/core/config.py`, read by no backend, frontend or Helm code). Nothing is safe to expose: no route enforces `artifacts:read/edit/share`, and since #1866 a grant request for them is refused | unset (`false`) | unset (`false`) | unset (`false`) |

All six default to `false` in `backend/src/core/config.py`. `HARNESS_BRIDGE_ENABLED`, `NOUS_MCP_ENABLED` and `ARTIFACTS_ENABLED` go in the `backend.env` list of `values-aws.yaml`; the three inert flags stay out of it. The backend, Celery worker, Celery beat and migration-job pods and the `nous-dev-aws-synthetic-traffic` CronJob (`suspend: true` today, so no pod) all render that list, and an explicit `env` entry wins over the `envFrom` secrets.

`NOUS_MCP_ENABLED` cannot separate reads from writes. The scope requested at `connect` is what separates them. **Phase 1 connects with `--tools` only.** `--publish` (`artifacts:publish`) and `--write` (`tools:write`) belong to Phase 2, which must wait until remediation slice A (#1863, `613fe13bf`: `disconnect` also revokes the CLI bearer) is **deployed**; it is merged. Until then `disconnect` revokes the grant but leaves the CLI bearer valid.

## Gate 0: repository evidence (no cluster)

Run from the repository root of a checkout at the SHA under test, with a local PostgreSQL 14 or later. No Docker is needed. `PY` is the main checkout's backend interpreter, because worktrees have no venv.

```sh
PY=/absolute/path/to/main/checkout/backend/.venv/bin/python
PGDIR=$(mktemp -d)
initdb -D "$PGDIR/data" -U postgres --auth=trust -E UTF8 --locale=C
pg_ctl -D "$PGDIR/data" -o "-p 55432 -k $PGDIR" -l "$PGDIR/pg.log" start
psql -h 127.0.0.1 -p 55432 -U postgres -c 'create database mig' -c 'create database orch'

# 0a. Migrations hb01..hb06, aw01/aw02, it01/it02 apply from empty. A failing
# step ends 0a with its status, so nothing after a failed upgrade can mask it.
(set -euo pipefail
  cd backend
  export DATABASE_URL=postgresql://postgres@127.0.0.1:55432/mig ENVIRONMENT=testing
  unset SUPABASE_DB_URL
  "$PY" -m alembic upgrade head 2>&1 | grep -E -- '-> (hb0[1-6]|aw0[12]|it0[12])_'
  "$PY" ../scripts/ci/check_alembic.py
  "$PY" -m alembic current)

# 0b. Two-session SKIP LOCKED on the harness outbox.
ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch ENVIRONMENT=testing \
  PYTHONPATH=backend "$PY" -m pytest -c backend/pytest.ini -q \
  backend/tests/integration/test_harness_dispatch_postgres.py

# 0d. Plan 06 same-project journey: consent bound to a chat, publication,
# fresh-browser discovery, a second device continuing, handoff races.
ORCHESTRATION_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/orch ENVIRONMENT=testing \
  PYTHONPATH=backend "$PY" -m pytest -c backend/pytest.ini -q \
  backend/tests/integration/test_same_project_continuation.py

# 0c. Unit suites and bridge package.
pnpm install --frozen-lockfile    # Node 24
PYTHONPATH=backend "$PY" -m pytest -c backend/pytest.ini -q \
  backend/tests/unit/services/harness backend/tests/unit/services/artifacts
pnpm --filter @nous/harness-bridge test
pnpm --filter @nous/harness-bridge type-check

pg_ctl -D "$PGDIR/data" stop -m fast
```

Expected: 0a prints ten `Running upgrade` lines, one for each of `hb01_integration_grants`, `hb02_harness_runs`, `hb03_bridge_delivery`, `hb04_harness_approvals`, `aw01_artifact_workspace`, `aw02_artifact_lifecycle`, `it01_integration_actions`, `it02_merge_integration_heads`, `hb05_grant_request_thread` and `hb06_integration_handoffs`, then `alembic current` prints the single head `hb06_integration_handoffs (head)`, which is the revision `check_alembic.py` reports (`single head 'hb06_integration_handoffs', 113 revisions` on `feat/plan06-slice5`, 2026-10-05, local PostgreSQL 14.23). Until Plan 06 Slices 2 and 3 (#1882, #1885) merge, `develop` prints only the first eight lines and its head is `d4a6c8e0f2b3`; judge a checkout by the head `check_alembic.py` reports for it. Without `-E UTF8` an initdb under an empty locale creates a `SQL_ASCII` cluster, and the upgrade then stops with `UnicodeEncodeError: 'ascii' codec` part way through the chain. The 0a subshell stops at its first failing command, so a traceback with no `alembic current` line, or a non-zero `echo $?` straight after it, is FAIL, never PASS. Run it only against a freshly created `mig`: on an already migrated database the upgrade prints no matching line and 0a fails. 0b and 0d each print `1 passed`. In hosted CI both are skipped, because no workflow sets `ORCHESTRATION_TEST_DATABASE_URL`, so the hosted result is NOT RUN. 0d runs in-process with mocked Redis and in-memory artifact storage, so it proves grant revocation (403) but not the CLI-bearer cutoff (401); that stays with P1-3 and P2-3. The hosted equivalent of 0a is the Test Pipeline job **Alembic Migration Check**, step **Advisory — upgrade head from empty DB**. That step is `continue-on-error`, so read its log for the eight `Running upgrade` lines; do not rely on the step colour.

Not covered here: the artifact lifecycle `SKIP LOCKED` (`backend/src/services/artifacts/lifecycle.py`), the `HarnessCommand` lease lock (`lease_commands`, `backend/src/services/harness/delivery.py:285`) and the `tool_actions` approved-to-executing compare-and-set have SQLite unit coverage only. Record them as NOT RUN on PostgreSQL.

## Gate 1: deployment prerequisites (USER-RUN)

Read-only cluster commands need `aws login` and the EKS kubeconfig.

1. **Release pipeline delivers images again.** Release Dev has failed since 2026-10-01 at **Verify release branch protection** (`gh: Resource not accessible by personal access token`; for example run 37181835414 on 2026-10-04). The release token needs `Administration: Read`. Until this is fixed, nothing merged after `8ad2c82` reaches dev. After the token is fixed, a release starts from the next push to `develop`, or from re-running the Release Dev run for `develop`'s current head.
2. **The deployed image contains the bridge.** The image must include `271bd5c1f` (`disconnect`, #1790), `6e1452e66` (grant renewal, #1786) and `bfc33c342` (`request_action`, `it01`, #1780). Phase 2 also needs `613fe13bf` (slice A, #1863: `disconnect` ends the user's CLI logins). The image pinned on 2026-10-04 is `8ad2c82` (2026-09-30) and contains none of the four.

   Phase 3 also needs Plan 06: Slice 1 #1879 (project artifact list and `list_project_artifacts` read tool), Slice 2 #1882 (chat-bound consent, `hb05_grant_request_thread`), Slice 3 #1885 (handoffs, `hb06_integration_handoffs`), Slice 4 #1886 (bridge binding reuse and handoff CLI), and Slice 5 (this runbook, the journey test and the `publish_version` flush-order fix; PR TBD). Without that fix every publish on PostgreSQL answers 409, so P2-1 and Phase 3 cannot pass on an image that lacks it. Merge commits: TBD, filled in when each PR merges; check each with `git merge-base --is-ancestor <merge commit> "$TAG"` as above.

   Read the promoted tag from the freshly fetched `origin/develop`, never from the working tree: Release Dev records a promoted image in a later commit, and `git fetch` does not change a checked-out file.

   ```sh
   git fetch origin develop
   TAG=$(git show origin/develop:infrastructure/helm/knowledge-graph-analytics/values-aws.yaml \
     | awk '/^backend:/{b=1} b&&/tag:/{gsub(/"/,"",$2);print $2;exit}')
   git merge-base --is-ancestor 271bd5c1f "$TAG" && echo "bridge in image $TAG" || echo "bridge NOT in image $TAG"
   git merge-base --is-ancestor 613fe13bf "$TAG" && echo "slice A in image $TAG" || echo "slice A NOT in image $TAG"
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
pnpm --filter @nous/harness-bridge start run --store "$STATE"     # leave running in its own terminal until P1-3 stops it
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

**P1-3: disconnect, then probe.** Copy the state before disconnecting so the old credentials can be replayed. The copy holds secrets: never print them, and delete the copy straight after. Replay them twice with the same two requests, first as a control while they are still live, then after `disconnect`.

```sh
PROBE=$(mktemp -d) && cp -Rp "$STATE"/. "$PROBE"/
H=$(jq -r .credentialHandle "$PROBE/connection.json")
AT=$(jq -r .accessToken "$PROBE/$H.json"); GT=$(jq -r .grantToken "$PROBE/$H.json")
probe() {
  curl -s -o /dev/null -w "$1 grant:%{http_code}\n" -H "Authorization: Bearer $AT" -H "X-NOUS-Integration-Grant: $GT" "$API/integrations/tools"
  curl -s -o /dev/null -w "$1 bearer:%{http_code}\n" -H "Authorization: Bearer $AT" "$API/integrations/devices"
}
probe before
```

The control must print `before grant:200` and `before bearer:200`. If it does not, stop before `disconnect`: the credentials are not usable, for example because the copy is stale (a grant lives 15 minutes and every renewal revokes the previous token). `rm -rf "$PROBE"`, fix the cause and copy again. Without this control a 403 cannot be told apart from a grant that had already expired or been renewed away. Once it passes, stop the bridge: press Ctrl-C in the terminal that runs `nous-harness run`. `disconnect` deletes the credentials but does not stop a running bridge, which would keep retrying with the old handle every five seconds and never exit, and the Phase 2 `run` would then be a second bridge on the same `$STATE/journal.sqlite`. Then, in the same shell:

```sh
pnpm --filter @nous/harness-bridge start disconnect --store "$STATE"
probe after
unset AT GT H; unset -f probe; rm -rf "$PROBE"
```

Expected: `disconnect` prints `Disconnected: NOUS revoked this device's access…`. The codes after it depend on whether the deployed image contains slice A, `613fe13bf` (#1863, merged; the second check in Gate 1 step 2):

- Without slice A: `after grant:403` and `after bearer:200`. `bearer:200` is the known gap: record it as **FAIL (known, slice A)**, not PASS.
- With slice A: `after grant:401` and `after bearer:401`. The revoked CLI bearer is rejected before the grant is looked up, so `grant:401` says nothing about the grant itself. Evidence for the grant comes from the `after grant:403` of a run without slice A, or from the grant row's `revoked_at` (table `integration_grants`). `after bearer:200` is a FAIL here, for example when Redis was unreachable: the CLI cutoff is written after the revocation commits and is best-effort, so `disconnect` still reports success. The cutoff covers every CLI login of that user, not only this device's, so run the proof as the test user: other machines logged in as that user must `connect` again.

## Phase 2: write proof (only after slice A is deployed, and after the Phase 2 values PR)

Do not start Phase 2 until Gate 1 step 2, re-run, prints `slice A in image`.

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

In the chat thread with Local Codex, send: `Call the nous artifacts_publish tool with relative_path "proof.md", title "harness live proof", publication_id "<uuid>". Then call it again with the same arguments. Reply with both results verbatim.` Expected: both results carry the same `version_id`, and their `sha256` equals the local `shasum` value. Phase 2 connects without `--chat`, so the grant carries no thread or run and `publish_version` (`backend/src/services/artifacts/service.py`) stores the version and its reference with `thread_id` and `run_id` null. The thread's artifact panel (`GET $API/artifacts/threads/{thread_id}`) lists only references whose `thread_id` matches, so the file does not appear there; that is expected, not a failed publish. With Plan 06 Slice 1 (#1879) deployed, the file is listed on the project's Files tab (`GET $API/artifacts/projects/$PROJECT_ID`) with `thread_id` null; record that. A connection made with `--chat <thread>` (Slice 2, #1882) binds the grant to that chat, and its publications land in the chat's panel as well; Phase 3 proves it.

**P2-2: approval after the grant lifetime.**

```sh
uuidgen | tr 'A-Z' 'a-z'      # invocation_id
date -u +%FT%TZ               # requested_at
```

Send: `Call the nous request_action tool with action "create_project_note", invocation_id "<uuid>", title "harness live proof note", content "approved after grant expiry". Reply with the result verbatim.` Expected: `Not created yet. The user must approve this note in NOUS: https://goodwiinz.tech/integrations/actions/<uuid>…`. Leave `nous-harness run` running. Wait at least 16 minutes, then open the approval URL as the project owner, approve, and record `date -u +%FT%TZ` as `approved_at`. Then send: `Call the nous get_action_status tool with invocation_id "<uuid>".` Expected: `Created. …`, and the note is visible in the project notes. Record `approved_at - requested_at`; it must be at least 16 minutes.

**P2-3: disconnect, then probe.** Repeat P1-3, control and bridge stop included. Slice A is deployed by now, so expect `before grant:200` and `before bearer:200`, then `after grant:401` and `after bearer:401`.

## Phase 3: same-project continuation (only after Plan 06 is deployed)

All steps are USER-RUN against the AWS dev lane (`values-aws.yaml`, Argo CD `nous-dev-aws`), never the frozen DigitalOcean `rag-dev`. Every row stays **NOT RUN** until it runs. Start only when Gate 1 step 2 shows the Plan 06 commits in the image, Gate 1 step 3 shows `hb05_grant_request_thread` and `hb06_integration_handoffs` applied, and Gate 1 step 4 shows `ARTIFACTS_ENABLED` and `NOUS_MCP_ENABLED` set to `"true"`. Phase 3 needs neither `--write` nor `HARNESS_BRIDGE_ENABLED`: the Codex sessions are standalone, launched from a terminal with NOUS registered through `mcp install`, not started from NOUS chat.

Use two state directories for the two devices, and one disposable fixture per session. In the NOUS UI, create a chat in the test project and copy its id from `?thread=`.

```sh
export THREAD_ID=<chat UUID in $PROJECT_ID>
export STATE_A="$HOME/.nous/harness-proof-a" STATE_B="$HOME/.nous/harness-proof-b"
pnpm --filter @nous/harness-bridge start connect --store "$STATE_A" --api "$API" \
  --project "$PROJECT_ID" --chat "$THREAD_ID" --label proof-a --tools --publish --handoff
pnpm --filter @nous/harness-bridge start workspace add --store "$STATE_A" --root "$FIXTURE" --label proof-a-fixture
pnpm --filter @nous/harness-bridge start status --store "$STATE_A"
pnpm --filter @nous/harness-bridge start mcp install --store "$STATE_A" --root "$FIXTURE"   # run the printed codex command
```

Expected: the approval page names the project and the chat title; scopes are `tools:read`, `artifacts:publish`, `handoff:read` and `handoff:write`; `status` prints the project, the chat and the grant expiry.

**P3-1: first session publishes and leaves a handoff.** Put `analysis.py`, `data.csv` and `plot.png` in `$FIXTURE` and record `shasum -a 256` of each. In a Codex session started in `$FIXTURE`, send: `Call the nous list_project_documents tool and reply with the document ids only. Then call artifacts_publish for analysis.py, data.csv and plot.png, each with a new publication_id, and reply with each version_id and sha256.` Then save handoff v1 from the CLI, naming those three version ids in `results`:

```sh
pnpm --filter @nous/harness-bridge start handoff save --store "$STATE_A" --file handoff-v1.json   # no --parent: first handoff
pnpm --filter @nous/harness-bridge start handoff list --store "$STATE_A"                          # no pending entries
```

Expected: each `sha256` equals the local `shasum`, and `handoff save` reports version 1. Close the Codex session.

**P3-2: fresh browser.** In a private window, log in as the project owner. Open the project's Files tab: all three files are listed with the chat as their source. Download each and compare `shasum -a 256` with P3-1. Open the chat: the handoff card shows version 1 with the goal and the three results.

**P3-3: second session continues.** On the second device (or with `$STATE_B` on the same machine), connect to the same project and chat, then confirm reuse by running `connect` again:

```sh
pnpm --filter @nous/harness-bridge start connect --store "$STATE_B" --api "$API" \
  --project "$PROJECT_ID" --chat "$THREAD_ID" --label proof-b --tools --publish --handoff
pnpm --filter @nous/harness-bridge start connect --store "$STATE_B" --api "$API" \
  --project "$PROJECT_ID" --chat "$THREAD_ID" --label proof-b --tools --publish --handoff   # prints "Reusing binding", no browser
pnpm --filter @nous/harness-bridge start handoff show --store "$STATE_B"
```

Expected: `handoff show` prints version 1, identical to P3-1. Change `plot.png`, record its new `shasum`, and in a new Codex session registered for `$STATE_B` send: `Call the nous artifacts_publish tool with relative_path "plot.png", title "plot.png", a new publication_id, artifact_id "<plot artifact_id from P3-1>" and expected_parent_version_id "<plot version_id from P3-1>". Reply with the result verbatim.` Expected: the same `artifact_id`, a new `version_id`, and the new `sha256`. Then:

```sh
pnpm --filter @nous/harness-bridge start handoff save --store "$STATE_B" --file handoff-v2.json --parent 1
```

Expected: the plot's version history in the browser shows two versions, and the first version still downloads with its P3-1 checksum. `handoff save` reports version 2, and after a reload the chat card shows version 2. A save with `--parent 1` repeated with a new `handoff_id` answers 409 and prints version 2 as the latest; record it, then `handoff discard <handoff_id>` it.

**P3-4: clean up.** `disconnect` both state directories, as in P1-3.

**Evidence.** Record in `docs/testing/evidence/harness-live-proof-YYYYMMDD/README.md`, created only when the phase runs: the deployed image tag, the device ids and grant ids of both sessions, the `thread_id`, the artifact and version ids, local and stored SHA-256 for every file and version, the handoff versions and `handoff_id`s, and the 409 code from P3-3. Record only redacted ids and hashes. Never record raw database rows, document text, file contents, tokens, emails or local absolute paths.

## Rollback and kill switch

- **Stop new work (USER APPROVAL REQUIRED):** open a PR that sets `HARNESS_BRIDGE_ENABLED`, `NOUS_MCP_ENABLED` and `ARTIFACTS_ENABLED` to `"false"` in `values-aws.yaml`, and merge it. Argo CD `nous-dev-aws` auto-syncs `develop`. Release Dev deliberately skips a rebuild when only `values-aws.yaml` changed. Never `kubectl edit/patch/set env`: self-heal reverts it and it bypasses review.
- **Expected after the flags are off:** a new Codex run is refused (`Local harness execution is disabled by server policy`). `GET $API/integrations/tools` with a live grant returns 503, and so does a new `request_action`. `get_action_status` for an existing invocation still answers. Accepted Codex runs still reconcile to a terminal state. Approved actions wait and do not execute.
- **Device side:** stop `nous-harness run` (Ctrl-C) and run `nous-harness disconnect --store "$STATE"`. A grant UUID can also be revoked through the owner API `DELETE $API/integrations/grants/{grant_id}`; with a browser login that revokes the grant and its consent only and leaves CLI logins alone (a CLI-authenticated DELETE, which is what `disconnect` sends, also ends them), and the browser device list and revoke routes of #1788 are not on `develop`. Do not edit or delete `journal.sqlite`; for uncertain interrupts use `recover-interrupt` as described in the package README.
- **Schema:** the harness, artifact and integration migrations are additive, and the flags are the rollback. Do not run `alembic downgrade` on RDS.

## Evidence table

Copy this table into a new bundle `docs/testing/evidence/harness-live-proof-YYYYMMDD/README.md` and add a row for the bundle to [evidence/README.md](evidence/README.md). Never record tokens, passwords, emails, local absolute paths or source text.

| # | Check | Evidence to record | Result |
| --- | --- | --- | --- |
| 0a | Alembic head on throwaway PostgreSQL | SHA, `postgres --version`, the ten revision lines (eight before Plan 06 merges), the `alembic current` line, `check_alembic.py` output | NOT RUN |
| 0b | Outbox `SKIP LOCKED`, two sessions | SHA, pytest summary line | NOT RUN |
| 0d | Plan 06 same-project journey on PostgreSQL | SHA, pytest summary line | NOT RUN |
| 0c | Unit suites and bridge package | SHA, summary lines | NOT RUN |
| G1 | Release unblocked, image contains required commits, migrate job Complete | Image tag and digest, `merge-base` output, job name | NOT RUN |
| G1-flags | Flag state per pod (Phase 1, then Phase 2) | grep output for backend, worker and beat; Argo CD revision | NOT RUN |
| P1-1 | Chat run, reload, single approval | Playwright summary; `thread_id`, `run_id`, `device_id`, `workspace_id` | NOT RUN |
| P1-2 | MCP read | `run_id`, returned document ids | NOT RUN |
| P1-3 | Disconnect, then probes | `before` and `after` codes for `grant:` and `bearer:`, and whether the image had slice A (`613fe13bf`); `after bearer:200` is FAIL either way (known gap without slice A) | NOT RUN |
| P2-1 | Publish and SHA-256 | `artifact_id`, `version_id` from both calls, local and stored SHA-256 | NOT RUN |
| P2-2 | Approval after more than 15 minutes | `invocation_id`, `requested_at`, `approved_at`, delta, final state | NOT RUN |
| P2-3 | Disconnect, then probes after slice A | `before grant:200`, `before bearer:200`, `after grant:401`, `after bearer:401`; grant evidence: the P1-3 `after grant:403` or the grant row's `revoked_at` | NOT RUN |
| P3-1 | First session: publish three files, handoff v1 | device and grant id, `thread_id`, version ids, local and stored SHA-256, handoff version and `handoff_id` | NOT RUN |
| P3-2 | Fresh browser: Files tab and chat card | file list with source chat, download checksums, card version | NOT RUN |
| P3-3 | Second session: reuse, `handoff show` == v1, plot v2, handoff v2, stale save 409 | second device and grant id, `Reusing binding` line, both plot version ids and checksums, handoff v2, 409 | NOT RUN |
| P3-4 | Disconnect both sessions | `disconnect` output for both | NOT RUN |
| RB | Flags off (if rehearsed) | PR number, Argo CD revision, 503 codes, existing status 200 | NOT RUN |
