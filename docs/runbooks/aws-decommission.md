# Decommission Runbook: DigitalOcean (post-AWS cutover)

**Date:** <EXECUTION_DATE> (template written 2026-09-15)
**Design:** `docs/plans/2026-09-14-do-to-aws-migration-design.md`
**Plan:** `docs/plans/2026-09-14-do-to-aws-migration.md` (Task 9)
**Cutover:** `docs/runbooks/2026-09-14-aws-cutover.md` (Step 10 says DO stays frozen 2 weeks; this runbook executes after that soak)
**Rollback:** ⚠️ **VOID after [Section 4](#4-teardown)** — see [Section 7](#7-rollback-impossible-notice) before starting.

## What gets removed (DO side)

| Resource | Identifier | Notes |
|---|---|---|
| DOKS cluster | `do-nyc3-rag-system-cluster` (nyc3) | Neo4j + Qdrant STSs inside; archives first |
| Managed PostgreSQL | db `multimodal_rag` | final snapshot kept through grace period |
| Managed Redis/Valkey | `<DO_VALKEY_ID>` | cold start on AWS (nothing to archive) |
| Spaces bucket | `rag-system-storage` (nyc3) | emptied + synced to S3 first |
| Container registry | `registry.digitalocean.com/ragsystemregistry` | ECR holds parity images (cutover Step 0) |
| Load balancers | `<DO_LB_ID>` (from `/tmp/do-ingress.txt` or `doctl` list) | DO ingress CCM may already have removed them |
| Spaces access keys | `<SPACES_KEY_ID>` | control-panel only, no doctl support |

**Working contexts** (set once, verify per step):

```bash
export DOKS_CONTEXT=<DOKS_CONTEXT>   # kubectl config get-contexts — DOKS entry
export EKS_CONTEXT=<EKS_CONTEXT>     # nous-dev-cluster
export AWS_REGION=us-east-1
```

> **Rule:** mirrors the cutover runbook — every command shows an "Expected";
> **HALT** if output differs. Archive (Section 2) must be 100% verified before
> any delete in Section 4 runs. If a doctl flag errors on your installed
> version, run `doctl <svc> --help` and use the documented equivalent — do not
> guess flags.

---

## 1. Preconditions checklist

All boxes checked before anything destructive. Any unchecked box = no go.

- [ ] **14-day soak elapsed.** Cutover date + 14 ≤ today. Verify cutover completion date from the close-out notes in `docs/runbooks/2026-09-14-aws-cutover.md` §10.
  ```bash
  # sanity: DO-side workloads still scaled to 0 (frozen), STSs up
  kubectl get deploy,sts -n rag-dev --context $DOKS_CONTEXT
  ```
  Expected: Deployments `0/N`; Neo4j + Qdrant StatefulSets `1/1`.
- [ ] **Zero rollback events during soak.** No one ran cutover Step 9; confirm via close-out notes + `kubectl get pods -n rag-dev --context $EKS_CONTEXT` uptime (no mass restarts aligned with a rollback).
- [ ] **AWS side healthy — smoke suite green.** Re-run every row of cutover Step 8 (login, upload→embed→search, chat SSE, WebSocket, entities page, Celery beat/worker, KEDA, S3 round-trip, synthetic CronJob) against `https://dev-api.gen-text.app`. Expected: all pass on the first clean run.
  ```bash
  curl -s https://dev-api.gen-text.app/health
  ```
  Expected: 200.
- [ ] **AWS session active:**
  ```bash
  aws sts get-caller-identity
  ```
  Expected: account ARN printed.
- [ ] **Disk space on operator machine** for final dumps: `df -h .` — need ≥ (Spaces usage + PG dump + Neo4j dump + Qdrant snapshots). Check source sizes:
  ```bash
  rclone lsd spaces: && rclone size spaces:rag-system-storage
  ```
- [ ] **ECR parity confirmed** (cutover Step 0 images still current):
  ```bash
  aws ecr describe-images --repository-name nous/backend --region us-east-1 | head -20
  ```
  Expected: ECR digests cover everything still in `ragsystemregistry`.
- [ ] **Stakeholder signoff:**

  | Role | Name | Date | Approve |
  |---|---|---|---|
  | Platform owner | <NAME> | <DATE> | ☐ |
  | App owner | <NAME> | <DATE> | ☐ |

**HALT:** any unchecked box → stop. DO stays frozen, AWS keeps serving, no harm done.

---

## 2. Final DO data archive (before ANY deletion)

Everything in this section is read-only on DO. Goal: a complete, verified
archive in S3 Glacier Deep Archive under
`s3://<ARCHIVE_BUCKET>/do-decommission-<YYYY-MM-DD>/`.

> `<ARCHIVE_BUCKET>`: use `nous-storage-us-east-1` or the terraform backups
> bucket from `infrastructure/terraform/backup.tf`
> (`${project}-${environment}-backups-<suffix>`; see `terraform output`).
> DEEP_ARCHIVE objects need an `aws s3 restore` initiation (hours) to read —
> this is intentional (last-resort archive, not backup).

**2a. Final Postgres dump → S3.** DO managed PG is still running (frozen). Same
pattern as cutover Step 2: transient pod on EKS reaching DO PG's public endpoint.

```bash
kubectl run pg-final -n rag-dev --context $EKS_CONTEXT \
  --image=postgres:16-alpine --restart=Never --command -- sleep 7200
kubectl wait --for=condition=Ready pod/pg-final -n rag-dev --context $EKS_CONTEXT --timeout=120s
# creds from Infisical project nous-platform env dev folder /database — never shell-history:
export DO_PGHOST=<DO_PG_HOST> DO_PGPORT=<DO_PG_PORT> DO_PGUSER=<DO_PG_USER>
kubectl exec -n rag-dev pg-final --context $EKS_CONTEXT -- env \
  PGPASSWORD='<DO_PG_PASSWORD>' \
  pg_dump -Fc -h "$DO_PGHOST" -p "$DO_PGPORT" -U "$DO_PGUSER" -d multimodal_rag -f /tmp/nous-final.dump
kubectl exec -n rag-dev pg-final --context $EKS_CONTEXT -- ls -lh /tmp/nous-final.dump
kubectl cp rag-dev/pg-final:/tmp/nous-final.dump /tmp/nous-final.dump --context $EKS_CONTEXT
kubectl delete pod pg-final -n rag-dev --context $EKS_CONTEXT
aws s3 cp /tmp/nous-final.dump \
  s3://<ARCHIVE_BUCKET>/do-decommission-<YYYY-MM-DD>/nous-final.dump --storage-class DEEP_ARCHIVE
aws s3 ls s3://<ARCHIVE_BUCKET>/do-decommission-<YYYY-MM-DD>/nous-final.dump
```

Expected: non-trivial dump size; `aws s3 ls` size == local size.

**HALT:** dump non-zero exit or 0-byte file → fix, retry; nothing has been deleted, DO intact.

**2b. Final Spaces → S3 sync + manifest.**

```bash
rclone sync spaces:rag-system-storage s3:nous-storage-us-east-1 --progress
rclone check spaces:rag-system-storage s3:nous-storage-us-east-1
rclone size spaces:rag-system-storage | tee /tmp/spaces-final-size.txt
rclone lsjson -R --files-only spaces:rag-system-storage | gzip > /tmp/spaces-manifest.json.gz
aws s3 cp /tmp/spaces-manifest.json.gz \
  s3://<ARCHIVE_BUCKET>/do-decommission-<YYYY-MM-DD>/spaces-manifest.json.gz --storage-class DEEP_ARCHIVE
```

Expected: check reports `0 differences` / `0 errors`; manifest non-empty; count of objects in manifest equals `rclone size` count.

**2c. Neo4j dump → S3.** Same as cutover Step 4 (DB must be stopped inside the DO pod for 5.x community):

```bash
export NEO4J_POD=nous-dev-knowledge-graph-analytics-neo4j-0
export NEO4J_PW=$(kubectl get secret -n rag-dev --context $DOKS_CONTEXT \
  nous-dev-knowledge-graph-analytics-neo4j-credentials -o jsonpath='{.data.NEO4J_AUTH}' | base64 -d | sed 's|^neo4j/||')
kubectl exec -n rag-dev --context $DOKS_CONTEXT $NEO4J_POD -- \
  cypher-shell -a bolt://localhost:7687 -u neo4j -p "$NEO4J_PW" "STOP DATABASE neo4j;"
kubectl exec -n rag-dev --context $DOKS_CONTEXT $NEO4J_POD -- \
  sh -c 'mkdir -p /data/dumps && neo4j-admin database dump neo4j --to-path=/data/dumps'
kubectl cp rag-dev/$NEO4J_POD:/data/dumps/neo4j.dump /tmp/nous-neo4j-final.dump -c neo4j --context $DOKS_CONTEXT
kubectl exec -n rag-dev --context $DOKS_CONTEXT $NEO4J_POD -- \
  cypher-shell -a bolt://localhost:7687 -u neo4j -p "$NEO4J_PW" "START DATABASE neo4j;"
aws s3 cp /tmp/nous-neo4j-final.dump \
  s3://<ARCHIVE_BUCKET>/do-decommission-<YYYY-MM-DD>/nous-neo4j.dump --storage-class DEEP_ARCHIVE
```

Expected: dump file non-trivial size on both pod and laptop; DB restarted (harmless — cluster dies in Section 4 anyway, but keep DO consistent until then).

**HALT:** dump errors → restart DB, re-attempt once, then halt and investigate.

**2d. Qdrant snapshots → S3.** Same as cutover Step 5 (discover names first — STS was manually applied):

```bash
kubectl get statefulset,svc,pvc -n rag-dev --context $DOKS_CONTEXT | grep -i qdrant
export QDRANT_POD=<qdrant-pod-0>
kubectl port-forward -n rag-dev --context $DOKS_CONTEXT $QDRANT_POD 6333:6333 &
mkdir -p /tmp/qdrant-final
curl -s http://localhost:6333/collections | jq -r '.result.collections[].name' | tee /tmp/qdrant-collections.txt
while read -r c; do
  snap=$(curl -s -X POST "http://localhost:6333/collections/$c/snapshots" | jq -r '.result.name')
  curl -s "http://localhost:6333/collections/$c/snapshots/$snap" -o "/tmp/qdrant-final/${c}.snapshot"
done < /tmp/qdrant-collections.txt
kill %1
ls -lh /tmp/qdrant-final
tar -czf /tmp/qdrant-final-snapshots.tgz -C /tmp/qdrant-final .
aws s3 cp /tmp/qdrant-final-snapshots.tgz \
  s3://<ARCHIVE_BUCKET>/do-decommission-<YYYY-MM-DD>/qdrant-snapshots.tgz --storage-class DEEP_ARCHIVE
```

Expected: one non-0-byte `.snapshot` per collection; tar size ≈ sum of snapshots.

**HALT:** empty collection list → confirm with team this is correct before continuing.

**2e. Gate: verify counts + sizes before any delete.** Fill this table and keep it with the signoff:

| Artifact | Where | Size | Verified by |
|---|---|---|---|
| PG dump | `s3://<ARCHIVE_BUCKET>/do-decommission-<YYYY-MM-DD>/nous-final.dump` | <SIZE> | `aws s3 ls` vs local `ls -l` |
| Spaces objects | `s3:nous-storage-us-east-1` | <COUNT> objects / <BYTES> | `rclone check` + manifest |
| Spaces manifest | `.../spaces-manifest.json.gz` | <SIZE> | gunzip + line count |
| Neo4j dump | `.../nous-neo4j.dump` | <SIZE> | `aws s3 ls` vs local |
| Qdrant snapshots | `.../qdrant-snapshots.tgz` | <SIZE> / <N> collections | tar listing |

```bash
aws s3 ls s3://<ARCHIVE_BUCKET>/do-decommission-<YYYY-MM-DD>/ --recursive
```

Expected: 4 objects, all sizes matching the table. **Do not start Section 4 until every row is green.**

---

## 3. DNS verification (DO must already be out of the path)

Run before teardown — proves nothing resolves to DO anymore.

```bash
dig +short dev-api.gen-text.app
dig +short dev-app.gen-text.app
dig +short dev-api.gen-text.app | grep -i digitalocean; echo "do-refs=$?"
```

Expected:

- `dev-api` → the ALB hostname from cutover Step 7a (`k8s-nousdev-...us-east-1.elb.amazonaws.com`).
- `dev-app` → Vercel (unchanged; must not be a DO address).
- grep exit `1` → zero `digitalocean` strings in any resolved record.

**HALT:** any record still resolves to `*.digitalocean.com` or the DO LB from `/tmp/do-ingress.txt` → fix DNS in Cloudflare first. Deleting the LB while DNS points at it is an outage.

---

## 4. Teardown (destructive — ordered)

> ⚠️ **From here on, the cutover rollback path is being destroyed.** Section 2
> gate must be green and Section 1 signed off. Each step verifies before the
> next begins.

**4.1. Scale + delete ArgoCD apps on DO.** (DO ArgoCD — `argocd context` to confirm you are on the DO server, not EKS.)

```bash
argocd app set nous-root --sync-policy none
argocd app set nous-dev  --sync-policy none
kubectl scale deployments --all -n rag-dev --context $DOKS_CONTEXT --replicas=0
argocd app delete nous-dev  --cascade   # add -y if your argocd version prompts
argocd app delete nous-root --cascade
kubectl get deploy,sts,pods -n rag-dev --context $DOKS_CONTEXT
```

Expected: apps gone from `argocd app list`; no resources left in `rag-dev` (Neo4j/Qdrant pods terminated by cascade delete).

**HALT:** app delete fails with finalizers → `kubectl patch`/`kubectl delete` the leftovers manually and re-verify empty namespace before continuing.

**4.2. Delete DO load balancers.** Ingress deletion (4.1 cascade) should have triggered the DO CCM to remove them; clean up any stragglers:

```bash
doctl compute load-balancer list
doctl compute load-balancer delete <DO_LB_ID> --force
doctl compute load-balancer list
```

Expected: list shows the LB(s), then empty after delete.

**HALT:** LB delete refused (targets attached) → confirm 4.1 left no services of type LoadBalancer (`kubectl get svc -A --context $DOKS_CONTEXT | grep LoadBalancer`), then retry.

**4.3. Delete DO container registry.** ECR already holds parity images (Section 1 gate).

```bash
# repo list for the Section 6 audit record (verify subcommand with `doctl registry --help`):
doctl registry repository list-v2
# per-repo delete exists on newer doctl as `doctl registry repository delete-v2 <repo> --force` —
# optional; deleting the whole registry removes all repos/images anyway
doctl registry delete ragsystemregistry --force
doctl registry get ragsystemregistry
```

Expected: repos listed first (audit line for Section 6); registry deleted; final `get` errors `registry not found`.

**HALT:** `registry delete` flag mismatch on installed doctl → run `doctl registry --help`, use documented delete; do not force-invent flags.

**4.4. Empty + delete Spaces bucket.**

```bash
aws s3 rm s3://rag-system-storage --recursive \
  --endpoint-url https://nyc3.digitaloceanspaces.com
aws s3 ls s3://rag-system-storage --endpoint-url https://nyc3.digitaloceanspaces.com
aws s3 rb s3://rag-system-storage --endpoint-url https://nyc3.digitaloceanspaces.com
```

Expected: first `ls` after `rm` returns nothing; `rb` succeeds; final `ls` errors `NoSuchBucket`.

**HALT:** `rm --recursive` errors partway → re-run; if objects keep reappearing, something still writes to Spaces — STOP and find the component (cutover Step 10 daily check should have caught it).

**4.5. Delete DO managed Redis/Valkey.** (Cold-start on AWS — no data to archive.)

```bash
doctl databases list
doctl databases delete <DO_VALKEY_ID> --force
doctl databases list
```

Expected: list before shows the valkey cluster; after, it is absent (PG may still be present until 4.6).

**4.6. Delete DO managed PostgreSQL — final snapshot first.**

```bash
doctl databases snapshots create <DO_PG_ID> nous-final-<YYYY-MM-DD>
# wait for AVAILABLE (poll; verify arg form with `doctl databases snapshots --help` if needed):
doctl databases snapshots list
```

Expected: snapshot listed with status `available` and size > 0. **This snapshot is retained through the Section 6 grace period — it is the last copy of DO-side Postgres.** Only then:

```bash
doctl databases delete <DO_PG_ID> --force
doctl databases list
```

Expected: database absent from list; snapshot still listed.

**HALT:** snapshot not `available` (or 0 bytes) → do NOT delete the database. Investigate snapshot creation before proceeding.

**4.7. Delete the DOKS cluster.** (Kills Neo4j/Qdrant PVCs + volumes — archives verified in 2e are the only copies from here.)

```bash
doctl kubernetes cluster delete do-nyc3-rag-system-cluster --dangerous --force
doctl kubernetes cluster list
```

Expected: `--dangerous` required by current doctl versions to delete a cluster with (former) running resources — omit it only if your version rejects the flag as unknown; cluster gone from list; associated node pools + block-storage volumes removed (verify under console → Volumes, or `doctl compute volume list`).

**HALT:** deletion blocked on volume attach errors → re-run after a few minutes; DO eventually releases. Do not leave orphan volumes — check `doctl compute volume list` empty at the end.

**4.8. Revoke Spaces access keys.** No doctl support — control panel: **API → Spaces Keys → revoke `<SPACES_KEY_ID>`**.

```bash
aws s3 ls s3://rag-system-storage --endpoint-url https://nyc3.digitaloceanspaces.com
```

Expected: `AccessDenied` — keys dead. Also remove the `spaces:` rclone remote config locally (`rclone config delete spaces`) so nothing re-authenticates with dead creds.

**4.9. Verify DO billing shows zero running resources.**

```bash
doctl balance get
doctl kubernetes cluster list
doctl databases list
doctl compute load-balancer list
doctl compute volume list
doctl compute droplet list
```

Expected: balance shows no accruing resources; every list empty. Console → Billing: no projected recurring charges beyond the retained PG snapshot storage (4.6, until grace period ends) and any Glacier-sized Spaces snapshot storage DO still bills.

---

## 5. AWS side hardening (replaces DO backup cron)

**5.1. EBS snapshot CronJob for Neo4j/Qdrant PVCs** — replaces the DO weekly
Qdrant backup CronJob (`bcef8931f`,
`infrastructure/helm/knowledge-graph-analytics/templates/qdrant-backup-cronjob.yaml`,
removed with DO). Mirror its shape: weekly `0 3 * * 0` UTC, `concurrencyPolicy:
Forbid`, history limits 3/1. Mechanism differs: CSI `VolumeSnapshot` of the EBS
PVCs instead of in-DB API snapshots.

Prerequisites (verify before installing):

- [ ] EBS CSI snapshot controller + CRDs installed on EKS (`kubectl get crd volumesnapshots.snapshot.storage.k8s.io` → exists; controller pod Running in `kube-system`).
- [ ] A `VolumeSnapshotClass` with `driver: ebs.csi.aws.com` and `deletionPolicy: Delete` exists (add to terraform if missing — manifest work, follow-up task).
- [ ] Qdrant PVC name discovered (STS is not git-managed): `kubectl get pvc -n rag-dev --context $EKS_CONTEXT | grep -i qdrant`.

Starter manifest (promote to `infrastructure/kubernetes/` or the helm chart as a
follow-up — do not leave it only in this doc):

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ebs-pvc-snapshot
  namespace: rag-dev
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ebs-pvc-snapshot
  namespace: rag-dev
rules:
  - apiGroups: ["snapshot.storage.k8s.io"]
    resources: ["volumesnapshots"]
    verbs: ["create", "get", "list", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ebs-pvc-snapshot
  namespace: rag-dev
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: ebs-pvc-snapshot
subjects:
  - kind: ServiceAccount
    name: ebs-pvc-snapshot
    namespace: rag-dev
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: ebs-pvc-snapshot
  namespace: rag-dev
spec:
  schedule: "0 3 * * 0"          # weekly Sunday 03:00 UTC — same cadence as the old DO qdrant-backup
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          serviceAccountName: ebs-pvc-snapshot
          containers:
            - name: snapshot
              image: bitnami/kubectl:1.31
              command: ["/bin/sh", "-c"]
              args:
                - |
                  set -eu
                  STAMP=$(date +%Y%m%d-%H%M%S)
                  for pvc in \
                    neo4j-data-nous-dev-knowledge-graph-analytics-neo4j-0 \
                    <QDRANT_PVC>; do
                    kubectl apply -n rag-dev -f - <<EOF
                  apiVersion: snapshot.storage.k8s.io/v1
                  kind: VolumeSnapshot
                  metadata:
                    name: ${pvc}-${STAMP}
                  spec:
                    volumeSnapshotClassName: <VOLUME_SNAPSHOT_CLASS>
                    source:
                      persistentVolumeClaimName: ${pvc}
                  EOF
                  done
                  kubectl get volumesnapshots -n rag-dev
                  # keep newest 8 total (≈4 per PVC), oldest first out
                  kubectl get volumesnapshots -n rag-dev \
                    --sort-by=.metadata.creationTimestamp -o name | head -n -8 \
                    | xargs -r kubectl delete -n rag-dev
                  kubectl get volumesnapshots -n rag-dev
              resources:
                limits:   { cpu: 100m, memory: 128Mi }
                requests: { cpu: 50m,  memory: 64Mi }
```

Verification after first fire:

```bash
kubectl get volumesnapshots -n rag-dev --context $EKS_CONTEXT \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.readyToUse}{"\n"}{end}'
```

Expected: `true` on the fresh snapshots; retention count ≤ 8.

**HALT:** snapshot stuck `readyToUse=false` > 15 min → check snapshot-controller logs; PVCs with heavy Neo4j writes may need `--freeze`-capable CSI or an app-level quiesce — do not let a silently failing backup replace the DO cron.

**5.2. Confirm RDS automated backups** (terraform set 7d retention; dev also has `skip_final_snapshot = true` — `infrastructure/terraform/main.tf:461`; flip it if you want a final snapshot on any future destroy):

```bash
aws rds describe-db-instances --region us-east-1 --db-instance-identifier <RDS_INSTANCE_ID> \
  --query 'DBInstances[0].[BackupRetentionPeriod,BackupWindow,PendingModifiedValues]'
```

Expected: retention `7`; no pending destructive changes.

**5.3. Confirm ElastiCache snapshots** (terraform `snapshot_retention_limit = 7`, `snapshot_window "03:00-05:00"` — `infrastructure/terraform/main.tf:546-547`):

```bash
aws elasticache describe-cache-clusters --region us-east-1 --show-cache-node-info \
  --query 'CacheClusters[].[CacheClusterId,SnapshotRetentionLimit,SnapshotWindow]'
```

Expected: retention `7`, window `03:00-05:00`; snapshots appear after the first fire:
`aws elasticache describe-snapshots --region us-east-1 --max-records 5`.

---

## 6. State archival + audit record

**6a. DO terraform state.** `infrastructure/terraform/` is the AWS stack only —
no DO terraform exists in this repo (verified). If any DO resources were ever
managed by terraform outside this repo, archive before 4.x makes the state
stale:

```bash
# in whatever DO state dir exists, if any:
terraform state pull > /tmp/do-tfstate.json
aws s3 cp /tmp/do-tfstate.json \
  s3://<ARCHIVE_BUCKET>/do-decommission-<YYYY-MM-DD>/do-terraform-state.json --storage-class DEEP_ARCHIVE
```

Expected: either no DO state dir exists (record that here), or the state blob is in the archive.

**6b. DO resource IDs for audit** — record actuals (values from before 4.x ran):

| Resource | ID | Notes |
|---|---|---|
| DOKS cluster | <DOKS_CLUSTER_ID> | `doctl kubernetes cluster get do-nyc3-rag-system-cluster --format ID,Region,Version,Created` (run during Section 1) |
| Load balancer | <DO_LB_ID> | also in `/tmp/do-ingress.txt` from cutover Step 0 |
| PostgreSQL | <DO_PG_ID> | final snapshot `nous-final-<YYYY-MM-DD>` |
| Redis/Valkey | <DO_VALKEY_ID> | |
| Registry | ragsystemregistry | repo list captured in 4.3 audit output |
| Spaces bucket | rag-system-storage | manifest in 2b |
| Spaces key ID | <SPACES_KEY_ID> | never record the secret, ID only |

**6c. Archive re-check reminders.** DEEP_ARCHIVE restore is slow + billable —
schedule two calendar reminders:

- **+30 days:** spot-check one archive object (`aws s3 restore` initiate, then
  `aws s3 ls` shows `ongoing-request=true`); confirm PG final snapshot (4.6)
  and DO snapshot storage costs are the only remaining DO line items.
- **+90 days:** if zero retrieval needs surfaced, decide whether to delete the
  DO PG final snapshot + downgrade/keep the Deep Archive tier. **Grace period
  ends here — the DO PG snapshot may be deleted only after this review.**

---

## 7. Rollback-impossible notice

> ### ⚠️ ROLLBACK PATH IS VOID
>
> The cutover runbook (`docs/runbooks/2026-09-14-aws-cutover.md`, Step 9)
> restores service by: DNS back to the DO LB (9.2), unfreezing DO ArgoCD apps
> (9.3), reverting Infisical to DO PG/Valkey (9.4). **Every one of those
> prerequisites is destroyed in Section 4:**
>
> - 4.1 deletes the ArgoCD apps Step 9.3 restarts
> - 4.2 deletes the load balancer Step 9.2 points DNS at
> - 4.5/4.6 delete the Valkey/Postgres Step 9.4 reverts to
> - 4.7 deletes the cluster everything else lived in
>
> From the start of Section 4 there is **no path back to DigitalOcean.** The
> only remaining artifacts are the Glacier archives (Section 2) and the DO PG
> final snapshot (4.6) — recovery from those is a rebuild, not a rollback.
> If AWS breaks after decommission, the fix happens on AWS.
