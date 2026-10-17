# Self-Hosted GitHub Actions Runner

> **Status: unverified, likely retired.** This page records a persistent runner
> that was provisioned on a DigitalOcean droplet. DigitalOcean resources are
> being decommissioned (see
> [../../docs/runbooks/aws-decommission.md](../../docs/runbooks/aws-decommission.md)),
> and whether this runner still exists or is online is **not verified**. The
> droplet address was removed from this page; look it up in the DigitalOcean
> console if you need it.

## Workflows using a self-hosted runner

Checked 2026-10-17: the only workflow with a self-hosted label is
`.github/workflows/agent-eval.yml` (`runs-on: [self-hosted, linux]`). The older
claims that `docker-build.yml` and `test-runner.yml` used this runner are stale:
`test-runner.yml` no longer exists, and `docker-build.yml` is not self-hosted.
If the runner is gone, `agent-eval.yml` jobs wait for a runner that never
arrives, so confirm runner status in **Settings -> Actions -> Runners** before
relying on that workflow.

## Original host profile (historical)

| Property | Value |
| -------- | ----- |
| Provider | DigitalOcean droplet, NYC3 |
| Specs | 8 vCPU / 16 GB RAM / 320 GB disk |
| OS | Ubuntu 24.04 LTS |
| Labels | `self-hosted`, `linux`, `x64` |
| User | `runner` (non-root, docker group) |
| Service name | `actions.runner.Goodwiinz-rag.do-droplet-runner` |

## Operating notes (apply only if the host still exists)

```bash
# On the runner host
systemctl status actions.runner.Goodwiinz-rag.do-droplet-runner
journalctl -u actions.runner.Goodwiinz-rag.do-droplet-runner -f
```

Re-registering a runner needs a fresh registration token created from the
repository settings or the GitHub API with an appropriately scoped token; never
paste a token into a shell history or a document. The runner is not ephemeral,
so periodic Docker cleanup is needed on the host.

The repository owner login is `Goodwiinz` (capital G). The earlier ARC/Kubernetes
runner setup is kept only as history in [../arc/](../arc/runner-deployment.yaml).
