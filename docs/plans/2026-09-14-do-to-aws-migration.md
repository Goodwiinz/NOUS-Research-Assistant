# DigitalOcean → AWS Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Migrate the entire NOUS stack (DOKS, DO managed PG/Redis, Spaces, DO registry) to AWS EKS with a single freeze-and-migrate cutover.

**Architecture:** Reuse existing AWS terraform in `infrastructure/terraform/` (already defines VPC/EKS/RDS/ElastiCache/S3) with lean dev sizing. Port existing Helm charts via new `values-aws.yaml` + ArgoCD overlay. Data migrated via pg_dump, rclone, neo4j dump, Qdrant snapshots. Rollback = DO stays frozen 2 weeks.

**Tech Stack:** Terraform (terraform-aws-modules), EKS 1.31, Helm 3, ArgoCD, external-secrets, aws-load-balancer-controller, rclone, pg_dump.

**Design doc:** `docs/plans/2026-09-14-do-to-aws-migration-design.md`

**Key pivot from design:** `infrastructure/terraform/` already contains the full AWS stack (was built for knowledge-graph-analytics). Tasks ADAPT it rather than create new terraform.

**Prereqs:** `aws login` session active; kubectl/helm/terraform/rclone installed; DO creds for Spaces + managed PG.

---

### Task 1: Terraform lean sizing + ECR repos

**Files:**
- Modify: `infrastructure/terraform/terraform.tfvars.example` (copy to `terraform.tfvars`)
- Modify: `infrastructure/terraform/variables.tf` (defaults if needed)
- Create: `infrastructure/terraform/ecr.tf`

**Step 1: Create tfvars with lean dev config**

```bash
cp infrastructure/terraform/terraform.tfvars.example infrastructure/terraform/terraform.tfvars
```

Set in `terraform.tfvars`:
- `aws_region = "us-east-1"`
- `cluster_name = "nous-dev-cluster"`
- `node_instance_types = ["t3.large"]`, `min_nodes = 1`, `max_nodes = 3`, `desired_nodes = 1`
- `database_instance_class = "db.t4g.small"`, `database_max_storage_size = 100`
- ElastiCache node type `cache.t4g.small` (check variable name in `variables.tf`/`main.tf:531`)
- Spot: ensure node group uses `capacity_type = "SPOT"` (add if missing in `main.tf:170` EKS module block)
- `terraform_state_bucket`, `terraform_lock_table` — existing values or create in Task 2

**Step 2: Create ECR repos** — `infrastructure/terraform/ecr.tf`:

```hcl
resource "aws_ecr_repository" "backend" {
  name                 = "nous/backend"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_repository" "frontend" {
  name                 = "nous/frontend"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name
  policy     = jsonencode({ rules = [{ rulePriority = 1, description = "keep last 20", selection = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }, action = [{ type = "expire" }] }] })
}

resource "aws_ecr_lifecycle_policy" "frontend" {
  repository = aws_ecr_repository.frontend.name
  policy     = jsonencode({ rules = [{ rulePriority = 1, description = "keep last 20", selection = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }, action = [{ type = "expire" }] }] })
}
```

**Step 3: Verify**

Run: `cd infrastructure/terraform && terraform fmt && terraform init && terraform validate`
Expected: `Success! The configuration is valid.`

**Step 4: Commit**

```bash
git add infrastructure/terraform/
git commit -m "feat(aws): lean dev sizing and ECR repos for NOUS migration"
```

---

### Task 2: Provision state backend + apply terraform

**Step 1: Ensure remote state exists** (skip if bucket/table already live):

```bash
aws s3 mb s3://nous-tfstate-us-east-1 --region us-east-1
aws s3api put-bucket-versioning --bucket nous-tfstate-us-east-1 --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name nous-tfstate-lock --attribute-definitions AttributeName=LockID,AttributeType=S --key-schema AttributeName=LockID,KeyType=HASH --billing-mode PAY_PER_REQUEST --region us-east-1
```

**Step 2: Apply** (long — 15-25 min for EKS):

