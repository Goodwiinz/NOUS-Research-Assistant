# AWS Cluster Addons — nous-dev-cluster (EKS, us-east-1)

Reproducible record of the cluster addon installs. The EKS cluster itself
(`nous-dev-cluster`) and its node groups are Terraform-managed
(`infrastructure/terraform/`). Helm releases below are installed out-of-band
via the CLI; re-run the exact commands to rebuild or upgrade.

## Credential protocol

AWS sessions expire every ~15 min. Every shell that touches AWS/kubectl/helm
must start with:

```sh
eval "$(aws configure export-credentials --format env)"
aws eks update-kubeconfig --name nous-dev-cluster --region us-east-1
```

Keep individual AWS operations under ~10 minutes; on `ExpiredToken`,
re-export and retry the failed step.

## Terraform-managed pieces

| Resource | File |
|---|---|
| ACM cert `*.goodwiinz.tech` + validation | `infrastructure/terraform/acm.tf` |
| ALB controller IRSA role + official v2.13.4 IAM policy | `infrastructure/terraform/alb-irsa.tf`, `alb-controller-iam-policy.json` |
| Cluster Autoscaler IRSA role + policy | `infrastructure/terraform/cluster-autoscaler-irsa.tf` |

The OIDC provider comes from the EKS module (`module.eks.oidc_provider*`,
`enable_irsa` default).

Apply (note: only target the certificate first — targeting
`aws_acm_certificate_validation` before the Cloudflare CNAME exists makes
terraform block until it times out):

```sh
terraform apply -target=aws_acm_certificate.cluster
# After the CNAME (see terraform output acm_validation_record) is added in
# Cloudflare and the cert is ISSUED:
terraform apply -target=aws_acm_certificate_validation.cluster
```

IRSA roles apply like normal targeted applies, e.g.:

```sh
terraform apply \
  -target=aws_iam_role.alb_controller \
  -target=aws_iam_policy.alb_controller \
  -target=aws_iam_role_policy_attachment.alb_controller
```

## Helm repos

```sh
helm repo add eks https://aws.github.io/eks-charts
helm repo add autoscaler https://kubernetes.github.io/autoscaler
helm repo add external-secrets https://charts.external-secrets.io
helm repo add external-dns https://kubernetes-sigs.github.io/external-dns
helm repo update
```

## aws-load-balancer-controller (chart 1.13.4 / app v2.13.4)

Requires the IRSA role `nous-development-alb-controller`
(`terraform output alb_controller_role_arn`). `region` and `vpcId` are set
explicitly so the controller never queries IMDS (hop-limit-1 on nodes blocks
pod access to IMDS):

```sh
eval "$(aws configure export-credentials --format env)"
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --version 1.13.4 \
  --set clusterName=nous-dev-cluster \
  --set region=us-east-1 \
  --set vpcId="$(terraform -chdir=infrastructure/terraform output -raw vpc_id)" \
  --set serviceAccount.create=true \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="$(terraform -chdir=infrastructure/terraform output -raw alb_controller_role_arn)"
```

NOTE: there is an install-order gotcha — once this controller's validating
webhook is registered but its pods are not Ready yet, other Helm installs
that create Services/Ingresses fail with
`failed calling webhook "mservice.elbv2.k8s.aws"`. Wait for
`kubectl rollout status deploy/aws-load-balancer-controller -n kube-system`
before installing anything else.

## cluster-autoscaler (chart 9.59.0, image v1.31.5 to match k8s 1.31)

Requires the IRSA role `nous-development-cluster-autoscaler`
(`terraform output cluster_autoscaler_role_arn`). The node groups already
carry the discovery tags `k8s.io/cluster-autoscaler/enabled=true` and
`k8s.io/cluster-autoscaler/nous-dev-cluster=owned` (set in `main.tf`).

```sh
eval "$(aws configure export-credentials --format env)"
helm upgrade --install cluster-autoscaler autoscaler/cluster-autoscaler \
  -n kube-system \
  --version 9.59.0 \
  --set autoDiscovery.clusterName=nous-dev-cluster \
  --set awsRegion=us-east-1 \
  --set image.tag=v1.31.5 \
  --set rbac.serviceAccount.name=cluster-autoscaler \
  --set rbac.serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="$(terraform -chdir=infrastructure/terraform output -raw cluster_autoscaler_role_arn)" \
  --set extraArgs.balance-similar-node-groups=false \
  --set extraArgs.expander=least-waste \
  --set extraArgs.scale-down-unneeded-time=10m0s \
  --set extraArgs.skip-nodes-with-system-pods=false
```

The `helm list` `app_version` field still shows the chart default (1.35.0);
the running image is pinned by `image.tag` to `v1.31.5`.

