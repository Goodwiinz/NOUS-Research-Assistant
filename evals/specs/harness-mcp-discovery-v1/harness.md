Status: historical protocol with dated amendments below. Earlier trials retain
their exact recorded revisions. The corrected verified-results revision is
NOT RUN; host calibration does not establish a current capability decision.

Agent under test: an external harness, not NOUS code. Harbor 0.6.6's built-in `claude-code` and `codex` agents (`harbor/agents/installed/claude_code.py`, `codex.py`); `NousProductionAgent` is not involved. Each agent writes the task's `[[environment.mcp_servers]]` entry into its CLI's native config, and the CLI spawns `/opt/nous-eval/start-mcp.sh` inside `main`:

- `claude-code` writes `{"mcpServers": {"nous": {"type": "stdio", "command": "/opt/nous-eval/start-mcp.sh", "args": []}}}` to `$CLAUDE_CONFIG_DIR/.claude.json` (`CLAUDE_CONFIG_DIR=/logs/agent/sessions`) and runs `claude --verbose --output-format=stream-json --permission-mode=bypassPermissions --print -- <instruction>`.
- `codex` appends `[mcp_servers.nous]` with `command = "/opt/nous-eval/start-mcp.sh"` to `$CODEX_HOME/config.toml` (`CODEX_HOME=/tmp/codex-home`) and runs `codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --model <m> --json --enable unified_exec -- <instruction>`. Harbor's codex writer folds `args` into the single `command` string, which is why the task declares no args.

Both renderings were checked against Harbor 0.6.6 with this `task.toml` on 2026-10-07.

Source: repository revision `57fc21bc740e086c9e5880b1e1f26634c6a0f1c5` for the bridge, lockfile and generated catalog. The harness CLIs are installed by Harbor at trial setup (Claude Code via `https://claude.ai/install.sh`, Codex via `npm install -g @openai/codex@<version|latest>` under nvm Node 22) and are not pinned by the task; see "Versions".

Session: one non-interactive harness session per trial, one instruction (`instruction.md`), no scripted follow-up. The verifier never reads the harness transcript: tool-use state comes from the gateway double's `/events` log and the answer from `/app/answer.md`, so the result does not depend on Harbor's trajectory conversion for either CLI.

Credentials (never in `task.toml`, compose files or anything committed):

- `claude-code` reads `ANTHROPIC_API_KEY` (falling back to `ANTHROPIC_AUTH_TOKEN`), plus optional `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_OAUTH_TOKEN`, from the host environment of the `harbor run` process (`os.environ.get` in `ClaudeCode.run`). `-m anthropic/claude-sonnet-5-5` becomes `ANTHROPIC_MODEL=claude-sonnet-5-5` (provider prefix stripped for the official API).
- `codex` reads `OPENAI_API_KEY` (an `--ae OPENAI_API_KEY=...` value takes priority over the host environment) and writes it into `auth.json` under the trial container's Codex secrets directory; optional `OPENAI_BASE_URL` goes into `config.toml`. Harbor raises `Model name is required` without `-m` and passes only the text after the last `/` to `codex exec --model`, so the Codex CLI's own default model cannot be used.
- Both keys exist in Infisical dev under `/app`. Export only the key the run needs into the shell that runs Harbor (or pass `--env-file` with a git-ignored file). Do not commit `evals/jobs/`.

Models (pre-registered):

- `claude-code`: `-m anthropic/claude-sonnet-5-5`.
- `codex`: `-m openai/gpt-6.1-sol`, the model the local Codex CLI is configured to use (`~/.codex/config.toml`, codex-cli 0.153.4, read 2026-10-07). Confirm before trial 1 that this model is reachable with the `OPENAI_API_KEY` used for the run; if it is not, choose the replacement before any codex trial and record it here. Changing a model after a trial has run starts a new pre-registration.