```bash
cd infrastructure/terraform && terraform plan -out=tfplan && terraform apply tfplan
```

Expected: EKS cluster, 1 spot t3.large node Ready, RDS + ElastiCache available, S3 bucket created.

**Step 3: Record outputs:**

```bash
terraform output > /tmp/nous-aws-outputs.txt
```

**Step 4: Update kubeconfig and verify:**

```bash
aws eks update-kubeconfig --name nous-dev-cluster --region us-east-1
kubectl get nodes
```

Expected: 1 node `Ready`.

---

### Task 3: Cluster addons (ALB controller, external-dns, external-secrets, autoscaler)

**Files:**
- Create: `infrastructure/kubernetes/overlays/aws-dev/kustomization.yaml`
- Create: `infrastructure/helm/aws-addons-values.yaml`

**Step 1: Install via helmfile-style commands (document in `infrastructure/kubernetes/README.md` AWS section):**

```bash
helm repo add eks https://aws.github.io/eks-charts
helm install aws-load-balancer-controller eks/aws-load-balancer-controller -n kube-system --set clusterName=nous-dev-cluster --set serviceAccount.create=true --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"=<ALB_IRSA_ROLE_ARN>
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace
```

Cluster autoscaler: set `enable_cluster_autoscaler = true` in tfvars if module supports helm release (check `main.tf`), else chart install with `--set autoDiscovery.clusterName=nous-dev-cluster`.

**Step 2: external-dns** — reuse existing deployment; change provider args to `--provider=cloudflare --cloudflare-proxied` with Cloudflare token via external-secrets.

**Step 3: Verify**

```bash
kubectl get pods -n kube-system | grep -E "lb|autoscaler|external"
```

Expected: all Running.

**Step 4: Commit manifests.**

---

### Task 4: values-aws.yaml + ArgoCD overlay

**Files:**
- Create: `infrastructure/helm/rag-system/values-aws.yaml`
- Create: `infrastructure/kubernetes/overlays/aws-dev/` (kustomization + patches referencing helm values)
- Modify: `infrastructure/kubernetes/manifests/ingress.yaml:25` — ALB annotations in aws overlay ingress patch

**Step 1: values-aws.yaml contents:**

```yaml
image:
  backend: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/nous/backend
  frontend: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/nous/frontend
storage:
  s3:
    bucket: <application_storage bucket from outputs>
    region: us-east-1   # no endpoint override — AWS SDK default
postgres:
  host: <RDS endpoint from outputs>
redis:
  host: <ElastiCache endpoint from outputs>
storageClass: gp3
```

**Step 2: Ingress patch** (ALB):

```yaml
annotations:
  kubernetes.io/ingress.class: alb
  alb.ingress.kubernetes.io/scheme: internet-facing
  alb.ingress.kubernetes.io/target-type: ip
  cert-manager.io/cluster-issuer: letsencrypt-prod   # or alb listener https with ACM
```

Keep `external-dns.alpha.kubernetes.io/provider: cloudflare` annotation.

**Step 3: Validate:** `kubectl kustomize infrastructure/kubernetes/overlays/aws-dev | kubeconform -strict -summary` (or `kubectl apply --dry-run=client`).

**Step 4: Commit.**

---

### Task 5: ArgoCD on EKS + app-of-apps

**Files:**
- Modify: `infrastructure/helm/argocd-values.yaml` (drop DOKS clusterlint comments)
- Create: `infrastructure/argocd/aws-dev-app.yaml`

**Step 1:** Install ArgoCD:

```bash
helm install argocd argo/argo-cd -n argocd --create-namespace -f infrastructure/helm/argocd-values.yaml
```

**Step 2:** Apply app-of-apps pointing at `github.com/goodwiins/rag`, path `infrastructure/kubernetes/overlays/aws-dev`, targetRevision `develop`, syncPolicy automated.

**Step 3:** Pause DO ArgoCD: `argocd app set <apps> --sync-policy none` (record commands in runbook for rollback).

