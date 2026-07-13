data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ── Execution role — what ECS itself uses to launch the task: pull images,
# write logs, and inject the Secrets Manager values into the container env.
resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_extra" {
  statement {
    sid       = "ReadAppSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.app.arn]
  }
  statement {
    sid       = "DecryptForInjection"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_role_policy" "execution_extra" {
  name   = "${local.name}-execution-extra"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_extra.json
}

# ── Task role — the application's own runtime identity (HARDENING.md §4):
# passwordless Aurora login via rds-db:connect + Bedrock inference. No static
# DB credential exists anywhere; stolen source yields zero usable secrets.
resource "aws_iam_role" "task" {
  name               = "${local.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid     = "RdsIamConnect"
    actions = ["rds-db:connect"]
    # Scoped to the single IAM login role (created by migration 0003), keyed on
    # the cluster's stable resource id — not the (mutable) cluster name.
    resources = [
      "arn:aws:rds-db:${var.aws_region}:${local.account_id}:dbuser:${aws_rds_cluster.aurora.cluster_resource_id}/oracle_app_login"
    ]
  }

  statement {
    sid = "BedrockInvoke"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    # Foundation models are account-less; cross-region inference uses profiles in
    # this account. Allow both so the us.* inference profiles resolve.
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:*:${local.account_id}:inference-profile/*",
    ]
  }

  # ── AWS observability dashboard (aws_observability.py) — READ-ONLY inventory +
  # metrics + cost. Broad but strictly read/describe/list; no mutating actions.
  # These describe/list/get APIs do not support resource-level scoping, so "*" is
  # required. Gated at runtime by AWS_OBSERVABILITY_ENABLED (ecs.tf). Only added
  # because the Neoh-embedded dashboard was opted into; drop this statement to
  # revoke the dashboard's access entirely.
  dynamic "statement" {
    for_each = var.observability_enabled ? [1] : []
    content {
      sid = "AwsObservabilityReadOnly"
      actions = [
        "cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData", "cloudwatch:ListMetrics",
        "ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeSecurityGroups",
        "rds:DescribeDBInstances", "rds:DescribeDBClusters",
        "lambda:ListFunctions", "lambda:GetFunction",
        "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTargetHealth",
        "cloudfront:ListDistributions",
        "ce:GetCostAndUsage",
        "securityhub:GetFindings",
        "iam:ListUsers", "iam:GetUser",
        "s3:ListAllMyBuckets", "s3:GetBucketLocation",
        "autoscaling:DescribeAutoScalingGroups",
      ]
      resources = ["*"]
    }
  }

  # ── AWS observability dashboard — the two mutating actions behind it
  # (POST .../autoscaling/{group}/scale, POST .../ec2/{id}/restart, both already
  # gated to platform_admin in aws_observability.py). Kept as its own statement,
  # deliberately NOT folded into AwsObservabilityReadOnly above, so that
  # statement's name stays true and a future reviewer doesn't have to re-derive
  # which actions are mutating. Scoped to this account/region (both actions
  # support resource-level ARNs, unlike the Describe/List calls above).
  dynamic "statement" {
    for_each = var.observability_enabled ? [1] : []
    content {
      sid = "AwsObservabilityMutate"
      actions = [
        "ec2:RebootInstances",
        "autoscaling:SetDesiredCapacity",
      ]
      resources = [
        "arn:aws:ec2:${var.aws_region}:${local.account_id}:instance/*",
        "arn:aws:autoscaling:${var.aws_region}:${local.account_id}:autoScalingGroup:*:autoScalingGroupName/*",
      ]
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${local.name}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# ─────────────────────────────────────────────────────────────────────────────
# Deploy role — scoped CI/operator identity that REPLACES the root access key the
# `swarm-admin` profile currently uses (SECURITY: a root AKIA key is the highest-
# risk credential type). Assumed by the operator IAM user via sts:AssumeRole;
# carries only the permissions the infra/scripts deploy + ops flows actually call.
#
# CUTOVER (do NOT delete the root key until this is proven):
#   1. terraform apply  (creates the role — additive, safe)
#   2. add to ~/.aws/config:
#        [profile neoh-deploy]
#        role_arn = arn:aws:iam::<acct>:role/neoh-prod-deploy
#        source_profile = default            # the YDNop IAM user
#   3. run a full deploy under AWS_PROFILE=neoh-deploy and confirm it works
#   4. THEN: aws iam delete-access-key for the root key; repoint scripts' default.
# ─────────────────────────────────────────────────────────────────────────────
# YDNop lives in a DIFFERENT account (364238921565) than prod (404870839825), so this
# is a CROSS-ACCOUNT trust. After apply, the YDNop side must also grant the user
# sts:AssumeRole on this role's ARN before `source_profile=default` can assume it.
variable "deploy_principal_arns" {
  description = "IAM principal ARNs allowed to assume the scoped deploy role."
  type        = list(string)
  default     = ["arn:aws:iam::364238921565:user/YDNop"]
}

data "aws_iam_policy_document" "deploy_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = var.deploy_principal_arns
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "${local.name}-deploy"
  assume_role_policy = data.aws_iam_policy_document.deploy_assume.json
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid = "EcsDeploy"
    actions = [
      "ecs:DescribeServices", "ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition",
      "ecs:UpdateService", "ecs:ListTasks", "ecs:DescribeTasks", "ecs:RunTask",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "PassEcsRoles"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
  }
  # setup-ses.sh: SES enable + DKIM in Route53 + grant ses:SendEmail on the task role.
  statement {
    sid       = "SesSetup"
    actions   = ["ses:*", "sesv2:*"]
    resources = ["*"]
  }
  statement {
    sid       = "Route53Dns"
    actions   = ["route53:ChangeResourceRecordSets", "route53:ListHostedZones", "route53:GetChange"]
    resources = ["*"]
  }
  statement {
    sid       = "PutRolePolicyForSes"
    actions   = ["iam:PutRolePolicy", "iam:GetRole"]
    resources = [aws_iam_role.task.arn]
  }
  # request-gpu-quota.sh
  statement {
    sid = "QuotaRequests"
    actions = [
      "servicequotas:GetServiceQuota", "servicequotas:RequestServiceQuotaIncrease",
      "servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota", "servicequotas:GetRequestedServiceQuotaChange",
    ]
    resources = ["*"]
  }
  # build-images.sh (CodeBuild). CreateRole/PassRole scoped to the neoh-codebuild role it provisions.
  statement {
    sid = "CodeBuildDeploy"
    actions = [
      "codebuild:BatchGetProjects", "codebuild:CreateProject", "codebuild:UpdateProject",
      "codebuild:StartBuild", "codebuild:BatchGetBuilds",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "CodeBuildRole"
    actions   = ["iam:CreateRole", "iam:PutRolePolicy", "iam:GetRole", "iam:PassRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/neoh-codebuild"]
  }
  statement {
    sid       = "ReconSourceBucket"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:CreateBucket", "s3:PutBucketPolicy"]
    resources = ["arn:aws:s3:::${local.name}-recon-${local.account_id}", "arn:aws:s3:::${local.name}-recon-${local.account_id}/*"]
  }
  statement {
    sid       = "AppSecretRW"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.app.arn, "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:neoh/dockerhub-*"]
  }
  # Read-only verification used by prod-smoke.sh / monitoring.
  statement {
    sid = "ReadOnlyVerify"
    actions = [
      "cloudwatch:DescribeAlarms", "rds:DescribeDBClusters", "elasticloadbalancing:Describe*",
      "ecr:GetAuthorizationToken", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
      "logs:GetLogEvents", "logs:FilterLogEvents", "sts:GetCallerIdentity",
      "s3:GetEncryptionConfiguration", "s3:GetBucketLocation", "s3:GetBucketPolicy",
      "s3:GetBucketPublicAccessBlock", "s3:GetBucketVersioning", "s3:ListBucket",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "${local.name}-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
