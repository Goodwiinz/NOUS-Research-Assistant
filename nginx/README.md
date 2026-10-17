# nginx

Holds one file, `websocket.conf`: a full `nginx.conf` for a standalone
WebSocket front end with two upstream nodes (`websocket-node-1` and
`websocket-node-2`, port 8001). It is **local/compose material, not the live
AWS path**. On AWS the websocket host `dev-ws.goodwiinz.tech` is served by an
ALB Ingress through the Argo CD application `nous-dev-aws-ingress`
(`infrastructure/kubernetes/overlays/aws-dev`); see the
[AWS deployment overview](../docs/deployment/aws-overview.md).

## What the config does

- Listens on 80 (health check and redirect), 443 with TLS 1.2/1.3, and 8080
  (`/nginx_status`, `/upstream_status`).
- Proxies `/ws`, `/ws/status` and `/ws/metrics` to the `websocket_backend`
  upstream with seven-day proxy timeouts, buffering off, `limit_req` (10 r/s,
  burst 20) and `limit_conn` (10 per client address).
- Expects certificate files at `/etc/nginx/ssl/cert.pem` and `key.pem`, which
  are not in the repository. Never commit key material.

## Consumer

Only `config/docker-compose/docker-compose.websocket.yml` mounts it
(`./nginx/websocket.conf` as `/etc/nginx/nginx.conf`, with `./nginx/ssl`). The
mount paths are relative to that compose file's project directory; whether they
resolve from the repository root is **not verified**. No workflow, Helm chart or
Argo CD application references this file (checked 2026-10-17).
