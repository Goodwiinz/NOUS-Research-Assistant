# Cutover Runbook: DigitalOcean → AWS (dev)

**Date:** 2026-09-14
**Design:** `docs/plans/2026-09-14-do-to-aws-migration-design.md`
**Plan:** `docs/plans/2026-09-14-do-to-aws-migration.md` (Task 8; Tasks 1-7 are pre-stage prerequisites)
**Window:** single freeze-and-migrate window, hours of acceptable downtime
**Phase 1 scope:** move API/workers, PostgreSQL, Redis, and Neo4j to AWS. Keep
DigitalOcean Spaces and DigitalOcean Knowledge Base as the live document store
and retrieval service. The AWS S3 copy is a backup/staging copy, not a serving
target. A later storage/KB migration needs its own plan and approval.
**Rollback:** DO kept frozen (not deleted) for 2 weeks — see [Step 9](#9-halt-criteria--rollback)

## Environments

| | Source (freeze) | Target (cutover) |
|---|---|---|
| Kubernetes | DOKS `do-nyc3-rag-system-cluster` | EKS `nous-dev-cluster` (us-east-1) |
| App namespace | `rag-dev` | `multimodal-rag-system` |
| Helm release | `nous-dev` (ArgoCD app of same name) | `nous-dev-aws` |
| Postgres | DO managed PG, db `multimodal_rag` | RDS PostgreSQL 16 (`db.t4g.small`) |
| Redis | DO managed Valkey | ElastiCache `cache.t4g.small` (**no data migration — cold start**) |
| Neo4j | STS `nous-dev-knowledge-graph-analytics-neo4j` (5.26) | same chart on EBS gp3 |
| Qdrant | Not used; only a stale PVC remains | Not deployed; skip Step 5 |
| Object storage | Spaces `rag-system-storage` (nyc3) | **Same DO Spaces bucket remains live**; AWS S3 copy excludes `buildcache/` |
| Document retrieval | DO Knowledge Base | **Same DO Knowledge Base remains live** |
| Images | DO registry | ECR `nous/backend`, `nous/frontend` |
| DNS | Cloudflare (external-dns) | Cloudflare (external-dns) → ALB group `nous-dev` |
| Frontend | Vercel `md-basit/nous` at `goodwiinz.tech` (DO-backed build) | same domain, new AWS-backed build required in Step 7 |

**Working contexts** (set once, verify per step):

```bash
export DOKS_CONTEXT=<DOKS_CONTEXT>   # kubectl config get-contexts — DOKS entry
export EKS_CONTEXT=<EKS_CONTEXT>     # nous-dev-cluster
```

> **Rule:** every `kubectl` in Steps 1-5 runs against `$DOKS_CONTEXT` unless the
> command explicitly says `-n multimodal-rag-system --context $EKS_CONTEXT`. Halt if a command
> errors or output differs from "Expected".
>
> The `argocd` CLI does NOT follow kubectl contexts; its saved default may
> even be an inactive localhost port-forward. This runbook patches ArgoCD
> `Application` CRs using explicit kubectl contexts. Pause each root app
> before its child; restore child before root. DO apps: `nous-root`,
> `nous-dev`. EKS apps: `aws-dev`, `nous-dev-aws`.
>
> AWS sessions expire. Prefix **each** command touching `aws`, `kubectl`,
> `helm`, or `terraform` with `eval "$(aws configure export-credentials --format env)" &&`.
> On `ExpiredToken`, stop and run `aws login` before retrying. Never pipe a
> long-running Terraform command to `grep`/`head`; redirect it to a file first.

---

## 0. Preconditions checklist

All **required** boxes must be checked before starting. The two optional AWS
S3 backup checks below do not gate a phase 1 cutover because DO Spaces remains
the live store.

- [ ] AWS session active: `aws login`, then `aws sts get-caller-identity` — Expected: account ARN printed.
- [ ] Both kubectl contexts present and authorized:
  ```bash
  doctl kubernetes cluster kubeconfig save do-nyc3-rag-system-cluster
  aws eks update-kubeconfig --name nous-dev-cluster --region us-east-1
  kubectl config get-contexts
  kubectl get nodes --context $DOKS_CONTEXT
  kubectl get nodes --context $EKS_CONTEXT
  ```
  Expected: DOKS nodes Ready; EKS ≥1 node Ready.
- [ ] **Optional backup:** rclone remotes configured and reachable:
  ```bash
  rclone lsd spaces: && rclone lsd s3:
  rclone lsf spaces:rag-system-storage --max-depth 1 | head
  rclone lsf s3:nous-development-storage-3ilp9pj2 --max-depth 1 | head
  ```
  Expected: both remotes list without auth errors (`spaces` = DO Spaces keys, `s3` = AWS profile).
- [ ] Disk space for dumps on the operator machine: `df -h .` — need ≥
  (2×PG dump size + 2×Neo4j dump). The optional Spaces backup streams
  bucket-to-bucket and needs no local Spaces-sized staging area.
- [ ] `infrastructure/terraform/terraform.tfvars` placeholders filled before the last `terraform apply`: `owner_email`, `alert_email`, and **`cluster_admin_role_arns` non-empty** (aws-auth lockout otherwise). Verify:
  ```bash
  grep -E "owner_email|alert_email|cluster_admin_role_arns" infrastructure/terraform/terraform.tfvars
  ```
  Expected: real email(s); `cluster_admin_role_arns = ["arn:aws:iam::<account>:role/<admin>", ...]`.
- [ ] Terraform outputs recorded (`terraform output > .cutover/nous-aws-outputs.txt`, Task 2; create the ignored `.cutover/` directory in this worktree first). Needed values: `database_endpoint`, `database_port`, `database_name`, `database_username`, `redis_endpoint`, `storage_bucket_name`, `caller_identity.account_id`.
- [ ] Task 5 wiring done: `infrastructure/helm/knowledge-graph-analytics/values-aws.yaml` placeholders filled (`<ACCOUNT_ID>`, `<APPLICATION_STORAGE_BUCKET>`, `<ELASTICACHE_ENDPOINT>`, `<ACM_CERT_ARN>`) and `infrastructure/kubernetes/overlays/aws-dev/` scaffold hosts (`*.multimodal-rag.example.com`) replaced with the real `*.goodwiinz.tech` hosts. Verify:
  ```bash
  grep -rn "example.com\|ACCOUNT_ID\|ELASTICACHE_ENDPOINT>\|APPLICATION_STORAGE_BUCKET>\|ACM_CERT_ARN>" \
    infrastructure/helm/knowledge-graph-analytics/values-aws.yaml \
    infrastructure/kubernetes/overlays/aws-dev/
  ```
  Expected: no matches (except comment lines — read them).
- [ ] Task 6 images in ECR:
  ```bash
  aws ecr describe-images --repository-name nous/backend --region us-east-1
  aws ecr describe-images --repository-name nous/frontend --region us-east-1
  ```
  Expected: the pinned ECR backend digest is from a tested source commit that
  contains the source commit of the currently running DO backend image. Resolve
  the DO image digest to its build SHA, then verify that SHA is an ancestor of
  the ECR source SHA with `git merge-base --is-ancestor <DO_SHA> <ECR_SHA>`.
  Verify the tag resolves to the exact digest pinned in `values-aws.yaml`;
  do not rely on a matching tag alone. The Vercel frontend is verified
  separately against its recorded DO-backed deployment below.
- [ ] **Optional backup:** Task 7 first rclone sync done and clean:
  ```bash
  rclone check spaces:rag-system-storage s3:nous-development-storage-3ilp9pj2 --exclude '/buildcache/**'
  ```
  Expected: `0 differences` at the time of the check. Later DO writes can
  legitimately change the live bucket; Step 3 is optional in phase 1.
- [ ] Phase 1 document path is live on EKS: `values-aws.yaml` sets
  `S3_ENDPOINT_URL=https://nyc3.digitaloceanspaces.com`,
  `S3_BUCKET_NAME=rag-system-storage`, `S3_REGION=nyc3`; the
  `infisical-spaces-credentials` CR is Ready and the generated
  `spaces-credentials` Secret contains `S3_ACCESS_KEY` and `S3_SECRET_KEY`.
  `do-kb-credentials` exists with `DO_KB_ENABLED=true` and
  `DO_KB_PRIMARY_READ=true`. Inspect key names and flags only; never print keys.
- [ ] Rollback route is real: verify the currently working DO API hostname
  (observed `dev-api.gen-text.app`) and a TLS-valid way for the live Vercel
  frontend (`https://goodwiinz.tech`) to switch back to it. The DO-backed
  production deployment observed 2026-09-22 is
  `dpl_7cfxWcqfJ93vDadt1Tg1khbxxgMi` in scope `md-basit` (project `nous`).
  `vercel inspect <deployment-id> --scope md-basit` must still show it Ready;
  record its ID again if the live alias has changed. Its `/frontend-build`
  Infisical `dev` values are `BACKEND_URL`, `NEXT_PUBLIC_API_URL`, and
  `NEXT_PUBLIC_API_BASE_URL` = `https://dev-api.gen-text.app`, and
  `NEXT_PUBLIC_WS_URL` = `wss://dev-api.gen-text.app/ws`. The current
  `goodwiinz.tech/api/v1/auth/me` and DO API both return 401 while the AWS
  hostname returns 503 with zero replicas. After cutover, rollback with
  `vercel rollback <recorded-DO-deployment-id> --scope md-basit --yes`, then
  restore the four Infisical build secrets for future deployments.
  Do not assume the DO ingress serves
  `dev-api.goodwiinz.tech`: on 2026-09-22 it did not, while that hostname
  already pointed at the AWS ALB and returned 503 with EKS replicas at zero.
  **No Ready DO-backed deployment and tested response = no GO.**
- [ ] Database migration path tested before freeze: `SELECT 1` and a schema-only
  `pg_dump` succeed from a DOKS pod using the current DO `raguser` credential;
  `SELECT 1` succeeds from an EKS pod using the **RDS-managed Secrets Manager
  master secret**; RDS and ElastiCache ports are reachable from EKS. The DO
  `/database` secret has a private `10.10.0.10` host, which is not routable
  from EKS. Step 2 streams a dump between pods instead of dialing DO from EKS.
- [ ] EKS addons healthy (Task 3) — required before Step 6 scale-up, check now:
  ```bash
  kubectl get pods -n kube-system --context $EKS_CONTEXT \
    | grep -E "aws-load-balancer-controller|cluster-autoscaler|external-dns|ebs-csi"
  kubectl get pods -n external-secrets --context $EKS_CONTEXT
  ```
  Expected: all Running/Ready.
- [ ] `infisical-universal-auth` secret exists in the EKS app namespace BEFORE
  the ArgoCD apps sync (the Infisical operator CRDs/SecretStore depend on it —
  see `infrastructure/kubernetes/infisical-bootstrap.md`). Verify:
  ```bash
  kubectl get secret infisical-universal-auth -n multimodal-rag-system --context $EKS_CONTEXT
  ```
- [ ] EKS app stack deployed and both EKS ArgoCD apps exist. The AWS values
  are pinned to **zero** backend/worker/beat replicas, worker HPA off, and
  synthetic CronJob suspended until Step 6. ArgoCD may auto-sync this safe
  preflight state; no EKS app writer may be running before the DO freeze.
  Verify:
  ```bash
  kubectl get applications -n argocd --context $EKS_CONTEXT
  kubectl get deploy,cronjob -n multimodal-rag-system --context $EKS_CONTEXT
  ```
  Expected: `aws-dev` and `nous-dev-aws` Synced; all three app Deployments
  desired/ready `0`, and `nous-dev-aws-synthetic-traffic` `SUSPEND=true`.
- [ ] ACM certificate issued (Task 3 terraform; DNS validation CNAME added manually in Cloudflare):
  ```bash
  aws acm list-certificates --region us-east-1 \
    --query "CertificateSummaryList[?Status=='ISSUED'].[CertificateArn,DomainName]"
  ```
  Expected: cert for `*.goodwiinz.tech` (or `dev-api.goodwiinz.tech` + ws host) `ISSUED`. ARN must equal `<ACM_CERT_ARN>` used in values-aws/overlay.

**Record for rollback (Step 9)** — run now and keep the output:

```bash
mkdir -p .cutover
kubectl get deployments -n rag-dev --context $DOKS_CONTEXT --no-headers \
  -o custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas > .cutover/do-replicas.txt
kubectl get ingress -n rag-dev --context $DOKS_CONTEXT -o wide > .cutover/do-ingress.txt   # DO LB hostname(s)
kubectl get cronjob nous-dev-synthetic-traffic -n rag-dev --context $DOKS_CONTEXT \
  -o jsonpath='{.spec.suspend}{"\n"}'   # record this flag too (observed false 2026-09-22)
cat .cutover/do-replicas.txt .cutover/do-ingress.txt
```

Expected: e.g. `nous-dev-knowledge-graph-analytics-backend 1`, `nous-dev-celery-beat 1`, `nous-dev-celery-worker 1`. `.cutover/do-ingress.txt` records the DO ingress and its actual hosts; it does **not** prove `dev-api.goodwiinz.tech` routes to DO.

**Infisical rollback anchor (needed by 9.2):** record current version numbers,
not values, in `.cutover/infisical-versions.txt` (Infisical UI → `nous-platform`,
env `dev` → each secret's Version History). Include `/database` keys
`DATABASE_URL`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`,
`POSTGRES_USER`, `POSTGRES_PASSWORD`, and its `REDIS_HOST`, `REDIS_PORT`,
`REDIS_URL`, `REDIS_PASSWORD` duplicates; `/redis` keys `REDIS_URL`,
`REDIS_PASSWORD`; `/app` key `NEO4J_PASSWORD`; and `/frontend-build` keys
`BACKEND_URL`, `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_API_BASE_URL`,
`NEXT_PUBLIC_WS_URL`. The two DO PG password-bearing `/database` keys were
repaired from the DO control plane on 2026-09-22; record their **current**
versions after that repair, not the older broken versions. Never copy secret
values to disk or commit them.

**HALT:** any precondition fails → fix before freeze. Do not enter the window.

---

## 1. Freeze DigitalOcean

Goal: stop all writers (CronJob and Deployments). Keep the Neo4j PVC intact;
Step 4 stops the Community Edition DBMS and dumps it offline from a helper
pod. The DO Spaces bucket and DO Knowledge Base remain provisioned and live
for EKS after cutover.

**1a. Pause ArgoCD auto-sync on DO** — `nous-root` first (it self-heals child apps; pausing it prevents the child policy from being reverted from git):

```bash
kubectl patch application nous-root -n argocd --context $DOKS_CONTEXT --type=json \
  -p='[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
kubectl patch application nous-dev -n argocd --context $DOKS_CONTEXT --type=json \
  -p='[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
sleep 120
kubectl get application nous-root nous-dev -n argocd --context $DOKS_CONTEXT \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.syncPolicy.automated}{"\n"}{end}'
```

Expected: both `automated` fields absent. If `nous-dev` regains it after the
wait, `nous-root` is still syncing it — re-pause the root and recheck.

**HALT:** if `nous-dev` sync policy cannot be pinned to none after two attempts — do not scale down; GitOps would undo the freeze.

**1b. Suspend the DO synthetic CronJob and wait for any active Job to finish.**
The 20-minute Job is a writer; scaling Deployments alone does not freeze it.

```bash
kubectl patch cronjob nous-dev-synthetic-traffic -n rag-dev --context $DOKS_CONTEXT \
  --type=merge -p '{"spec":{"suspend":true}}'
kubectl get cronjob nous-dev-synthetic-traffic -n rag-dev --context $DOKS_CONTEXT
kubectl get cronjob nous-dev-synthetic-traffic -n rag-dev --context $DOKS_CONTEXT \
  -o jsonpath='{.status.active[*].name}{"\n"}'
```

Expected: `SUSPEND=true` and no active Job names. If one is active, wait for
it to finish and recheck; do not delete a running Job or proceed while it
can write.

**1c. Scale DO Deployments to 0** (StatefulSets keep running):

```bash
kubectl scale deployments --all -n rag-dev --context $DOKS_CONTEXT --replicas=0
kubectl get deploy -n rag-dev --context $DOKS_CONTEXT
kubectl get statefulset -n rag-dev --context $DOKS_CONTEXT
```

Expected: every Deployment `0/1`; Neo4j StatefulSet still `1/1` Ready until
Step 4. A stale Qdrant PVC is not a running workload.

**1d. Confirm quiescence externally** (pods are gone, so check through the LB):

```bash
sleep 60 && curl -sS -m 5 https://dev-api.gen-text.app/health; echo "exit=$?"
```

Expected: non-200 / connection failure — no DO backend is running, so no DO writers can reach PG/Neo4j.

**1e. (optional, self-protection)** DO-side ArgoCD UI: mark `nous-dev` app as suspended so nobody clicks sync.

**HALT:** Deployments stuck terminating >5 min → investigate before proceeding; writers still alive would fork data.

---

## 2. Postgres: DO managed PG → RDS

Use one transient PostgreSQL client pod in each cluster. The DO secret points
at a private `10.10.0.10` address, which EKS cannot reach. Stream the dump
from the DOKS pod to the EKS pod without a laptop dump file. RDS is VPC-private.

**2a. Launch migration pods:**

```bash
kubectl run pg-mig -n multimodal-rag-system --context $EKS_CONTEXT \
  --image=postgres:16-alpine --restart=Never --command -- sleep 7200
kubectl wait --for=condition=Ready pod/pg-mig -n multimodal-rag-system --context $EKS_CONTEXT --timeout=120s
kubectl run pg-do -n rag-dev --context $DOKS_CONTEXT \
  --image=postgres:16-alpine --restart=Never \
  --overrides='{"spec":{"containers":[{"name":"pg-do","image":"postgres:16-alpine","envFrom":[{"secretRef":{"name":"database-credentials"}}],"command":["sleep","7200"]}]}}'
kubectl wait --for=condition=Ready pod/pg-do -n rag-dev --context $DOKS_CONTEXT --timeout=120s
```

Expected: `condition met`.

**2b. Define connection params** (values never printed or committed). The RDS
module defaults to `manage_master_user_password=true`: its master password
is in the RDS-managed **Secrets Manager** secret, NOT the unused Terraform
`random_password.db_password`. `terraform output -raw database_endpoint`
includes `:5432`, so split it before passing `-h` and `-p`:

```bash
cd infrastructure/terraform
RDS_ENDPOINT=$(terraform output -raw database_endpoint)
RDS_HOST=${RDS_ENDPOINT%:*}
RDS_PORT=${RDS_ENDPOINT##*:}
RDS_USER=$(terraform output -raw database_username)
RDS_SECRET_ARN=$(aws rds describe-db-instances --db-instance-identifier nous-postgres \
  --region us-east-1 --query 'DBInstances[0].MasterUserSecret.SecretArn' --output text)
RDS_PASSWORD=$(aws secretsmanager get-secret-value --secret-id "$RDS_SECRET_ARN" \
  --region us-east-1 --query SecretString --output text | jq -r '.password')
cd ../..

# The DO pod retains its frozen DO DATABASE_URL even after Step 2g changes
# Infisical; SQL arguments pass through shell positional parameters unchanged.
psql_do()  { kubectl exec -i -n rag-dev pg-do --context "$DOKS_CONTEXT" -- sh -c 'exec psql "$DATABASE_URL" "$@"' _ "$@"; }
psql_rds() { kubectl exec -i -n multimodal-rag-system pg-mig --context "$EKS_CONTEXT" -- env PGPASSWORD="$RDS_PASSWORD" PGSSLMODE=require psql -h "$RDS_HOST" -p "$RDS_PORT" -U "$RDS_USER" -d multimodal_rag "$@"; }

psql_do -c "SELECT version();" && psql_rds -c "SELECT version();"
```

Expected: both print a PostgreSQL version banner.

**HALT:** either connection fails → stop. Do not switch to the DO public
endpoint or open its firewall during the freeze without a separate verified
path. A schema-only DO dump and authenticated RDS query were preflighted.

**2c. Verify role/db on RDS exist** (Task 2 tfvars `db_name = "multimodal_rag"`, `db_username = "raguser"` create them; verify, create only if missing):

```bash
psql_rds -tAc "SELECT 1 FROM pg_database WHERE datname = 'multimodal_rag';"
psql_rds -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$RDS_USER';"
```

Expected: `1` and `1`. If the db is missing:

```bash
kubectl exec -i -n multimodal-rag-system pg-mig --context "$EKS_CONTEXT" -- env PGPASSWORD="$RDS_PASSWORD" PGSSLMODE=require \
  psql -h "$RDS_HOST" -p "$RDS_PORT" -U "$RDS_USER" -d postgres -c 'CREATE DATABASE multimodal_rag;'
```

(The role is the RDS master user — created by terraform.)

**2d. Dump DO:**

```bash
set -o pipefail
kubectl exec -n rag-dev pg-do --context "$DOKS_CONTEXT" -- \
  sh -c 'exec pg_dump -Fc "$DATABASE_URL"' | \
  kubectl exec -i -n multimodal-rag-system pg-mig --context "$EKS_CONTEXT" -- \
  sh -c 'cat > /tmp/nous.dump'
kubectl exec -n multimodal-rag-system pg-mig --context "$EKS_CONTEXT" -- ls -lh /tmp/nous.dump
kubectl exec -n multimodal-rag-system pg-mig --context "$EKS_CONTEXT" -- pg_restore --list /tmp/nous.dump | tail -5
```

Expected: non-trivial file size; `pg_restore --list` prints TOC entries without error.

**HALT:** dump exits non-zero (e.g. version mismatch, auth failure) — nothing has been written to RDS yet; safe to fix and retry.

**2e. Restore to RDS:**

```bash
kubectl exec -i -n multimodal-rag-system pg-mig --context "$EKS_CONTEXT" -- env PGPASSWORD="$RDS_PASSWORD" PGSSLMODE=require \
  pg_restore -h "$RDS_HOST" -p "$RDS_PORT" -U "$RDS_USER" -d multimodal_rag \
  --no-owner --no-privileges --exit-on-error -j 2 /tmp/nous.dump
psql_rds -c "ANALYZE;"
```

Expected: exit 0, no `error:` lines. (Warnings about extensions/privileges that were skipped by `--no-privileges` are acceptable.)

**HALT:** any error line or non-zero exit → **rollback decision now** (nothing else migrated yet; simply fix or abandon window — DO still frozen and intact).

**2f. Verify row counts match** — exact count per public table on both sides, then diff:

```bash
COUNTS='SELECT string_agg(format('"'"'SELECT %L AS tbl, count(*) FROM %I'"'"', tablename, tablename), '"'"' UNION ALL '"'"' ORDER BY tablename) FROM pg_tables WHERE schemaname = '"'"'public'"'"';'
psql_do  -Atc "$COUNTS" | psql_do  > .cutover/counts-do.txt
psql_rds -Atc "$COUNTS" | psql_rds > .cutover/counts-rds.txt
diff .cutover/counts-do.txt .cutover/counts-rds.txt && echo COUNTS-MATCH
```

Expected: `COUNTS-MATCH` (every table, exact `count(*)`, source vs target identical).

**HALT:** any row differs → DO writes are frozen so the source is stable: investigate the differing table (sequence values, partial copy) before continuing.

**2g. Point Infisical at RDS** — in Infisical UI (project `nous-platform`, env `dev`), update folder `/database`:

- `DATABASE_URL` → `postgresql://<RDS_USER>:<RDS_PASSWORD>@<RDS_HOST>:5432/multimodal_rag`
- `POSTGRES_HOST` → `<RDS_HOST>` (bare hostname; the `wait-for-postgres` init container uses it)
- `POSTGRES_PORT` → `5432`; `POSTGRES_DB` stays `multimodal_rag`
- `POSTGRES_USER` → `<RDS_USER>` (`raguser`)
- `POSTGRES_PASSWORD` → `<RDS_PASSWORD>`

Percent-encode special characters in the password segment of `DATABASE_URL`.
Leave the old DO secret versions available for rollback.

Expected: secrets saved (operator re-syncs on next reconcile, ≤60s; new backend pods in Step 6 pick these up via secret `database-credentials`).

**2h. Remove migration pod** (kept until here for re-verification; delete now, recreate if Step 9 rollback needs counts):

```bash
kubectl delete pod pg-mig -n multimodal-rag-system --context $EKS_CONTEXT
kubectl delete pod pg-do -n rag-dev --context $DOKS_CONTEXT
unset RDS_PASSWORD
```

---

## 3. Optional Spaces → S3 backup checkpoint (not a serving switch)

If rclone is available, DO app writers are frozen (Step 1), so take a
consistent pre-cutover copy. Skip this step if rclone is unavailable; AWS S3
is not the serving path in phase 1. Record the last verified copy's age.
After EKS starts, new document writes intentionally continue to DO Spaces;
the AWS S3 copy may then diverge. **Never delete or disable Spaces.**

```bash
rclone sync spaces:rag-system-storage s3:nous-development-storage-3ilp9pj2 --exclude '/buildcache/**' --progress
rclone check spaces:rag-system-storage s3:nous-development-storage-3ilp9pj2 --exclude '/buildcache/**'
```

Expected: check reports `0 differences` and `0 errors`.

If the optional copy fails, report it and leave the last verified AWS copy
untouched; do not switch storage away from DO Spaces to compensate.

Do **not** point `values-aws.yaml` at this AWS bucket in phase 1. Its
`S3_BUCKET_NAME` must remain `rag-system-storage`, with the DO endpoint and
`spaces-credentials` secret, for upload and DO KB ingestion to work.

---

## 4. Neo4j: dump on DOKS → load into EKS PVC

Chart facts (verified): STS `nous-dev-knowledge-graph-analytics-neo4j`, single
pod `-0`, PVC `neo4j-data-nous-dev-knowledge-graph-analytics-neo4j-0` mounted
at `/data`, image `neo4j:5.26-community`, data owned by UID/GID 7474.
`STOP DATABASE` is Enterprise-only; Community Edition needs the entire DBMS
offline for `neo4j-admin database dump`.

**4a. Stop the DO Neo4j StatefulSet and mount its PVC in a temporary helper.**
DO ArgoCD was paused in Step 1. Never delete the PVC.

```bash
kubectl scale statefulset nous-dev-knowledge-graph-analytics-neo4j -n rag-dev \
  --context $DOKS_CONTEXT --replicas=0
kubectl wait --for=delete pod/nous-dev-knowledge-graph-analytics-neo4j-0 \
  -n rag-dev --context $DOKS_CONTEXT --timeout=180s
kubectl apply -n rag-dev --context $DOKS_CONTEXT -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: neo4j-source
spec:
  restartPolicy: Never
  securityContext:
    runAsUser: 7474
    runAsGroup: 7474
  containers:
    - name: neo4j-source
      image: neo4j:5.26-community
      command: ["sleep", "7200"]
      volumeMounts:
        - name: neo4j-data
          mountPath: /data
  volumes:
    - name: neo4j-data
      persistentVolumeClaim:
        claimName: neo4j-data-nous-dev-knowledge-graph-analytics-neo4j-0
EOF
kubectl wait --for=condition=Ready pod/neo4j-source -n rag-dev --context $DOKS_CONTEXT --timeout=180s
```

Expected: original Neo4j pod gone, helper Ready with the same PVC. If the
helper cannot mount the PVC, halt and restore the DO StatefulSet before
attempting another path.

**4b. Dump offline on DO:**

```bash
kubectl exec -n rag-dev --context $DOKS_CONTEXT neo4j-source -- \
  sh -c 'mkdir -p /data/dumps && neo4j-admin database dump neo4j --to-path=/data/dumps'
kubectl exec -n rag-dev --context $DOKS_CONTEXT neo4j-source -- ls -lh /data/dumps/neo4j.dump
```

Expected: non-trivial `/data/dumps/neo4j.dump` owned by `neo4j`. If dump
errors, leave DO data untouched; remove the helper and restart the DO
StatefulSet for rollback or halt and diagnose.

**4c. Keep the dump in DO until the EKS staging pod is Ready.** Record its
byte count; Step 4e streams it directly to the EKS PVC. Keep the DO helper
mounted until the transfer is verified, then delete it (not its PVC).

**4d. Pause EKS GitOps and Neo4j before loading.** If the EKS STS has already
started once with an empty volume that's fine — the load below overwrites —
but it must stay at 0 during the load. Pause the root app first so it cannot
restore child auto-sync:

```bash
kubectl patch application aws-dev -n argocd --context $EKS_CONTEXT --type=json \
  -p='[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
kubectl patch application nous-dev-aws -n argocd --context $EKS_CONTEXT --type=json \
  -p='[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
kubectl scale statefulset nous-dev-aws-knowledge-graph-analytics-neo4j -n multimodal-rag-system \
  --context $EKS_CONTEXT --replicas=0
kubectl get pods -n multimodal-rag-system --context $EKS_CONTEXT -l app.kubernetes.io/component=neo4j
```

Expected: no neo4j pods.

**4e. Stage dump into the EKS PVC** via a helper pod mounting the STS's PVC `neo4j-data-nous-dev-aws-knowledge-graph-analytics-neo4j-0`:

```bash
kubectl apply -n multimodal-rag-system --context $EKS_CONTEXT -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: neo4j-stage
spec:
  restartPolicy: Never
  securityContext:
    runAsUser: 7474
    runAsGroup: 7474
  containers:
    - name: neo4j-stage
      image: neo4j:5.26-community
      command: ["sleep", "7200"]
      volumeMounts:
        - name: neo4j-data
          mountPath: /data
  volumes:
    - name: neo4j-data
      persistentVolumeClaim:
        claimName: neo4j-data-nous-dev-aws-knowledge-graph-analytics-neo4j-0
EOF
kubectl wait --for=condition=Ready pod/neo4j-stage -n multimodal-rag-system --context $EKS_CONTEXT --timeout=180s
kubectl exec -n multimodal-rag-system --context $EKS_CONTEXT neo4j-stage -- mkdir -p /data/dumps
set -o pipefail
kubectl exec -n rag-dev --context "$DOKS_CONTEXT" neo4j-source -- cat /data/dumps/neo4j.dump | \
  kubectl exec -i -n multimodal-rag-system --context "$EKS_CONTEXT" neo4j-stage -- \
  sh -c 'cat > /data/dumps/neo4j.dump'
kubectl exec -n multimodal-rag-system --context $EKS_CONTEXT neo4j-stage -- ls -lh /data/dumps
```

Expected: dump file inside the PVC at `/data/dumps/neo4j.dump` (no server running → load happens before first start).

**4f. Load into the PVC** (runs in the helper before the real pod ever starts):

```bash
kubectl exec -n multimodal-rag-system --context $EKS_CONTEXT neo4j-stage -- \
  neo4j-admin database load neo4j --from-path=/data/dumps --overwrite-destination=true
```

Expected: load completes, no error. EKS's pre-cutover empty database is
overwritten; the DO source PVC/dump are untouched.

**4g. Clean up helper; leave STS at 0** until Step 6:

```bash
kubectl delete pod neo4j-stage -n multimodal-rag-system --context $EKS_CONTEXT
kubectl delete pod neo4j-source -n rag-dev --context $DOKS_CONTEXT
```

Note: the `neo4j` database dump carries graph data only — users/credentials live in the `system` db and are NOT migrated. Auth on EKS is governed by the secret `nous-dev-aws-knowledge-graph-analytics-neo4j-credentials` (`NEO4J_AUTH`) — pre-created OUT-OF-BAND (`kubectl create secret`, 32-hex password); ArgoCD NEVER manages this secret (chart renders it only when `neo4j.password` is set, which values-aws deliberately does not). Backend's `NEO4J_PASSWORD` (Infisical `app-secrets`) must match. NEVER commit the password.

**4h. Align the shared backend graph credential only after DO writers are
frozen.** The EKS `NEO4J_AUTH` value and Infisical `/app/NEO4J_PASSWORD`
did not match during 2026-09-22 preflight. Privately set the latter to the
password portion of the EKS secret (`neo4j/<password>`); never print either
value. Verify the generated EKS `app-secrets` matches before Step 6. Rollback
must restore the recorded DO `/app/NEO4J_PASSWORD` version before DO starts.

---

## 5. Qdrant — not part of this cutover

Qdrant is not used: the user confirmed this, and preflight found no Qdrant
workload on DO or EKS. A stale DO PVC is not a service or a data source.
Skip this step. Do not deploy Qdrant or migrate the PVC.

---

## 6. Scale up EKS

**6a. Controllers healthy first** (repeat the kube-system check from Step 0):

```bash
kubectl get pods -n kube-system --context $EKS_CONTEXT \
  | grep -E "aws-load-balancer-controller|cluster-autoscaler|external-dns"
```

Expected: all Running/Ready. **HALT:** ALB controller or external-dns not ready — DNS and TLS cannot come up without them.

**6b. Infisical on EKS** — confirm the operator synced the updated `/database` and `/redis` secrets into `multimodal-rag-system`:

```bash
kubectl -n multimodal-rag-system --context $EKS_CONTEXT get secret \
  database-credentials redis-credentials app-secrets supabase-credentials azure-openai-credentials spaces-credentials do-kb-credentials -o name
kubectl -n multimodal-rag-system --context $EKS_CONTEXT get infisicalsecrets
```

Expected: all secrets present and CRDs synced. Confirm the two DO document
service secrets without printing their secret values. Missing either = HALT.

**6c. Sync Redis endpoint into Infisical** (ElastiCache auth token — transit encryption is ON, so clients use `rediss://` + AUTH token):

```bash
# Retrieve the terraform-generated Redis auth token privately from the
# approved secrets store; do not print it in terminal logs or this runbook.
```

In Infisical UI, env `dev`, folder `/redis`:
- `REDIS_URL` → `rediss://default:<AUTH_TOKEN>@<ELASTICACHE_ENDPOINT>:6379/0`
- `REDIS_PASSWORD` → `<AUTH_TOKEN>`

Also update the duplicate keys in `/database`: `REDIS_HOST` → bare
`<ELASTICACHE_ENDPOINT>`, `REDIS_PORT` → `6379`, `REDIS_URL` and
`REDIS_PASSWORD` → the same values as `/redis`. Some backend paths read
`REDIS_HOST` directly; leaving the DO host would split cache traffic.

Expected: secrets saved. (KEDA reads `keda.redis.address` = `<ELASTICACHE_ENDPOINT>:6379` from `values-aws.yaml` and the password from secret `redis-credentials` key `REDIS_PASSWORD`, TLS on.)

**HALT:** auth token unavailable → stop; do not guess.

**6d. Open the AWS writer gate and enable GitOps.** In the existing
`migration/aws` worktree, edit `values-aws.yaml` to set
`backend.replicaCount=1`, `celeryWorker.replicaCount=1`,
`celeryBeat.replicaCount=1`, and `celeryWorker.autoscaling.enabled=true`.
Leave `syntheticTraffic.suspend=true` until user-path smoke tests pass.
Render and verify the chart, commit and push this scoped values change, then:

```bash
kubectl scale statefulset nous-dev-aws-knowledge-graph-analytics-neo4j -n multimodal-rag-system --context $EKS_CONTEXT --replicas=1
kubectl patch application nous-dev-aws -n argocd --context $EKS_CONTEXT --type=merge \
  -p='{"spec":{"syncPolicy":{"automated":{"allowEmpty":false,"prune":true,"selfHeal":true}}}}'
kubectl patch application aws-dev -n argocd --context $EKS_CONTEXT --type=merge \
  -p='{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
kubectl get pods -n multimodal-rag-system --context $EKS_CONTEXT -w   # Ctrl-C when settled
```

Expected: backend, celery-worker, celery-beat, and neo4j pods Running/Ready; backend `wait-for-postgres` init container passes (RDS reachable). Qdrant is not deployed.

**HALT:** CrashLoopBackOff on backend/worker → `kubectl logs` the pod; most common cause is a wrong endpoint in Infisical — fix value, let the operator re-sync, restart the deployment.

---

## 7. DNS: Cloudflare → ALB

**7a. Get the ALB hostname:**

```bash
kubectl get ingress -n multimodal-rag-system --context $EKS_CONTEXT
```

Expected: `backend-ingress` (+ `websocket-ingress`) ADDRESS = `k8s-nousdev-...us-east-1.elb.amazonaws.com`.

**7b. Verify external-dns created the CNAMEs** (records per the `external-dns.alpha.kubernetes.io/hostname` annotations, e.g. `dev-api.goodwiinz.tech` and the wired ws host):

```bash
dig +short CNAME dev-api.goodwiinz.tech
```

Expected: the ALB hostname from 7a. external-dns may take a few minutes (TTL annotation 300; if it stays absent >10 min: `kubectl logs -n kube-system deploy/external-dns --context $EKS_CONTEXT | tail`).

**7c. Fallback — create manually in Cloudflare** (only if external-dns cannot): CNAME `dev-api` (and ws host) → ALB hostname, **TTL 60**, proxy OFF initially (DNS-only) to let ACM/ALB validation behave predictably.

**7d. Confirm ACM cert in use:**

```bash
curl -svI https://dev-api.goodwiinz.tech/health -o /dev/null 2>&1 | grep -E "subject:|issuer:|HTTP/"
curl -s https://dev-api.goodwiinz.tech/health
```

Expected: issuer `Amazon`, HTTP 200 (or the app's health response), no cert warnings.

**HALT:** cert mismatch/`SSL_ERROR` → ACM cert ARN on the ingress ≠ cert covering the host; fix annotation + `kubectl rollout restart` is not enough — the ingress must be re-applied (argocd app sync). Do not proceed to smoke tests on a broken TLS chain.

**7e. Switch the live Vercel frontend build.** The `md-basit/nous` project
build command injects Infisical `dev` `/frontend-build`; the current build is
hard-coded to DO. After AWS `/health` and `/ws/health` pass, update its four
values (recorded in Step 0):

- `BACKEND_URL`, `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_API_BASE_URL` → `https://dev-api.goodwiinz.tech`
- `NEXT_PUBLIC_WS_URL` → `wss://dev-ws.goodwiinz.tech` (**origin only**; callers append `/ws` or other paths)

Rebuild in the correct Vercel scope, retaining the Step 0 DO-backed deployment
as the rollback anchor:

```bash
vercel redeploy <recorded-DO-deployment-id> --target production --scope md-basit
vercel inspect <new-deployment-id> --scope md-basit
curl -sS -o /dev/null -w '%{http_code}\n' https://goodwiinz.tech/
```

Expected: new deployment Ready, `goodwiinz.tech` and `www.goodwiinz.tech`
alias it, and its built API/WS settings point to AWS. Verify the browser
bundle or network requests, not just a 401 (both DO and AWS can return 401).
If deployment fails, run `vercel rollback <recorded-DO-deployment-id> --scope md-basit --yes`
and restore `/frontend-build` versions before retrying.
**HALT:** live alias or build still points to DO → do not claim cutover.

---

## 8. Smoke tests

Run all from outside the cluster (real user path, via ALB). Use the
**verified live frontend URL** recorded in Step 0; do not assume
`dev-app.goodwiinz.tech` exists.

| # | Test | Command / action | Expected |
|---|---|---|---|
| 1 | Auth/login | Log in on the verified live frontend | Session created; no 5xx on `/api` calls |
| 2 | Upload → embed → search | Upload a small document in the UI, wait for ingestion, run a search | Doc status becomes ingested; search returns it |
| 3 | Chat streaming (SSE) | Send a chat message | Tokens stream incrementally (ALB `idle-timeout: 300` ≥ app `TIMEOUT: 300`) |
| 4 | WebSocket | Open a session that uses the ws path | Socket connects (ALB `idle-timeout: 4000` on websocket-ingress); `/ws/health` 200 |
| 5 | Knowledge-graph entities page | Open entities page | Nodes/edges render (Neo4j restored — data visible from Step 4) |
| 6 | Celery beat | `kubectl logs -n multimodal-rag-system --context $EKS_CONTEXT deploy/nous-dev-aws-celery-beat --tail=50` | Heartbeat/tick lines; no task failures |
| 7 | Celery worker | `kubectl logs -n multimodal-rag-system --context $EKS_CONTEXT deploy/nous-dev-aws-celery-worker --tail=50` | Tasks consumed from ElastiCache queue |
| 8 | Worker autoscaling (HPA) | `kubectl get hpa -n multimodal-rag-system --context $EKS_CONTEXT` | Worker HPA present (KEDA is NOT installed on EKS — `keda.enabled: false`; re-test ScaledObject after KEDA install) |
| 9 | DO Spaces round-trip | Download a pre-migration file and upload/download a small new file through the UI/app | Both served from DO Spaces (`rag-system-storage`); new file indexes in DO KB and is searchable |
| 10 | Synthetic traffic | After tests 1-9 pass, commit `syntheticTraffic.suspend=false` in `values-aws.yaml`, wait for Argo sync and next `*/20` tick, then check job logs | Synthetic job succeeds; leave suspended if earlier smoke tests fail |

**HALT:** any test fails → capture logs, then decide: quick fix inside the window, or rollback (Step 9). Timebox: if >2 smoke tests fail or the failure class is data-related, roll back.

---

## 9. Halt criteria + ROLLBACK

**Halt immediately if any of:**

- a precondition in Step 0 was skipped or failed
- any "HALT" criterion in Steps 1-8 triggers and cannot be resolved within the window
- PG row counts differ (Step 2f)
- smoke tests fail per the Step 8 timebox rule
- window timebox exceeded — better an aborted cutover than a half-migrated dev

### Rollback (independently executable — EKS becomes irrelevant)

Requires: a **fresh shell** — re-export `$DOKS_CONTEXT`/`$EKS_CONTEXT`;
all ArgoCD changes below use explicit kubectl contexts (no saved CLI login).
Also: `.cutover/do-replicas.txt`, `.cutover/do-ingress.txt`,
`.cutover/infisical-versions.txt` (recorded in Step 0), the tested frontend
rollback route, and DO ArgoCD apps still present (only paused, not deleted).

**9.1. Pause GitOps, then scale EKS down** (prevents split-brain writers):

```bash
kubectl patch application aws-dev -n argocd --context $EKS_CONTEXT --type=json \
  -p='[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
kubectl patch application nous-dev-aws -n argocd --context $EKS_CONTEXT --type=json \
  -p='[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
kubectl scale deployments --all -n multimodal-rag-system --context $EKS_CONTEXT --replicas=0
kubectl get deploy -n multimodal-rag-system --context $EKS_CONTEXT
```

Expected: all EKS app Deployments remain at zero after GitOps is paused.

**9.2. Revert shared Infisical secrets before restarting DO:** restore
`/database` (DO PG values) and `/redis` (DO Valkey values) from Infisical
secret version history, including the `/database` Redis duplicates, and
`/app/NEO4J_PASSWORD`, using the versions in `.cutover/infisical-versions.txt`.
These `dev` secrets sync into **both** clusters; wait until the DO
`database-credentials` and `redis-credentials` Secrets show the restored
versions before scaling any DO pod. Do not retype values from memory or print
them. DO PG/Valkey are still running.

**9.3. Unfreeze DO:**

```bash
kubectl patch application nous-dev -n argocd --context $DOKS_CONTEXT --type=merge \
  -p='{"spec":{"syncPolicy":{"automated":{"allowEmpty":false,"prune":true,"selfHeal":true}}}}'
kubectl patch application nous-root -n argocd --context $DOKS_CONTEXT --type=merge \
  -p='{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
kubectl scale statefulset nous-dev-knowledge-graph-analytics-neo4j -n rag-dev \
  --context $DOKS_CONTEXT --replicas=1
kubectl rollout status statefulset/nous-dev-knowledge-graph-analytics-neo4j \
  -n rag-dev --context $DOKS_CONTEXT --timeout=180s
# restore recorded replica counts (selfHeal also enforces git values)
while read -r name reps; do
  [ -n "$reps" ] && kubectl scale deploy "$name" -n rag-dev --context $DOKS_CONTEXT --replicas="$reps"
done < .cutover/do-replicas.txt
kubectl get pods -n rag-dev --context $DOKS_CONTEXT -w
```

**9.4. Restore the tested frontend → DO API route:**
`vercel rollback <recorded-DO-deployment-id> --scope md-basit --yes`; verify
the Ready DO-backed deployment again owns both `goodwiinz.tech` and
`www.goodwiinz.tech`. Restore the four `/frontend-build` Infisical versions
recorded in Step 0, so subsequent Vercel builds also target DO. Merely pointing
`dev-api.goodwiinz.tech` at the DO LB is **not sufficient** unless the DO
ingress and its certificate have been verified for that host. If external-dns
on EKS fights a DNS change, scale it to 0 first:
`kubectl scale deploy -n kube-system external-dns --context $EKS_CONTEXT --replicas=0`.

Expected after 9.2-9.4: the Step 0 recorded DO API hostname returns 200
after pod-ready time; the actual live frontend login + chat work again.
Resume the DO synthetic-traffic CronJob to its recorded pre-freeze suspend
state only after DO app/database health and the frontend route are verified
(if Step 0 recorded `false`, patch `spec.suspend=false` then verify it).

**9.5. Decision point:** schedule a retry window; re-dump PG and Neo4j in the
next window rather than reusing stale dumps. RCA the failure before re-entry.

---

## 10. Post-cutover

- **First 24h — monitor:**
  - Grafana (kube-prometheus-stack): NOT deployed on EKS — `prometheus.enabled: false`
    in `values-aws.yaml`, so there is no `rag-system-monitoring` namespace; rely on
    CloudWatch + Sentry until a monitoring stack is stood up (fast-follow)
  - CloudWatch (us-east-1): ALB 5xx/target health, RDS connections+CPU, ElastiCache evictions
  - Sentry (`SENTRY_ENVIRONMENT=dev`): new error classes vs pre-cutover baseline
  - Check that new uploads land in DO Spaces and are indexed by DO KB. The
    AWS S3 copy is only a checkpoint; a fresh `rclone check` may show expected
    differences after new writes. If desired, run a new cache-excluded backup
    sync and verify it without deleting the DO source.
- **DO app workloads stay frozen 2 weeks** as the rollback source. DO Spaces
  and DO KB remain live dependencies throughout this phase. Do not delete DO
  resources or resume `nous-dev`/`nous-root` auto-sync on DO ArgoCD.
- **Do not execute** `docs/runbooks/aws-decommission.md` after this soak.
  It assumes Spaces/KB have already moved and would delete live dependencies.
  A separate storage/KB migration and explicit go/no-go are required first.
- Close-out notes: record actual cutover times, any deviations from this runbook, and the ECR tag ↔ source SHA mapping for the deployed images.
