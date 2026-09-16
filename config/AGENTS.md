# AGENTS.md

Configuration-specific guidance. The repository-root `AGENTS.md` still
applies.

## Scope and sources of truth

This directory owns Compose targets under `config/docker-compose/`,
environment-specific configuration under `config/environments/`, and other
application/tool configuration. The [invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md),
the [backend/operations gotchas](../docs/engineering/gotchas.md),
the [repository README](../README.md), and each named workflow or manifest
are the sources of truth for ownership and reachability.

The root files `docker-compose.yml`, `docker-compose.development.yml`,
`docker-compose.ci.yml`, and `docker-compose.prod.yml` are symlinks to targets
in `config/docker-compose/`. The symlinks are intentional aliases, not a
second set of files to maintain. A tracked configuration file is not proof
that its environment is live; current repository guidance says only the dev
Argo CD path is live while older staging/production material remains for
historical or unresolved consumers.

## Invalid patterns

- Do not edit a root Compose symlink and its target as if they were separate
  copies. Edit the canonical target under `config/docker-compose/` once and
  preserve the symlink relationship.
- Do not flatten development, CI, security, services, or production layering
  into one file, silently change an override's precedence, or make CI and
  production defaults diverge without tracing their consumers.
- Do not infer deployment or runtime reachability from a filename, a values
  file, or an old README. Confirm the workflow, manifest, or application that
  consumes the configuration before changing it.
- Do not copy environment-file values into reports, logs, commits, or test
  output. Keep secrets indirect through the existing environment/secret
  injection boundary; never hard-code credentials into a tracked config.
- Do not “clean up” duplicate-looking files by deleting or synchronizing them
  in bulk. First determine whether each copy is live, historical, generated,
  or an unresolved consumer.

## Required workflow

- Identify the canonical file, the layer being changed, and every consumer
  before editing. Keep environment-specific overrides explicit and preserve
  the root symlink targets.
- Review rendered service names, mounts, networks, health checks, and
  dependency ordering at the affected layer. Keep secret names and indirect
  references intact without opening or recording their values.
- Reconcile changes with the current dev-only deployment status in
  `docs/engineering/gotchas.md`; do not revive retired staging/production
  paths by implication.
- Keep configuration validation read-only. Any command that starts services,
  changes a shared environment, or applies a deployment requires explicit
  authorization outside routine local validation.

## Verification

Both checks below require Docker/Compose to be installed and available; they
validate configuration shape, not live service health. Run them from the
repository root and report `NOT RUN` when Docker is unavailable:

```sh
docker compose -f config/docker-compose/docker-compose.development.yml config --quiet
bash infrastructure/tests/docker_compose_development_config_test.sh
```
