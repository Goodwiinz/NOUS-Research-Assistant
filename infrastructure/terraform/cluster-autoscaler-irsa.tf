# =============================================================================
# IRSA role for Cluster Autoscaler
# (system:serviceaccount:kube-system:cluster-autoscaler)
#
# The helm chart (see aws-addons.md) creates a service account named
# "cluster-autoscaler" annotated with this role's ARN.
# =============================================================================

data "aws_iam_policy_document" "cluster_autoscaler_assume_role" {
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
      values   = ["system:serviceaccount:kube-system:cluster-autoscaler"]
    }
  }
}

# Least-privilege policy per the autoscaler AWS cloudprovider docs:
# https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/cloudprovider/aws/README.md
data "aws_iam_policy_document" "cluster_autoscaler" {
  statement {
    effect = "Allow"
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeAutoScalingInstances",
      "autoscaling:DescribeLaunchConfigurations",
      "autoscaling:DescribeTags",
      "autoscaling:SetDesiredCapacity",
      "autoscaling:TerminateInstanceInAutoScalingGroup",
      "ec2:DescribeLaunchTemplateVersions",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "cluster_autoscaler" {
  name               = "${var.project_name}-${var.environment}-cluster-autoscaler"
  assume_role_policy = data.aws_iam_policy_document.cluster_autoscaler_assume_role.json
}

resource "aws_iam_policy" "cluster_autoscaler" {
  name        = "${var.project_name}-${var.environment}-cluster-autoscaler-iam-policy"
  description = "IAM policy for the Kubernetes Cluster Autoscaler (ASG autodiscovery)"

  policy = data.aws_iam_policy_document.cluster_autoscaler.json
}

resource "aws_iam_role_policy_attachment" "cluster_autoscaler" {
  role       = aws_iam_role.cluster_autoscaler.name
  policy_arn = aws_iam_policy.cluster_autoscaler.arn
}

output "cluster_autoscaler_role_arn" {
  description = "IRSA role ARN for the cluster-autoscaler service account"
  value       = aws_iam_role.cluster_autoscaler.arn
}
