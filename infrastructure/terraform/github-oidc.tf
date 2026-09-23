# =============================================================================
# GitHub Actions OIDC federation -> ECR push role + EKS deploy role for CI
# =============================================================================
# Replaces the static DO_REGISTRY_TOKEN secret used by
# .github/workflows/docker-build.yml. At cutover, resolve the roles with:
#   terraform output -raw github_oidc_role_arn         # ECR push (docker-build)
#   terraform output -raw github_eks_deploy_role_arn   # EKS deploys (deploy)
# and fill the <GITHUB_OIDC_ROLE_ARN_FULL> /
# <GITHUB_OIDC_ROLE_ARN_FULL_EKS_DEPLOY> placeholders in the workflows.

# Look up the account-wide GitHub OIDC provider instead of managing it here:
# a hard `aws_iam_openid_connect_provider` resource collides with
# EntityAlreadyExists as soon as the account already has one (manual
# bootstrap or another stack owns it). If the provider is ever MISSING,
# bootstrap it once (AWS console/CLI or a temporary resource block adopted
# into whichever stack should own it), e.g.:
#   terraform import aws_iam_openid_connect_provider.github \
#     arn:aws:iam::<AWS_ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

# The deploy role retains the original develop-only trust. ECR builds need
# a separate trust policy for the migration branch in the renamed repository;
# do not grant that branch the EKS deploy role.
data "aws_iam_policy_document" "github_oidc_assume_role" {
  statement {
    sid     = "AssumeRoleByWebIdentity"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:goodwiins/rag:ref:refs/heads/develop"]
    }
  }
}

data "aws_iam_policy_document" "github_ecr_oidc_assume_role" {
  statement {
    sid     = "AssumeRoleByWebIdentity"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:goodwiins/rag:ref:refs/heads/develop",
        "repo:Goodwiinz/NOUS-Research-Assistant:ref:refs/heads/develop",
        "repo:Goodwiinz/NOUS-Research-Assistant:ref:refs/heads/migration/aws",
      ]
    }
  }
}

resource "aws_iam_role" "github_ecr_push" {
  name               = "${var.project_name}-github-ecr-push"
  assume_role_policy = data.aws_iam_policy_document.github_ecr_oidc_assume_role.json

  tags = {
    Name = "${var.project_name}-github-ecr-push"
  }
}

# Least privilege: ECR push/pull scoped to the two nous/* repositories only.
# ecr:GetAuthorizationToken is not resource-scopable and must target "*".
data "aws_iam_policy_document" "github_ecr_push" {
  statement {
    sid    = "EcrAuthToken"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPushPull"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [
      "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/nous/backend",
      "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/nous/frontend",
    ]
  }
}

resource "aws_iam_role_policy" "github_ecr_push" {
  name   = "${var.project_name}-github-ecr-push"
  role   = aws_iam_role.github_ecr_push.id
  policy = data.aws_iam_policy_document.github_ecr_push.json
}

# Deploy jobs (staging/production/rollback) talk to EKS (update-kubeconfig,
# kubectl, helm) and never touch ECR, so they assume this dedicated role.
# It reuses the same trust contract as the ECR push role (same OIDC provider,
# sub pinned to repo:goodwiins/rag:ref:refs/heads/develop).
# NOTE: kubelet-side image pulls use the node instance role, not this role.
# IMPORTANT: for the cluster to authorize these calls, this role's ARN must
# ALSO be listed in `cluster_admin_role_arns` (terraform.tfvars) at apply
# time, so the aws-auth ConfigMap maps it to system:masters (k8s RBAC).
resource "aws_iam_role" "github_eks_deploy" {
  name               = "${var.project_name}-github-eks-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_oidc_assume_role.json

  tags = {
    Name = "${var.project_name}-github-eks-deploy"
  }
}

data "aws_iam_policy_document" "github_eks_deploy" {
  statement {
    sid    = "EksDescribeCluster"
    effect = "Allow"
    actions = [
      "eks:DescribeCluster",
    ]
    resources = [
      "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}",
    ]
  }
}

resource "aws_iam_role_policy" "github_eks_deploy" {
  name   = "${var.project_name}-github-eks-deploy"
  role   = aws_iam_role.github_eks_deploy.id
  policy = data.aws_iam_policy_document.github_eks_deploy.json
}

output "github_oidc_role_arn" {
  description = "IAM role assumed by GitHub Actions (OIDC) to push images to ECR"
  value       = aws_iam_role.github_ecr_push.arn
}

output "github_eks_deploy_role_arn" {
  description = "IAM role assumed by GitHub Actions (OIDC) to deploy to EKS"
  value       = aws_iam_role.github_eks_deploy.arn
}

output "cluster_admin_role_arn" {
  description = "Assume this role for cluster-admin kubectl access: aws sts assume-role --role-arn <value> --role-session-name cluster-admin"
  value       = aws_iam_role.cluster_admin.arn
}
