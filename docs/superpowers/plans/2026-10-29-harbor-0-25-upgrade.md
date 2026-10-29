# Harbor 0.6.6 → 0.25 Upgrade Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Status:** draft, 2026-10-29. Nothing implemented. Owner decisions D1–D3 below are open.

**Goal:** Run the NOUS eval suite on Harbor 0.25.x without losing comparability with the recorded 0.6.6 baselines, then retire the deprecated 0.6.6 idioms.

**Why now:** every `evals/*/task.toml` (18 files), source manifest and baseline records `harbor_version = "0.6.6"`. The installed CLI is a uv tool at 0.6.6. Upstream (https://github.com/harbor-framework/harbor; `laude-institute/harbor` redirects there) shipped 0.25.0 on 2026-10-25. The forward link from [Plan 3's 2026-10-29 amendment](2026-08-08-tool-coverage-plan-3-kg-and-memory.md) points here.

**Evidence base:** a static diff of the 0.6.6 and 0.25.0 sdists plus the upstream CHANGELOG, done 2026-10-29. Nothing was run end to end on 0.25. Every "no break" claim below is a static finding and is unproven until Task 3 runs.

## What changes for us (static findings)

| Area | Change (version) | Effect on our files |
|---|---|---|
| task.toml `allow_internet` | Deprecated (0.13.0). `true` maps to `[environment].network_mode = "public"`; new modes are `no-network` (egress sidecar) and `allowlist` | Loads with a DeprecationWarning. The comment in our task.toml files ("false forces network_mode:none") is stale. |
| task.toml `schema_version` | Default became `1.3` (0.13.0), then `1.4` (0.21.0). Not validated | Our `1.2` still loads. A bump is optional (D1). |
| Resource defaults | Removed (0.8.0) | No effect: we set `cpus`/`memory_mb`/`storage_mb`/`gpus` explicitly. |
| Reward contract | `reward.json` now wins over `reward.txt` (0.7.0). Non-finite rewards are rejected. Missing reward file still means infra error | No effect: we write only `reward.txt`, and exit 0/10/else still holds. |
| Compose | `docker-compose-base.yaml` replaced by a generated mounts override. `[environment.env]` is also injected into `services.main.environment` (0.20.0) | Same values as today. Sidecars (neo4j, Postgres, Squid) on our own `networks:` are untouched in `public` mode. |
| Job config | Keys unchanged. New check that per-agent `n_concurrent` ≤ `n_concurrent_trials` | `evals/agent-full-benchmark-v1.json` loads as-is. |
| CLI | `--agent-import-path` is hidden and deprecated in favor of `--agent module:Class` (0.16.0). `harbor run` = `harbor job start` | `evals/README.md:102` command still works but is deprecated. |
| Trial artifacts on the host | Stored at their full container path, e.g. `<trial>/artifacts/logs/agent/evidence.json` (0.14.0) | No reader of the old layout found under `evals/`, `scripts/` or `tests/`. Check the recording procedure in Task 3. |
| ATIF | v1.8 adds audio only (0.23.0). v1.0–1.7 still validate | `harbor_common.trajectory` stays on v1.7. |
| Custom agent | `SUPPORTS_ATIF = True` is deprecated in favor of `capabilities = AgentCapabilities(atif=True)` (0.23.0), with removal planned for the next major | `evals/harbor_agents/nous_production_agent.py:23` warns. `harbor.agents.capabilities` does not exist in 0.6.6. |
| Python | `>=3.12` in both versions | No change. |

## Global constraints

- **Comparability first.** A baseline's identity includes its `harbor_version` and `harness_digest`. A 0.25 result is not comparable to a 0.6.6 baseline until a parity run on the same task tree shows the same rewards (Task 3).
- **Recorded artifacts are history.** Do not edit existing `evals/source-manifests/*.json`, `evals/baselines/*.json` or recorded sections of `evals/AGENT_FLOW_BASELINE.md` to say 0.25. New runs get new dated manifests, and current-contract text gets dated amendments (see `docs/AGENTS.md`).
- **Digest-pinned tasks.** Any task-dir or `harbor_common`/`harbor_agents` edit changes the digests. Phase A changes none of them.
- **Plan 3 Task 5 records on 0.6.6.** Gating capabilities 7 and 9 should not wait on, or be confounded by, this upgrade.
- **Never patch a verifier or judge to make a 0.25 run pass.** A reward difference between versions is the signal this plan exists to find.

## Owner decisions (open)

- **D1, schema bump:** keep `schema_version = "1.2"` (recommended for Phase A, zero digest change), or bump to `1.4` in Phase B together with `network_mode`.
- **D2, cutover order:** record Plan 3 Task 5 on 0.6.6 first, then start Phase A (recommended). Or run Phase A first and accept that capabilities 7 and 9 get their first gate on 0.25.
- **D3, Phase B shape:** edit the 18 task.toml files in place (new digests and dated manifests for every task). Or leave the frozen originals on deprecated keys and only use the new idioms in new or `-v2` tasks.

---

### Task 0: side-by-side CLI (no repo change)

- [ ] Leave the 0.6.6 uv tool installed. Run 0.25 without replacing it: `uvx --from 'harbor==0.25.0' harbor --version`.
- [ ] Record both `--version` outputs and the resolved dependency set in the PR body.

### Task 1: Phase A load check, zero edits (no Docker)

- [ ] With 0.25, load every `evals/*/task.toml` and `evals/agent-full-benchmark-v1.json` through Harbor's own config models. Expect only DeprecationWarnings (`allow_internet`, `SUPPORTS_ATIF`). Any validation error stops the plan and gets an amendment here.
- [ ] Import `evals.harbor_agents.nous_production_agent:NousProductionAgent` under 0.25 and record the warnings.
- [ ] `harbor_common/selftest.py` → `selftest ok`. Run each task's calibration fixtures (`BENCHMARK_CALIBRATION_FIXTURE` + `VERIFIER_REPORT_PATH`). These do not depend on Harbor and must match 0.6.6 exactly.

### Task 2: Phase A smoke run (Docker; manual)

- [ ] Run one sidecar-heavy task (`agent-knowledge-graph-flow-v1`: neo4j + Postgres + Squid) and one single-container task under 0.25 with `--agent module:Class`. Check that the egress boundary probes still pass, that sidecars stay on `benchmark-internal`, and that `reward.txt` is read.
- [ ] Check where trial artifacts land on the host (0.14.0 layout) and update the recording procedure in `evals/README.md` if it names old paths.

### Task 3: parity run (Docker + Azure + judge creds; manual, GATED)

- [ ] For every task that is GATED today, plus capabilities 7 and 9 once Plan 3 Task 5 is recorded: run 5 trials on 0.6.6 and 5 trials on 0.25 against the same `repository_revision` and image digest.
- [ ] Parity passes when the reward for each trial class (objective, semantic, infra) matches within the task's gate (objective 5/5, semantic ≥4/5, 0 infra). Write the parity table into a new dated source manifest and baseline with `harbor_version: "0.25.0"`.
- [ ] If parity fails, root-cause the difference before going further. Do not adjust verifiers to match.

### Task 4: cutover docs

- [ ] `evals/README.md`: switch the run command to `--agent evals.harbor_agents.nous_production_agent:NousProductionAgent`, name 0.25 as the supported CLI, and keep a line for reproducing 0.6.6 baselines with `uvx --from 'harbor==0.6.6'`.
- [ ] `evals/AGENT_FLOW_BASELINE.md`: add a dated amendment for the `harbor_version` row and leave the recorded text alone.
- [ ] `evals/specs/harness-mcp-discovery-v1/harness.md` cites 0.6.6 `ClaudeCode.CLI_FLAGS`. Add a dated note that 0.25 uses the `disallowed_tools`/`disable_web_search` options.
- [ ] Older plan docs that use `--agent-import-path` stay as they are (historical).

### Task 5: Phase B, retire deprecated idioms (after D1/D3; new digests)

- [ ] `NousProductionAgent`: set `capabilities = AgentCapabilities(atif=True)` and remove `SUPPORTS_ATIF`. This drops 0.6.6 support for the agent, so do it only after cutover.
- [ ] Per D3: replace `allow_internet = true` with `[environment] network_mode = "public"` and fix the stale network comment. Keep Squid: under `allowlist`, Harbor does not confine a `main` service that declares its own `networks:`, which ours does.
- [ ] Per D1: bump `schema_version`.
- [ ] Recompute digests and write new dated manifests, then re-run calibration (Task 1) and one smoke run (Task 2).

## Risks

1. **Static-only evidence.** No 0.25 run has happened. Tasks 1–2 exist to turn the "no break" claims into observed results.
2. **Base image pullability.** Task Dockerfiles still use `registry.digitalocean.com/ragsystemregistry/backend:3a436b2-r1` after the AWS move. If that registry is gone, both Harbor versions are blocked equally. Check this before Task 2.
3. **Silent environment drift.** The generated compose overrides (mounts, env injection) could change which container sees which variable. The network boundary probes are the guard, along with the `env_flags` assertions in the verifiers that have them; do not weaken either.
4. **Deprecated keys being removed.** `allow_internet` and `SUPPORTS_ATIF` are only scheduled for removal at the next major version. Phase A is safe on 0.25.x but not on 1.0, so pin `harbor==0.25.*` until Phase B lands.
