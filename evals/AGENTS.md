# AGENTS.md

Evaluation-specific guidance. The repository-root `AGENTS.md` still applies.

## Scope and sources of truth

This directory contains Harbor production-flow tasks, legacy coding-agent
suites, task instructions, environments, verifiers, calibration/truth
fixtures, recorded baselines, and source manifests. [`evals/README.md`](README.md)
describes the task families and run boundary; [`AGENT_FLOW_BASELINE.md`](AGENT_FLOW_BASELINE.md)
defines the recorded agent-flow manifest, scoring layers, trial structure, and
baseline revision rules.

`source-manifests/` hashes, `baselines/` JSON, and accepted trial evidence are
records for their declared task/revision/environment. A `GATED`, `NOT GATED`,
or `NOT YET GATED` status is evidence at that recorded revision, not proof of
current runtime behavior after the task, agent, harness, or environment moves.

## Invalid patterns

- Do not edit a source manifest or recorded baseline in place to hide a task,
  harness, dataset, environment, or verifier change. Do not reuse a score
  after a task digest, repository/agent revision, harness digest, or `env_flags`
  change; declare a new baseline and preserve the old record.
- Do not mix the agent, environment, and verifier boundaries. Keep task
  fixtures and verifier state isolated from the agent container, and keep judge
  credentials in `[verifier.env]` only—not `[environment.env]` or the agent
  environment.
- Do not weaken truth/calibration fixtures, verifier assertions, task
  postconditions, network-boundary probes, or isolation checks to make an
  evaluation pass. A calibration set must remain capable of detecting a wrong
  answer, leakage, mutation order, or fabricated result.
- Do not turn an adapter, dependency, timeout, seed/config, service, network,
  credential, judge, or verifier failure into reward zero. Report it as an
  infrastructure failure and keep it separate from objective and semantic
  gates.
- Do not call an old report or checked task box a current gate, and do not
  commit generated trial traces, credentials, auth state, or other private
  evaluation output from ignored `jobs/` artifacts.

## Required workflow

- Before a run, identify the exact task, source revision, environment, agent,
  verifier, runner image, fixture set, and manifest digests. Preserve the task
  and environment/verifier separation and confirm the verifier calibration
  fixtures before collecting scored trials.
- Record the manifest identity, including task digests, harness and dataset
  digests, runner/tool versions, and the executed `env_flags`. Run at least the
  declared canonical and near-boundary trials, retaining objective, semantic,
  and infrastructure verdicts separately. Semantic `N/A` is a declared pass
  only where the capability has no judgeable answer.
- If a task, repository/agent revision, harness, dataset, or environment flag
  changes, archive the prior baseline and create a new dated baseline plus
  source-manifest record. Never compare across digest boundaries without the
  explicit new-baseline procedure.
- Treat verifier task scripts and Harbor commands as environment-dependent.
  They require the pinned Harbor/tooling version, declared images and
  services, and any authorized model/judge credentials. Do not present a host
  checkout or a static fixture read as proof of an end-to-end evaluation.
- Keep judge access outside the agent's environment, redact secrets from
  captured artifacts, and report an `infra-degraded` run for retry rather than
  attributing its failure to agent reward.

## Verification

Harbor execution is environment-dependent and requires the pinned Harbor
version, private runner image, service doubles or databases, and authorized
model/judge configuration. Use the repository's `harbor run` command from
[`evals/README.md`](README.md) only in that declared environment. Each task's
`evals/<task>/tests/test.sh` is likewise an in-environment verifier, not a
generic offline host check; it must preserve the verifier's distinction
between a scoreable failure and infrastructure failure. If prerequisites are
unavailable, report the evaluation as `NOT RUN` with the missing prerequisite,
not as a passing or zero-reward result.