## external-secrets (chart 2.10.0 / app v2.10.0)

Default values, dedicated namespace:

```sh
helm upgrade --install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace
```

SecretStores/ClusterStores are configured per-app later; no AWS IRSA is
wired for ESO yet (add one when the first SecretStore needs it).

## external-dns (chart 1.22.0 / app v0.22.0)

DNS is Cloudflare (NOT Route53). Needs a Cloudflare API token in the secret
`kube-system/cloudflare-api-token` (key `api-token`). A placeholder was
created at install time, so the pod crash-loops with
`Invalid format for Authorization header` until the real token is supplied
(apply it via external-secrets or create the secret manually):

```sh
eval "$(aws configure export-credentials --format env)" && \
kubectl create secret generic cloudflare-api-token -n kube-system \
  --from-literal=api-token="<REAL_CLOUDFLARE_API_TOKEN>" \
  --save-config --dry-run=client -o yaml | kubectl apply -f -
```

DO NOT commit the token anywhere.

```sh
eval "$(aws configure export-credentials --format env)" && \
helm upgrade --install external-dns external-dns/external-dns \
  -n kube-system \
  --version 1.22.0 \
  --set provider.name=cloudflare \
  --set 'env[0].name=CF_API_TOKEN' \
  --set 'env[0].valueFrom.secretKeyRef.name=cloudflare-api-token' \
  --set 'env[0].valueFrom.secretKeyRef.key=api-token' \
  --set policy=upsert-only \
  --set txtOwnerId=nous-eks \
  --set 'sources[0]=ingress' \
  --set 'sources[1]=service' \
  --set 'domainFilters[0]=goodwiinz.tech'
```

For the existing Helm-managed deployment that was installed with the stale
`gen-text.app` filter, apply this guarded one-time patch and restart. The Helm
command above is already the source of truth for subsequent upgrades:

```sh
eval "$(aws configure export-credentials --format env)" && \
kubectl patch deployment external-dns -n kube-system --type=json \
  -p='[{"op":"test","path":"/spec/template/spec/containers/0/args/8","value":"--domain-filter=gen-text.app"},{"op":"replace","path":"/spec/template/spec/containers/0/args/8","value":"--domain-filter=goodwiinz.tech"}]'

eval "$(aws configure export-credentials --format env)" && \
kubectl rollout restart deployment/external-dns -n kube-system
```

## Verify

```sh
kubectl get pods -n kube-system
helm list -n kube-system; helm list -n external-secrets
```

## Known issues / flags

- `external-dns` CrashLoopBackOff until the real Cloudflare token replaces
  the `CHANGE_ME_CLOUDFLARE_API_TOKEN` placeholder — expected, see above.
- ~~`ebs-csi-controller` CrashLoopBackOff~~ RESOLVED: the IRSA role
  (`nous-development-ebs-csi`) is codified in `infrastructure/terraform/ebs-csi-irsa.tf`
  and the `aws-ebs-csi-driver` addon now has `serviceAccountRoleArn` set
  (terraform-managed, `main.tf cluster_addons`).
- ACM cert stays `PENDING_VALIDATION` until the Cloudflare CNAME from
  `terraform output acm_validation_record` is added manually.

## Out-of-band IAM/secret notes

- **ArgoCD git fetch timeout**: the repo pack is 1.28GiB — the default 90s
  `git fetch` subprocess timeout causes ComparisonError on a fresh repo-server
  clone. `reposerver.git.request.timeout` alone does not control this fetch.
  `infrastructure/helm/argocd-values.yaml` sets `ARGOCD_EXEC_TIMEOUT=600s`
  on the repo-server and extends the controller/server RPC deadlines. After
  upgrading the ArgoCD Helm release with those values, verify all three AWS
  applications return to Synced/Healthy.
- **Node role SSM**: `AmazonSSMManagedInstanceCore` is attached OUT-OF-BAND to
  the EKS-managed node instance role
  (`default-eks-node-group-20260916065522520500000002`) for SSM access during
  debugging (Session Manager into nodes). Not codified in terraform — if the
  node group/role is recreated, re-attach it.
- **Neo4j credentials**: the secret
  `multimodal-rag-system/nous-dev-aws-knowledge-graph-analytics-neo4j-credentials`
  (key `NEO4J_AUTH`, value `neo4j/<32-hex>`) is PRE-CREATED out-of-band via
  `kubectl create secret`. ArgoCD NEVER manages this secret (the chart only
  renders it when `neo4j.password` is set, which values-aws deliberately does
  not). The backend's `NEO4J_PASSWORD` (Infisical `app-secrets`) must match.
  NEVER commit the password.
