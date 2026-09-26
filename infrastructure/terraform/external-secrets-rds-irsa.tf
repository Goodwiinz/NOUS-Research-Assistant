# Read only the RDS-managed, rotating master credential from EKS.
data "aws_iam_policy_document" "rds_secret_reader_assume_role" {
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
      values   = ["system:serviceaccount:multimodal-rag-system:aws-rds-secret-reader"]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "rds_secret_reader" {
  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [module.rds.db_instance_master_user_secret_arn]
  }
}

resource "aws_iam_role" "rds_secret_reader" {
  name               = "${var.project_name}-${var.environment}-rds-secret-reader"
  assume_role_policy = data.aws_iam_policy_document.rds_secret_reader_assume_role.json
}

resource "aws_iam_role_policy" "rds_secret_reader" {
  name   = "${var.project_name}-${var.environment}-rds-secret-reader"
  role   = aws_iam_role.rds_secret_reader.id
  policy = data.aws_iam_policy_document.rds_secret_reader.json
}
