# =============================================================================
# IRSA role for the EBS CSI driver addon
# (system:serviceaccount:kube-system:ebs-csi-controller-sa)
#
# The addon itself is managed by the EKS module (main.tf cluster_addons);
# this role is what the addon's service_account_role_arn points at.
# NOTE: the role + policy attachment were first created out-of-band and are
# terraform-imported (see git history / runbook); config here is authoritative.
# =============================================================================

data "aws_iam_policy_document" "ebs_csi_assume_role" {
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
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }

    # Matches the out-of-band-created role's live trust policy (aud pin)
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.project_name}-${var.environment}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_assume_role.json
}

# AWS-managed policy (aws_iam_role_policy_attachment, not inline policy)
resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

output "ebs_csi_role_arn" {
  description = "IRSA role ARN used by the aws-ebs-csi-driver EKS addon"
  value       = aws_iam_role.ebs_csi.arn
}
