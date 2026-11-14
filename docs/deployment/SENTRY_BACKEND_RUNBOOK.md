# Backend Sentry — finish-up plan

Status as of 2026-05-03: code is merged on `develop` (PRs #461, #465, #466).
Frontend Sentry verified working end-to-end. Backend Sentry deployed but
**inert** until the `SENTRY_DSN` k8s secret key is added in the `rag-dev`
namespace.

This document is the runbook for finishing that. Run top-to-bottom from a
terminal that has `kubectl` pointed at `do-nyc3-rag-system-cluster`.

---

## 1. Confirm code rolled to dev

- https://github.com/goodwiins/NOUS-Research-Assistant/actions/workflows/docker-build.yml — wait
  until the `develop` build triggered by PR #466 is green.
- ArgoCD app `nous-dev` should be **Synced** + **Healthy**.

The new helm env vars (`SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE`,
`SENTRY_PROFILES_SAMPLE_RATE`) trigger an automatic backend pod roll, but at
this point the SDK still no-ops because `SENTRY_DSN` isn't set.

## 2. Get the Sentry DSN

- https://goodwiinz-uk.sentry.io/settings/projects/nous-frontend/keys/
- Copy the DSN. Same DSN works for backend events — they get tagged
  `service: nous-backend` for filtering. Create a separate `nous-backend`
  Sentry project first if you'd rather isolate.

## 3. Local terminal setup (skip if already done)

```sh
brew install doctl kubectl jq
doctl auth init
doctl kubernetes cluster kubeconfig save do-nyc3-rag-system-cluster

kubectl config current-context
kubectl -n rag-dev get pods -l app.kubernetes.io/component=backend
```

## 4. Patch the secret

```sh
DSN='<paste-dsn-from-step-2>'

# Verify the key isn't already there
kubectl -n rag-dev get secret app-secrets -o jsonpath='{.data}' | jq 'keys'

# Add it
kubectl -n rag-dev patch secret app-secrets --type=json \
  -p="[{\"op\":\"add\",\"path\":\"/data/SENTRY_DSN\",\"value\":\"$(printf '%s' "$DSN" | base64 -w0)\"}]"

# Confirm it's now in the list
kubectl -n rag-dev get secret app-secrets -o jsonpath='{.data}' | jq 'keys'
```

If the key already existed, use `replace` instead of `add` in the JSON patch.

## 5. Restart backend

```sh
kubectl -n rag-dev rollout restart deploy/nous-dev-knowledge-graph-analytics-backend
kubectl -n rag-dev rollout status deploy/nous-dev-knowledge-graph-analytics-backend
```

## 6. Verify init log

```sh
kubectl -n rag-dev logs -l app.kubernetes.io/component=backend --tail=200 | grep -i sentry
```

Expect:

```
Sentry initialized (env=dev, traces=1.0, profiles=0.0, release=<sha>)
```

If you see `Sentry DSN not set — error tracking disabled` → secret didn't
reach the pod. Re-check step 4 and confirm the pod was actually replaced
(`kubectl -n rag-dev get pods -l app.kubernetes.io/component=backend` should
show new ages).

## 7. Trigger a test event

```sh
kubectl -n rag-dev exec -i deploy/nous-dev-knowledge-graph-analytics-backend -- python - <<'PY'
import sentry_sdk

from src.observability.sentry import init_sentry

if not init_sentry():
    raise SystemExit("Sentry is disabled")
try:
    raise RuntimeError("nous-backend Sentry verification")
except RuntimeError as exc:
    sentry_sdk.capture_exception(exc)
sentry_sdk.flush(timeout=5)
PY
```

Expect the command to exit successfully. The test runs inside the backend pod;
there is no public HTTP error trigger.

## 8. Confirm event in Sentry

- https://goodwiinz-uk.sentry.io/issues/?project=nous-frontend
- Filter by `service:nous-backend environment:dev`
- New issue appears within ~30s. Stack trace points to
  the in-pod verification command.

---

## Promote to staging / production

When ready:

1. Repeat steps 4–6 in `rag-staging` and `rag-prod` namespaces.
2. Sample rates are already configured in `values-staging.yaml` (0.5) and
   `values-production.yaml` (0.1).
3. Consider creating a separate `nous-backend` Sentry project at that
   point and using a distinct DSN per environment.

## Reference

- Init logic: `backend/src/observability/sentry.py`
- App wiring: `backend/src/main.py` (calls `init_sentry()` before
  `FastAPI(...)`)
- Verification: run the controlled SDK command in step 7 from inside a backend pod
- Helm env vars: `infrastructure/helm/knowledge-graph-analytics/values-{dev,staging,production}.yaml`
- DSN flows from existing `app-secrets` k8s secret via the existing
  `envFrom` block in `values-*.yaml` — no helm template change needed for
  the DSN itself.
