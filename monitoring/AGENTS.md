# Monitoring guidance

## Scope and sources of truth

This directory contains the local monitoring Compose target and its
Prometheus, Grafana, Alertmanager, OpenTelemetry, logging, dashboard, and
alert-rule configuration. [`monitoring/README.md`](README.md) describes the
local setup, while the deployment and infrastructure trees contain separate
consumers with repeated Prometheus/Grafana/Alertmanager/OTel names. A filename
such as `prometheus.yml`, `alert_rules.yml`, `alertmanager.yml`, or an
identically named dashboard does not establish a shared canonical copy.

The invalid-pattern audit records this duplicate-configuration risk and the
repository-wide secret-scanning invariant in
[`docs/audits/2026-09-14-invalid-patterns-audit.md`](../docs/audits/2026-09-14-invalid-patterns-audit.md).
Use the concrete Compose, Helm, Kubernetes, or workflow consumer as the source
of truth for each change.

## Invalid patterns

- Do not synchronize same-named monitoring files across `monitoring/`,
  `deployment/`, and `infrastructure/` without identifying each consumer and
  its environment. A local Compose config and a cluster/Helm config are not
  interchangeable.
- Do not remove, rename, or change Prometheus labels, recording-rule labels,
  or dashboard dimensions without tracing both alert expressions/templates and
  Grafana queries that consume them. The contract is to preserve labels shared by alerts and dashboards, including the environment/service/instance dimensions used for routing and display.
- Keep `.env` files, webhook URLs, SMTP/auth values, passwords, tokens, and
  private dashboard or trace data out of commits, reports, logs, screenshots,
  and command output. Placeholder examples are not permission to replace the
  existing secret boundary with hard-coded values.
- Do not call a rendered or parsed configuration proof of a live scrape,
  alert delivery, dashboard query, OTel export, or service health. Those are
  separate service-dependent checks requiring an authorized target.
- Do not start, stop, recreate, or remove monitoring services or volumes as a
  side effect of routine configuration validation.

## Required workflow

Before editing, name the exact local Compose, Prometheus, Grafana,
Alertmanager, OTel, Kubernetes, or Helm consumer. Read the relevant rule,
dashboard, scrape, and secret-indirection sections together; classify other
same-named files as separate, historical, generated, or unresolved until
their consumers are proved. Keep `.env` private and preserve webhook/auth
configuration through the existing environment or secret injection boundary.

## Verification

The only routine monitoring check is the repository script below. It requires
Docker to be installed and the Docker daemon running; the script checks
Compose configuration and does not establish live scrape or alert health.

```sh
bash scripts/validate_docker_compose.sh
```

If Docker or the script's Compose prerequisite is unavailable, report this
check as `NOT RUN`. Do not substitute a service startup, live dashboard check,
or external webhook test and call it configuration validation.