Amendment 2026-10-07, before any trial ran. `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in Infisical dev `/app` are placeholders, and no other Infisical environment holds either key, so neither model above is reachable. Replacements:

- `codex`: the Azure OpenAI deployment `gpt-5.6-terra`, the strongest GPT deployment on the dev resource. Run with `-m openai/gpt-5.6-terra`, `OPENAI_BASE_URL=https://goodwiinzapix.cognitiveservices.azure.com/openai/v1` and `OPENAI_API_KEY` set to `AZURE_OPENAI_CHAT_API_KEY` from Infisical dev `/azure-openai`, both exported in the host shell. Harbor writes the key to `auth.json` and the base URL to `config.toml` (`codex.py` 755-776); Codex then posts to `<base>/responses` with a Bearer key. A wire-level probe on 2026-10-07 got HTTP 200 for Bearer auth, a streamed Responses call and a function call on this deployment.
- `claude-code`: Amazon Bedrock `us.anthropic.claude-sonnet-5-5` with a short-term Bedrock API key minted from the owner's AWS session (`aws-bedrock-token-generator`), exported in the host shell as `AWS_BEARER_TOKEN_BEDROCK` with `AWS_REGION=us-east-1`, plus `--ae ANTHROPIC_DEFAULT_HAIKU_MODEL=us.anthropic.claude-sonnet-5-5`. The dev Azure resource has no Claude deployment. Local AWS credentials had expired on 2026-10-07, so Claude Code trials stay NOT RUN until the owner runs `aws login`.

Decision with one harness: the rule below applies to each harness that has five scoreable trials. If only Codex has run, PRIMITIVES INSUFFICIENT for Codex opens Slice 5, and PRIMITIVES SUFFICE for Codex closes Slice 5 as not needed, recorded as Codex-only evidence. A later Claude Code run with 3 or fewer passes reopens it.

Versions: pin both CLIs for the decision run with `--ak version=<x>` and record the versions Harbor reports in each trial's `result.json` (`agent_info`). Versions observed locally on 2026-10-07: Claude Code 2.1.292, codex-cli 0.153.4. The pinned codex version must accept the image's `web_search = "disabled"` (checked on 0.153.4: an unknown key value fails config load, which would surface as an infrastructure failure).

Commands (repository root; Docker running; Harbor 0.6.6):

```bash
# Pre-flight: the catalog matches backend source, and the verifier calibrates.
PYTHONPATH=backend backend/.venv/bin/python \
  evals/harness-mcp-discovery-v1/environment/mock_nous/generate_catalog.py --check
# generate_catalog.py: 0 match, 1 drift, 2 generator failure (never a verdict).
for f in pass wrong-missing-external wrong-fabricated-id wrong-fabricated-doi \
         wrong-uncited-row wrong-symlinked-answer wrong-bypassed-bridge infra-no-catalog; do
  BENCHMARK_CALIBRATION_FIXTURE=evals/harness-mcp-discovery-v1/tests/calibration/$f.json \
  VERIFIER_REPORT_PATH=/tmp/harness-mcp-discovery-$f.json \
  backend/.venv/bin/python evals/harness-mcp-discovery-v1/tests/verify.py >/dev/null
  echo "$f -> $?"   # expect 0, then 10 for every wrong-*, then 2
done

# Claude Code (amended 2026-10-07): 5 trials against Bedrock.
# export AWS_BEARER_TOKEN_BEDROCK=<short-term key>  AWS_REGION=us-east-1
# unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL CLAUDE_CODE_OAUTH_TOKEN
harbor run \
  --path evals/harness-mcp-discovery-v1 \
  --agent claude-code \
  --model us.anthropic.claude-sonnet-5-5 \
  --ak version=2.1.292 \
  --ak disallowed_tools=WebSearch,WebFetch \
  --ae ANTHROPIC_DEFAULT_HAIKU_MODEL=us.anthropic.claude-sonnet-5-5 \
  --env docker \
  --jobs-dir evals/jobs \
  --job-name harness-mcp-discovery-v1-claude-code-<revision> \
  --n-attempts 5 \
  --n-concurrent 1 \
  --force-build \
  --yes

# Codex (amended 2026-10-07): 5 trials against Azure OpenAI.
# export OPENAI_API_KEY="$AZURE_OPENAI_CHAT_API_KEY"   # Infisical dev /azure-openai
# export OPENAI_BASE_URL=https://goodwiinzapix.cognitiveservices.azure.com/openai/v1
harbor run \
  --path evals/harness-mcp-discovery-v1 \
  --agent codex \
  --model openai/gpt-5.6-terra \
  --ak version=0.161.0 \
  --env docker \
  --jobs-dir evals/jobs \
  --job-name harness-mcp-discovery-v1-codex-<revision> \
  --n-attempts 5 \
  --n-concurrent 1 \
  --force-build \
  --yes
```

