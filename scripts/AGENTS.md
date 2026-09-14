# Script guidance

## Scope and sources of truth

This directory mixes static checks and CI helpers with scripts that can touch
backups, deployments, databases, maintenance data, load targets, secrets, or
shared environments. Classify a script from its implementation and consumer,
not from a reassuring filename. The CI workflow and
[`scripts/docs/README.md`](docs/README.md) define the directory-doc and test
contracts; the invalid-pattern audit records the reviewed SQL/process cases
and the secret-scanning boundary.

Static/CI scripts include checks under `scripts/ci/`, `scripts/docs/`, and
test helpers. Backup, deployment, maintenance, load/performance, database,
secret/setup, and shared-environment scripts are side-effecting or
potentially side-effecting until proven otherwise.

## Invalid patterns

- Do not execute a script before reading the whole file, its called helpers,
  and its README/workflow consumer. Require explicit authorization for
  deployment, cluster, database, backup/restore, secret, remote, destructive,
  or shared-environment actions, even when the script also performs checks.
- Do not introduce unsafe shell construction: avoid `shell=True`, string
  evaluation, unquoted caller-controlled expansions, or a shell pipeline where
  an argument list and explicit status handling is possible. Validate inputs
  and use argv-based subprocess calls for external commands.
- Never pass an unresolved broad target to a destructive command. Resolve and
  validate exact task-specific paths; do not recursively target `/`, `$HOME`,
  `~`, the workspace root, or an unresolved variable. Use a task-specific
  variable and a safe temporary directory such as one created by `mktemp -d`.
- Do not print secrets or sensitive environment values. Avoid tracing shell
  commands containing credentials, redact error output, and keep tokens,
  passwords, kubeconfigs, webhook URLs, and backup contents out of logs and
  reports.
- Do not swallow non-zero status, partial failure, cleanup failure, or a
  failed rollback. Use explicit error handling, bounded operations, and a
  cleanup trap for temporary resources; document what remains after a partial
  failure before any authorized retry.
- Do not weaken CI/test ratchets or rewrite fixtures to make a script pass.
  Preserve validated SQL identifiers, parameterized values, and the intended
  sandbox/remote boundary when those consumers are in scope.

## Required workflow

First classify the script as static/CI or side-effecting, identify every
consumer and exact target, then read the complete call graph and usage docs.
For a change, keep inputs bounded and validated, use task-specific variables
and safe temporary paths, preserve cleanup and partial-failure behavior, and
state the required authorization before execution. A static check may be run
locally when its prerequisites are available; a script that can mutate a
shared, remote, secret, or destructive target remains an explicitly
authorized operation.

The NOUS workflow contract test and directory-doc lint are the deterministic
checks named by the CI/docs contracts:

```sh
python3 -m pytest tests/unit/scripts/ \
  --confcutdir=tests/unit/scripts \
  -q --no-cov

python3 scripts/docs/check_dir_docs.py
```

## Verification

Run the two commands above for changes to the covered script contracts. They
are static/test evidence only and require the repository's Python/test
dependencies; they do not authorize deployment, database changes, secret
operations, backup/restore, load generation, or other external mutation. Do
not report a blocked dependency run as a pass, and report service-,
credential-, or environment-dependent script execution as `NOT RUN` unless it
was separately authorized and actually exercised.
