# ─────────────────────────────────────────────────────────────────────────────
# CloudWatch alarms → SNS email.
#
# One SNS topic ("${local.name}-alarms") fans every alarm out to an email
# subscription (ydnop@ydnhft.com). The subscription is created in PENDING state;
# the operator must click the confirmation link AWS emails before notifications
# flow. All alarms set both alarm_actions and ok_actions so you get the
# breach AND the recovery.
#
# Why the topic is NOT KMS-encrypted: CloudWatch Alarms publish via the
# cloudwatch.amazonaws.com service principal. Publishing to a CMK-encrypted SNS
# topic requires that KMS key's policy to grant kms:GenerateDataKey/Decrypt to
# that principal; the AWS-managed alias/aws/sns key cannot be modified to do so,
# which silently drops alarm notifications. Leaving the topic unencrypted keeps
# delivery reliable. (No sensitive payload — just alarm state text.)
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_sns_topic" "alarms" {
  name = "${local.name}-alarms"
}

resource "aws_sns_topic_subscription" "alarms_email" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = "ydnop@ydnhft.com"
}

# Explicitly let same-account CloudWatch alarms publish to the topic. Default SNS
# policy already permits the account owner, but this makes the intent auditable
# and pins the source account.
resource "aws_sns_topic_policy" "alarms" {
  arn = aws_sns_topic.alarms.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudWatchAlarmsPublish"
      Effect    = "Allow"
      Principal = { Service = "cloudwatch.amazonaws.com" }
      Action    = "SNS:Publish"
      Resource  = aws_sns_topic.alarms.arn
      Condition = {
        StringEquals = { "AWS:SourceOwner" = local.account_id }
      }
    }]
  })
}

# ── ALB: target 5XX ──────────────────────────────────────────────────────────
# Sustained app-level 5XXs from either target group. Sum >= 5 over a 5-min window.
resource "aws_cloudwatch_metric_alarm" "alb_target_5xx" {
  alarm_name        = "${local.name}-alb-target-5xx"
  alarm_description = "ALB target 5XX responses >= 5 in 5 minutes (backend or frontend returning errors)."

  namespace   = "AWS/ApplicationELB"
  metric_name = "HTTPCode_Target_5XX_Count"
  dimensions  = { LoadBalancer = aws_lb.main.arn_suffix }

  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ── ALB: unhealthy hosts per target group ────────────────────────────────────
# >= 1 unhealthy target for 2 consecutive minutes = a task is failing its
# health check (deploy gone bad, crash loop, DB unreachable).
resource "aws_cloudwatch_metric_alarm" "tg_unhealthy_backend" {
  alarm_name        = "${local.name}-tg-backend-unhealthy"
  alarm_description = "Backend target group has >= 1 unhealthy host for 2 minutes."

  namespace   = "AWS/ApplicationELB"
  metric_name = "UnHealthyHostCount"
  dimensions = {
    TargetGroup  = aws_lb_target_group.backend.arn_suffix
    LoadBalancer = aws_lb.main.arn_suffix
  }

  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "tg_unhealthy_frontend" {
  alarm_name        = "${local.name}-tg-frontend-unhealthy"
  alarm_description = "Frontend target group has >= 1 unhealthy host for 2 minutes."

  namespace   = "AWS/ApplicationELB"
  metric_name = "UnHealthyHostCount"
  dimensions = {
    TargetGroup  = aws_lb_target_group.frontend.arn_suffix
    LoadBalancer = aws_lb.main.arn_suffix
  }

  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ── ECS: running task count below desired ────────────────────────────────────
# Uses Container Insights (enabled on the cluster in ecs.tf). Fires when running
# tasks drop below the desired count for 3 consecutive minutes. Missing data is
# treated as breaching: no RunningTaskCount datapoints means the service stopped
# reporting (all tasks gone), which is the exact outage we want paged on.
resource "aws_cloudwatch_metric_alarm" "ecs_running_backend" {
  alarm_name        = "${local.name}-ecs-backend-running-low"
  alarm_description = "Backend ECS running task count below desired (${var.backend_desired_count}) for 3 minutes."

  namespace   = "ECS/ContainerInsights"
  metric_name = "RunningTaskCount"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.backend.name
  }

  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = var.backend_desired_count
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "ecs_running_frontend" {
  alarm_name        = "${local.name}-ecs-frontend-running-low"
  alarm_description = "Frontend ECS running task count below desired (${var.frontend_desired_count}) for 3 minutes."

  namespace   = "ECS/ContainerInsights"
  metric_name = "RunningTaskCount"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.frontend.name
  }

  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = var.frontend_desired_count
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# ── Aurora: CPU / connections / memory ───────────────────────────────────────
# Cluster-level (DBClusterIdentifier) rollups for the Serverless v2 cluster.
resource "aws_cloudwatch_metric_alarm" "aurora_cpu" {
  alarm_name        = "${local.name}-aurora-cpu-high"
  alarm_description = "Aurora cluster CPU > 80% for 10 minutes (likely under-provisioned ACU or a hot query)."

  namespace   = "AWS/RDS"
  metric_name = "CPUUtilization"
  dimensions  = { DBClusterIdentifier = aws_rds_cluster.aurora.cluster_identifier }

  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# Tunable: 200 is a "something is leaking/holding connections" warning. Aurora
# PG max_connections scales with ACU (well above this at the 4-ACU ceiling), so
# this is a leading indicator, not the hard cap.
resource "aws_cloudwatch_metric_alarm" "aurora_connections" {
  alarm_name        = "${local.name}-aurora-connections-high"
  alarm_description = "Aurora DatabaseConnections > 200 for 10 minutes (possible connection leak / pool exhaustion)."

  namespace   = "AWS/RDS"
  metric_name = "DatabaseConnections"
  dimensions  = { DBClusterIdentifier = aws_rds_cluster.aurora.cluster_identifier }

  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 200
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

# FreeableMemory in bytes. 256 MiB floor → the instance is memory-starved and at
# risk of OOM / swap; bump db_min_acu if this fires under normal load.
resource "aws_cloudwatch_metric_alarm" "aurora_memory" {
  alarm_name        = "${local.name}-aurora-freeable-memory-low"
  alarm_description = "Aurora FreeableMemory < 256 MiB for 10 minutes (memory pressure / OOM risk)."

  namespace   = "AWS/RDS"
  metric_name = "FreeableMemory"
  dimensions  = { DBClusterIdentifier = aws_rds_cluster.aurora.cluster_identifier }

  statistic           = "Average"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 268435456 # 256 * 1024 * 1024 bytes
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}