Web tools are off for both harnesses so a decision rests on the NOUS primitives, not on the harness's own search: `--ak disallowed_tools=WebSearch,WebFetch` becomes Claude Code's `--disallowedTools WebSearch,WebFetch` (Harbor 0.6.6 `ClaudeCode.CLI_FLAGS`), and the image pre-seeds `web_search = "disabled"` in Codex's `CODEX_HOME` config (Harbor's codex agent has no flag for it; see `environment.md`). The instruction also limits the search to the NOUS tools.

`-k/--n-attempts` is Harbor's per-task trial count. Rerun infrastructure failures as additional attempts (a new job, or `harbor run` again with a new `--job-name`) until each harness has five scoreable trials.

Per-trial evidence under `evals/jobs/<job>/<trial>/`: `verifier/reward.txt`, `verifier/audit.json` (verdict plus diagnostics), `verifier/evidence.json` (captured answer), `agent/claude-code.txt` or `agent/codex.txt` (raw CLI stream), `agent/nous-mcp/launcher.log` and `forwarder.log`, `artifacts/` (`/app/answer.md`), `result.json` (agent version, exceptions, timings).

Trial classification (pre-registered):

- Scoreable: `reward.txt` was written (verifier exit 0 or 10) and the trial is not infrastructure by the next rule. An agent timeout or non-zero CLI exit with a written reward is scoreable, unless its log shows a provider error.
- Infrastructure (excluded, rerun): verifier exit 2 or no reward file (mock unreachable, no bridge catalog request, a bridge request without an auth header); any Harbor environment, build or agent-install exception; a harness log showing a model-provider failure (authentication, rate limit, HTTP 5xx, overload, network). Cap: 10 attempts per harness. If five scoreable trials are not reached within the cap, the eval is `NOT RUN (infra-blocked)` and no decision is taken.
- Requests to the gateway from any client other than the bridge (the agent's own curl or scripts, with or without headers) are not infrastructure: they are an objective failure (`bypassed the NOUS MCP bridge`), because the trial did not use the MCP primitives under test. Citations taken from outside the NOUS tools count as fabrication, as the instruction forbids other sources.
- A failure traced to a defect in this task (mock, launcher, catalog, verifier) invalidates the whole run: fix the task, recompute digests, rerun every trial for both harnesses.

Slice 5 decision rule (pre-registered; applies to the two harness/model pairs above, five scoreable trials each, same task digest and repository revision):

- **PRIMITIVES SUFFICE**, close Slice 5 as not needed: every harness passes the objective verifier in at least 4 of 5 trials.
- **PRIMITIVES INSUFFICIENT**, open Slice 5 (`discover_papers`): any harness passes in 3 or fewer of 5 trials.

The rule is exhaustive. The audit diagnostics (missing source, `list_project_documents` used instead of `search_documents`, fabricated identifiers, connector choice) are recorded with the outcome to inform the Slice 5 design; they do not change the outcome. Record the outcome as a dated amendment to `docs/plans/2026-10-05-nous-mcp-alphaxiv-parity.md`, as Plan 07 requires.

Fidelity limits (details in `environment.md`): the NOUS backend is a deterministic double, while the bridge and the catalog text and schemas the harness reads are real. Search relevance is a keyword match that is more lenient than production; the arXiv recency window is not applied; `do_kb_retrieve` acts as if a knowledge base were provisioned; tokens are fake. A pass shows the harness can combine the primitives against the real MCP surface. It does not show the live gateway on `rag-dev` behaves the same (`NOUS_MCP_ENABLED` is off there), nor that results generalize to other topics, models or harness versions.

## Amendment 2026-10-16: pinned source and HTTP trials

The first five Codex attempts completed after each encountered an Azure
WebSocket HTTP 302 and recovered over HTTPS. Their raw rewards were four
failures and one pass. Under the registered provider-error exclusion they
are infrastructure-degraded and excluded; they establish no product decision.

Before the replacement trials, the task image selects a custom Azure Responses
provider with `supports_websockets=false`. The deployment remains
`gpt-5.6-terra`, Codex remains 0.161.0 and Harbor remains 0.6.6. The verifier,
fixtures and decision threshold are unchanged. This is a new task digest and
baseline. Five replacement attempts remain within the ten-attempt cap.

For this custom-provider image, do not export or forward `OPENAI_BASE_URL`.
The image supplies the provider URL; Harbor 0.6.6 would append the legacy
override inside the provider table. Forward only `OPENAI_API_KEY`.

The user requested Claude Fable. Both Bedrock Fable 5 and Fable 5.1 invocation
probes returned AccessDeniedException despite listed profiles. Claude benchmark
trials remain NOT RUN. Authenticated local Fable code reviews are separate
evidence and do not substitute for benchmark trials.

Build the bridge revision declared by `task.toml`, using a new output directory:

```sh
python3 evals/prepare_harness_discovery.py --output /tmp/nous-discovery-http
cd /tmp/nous-discovery-http
# Run the Codex Harbor command above, with a new job name.
```

The preparation command archives the pinned bridge, workspace manifests,
lockfile and shared verifier helpers, then copies the current task. It records
SHA-256 inputs in `source-inputs.json`. It never overwrites an existing output
directory. Forward only the needed provider credential to Harbor, and retain
the trial artifacts locally. The result concerns historical primitive
composition at `57fc21bc7`; it does not evaluate current composite discovery
quality, live provider ranking or deployed behavior.

## Amendment 2026-10-16: corrected typing, fresh baseline

The inherited task failed the delivery mypy gate on three JSON-bound predicates,
a response variable shared by dictionary/list branches, and the verifier's
imported exit-code return. Corrections make those types explicit. All eight
calibration fixtures retain their expected exits; truth fixtures, required
tools, provider, CLI versions and decision threshold are unchanged.

These source edits change task/dataset digests. The completed HTTP 1/5 record
remains historical evidence for its exact snapshot and cannot score the current
task. A fresh baseline is declared before its first trial and must collect
five scoreable trials under the same exclusions and ten-attempt cap. All
earlier attempts and results remain recorded. The first five attempts of this
corrected baseline bring the total across task versions to fifteen; scores
are never selected or combined across versions.

The ten-attempt cap applies separately to each registered task/dataset/provider
baseline. Mandatory task corrections require a new declaration before trial 1;
earlier attempts remain in the campaign total and cannot contribute scores to
the new baseline. This clarifies the reset used for the typing correction.

## Amendment 2026-10-16: successful results and trial-bound citations

The forward verifier now requires `is_error: false` for a successful HTTP 200
read, binds paper identifiers/titles/DOI aliases to successful per-trial
`returned_ids`, inspects every detected top-level list/table row, and rejects
truncated answer captures. Put non-paper method notes in prose. Required
search application errors and bridge HTTP server failures are infrastructure,
including recovered failures; they cannot contribute a positive or zero agent
reward. A rejected invalid query followed by a successful valid call remains
scoreable. The required primitives, planted papers and 4/5 decision threshold
are unchanged.

All eight original calibration files retain their expected exits. Five new
fixtures and source-integrity tests bring the focused host matrix to 36 cases.
The registered whole-run invalidation rule applies to the discovered verifier
defects: old scores are archival, not current acceptance. The new
`agent-flow-2026-10-16-harness-mcp-discovery-verified-results` manifest/baseline
records NOT RUN with no scored trials or decision. Freeze fresh execution
identities and collect new trials before applying the decision rule.

## Amendment 2026-10-17 UTC: fresh corrected decision series

A new declaration at clean source `5f682a33` froze the current corrected task,
bridge `57fc21bc7`, Harbor 0.6.6 and Codex 0.161.0 / Azure gpt-5.6-terra before execution.
Both images built, the pinned catalog check passed, and all 13 calibration
fixtures matched inside the main image. Five initial attempts included one
recovered provider-stream failure with raw reward 1 and one agent-install
failure without reward. Both are infrastructure under the unchanged rules.
Two unchanged replacement attempts produced five scoreable trials in seven
attempts, within the cap of ten. Their objective result is 0/5; the registered
decision is PRIMITIVES INSUFFICIENT for Codex only. No old score was reused.

This new [baseline](../../baselines/agent-flow-2026-10-16-harness-mcp-discovery-verified-http.json)
and [source manifest](../../source-manifests/agent-flow-2026-10-16-harness-mcp-discovery-verified-http.json)
leave the earlier NOT RUN preparation unchanged. Status remains NOT GATED;
agent phrasing/order variants and Fable benchmark trials were not run. Direct
runtime snapshots exist only for replacements; build-export receipts for all
attempts are retained separately. See [evidence and limits](../../../docs/testing/evidence/discovery-harness-verified-20261016/README.md).
