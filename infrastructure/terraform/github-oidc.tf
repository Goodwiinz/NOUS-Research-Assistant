# =============================================================================
# GitHub Actions OIDC federation -> ECR push role for CI
# =============================================================================
# Replaces the static DO_REGISTRY_TOKEN secret used by
# .github/workflows/docker-build.yml. At cutover, resolve the role with:
#   terraform output -raw github_oidc_role_arn
# and fill the <GITHUB_OIDC_ROLE_ARN> placeholder in the workflows.

resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = ["sts.amazonaws.com"]

  # Root CA thumbprints for token.actions.githubusercontent.com. AWS now
  # validates against its trusted root CA set, but the provider resource
  # still requires a non-empty list.
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]

  tags = {
    Name = "${var.project_name}-github-oidc-provider"
  }
}

# Trust is limited to the goodwiins/rag repository building on the develop
# branch (the only branch that runs the image build/push pipeline).
data "aws_iam_policy_document" "github_oidc_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
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

resource "aws_iam_role" "github_ecr_push" {
  name               = "${var.project_name}-github-ecr-push"
  assume_role_policy = data.aws_iam_policy_document.github_oidc_assume_role.json

  tags = {
    Name = "${var.project_name}-github-ecr-push"
  }
}

# Least privilege: ECR push/pull scoped to the nous/* repositories only.
# ecr:GetAuthorizationToken is not resource-scopable and must target "*".
data "aws_iam_policy_document" "github_ecr_push" {
  statement {
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
    ]
    resources = ["*"]
  }

  statement {
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

output "github_oidc_role_arn" {
  description = "IAM role assumed by GitHub Actions (OIDC) to push images to ECR"
  value       = aws_iam_role.github_ecr_push.arn
}