**Step 4: Commit.**

---

### Task 6: Images → ECR (build + push + CI)

**Files:**
- Modify: `docker-bake.hcl` (registry var → ECR)
- Modify: `deployment/github-actions/workflows/deploy.yml` — replace DO registry login with `aws-actions/amazon-ecr-login@v2` + OIDC (`aws-actions/configure-aws-credentials@v4`, role from terraform output, `permissions: id-token: write`)
- Remove: `do-api-token`/`do-kb-project-id` secret refs in `deploy.yml:237-238` (keep during transition, mark TODO)

**Step 1: Local push test:**

```bash
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
docker buildx bake --push   # or per-service build per existing bake targets
```

**Step 2: Verify:** `aws ecr describe-images --repository-name nous/backend` shows tags.

**Step 3: Commit workflow changes.** CI runs on next push — confirm image build job green.

---

### Task 7: Pre-stage data migration (before freeze)

**Step 1: Spaces → S3 first sync** (repeatable):

```bash
rclone sync spaces:rag-system-storage s3:nous-storage-us-east-1 --progress
```

(rclone remotes: `spaces` = DO Spaces keys, `s3` = AWS profile. Verify object count/sample MD5 after.)

**Step 2: Verify**: `rclone check spaces:rag-system-storage s3:nous-storage-us-east-1` — expected 0 differences (pre-freeze delta acceptable).

**Step 3: Secrets → Secrets Manager:**

```bash
aws secretsmanager create-secret --name nous/dev/backend --secret-string file://<(compose from current k8s secrets)
```

Create `SecretProviderClass` / `ExternalSecret` manifests in `infrastructure/kubernetes/secrets/` mapping each key. **Exclude in plan:** never commit secret values; pull live values from current cluster at execution time.

**Step 4: Commit secret manifest scaffolding (values redacted).**

---

### Task 8: Cutover runbook (execute in window)

Create `docs/runbooks/2026-09-14-aws-cutover.md` with exact ordered commands:

1. Freeze: DO ArgoCD sync none; `kubectl scale deploy --all -n <ns> --replicas=0` on DOKS
2. PG: `pg_dump -Fc <DO_DSN> > /tmp/nous.dump` → `pg_restore -d <RDS_DSN> --no-owner /tmp/nous.dump` (create role/db first: `multimodal_rag`)
3. Final `rclone sync` Spaces→S3
4. Neo4j: `kubectl exec` on DO neo4j pod → `neo4j-admin database dump neo4j --to-path=/dumps`; `kubectl cp` out; restore into EKS PVC before scaling StatefulSet
5. Qdrant: snapshot API per collection (`POST /collections/<name>/snapshots`), download, restore on EKS
6. Scale EKS deployments up; ArgoCD auto-sync on
7. DNS: update Cloudflare CNAMEs `dev-app`/`dev-api` → ALB hostname (external-dns may auto-create; verify + set TTL 60)
8. Smoke test: login, doc upload→embed→search, chat streaming, graph entities page, Celery beat ticks
9. Halt criteria: any failure → rollback section (unfreeze DO, DNS back)

---

### Task 9: Post-cutover + decommission checklist

**Files:**
- Create: `docs/runbooks/aws-decommission.md`

Contents: after 2-week soak — archive DO PG dump + Spaces final export to S3 Glacier (`aws s3 cp --storage-class GLACIER`), delete DO LBs, `doctl kubernetes cluster delete do-nyc3-rag-system-cluster`, archive DO terraform state, revoke Spaces keys. EBS snapshot CronJob for Neo4j/Qdrant PVCs (replaces DO weekly Qdrant backup).

---

## Execution Notes

- Tasks 1-7 are safe pre-stage work (no DO impact). Task 8 is the only disruptive step — needs explicit user go.
- Skills: use `signing-in-to-aws` if creds expire; use `aws-sdk-python-usage` if backend S3 code changes needed (only env/config changes expected).
- Terraform state is remote S3 — never run `terraform destroy` against DO resources from this stack.
