resource "aws_cloudwatch_log_group" "backend" {
  name              = "/ecs/${local.name}/backend"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "frontend" {
  name              = "/ecs/${local.name}/frontend"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

# pgaudit trail (HARDENING.md §1). Pre-created so it carries our retention + KMS
# instead of RDS's defaults; the cluster depends_on this so RDS writes into it.
# The name is RDS's fixed convention: /aws/rds/cluster/<cluster-id>/postgresql.
resource "aws_cloudwatch_log_group" "pgaudit" {
  name              = "/aws/rds/cluster/${local.name}-aurora/postgresql"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}
