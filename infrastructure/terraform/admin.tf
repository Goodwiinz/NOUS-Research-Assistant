# =============================================================================
# Cluster Admin Role — assumed from the AWS account to operate the EKS cluster
# =============================================================================
# aws-auth maps this role to system:masters via var.cluster_admin_role_arns.
# The role itself needs no IAM permissions — only sts:AssumeRole from the
# account principal. Use with:
#   aws sts assume-role --role-arn <output cluster_admin_role_arn> \
#     --role-session-name cluster-admin

data "aws_iam_policy_document" "cluster_admin_trust" {
  statement {
    sid     = "AssumeFromAccount"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_iam_role" "cluster_admin" {
  name               = "${var.project_name}-${var.environment}-cluster-admin"
  assume_role_policy = data.aws_iam_policy_document.cluster_admin_trust.json

  tags = {
    Project     = "Knowledge Graph Analytics"
    Environment = var.environment
    ManagedBy   = "Terraform"
    Owner       = var.owner_email
  }
}
