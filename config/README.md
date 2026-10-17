# config/

Application and deployment configuration that is not Helm or Terraform.

- `docker-compose/` holds the Compose targets (`docker-compose.yml`,
  `.development.yml`, `.ci.yml`, `.prod.yml`, plus azure, observability,
  security, services, staging and websocket variants) and the auth-proxy
  config and entrypoint script they use. The root `docker-compose.yml`,
  `docker-compose.development.yml`, `docker-compose.ci.yml` and
  `docker-compose.prod.yml` are symlinks into this directory.
- `environments/` holds the environment templates `.env.example`,
  `.env.azure` and `.env.production` (hidden files; look with `ls -A`). They
  are templates; never commit real credentials.
- `security/monitoring-security.yml` is monitoring-related security
  configuration.
- `goodflows.config.json`, `obsidian-mcp-config.json`, `opencode.json` and
  `package.next.json` are tool and editor configuration files.

A file being tracked here does not mean its environment is live. Only the
`dev` deployment is live; see [`docs/engineering/gotchas.md`](../docs/engineering/gotchas.md).
Editing rules are in [`AGENTS.md`](AGENTS.md).
