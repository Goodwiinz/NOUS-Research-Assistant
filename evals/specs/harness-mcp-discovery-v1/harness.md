Status: draft (pre-registered 2026-10-07; no trial run yet). Results stay local and exploratory-tier until recorded under the `AGENT_FLOW_BASELINE.md` procedure (dated source manifest with `recorded_at`, baseline JSON, artifacts under `evals/jobs/`).

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

# Claude Code: 5 trials (ANTHROPIC_API_KEY exported in this shell)
harbor run \
  --path evals/harness-mcp-discovery-v1 \
  --agent claude-code \
  --model anthropic/claude-sonnet-5-5 \
  --ak version=<claude-code version> \
  --ak disallowed_tools=WebSearch,WebFetch \
  --env docker \
  --jobs-dir evals/jobs \
  --job-name harness-mcp-discovery-v1-claude-code-<revision> \
  --n-attempts 5 \
  --n-concurrent 1 \
  --force-build \
  --yes

# Codex: 5 trials (OPENAI_API_KEY exported in this shell)
harbor run \
  --path evals/harness-mcp-discovery-v1 \
  --agent codex \
  --model openai/gpt-6.1-sol \
  --ak version=<codex version> \
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
