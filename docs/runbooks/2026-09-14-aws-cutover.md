# Cutover Runbook: DigitalOcean → AWS (dev)

**Date:** 2026-09-14
**Design:** `docs/plans/2026-09-14-do-to-aws-migration-design.md`
**Plan:** `docs/plans/2026-09-14-do-to-aws-migration.md` (Task 8; Tasks 1-7 are pre-stage prerequisites)
**Window:** single freeze-and-migrate window, hours of acceptable downtime
**Rollback:** DO kept frozen (not deleted) for 2 weeks — see [Step 9](#9-halt-criteria--rollback)

## Environments

| | Source (freeze) | Target (cutover) |
|---|---|---|
| Kubernetes | DOKS `do-nyc3-rag-system-cluster` | EKS `nous-dev-cluster` (us-east-1) |
| App namespace | `rag-dev` | `rag-dev` |
| Helm release | `nous-dev` (ArgoCD app of same name) | `nous-dev` |
| Postgres | DO managed PG, db `multimodal_rag` | RDS PostgreSQL 16 (`db.t4g.small`) |
| Redis | DO managed Valkey | ElastiCache `cache.t4g.small` (**no data migration — cold start**) |
| Neo4j | STS `nous-dev-knowledge-graph-analytics-neo4j` (5.26) | same chart on EBS gp3 |
| Qdrant | STS in `rag-dev` (1.13, manually applied — not in git) | same on EBS gp3 |
| Object storage | Spaces `rag-system-storage` (nyc3) | S3 `nous-storage-us-east-1` |
| Images | DO registry | ECR `nous/backend`, `nous/frontend` |
| DNS | Cloudflare (external-dns) | Cloudflare (external-dns) → ALB group `nous-dev` |
| Frontend | Vercel `dev-app.gen-text.app` | unchanged |

**Working contexts** (set once, verify per step):

```bash
export DOKS_CONTEXT=<DOKS_CONTEXT>   # kubectl config get-contexts — DOKS entry
export EKS_CONTEXT=<EKS_CONTEXT>     # nous-dev-cluster
```

> **Rule:** every `kubectl` in Steps 1-5 runs against `$DOKS_CONTEXT` unless the
> command explicitly says `-n rag-dev --context $EKS_CONTEXT`. Halt if a command
> errors or output differs from "Expected".
>
> `argocd` CLI does NOT follow kubectl contexts — it talks to whatever server it
> is logged into. Steps 1/9.3 run against the **DO** ArgoCD; the Step 0
> pre-check, Step 6, and 9.1 run against the **EKS** ArgoCD. On EKS the app
> names are: `aws-dev` (root app-of-apps) and `nous-dev` (child, Helm release).
> Re-run `argocd login` (or `argocd context`) when switching.

---

## 0. Preconditions checklist

All boxes must be checked before starting. Any unchecked box = no go.

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
- [ ] rclone remotes configured and reachable:
  ```bash
  rclone lsd spaces: && rclone lsd s3:
  rclone lsf spaces:rag-system-storage --max-depth 1 | head
  rclone lsf s3:nous-storage-us-east-1 --max-depth 1 | head
  ```
  Expected: both remotes list without auth errors (`spaces` = DO Spaces keys, `s3` = AWS profile).
- [ ] Disk space for dumps on the operator machine: `df -h .` — need ≥ (Spaces usage + 2×PG dump size + 2×Neo4j dump). Check source sizes first:
  ```bash
  rclone size spaces:rag-system-storage
  ```
- [ ] `infrastructure/terraform/terraform.tfvars` placeholders filled before the last `terraform apply`: `owner_email`, `alert_email`, and **`cluster_admin_role_arns` non-empty** (aws-auth lockout otherwise). Verify:
  ```bash
  grep -E "owner_email|alert_email|cluster_admin_role_arns" infrastructure/terraform/terraform.tfvars
  ```
  Expected: real email(s); `cluster_admin_role_arns = ["arn:aws:iam::<account>:role/<admin>", ...]`.
- [ ] Terraform outputs recorded (`terraform output > /tmp/nous-aws-outputs.txt`, Task 2). Needed values: `database_endpoint`, `database_port`, `database_name`, `database_username`, `redis_endpoint`, `storage_bucket_name`, `caller_identity.account_id`.
- [ ] Task 5 wiring done: `infrastructure/helm/knowledge-graph-analytics/values-aws.yaml` placeholders filled (`<ACCOUNT_ID>`, `<APPLICATION_STORAGE_BUCKET>`, `<ELASTICACHE_ENDPOINT>`, `<ACM_CERT_ARN>`) and `infrastructure/kubernetes/overlays/aws-dev/` scaffold hosts (`*.multimodal-rag.example.com`) replaced with the real `*.gen-text.app` hosts. Verify:
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
  Expected: image tagged with the same source SHA currently running on DO (`kubectl -n rag-dev --context $DOKS_CONTEXT get deploy -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.template.spec.containers[0].image}{"\n"}{end}'` — match digests, not tags).
- [ ] Task 7 first rclone sync done and clean:
  ```bash
  rclone check spaces:rag-system-storage s3:nous-storage-us-east-1
  ```
  Expected: `0 differences` (pre-freeze drift acceptable; final delta synced in Step 3).
- [ ] EKS addons healthy (Task 3) — required before Step 6 scale-up, check now:
  ```bash
  kubectl get pods -n kube-system --context $EKS_CONTEXT \
    | grep -E "aws-load-balancer-controller|cluster-autoscaler|external-dns|ebs-csi"
  kubectl get pods -n external-secrets --context $EKS_CONTEXT
  ```
  Expected: all Running/Ready.
- [ ] EKS app stack deployed and both EKS ArgoCD apps exist but **paused** (sync none) per Task 5: root app-of-apps `aws-dev` and its `nous-dev` child. Verify:
  ```bash
  argocd app list   # (EKS ArgoCD)
  ```
  Expected: `aws-dev` (root) and `nous-dev` (child) on EKS, both `SyncPolicy: <none>` (manual).
- [ ] ACM certificate issued (Task 3 terraform; DNS validation CNAME added manually in Cloudflare):
  ```bash
  aws acm list-certificates --region us-east-1 \
    --query "CertificateSummaryList[?Status=='ISSUED'].[CertificateArn,DomainName]"
  ```
  Expected: cert for `*.gen-text.app` (or `dev-api.gen-text.app` + ws host) `ISSUED`. ARN must equal `<ACM_CERT_ARN>` used in values-aws/overlay.

**Record for rollback (Step 9)** — run now and keep the output:

```bash
kubectl get deployments -n rag-dev --context $DOKS_CONTEXT --no-headers \
  -o custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas > /tmp/do-replicas.txt
kubectl get ingress -n rag-dev --context $DOKS_CONTEXT -o wide > /tmp/do-ingress.txt   # DO LB hostname(s)
cat /tmp/do-replicas.txt /tmp/do-ingress.txt
```

Expected: e.g. `nous-dev-knowledge-graph-analytics-backend 1`, `nous-dev-celery-beat 1`, `nous-dev-celery-worker 1`. `/tmp/do-ingress.txt` shows the current DO LB address behind `dev-api.gen-text.app`.

**Infisical rollback anchor (needed by 9.4):** rollback restores `/database` and
`/redis` from **Infisical secret version history** — Infisical keeps every prior
version, so no secret values are copied to disk. Before freezing, record the
current version number of each secret in `/database` (`DATABASE_URL`,
`POSTGRES_USER`, `POSTGRES_PASSWORD`) and `/redis` (`REDIS_URL`, `REDIS_PASSWORD`)
into `/tmp/infisical-versions.txt` (Infisical UI → project `nous-platform`,
env `dev` → each secret's history), so 9.4 can restore the exact prior versions.

**HALT:** any precondition fails → fix before freeze. Do not enter the window.

---

## 1. Freeze DigitalOcean

Goal: stop all writers (Deployments) while keeping StatefulSets (Neo4j, Qdrant) up for dumps.

**1a. Pause ArgoCD auto-sync on DO** — `nous-root` first (it self-heals child apps; pausing it prevents the child policy from being reverted from git):

```bash
argocd app set nous-root  --sync-policy none
argocd app set nous-dev   --sync-policy none
sleep 120
argocd app list
```

Expected: both apps show `SyncPolicy: <none>`. If `nous-dev` reverts to `automated` after the wait, `nous-root` is still syncing it — re-run `argocd app set nous-root --sync-policy none`, wait, re-check.

**HALT:** if `nous-dev` sync policy cannot be pinned to none after two attempts — do not scale down; GitOps would undo the freeze.

**1b. Scale DO Deployments to 0** (StatefulSets keep running):

```bash
kubectl scale deployments --all -n rag-dev --context $DOKS_CONTEXT --replicas=0
kubectl get deploy -n rag-dev --context $DOKS_CONTEXT
kubectl get statefulset -n rag-dev --context $DOKS_CONTEXT
```

Expected: every Deployment `0/1`; Neo4j and Qdrant StatefulSets still `1/1` Ready.

**1c. Confirm quiescence externally** (pods are gone, so check through the LB):

```bash
sleep 60 && curl -sS -m 5 https://dev-api.gen-text.app/health; echo "exit=$?"
```

Expected: non-200 / connection failure — no backend is running, so no writers can reach PG/Neo4j/Qdrant.

**1d. (optional, self-protection)** DO-side ArgoCD UI: mark `nous-dev` app as suspended so nobody clicks sync.

**HALT:** Deployments stuck terminating >5 min → investigate before proceeding; writers still alive would fork data.

---

## 2. Postgres: DO managed PG → RDS

Run inside a transient pod on **EKS** (RDS is VPC-private; DO managed PG is reachable from EKS pods over the public endpoint — see HALT note).

**2a. Launch migration pod:**

```bash
kubectl run pg-mig -n rag-dev --context $EKS_CONTEXT \
  --image=postgres:16-alpine --restart=Never --command -- sleep 7200
kubectl wait --for=condition=Ready pod/pg-mig -n rag-dev --context $EKS_CONTEXT --timeout=120s
```

Expected: `condition met`.

**2b. Define connection params** (values never committed; DO values from Infisical `/database` → `DATABASE_URL`, RDS master password from your secrets store):

```bash
export DO_PGHOST=<DO_PG_HOST>      export DO_PGPORT=<DO_PG_PORT>
export DO_PGUSER=<DO_PG_USER>      export DO_PGPASSWORD='<DO_PG_PASSWORD>'
export RDS_HOST=<RDS_HOST>         # terraform output database_endpoint
export RDS_PORT=5432               # terraform output database_port
export RDS_USER=raguser            # terraform output database_username (tfvars db_username)
export RDS_PASSWORD='<RDS_PASSWORD>'

# Helpers — expand on the laptop, run inside the pg-mig pod. Arguments are
# passed verbatim to psql (no shell layer inside the pod), so SQL containing
# quotes, parentheses, or semicolons is safe. Passwords are only parsed by
# your login shell: keep the export lines single-quoted (escape ' as '\'').
psql_do()  { kubectl exec -i -n rag-dev pg-mig --context "$EKS_CONTEXT" -- env PGPASSWORD="$DO_PGPASSWORD" psql -h "$DO_PGHOST" -p "$DO_PGPORT" -U "$DO_PGUSER" -d multimodal_rag "$@"; }
psql_rds() { kubectl exec -i -n rag-dev pg-mig --context "$EKS_CONTEXT" -- env PGPASSWORD="$RDS_PASSWORD" psql -h "$RDS_HOST" -p "$RDS_PORT" -U "$RDS_USER" -d multimodal_rag "$@"; }

psql_do -c "SELECT version();" && psql_rds -c "SELECT version();"
```

Expected: both print a PostgreSQL version banner.

**HALT:** cannot connect to either side from the pod → stop. If DO PG rejects the EKS pod source, dump from the operator laptop instead (`PGPASSWORD=... pg_dump -Fc -h $DO_PGHOST -U $DO_PGUSER -d multimodal_rag -f nous.dump` locally, `kubectl cp` into `pg-mig`) — client version must be ≥ server version.

**2c. Verify role/db on RDS exist** (Task 2 tfvars `db_name = "multimodal_rag"`, `db_username = "raguser"` create them; verify, create only if missing):

```bash
psql_rds -tAc "SELECT 1 FROM pg_database WHERE datname = 'multimodal_rag';"
psql_rds -tAc "SELECT 1 FROM pg_roles WHERE rolname = '$RDS_USER';"
```

Expected: `1` and `1`. If the db is missing:

```bash
kubectl exec -i -n rag-dev pg-mig --context "$EKS_CONTEXT" -- env PGPASSWORD="$RDS_PASSWORD" \
  psql -h "$RDS_HOST" -p "$RDS_PORT" -U "$RDS_USER" -d postgres -c 'CREATE DATABASE multimodal_rag;'
```

(The role is the RDS master user — created by terraform.)

**2d. Dump DO:**

```bash
kubectl exec -n rag-dev pg-mig --context "$EKS_CONTEXT" -- env PGPASSWORD="$DO_PGPASSWORD" \
  pg_dump -Fc -h "$DO_PGHOST" -p "$DO_PGPORT" -U "$DO_PGUSER" -d multimodal_rag -f /tmp/nous.dump
kubectl exec -n rag-dev pg-mig --context "$EKS_CONTEXT" -- ls -lh /tmp/nous.dump
kubectl exec -n rag-dev pg-mig --context "$EKS_CONTEXT" -- pg_restore --list /tmp/nous.dump | tail -5
```

Expected: non-trivial file size; `pg_restore --list` prints TOC entries without error.

**HALT:** dump exits non-zero (e.g. version mismatch, auth failure) — nothing has been written to RDS yet; safe to fix and retry.

**2e. Restore to RDS:**

```bash
kubectl exec -i -n rag-dev pg-mig --context "$EKS_CONTEXT" -- env PGPASSWORD="$RDS_PASSWORD" \
  pg_restore -h "$RDS_HOST" -p "$RDS_PORT" -U "$RDS_USER" -d multimodal_rag \
  --no-owner --no-privileges --exit-on-error -j 2 /tmp/nous.dump
psql_rds -c "ANALYZE;"
```

Expected: exit 0, no `error:` lines. (Warnings about extensions/privileges that were skipped by `--no-privileges` are acceptable.)

**HALT:** any error line or non-zero exit → **rollback decision now** (nothing else migrated yet; simply fix or abandon window — DO still frozen and intact).

**2f. Verify row counts match** — exact count per public table on both sides, then diff:

```bash
COUNTS='SELECT string_agg(format('"'"'SELECT %L AS tbl, count(*) FROM %I'"'"', tablename, tablename), '"'"' UNION ALL '"'"') FROM pg_tables WHERE schemaname = '"'"'public'"'"';'
psql_do  -Atc "$COUNTS" | psql_do  > /tmp/counts-do.txt
psql_rds -Atc "$COUNTS" | psql_rds > /tmp/counts-rds.txt
diff /tmp/counts-do.txt /tmp/counts-rds.txt && echo COUNTS-MATCH
```

Expected: `COUNTS-MATCH` (every table, exact `count(*)`, source vs target identical).

**HALT:** any row differs → DO writes are frozen so the source is stable: investigate the differing table (sequence values, partial copy) before continuing.

**2g. Point Infisical at RDS** — in Infisical UI (project `nous-platform`, env `dev`), update folder `/database`:

- `DATABASE_URL` → `postgresql://<RDS_USER>:<RDS_PASSWORD>@<RDS_HOST>:5432/multimodal_rag`
- `POSTGRES_USER` → `<RDS_USER>` (`raguser`)
- `POSTGRES_PASSWORD` → `<RDS_PASSWORD>`

Expected: secrets saved (operator re-syncs on next reconcile, ≤60s; new backend pods in Step 6 pick these up via secret `database-credentials`).

**2h. Remove migration pod** (kept until here for re-verification; delete now, recreate if Step 9 rollback needs counts):

```bash
kubectl delete pod pg-mig -n rag-dev --context $EKS_CONTEXT
```

---

## 3. Final Spaces → S3 sync + verify

DO writes are frozen (Step 1), so this is the last delta.

```bash
rclone sync spaces:rag-system-storage s3:nous-storage-us-east-1 --progress
rclone check spaces:rag-system-storage s3:nous-storage-us-east-1
```

Expected: check reports `0 differences` and `0 errors`.

**HALT:** any difference or error → re-run sync; repeated failures (auth, >5 min) → halt window.

Then confirm the target bucket name matches the chart: `storage_bucket_name` terraform output must equal `S3_BUCKET_NAME` in `values-aws.yaml`.

---

## 4. Neo4j: dump on DOKS → load into EKS PVC

Chart facts (verified): STS `nous-dev-knowledge-graph-analytics-neo4j`, single pod `-0`, only `/data` is PVC-mounted, image `neo4j:5.26-community`. Dump goes to `/data/dumps`.

**4a. Stop the database inside DO pod** (writes already frozen; `neo4j-admin database dump` needs the database stopped in 5.x community):

```bash
export NEO4J_POD=nous-dev-knowledge-graph-analytics-neo4j-0
export NEO4J_PW=$(kubectl get secret -n rag-dev --context $DOKS_CONTEXT \
  nous-dev-knowledge-graph-analytics-neo4j-credentials -o jsonpath='{.data.NEO4J_AUTH}' | base64 -d | sed 's|^neo4j/||')
kubectl exec -n rag-dev --context $DOKS_CONTEXT $NEO4J_POD -- \
  cypher-shell -a bolt://localhost:7687 -u neo4j -p "$NEO4J_PW" "STOP DATABASE neo4j;"
```

Expected: `0 rows` ready indicator, no error.

**HALT:** auth failure → fix password sourcing; database still running.

**4b. Dump on DO:**

```bash
kubectl exec -n rag-dev --context $DOKS_CONTEXT $NEO4J_POD -- \
  sh -c 'mkdir -p /data/dumps && neo4j-admin database dump neo4j --to-path=/data/dumps'
kubectl exec -n rag-dev --context $DOKS_CONTEXT $NEO4J_POD -- ls -lh /data/dumps
```

Expected: `/data/dumps/neo4j.dump` listed with non-trivial size. If the container FS denies `mkdir` (read-only or permission), fall back to streaming: `kubectl exec ... -- neo4j-admin database dump neo4j --to-stdout > nous-neo4j.dump` on the laptop.

**HALT:** dump command errors → database can be restarted (`START DATABASE neo4j;`) to re-attempt; halt window if it fails twice.

**4c. Copy dump off DO:**

```bash
kubectl cp rag-dev/$NEO4J_POD:/data/dumps/neo4j.dump /tmp/nous-neo4j.dump -c neo4j --context $DOKS_CONTEXT
ls -lh /tmp/nous-neo4j.dump
```

Expected: local file, same size as listed in 4b.

**4d. Pause EKS Neo4j before first data-bearing start.** If the EKS STS has already started once with an empty volume that's fine — the load below overwrites — but the STS must be at 0 during the load:

```bash
kubectl scale statefulset nous-dev-knowledge-graph-analytics-neo4j -n rag-dev \
  --context $EKS_CONTEXT --replicas=0
kubectl get pods -n rag-dev --context $EKS_CONTEXT -l app.kubernetes.io/component=neo4j
```

Expected: no neo4j pods.

**4e. Stage dump into the EKS PVC** via a helper pod mounting the STS's PVC `neo4j-data-nous-dev-knowledge-graph-analytics-neo4j-0`:

```bash
kubectl apply -n rag-dev --context $EKS_CONTEXT -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: neo4j-stage
spec:
  restartPolicy: Never
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
        claimName: neo4j-data-nous-dev-knowledge-graph-analytics-neo4j-0
EOF
kubectl wait --for=condition=Ready pod/neo4j-stage -n rag-dev --context $EKS_CONTEXT --timeout=180s
kubectl exec -n rag-dev --context $EKS_CONTEXT neo4j-stage -- mkdir -p /data/dumps
kubectl cp /tmp/nous-neo4j.dump rag-dev/neo4j-stage:/data/dumps/neo4j.dump --context $EKS_CONTEXT
kubectl exec -n rag-dev --context $EKS_CONTEXT neo4j-stage -- ls -lh /data/dumps
```

Expected: dump file inside the PVC at `/data/dumps/neo4j.dump` (no server running → load happens before first start).

**4f. Load into the PVC** (runs in the helper before the real pod ever starts):

```bash
kubectl exec -n rag-dev --context $EKS_CONTEXT neo4j-stage -- \
  neo4j-admin database load neo4j --from-path=/data/dumps
```

Expected: load completes, no error. (If the empty default database was ever created, add `--overwrite-destination=true`.)

**4g. Clean up helper; leave STS at 0** until Step 6:

```bash
kubectl delete pod neo4j-stage -n rag-dev --context $EKS_CONTEXT
```

Note: the `neo4j` database dump carries graph data only — users/credentials live in the `system` db and are NOT migrated. Auth on EKS is governed by the chart secret `nous-dev-knowledge-graph-analytics-neo4j-credentials` (`NEO4J_AUTH`, password supplied at deploy per `values-aws.yaml`).

---

## 5. Qdrant: per-collection snapshots → restore on EKS

Qdrant's StatefulSet in `rag-dev` was applied manually (not in git) — discover the real pod/service name first. Port `6333` (HTTP).

**5a. Discover:**

```bash
kubectl get statefulset,svc,pvc -n rag-dev --context $DOKS_CONTEXT | grep -i qdrant
export QDRANT_POD=<qdrant-pod-0>        # from statefulset output above
export QDRANT_SVC=<qdrant-service>      # from svc output above
```

**HALT:** no Qdrant found → confirm with the team whether dev still runs Qdrant; if truly absent, skip Step 5 and record it.

**5b. Snapshot every collection on DO** (collections are frozen since Step 1):

```bash
mkdir -p /tmp/qdrant-snaps
kubectl port-forward -n rag-dev --context $DOKS_CONTEXT $QDRANT_POD 6333:6333 &
curl -s http://localhost:6333/collections | jq -r '.result.collections[].name' | tee /tmp/qdrant-collections.txt
while read -r c; do
  curl -s -X POST "http://localhost:6333/collections/$c/snapshots" | jq . | tee "/tmp/qdrant-snaps/${c}.snap.json"
done < /tmp/qdrant-collections.txt
```

Expected: non-empty collection list; each POST returns snapshot info incl. `name` and `size`, saved to `/tmp/qdrant-snaps/<collection>.snap.json` (5c downloads the exact snapshot named there).

**HALT:** empty collection list → verify with team (could be legitimately empty; record and continue only on confirmation).

**5c. Download snapshots:**

```bash
while read -r c; do
  snap=$(jq -r '.result.name' "/tmp/qdrant-snaps/${c}.snap.json")
  if [ -z "$snap" ]; then echo "MISSING: no snapshot recorded for $c — rerun 5b"; continue; fi
  curl -s "http://localhost:6333/collections/$c/snapshots/$snap" \
    -o "/tmp/qdrant-snaps/${c}.snapshot"
done < /tmp/qdrant-collections.txt
ls -lh /tmp/qdrant-snaps
```

Expected: one `.snapshot` file per collection, sizes matching 5b.

**HALT:** any 0-byte file → re-download that collection before proceeding.

**5d. Restore on EKS** — port-forward the EKS qdrant pod and upload each snapshot:

```bash
kubectl get statefulset,svc,pvc -n rag-dev --context $EKS_CONTEXT | grep -i qdrant
export EKS_QDRANT_POD=<eks-qdrant-pod-0>
kubectl port-forward -n rag-dev --context $EKS_CONTEXT $EKS_QDRANT_POD 6334:6333 &
for f in /tmp/qdrant-snaps/*.snapshot; do
  c=$(basename "$f" .snapshot)
  curl -s -X POST "http://localhost:6334/collections/$c/snapshots/upload?priority=snapshot" \
    -F "snapshot=@$f" | jq .
done
```

Expected: each upload returns `status: ok` and the restored collection name. (If the EKS STS pods aren't running yet because ArgoCD is paused, scale the qdrant STS to 1 first — same pattern as 4d in reverse.)

**5e. Verify collection counts match:**

```bash
while read -r c; do
  src=$(curl -s "http://localhost:6333/collections/$c" | jq -r '.result.points_count')
  dst=$(curl -s "http://localhost:6334/collections/$c" | jq -r '.result.points_count')
  printf '%s src=%s dst=%s\n' "$c" "$src" "$dst"
done < /tmp/qdrant-collections.txt
kill %1 %2 2>/dev/null   # both port-forwards
```

Expected: `src` == `dst` for every collection.

**HALT:** any mismatch → re-upload that collection; repeated mismatch → halt window.

---

## 6. Scale up EKS

**6a. Controllers healthy first** (repeat the kube-system check from Step 0):

```bash
kubectl get pods -n kube-system --context $EKS_CONTEXT \
  | grep -E "aws-load-balancer-controller|cluster-autoscaler|external-dns"
```

Expected: all Running/Ready. **HALT:** ALB controller or external-dns not ready — DNS and TLS cannot come up without them.

**6b. Infisical on EKS** — confirm the operator synced the updated `/database` and `/redis` secrets into `rag-dev`:

```bash
kubectl -n rag-dev --context $EKS_CONTEXT get secret \
  database-credentials redis-credentials app-secrets supabase-credentials azure-openai-credentials -o name
kubectl -n rag-dev --context $EKS_CONTEXT get infisicalsecrets
```

Expected: all secrets present, CRDs synced. (`spaces-credentials` must be absent — pruned by `values-aws.yaml`; `do-kb-credentials` optional during transition.)

**6c. Sync Redis endpoint into Infisical** (ElastiCache auth token — transit encryption is ON, so clients use `rediss://` + AUTH token):

```bash
# Auth token was generated by terraform: random_password.redis_auth_token
terraform -chdir=infrastructure/terraform state show random_password.redis_auth_token | grep result
```

In Infisical UI, env `dev`, folder `/redis`:
- `REDIS_URL` → `rediss://default:<AUTH_TOKEN>@<ELASTICACHE_ENDPOINT>:6379/0`
- `REDIS_PASSWORD` → `<AUTH_TOKEN>`

Expected: secrets saved. (KEDA reads `keda.redis.address` = `<ELASTICACHE_ENDPOINT>:6379` from `values-aws.yaml` and the password from secret `redis-credentials` key `REDIS_PASSWORD`, TLS on.)

**HALT:** `terraform state show` fails (state elsewhere) → fetch the auth token from your secrets manager; do not guess.

**6d. Scale up + enable GitOps:**

```bash
kubectl scale deployments --all -n rag-dev --context $EKS_CONTEXT --replicas=1
kubectl scale statefulset nous-dev-knowledge-graph-analytics-neo4j -n rag-dev --context $EKS_CONTEXT --replicas=1
argocd app set nous-dev --sync-policy automated   # EKS ArgoCD child app
argocd app set aws-dev  --sync-policy automated   # EKS ArgoCD root app-of-apps
kubectl get pods -n rag-dev --context $EKS_CONTEXT -w   # Ctrl-C when settled
```

Expected: backend, celery-worker, celery-beat, neo4j (and qdrant) pods Running/Ready; backend `wait-for-postgres` init container passes (RDS reachable).

**HALT:** CrashLoopBackOff on backend/worker → `kubectl logs` the pod; most common cause is a wrong endpoint in Infisical — fix value, let the operator re-sync, restart the deployment.

---

## 7. DNS: Cloudflare → ALB

**7a. Get the ALB hostname:**

```bash
kubectl get ingress -n rag-dev --context $EKS_CONTEXT
```

Expected: `backend-ingress` (+ `websocket-ingress`) ADDRESS = `k8s-nousdev-...us-east-1.elb.amazonaws.com`.

**7b. Verify external-dns created the CNAMEs** (records per the `external-dns.alpha.kubernetes.io/hostname` annotations, e.g. `dev-api.gen-text.app` and the wired ws host):

```bash
dig +short CNAME dev-api.gen-text.app
```

Expected: the ALB hostname from 7a. external-dns may take a few minutes (TTL annotation 300; if it stays absent >10 min: `kubectl logs -n kube-system deploy/external-dns --context $EKS_CONTEXT | tail`).

**7c. Fallback — create manually in Cloudflare** (only if external-dns cannot): CNAME `dev-api` (and ws host) → ALB hostname, **TTL 60**, proxy OFF initially (DNS-only) to let ACM/ALB validation behave predictably.

**7d. Confirm ACM cert in use:**

```bash
curl -svI https://dev-api.gen-text.app/health -o /dev/null 2>&1 | grep -E "subject:|issuer:|HTTP/"
curl -s https://dev-api.gen-text.app/health
```

Expected: issuer `Amazon`, HTTP 200 (or the app's health response), no cert warnings.

**HALT:** cert mismatch/`SSL_ERROR` → ACM cert ARN on the ingress ≠ cert covering the host; fix annotation + `kubectl rollout restart` is not enough — the ingress must be re-applied (argocd app sync). Do not proceed to smoke tests on a broken TLS chain.

---

## 8. Smoke tests

Run all from outside the cluster (real user path, via ALB). Frontend: `https://dev-app.gen-text.app` (Vercel, unchanged).

| # | Test | Command / action | Expected |
|---|---|---|---|
| 1 | Auth/login | Log in on dev-app | Session created; no 5xx on `/api` calls |
| 2 | Upload → embed → search | Upload a small document in the UI, wait for ingestion, run a search | Doc status becomes ingested; search returns it |
| 3 | Chat streaming (SSE) | Send a chat message | Tokens stream incrementally (ALB `idle-timeout: 300` ≥ app `TIMEOUT: 300`) |
| 4 | WebSocket | Open a session that uses the ws path | Socket connects (ALB `idle-timeout: 4000` on websocket-ingress); `/ws/health` 200 |
| 5 | Knowledge-graph entities page | Open entities page | Nodes/edges render (Neo4j restored — data visible from Step 4) |
| 6 | Celery beat | `kubectl logs -n rag-dev --context $EKS_CONTEXT deploy/nous-dev-celery-beat --tail=50` | Heartbeat/tick lines; no task failures |
| 7 | Celery worker | `kubectl logs -n rag-dev --context $EKS_CONTEXT deploy/nous-dev-celery-worker --tail=50` | Tasks consumed from ElastiCache queue |
| 8 | KEDA scale-from-zero | Push enough queue depth (or wait for load) then `kubectl get hpa,so -n rag-dev --context $EKS_CONTEXT` | ScaledObject active; worker replicas > min; scales back down after |
| 9 | S3 round-trip | Download a pre-migration file through the UI/app | File served from S3 (`nous-storage-us-east-1`) |
| 10 | Synthetic traffic | `kubectl get cronjob -n rag-dev --context $EKS_CONTEXT` → wait for next `*/20` tick → check job logs | Synthetic job succeeds |

**HALT:** any test fails → capture logs, then decide: quick fix inside the window, or rollback (Step 9). Timebox: if >2 smoke tests fail or the failure class is data-related, roll back.

---

## 9. Halt criteria + ROLLBACK

**Halt immediately if any of:**

- a precondition in Step 0 was skipped or failed
- any "HALT" criterion in Steps 1-8 triggers and cannot be resolved within the window
- PG row counts differ (Step 2f) or Qdrant counts differ (Step 5e)
- smoke tests fail per the Step 8 timebox rule
- window timebox exceeded — better an aborted cutover than a half-migrated dev

### Rollback (independently executable — EKS becomes irrelevant)

Requires: a **fresh shell** — re-export `$DOKS_CONTEXT`/`$EKS_CONTEXT` and `argocd login` to **both** ArgoCDs (EKS for 9.1, DO for 9.3; rollback must not depend on shell state from the cutover window). Also: `/tmp/do-replicas.txt`, `/tmp/do-ingress.txt`, `/tmp/infisical-versions.txt` (recorded in Step 0), DO ArgoCD still installed with `nous-root`/`nous-dev` apps present (they were only paused, not deleted).

**9.1. Scale EKS down** (prevents split-brain writers when DNS flips back):

```bash
kubectl scale deployments --all -n rag-dev --context $EKS_CONTEXT --replicas=0
argocd app set nous-dev --sync-policy none   # EKS ArgoCD child app
argocd app set aws-dev  --sync-policy none   # EKS ArgoCD root app-of-apps
```

**9.2. DNS back to DO:** in Cloudflare, point the CNAMEs that were moved (e.g. `dev-api`, ws host) back to the DO LB hostname from `/tmp/do-ingress.txt`. TTL 60. (If external-dns on EKS fights the records, scale it to 0 first: `kubectl scale deploy -n kube-system external-dns --context $EKS_CONTEXT --replicas=0`.)

**9.3. Unfreeze DO:**

```bash
argocd app set nous-dev  --sync-policy automated   # DO ArgoCD
argocd app set nous-root --sync-policy automated
# restore recorded replica counts (selfHeal also enforces git values)
while read -r name reps; do
  [ -n "$reps" ] && kubectl scale deploy "$name" -n rag-dev --context $DOKS_CONTEXT --replicas="$reps"
done < /tmp/do-replicas.txt
kubectl get pods -n rag-dev --context $DOKS_CONTEXT -w
```

**9.4. Revert Infisical:** restore `/database` (DO PG values) and `/redis` (DO Valkey values) from **Infisical secret version history** — roll each secret back to the versions recorded in Step 0 (see `/tmp/infisical-versions.txt`); do not retype values from memory. (DO PG/Valkey are still running — nothing was deleted on DO.)

Expected after 9.2-9.4: `curl -s https://dev-api.gen-text.app/health` returns 200 within TTL (60s) + pod-ready time; dev-app login + chat work again.

**9.5. Decision point:** schedule retry window; bring `nous.dump`, `/tmp/nous-neo4j.dump`, `/tmp/qdrant-snaps/` forward (re-dump in the next window — do not reuse stale dumps). RCA the failure before re-entry.

---

## 10. Post-cutover

- **First 24h — monitor:**
  - Grafana (kube-prometheus-stack, ns `rag-system-monitoring` on EKS): pod restarts, backend p95, Celery queue depth, RDS/ElastiCache panels
  - CloudWatch (us-east-1): ALB 5xx/target health, RDS connections+CPU, ElastiCache evictions
  - Sentry (`SENTRY_ENVIRONMENT=dev`): new error classes vs pre-cutover baseline
  - `rclone check spaces:rag-system-storage s3:nous-storage-us-east-1` daily for the first week (Spaces must not receive new writes; if it does, some component still points at DO → fix immediately)
- **DO stays frozen 2 weeks** as the rollback source. Do not delete DO resources in this window. Do not resume `nous-dev`/`nous-root` auto-sync on the DO ArgoCD.
- After the soak, follow the decommission checklist: `docs/runbooks/aws-decommission.md` (written in Task 9 — file does not exist yet; covers DO LB deletion, snapshot archival to S3 Glacier, DOKS destruction, Spaces key revocation).
- Close-out notes: record actual cutover times, any deviations from this runbook, and the ECR tag ↔ source SHA mapping for the deployed images.
