# =============================================================================
# IRSA role for the application backend (S3 object storage access)
# (system:serviceaccount:multimodal-rag-system:nous-dev-aws-knowledge-graph-analytics)
#
# Namespace/release are fixed for the dev cluster: the backend ServiceAccount
# is rendered by the helm chart as <release>-<chart>, release = nous-dev-aws.
# The S3 bucket itself was created out-of-band (not managed in this stack).
# Annotate the SA via values-aws.yaml serviceAccount.annotations.
# =============================================================================

locals {
  app_s3_bucket = "nous-development-storage-3ilp9pj2"
}

data "aws_iam_policy_document" "app_s3_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:multimodal-rag-system:nous-dev-aws-knowledge-graph-analytics"]
    }
  }
}

data "aws_iam_policy_document" "app_s3" {
  statement {
    sid    = "AppS3Objects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["arn:aws:s3:::${local.app_s3_bucket}/*"]
  }

  statement {
    sid    = "AppS3List"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
    ]
    resources = ["arn:aws:s3:::${local.app_s3_bucket}"]
  }
}

resource "aws_iam_role" "app_s3" {
  name               = "${var.project_name}-${var.environment}-app-s3"
  assume_role_policy = data.aws_iam_policy_document.app_s3_assume_role.json
}

resource "aws_iam_role_policy" "app_s3" {
  name   = "${var.project_name}-${var.environment}-app-s3"
  role   = aws_iam_role.app_s3.id
  policy = data.aws_iam_policy_document.app_s3.json
}

output "app_s3_role_arn" {
  description = "IRSA role ARN to annotate the backend ServiceAccount with (values-aws.yaml)"
  value       = aws_iam_role.app_s3.arn
}
