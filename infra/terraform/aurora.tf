resource "aws_db_subnet_group" "aurora" {
  name       = "${local.name}-aurora"
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = "${local.name}-aurora" }
}

# Cluster parameter group (HARDENING.md §1). pgaudit + force_ssl + SCRAM + TLS1.3.
# shared_preload_libraries is STATIC → requires a cluster reboot to take effect
# (the runbook reboots before running migrations 0001→0003).
resource "aws_rds_cluster_parameter_group" "aurora" {
  name        = "${local.name}-aurora-pg16"
  family      = "aurora-postgresql16"
  description = "${local.name} hardened cluster params"

  parameter {
    name         = "shared_preload_libraries"
    value        = "pgaudit"
    apply_method = "pending-reboot"
  }
  parameter {
    name  = "pgaudit.log"
    value = "ddl,role,write"
  }
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name         = "password_encryption"
    value        = "scram-sha-256"
    apply_method = "pending-reboot"
  }
  parameter {
    name  = "ssl_min_protocol_version"
    value = "TLSv1.3"
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
  parameter {
    name  = "log_disconnections"
    value = "1"
  }
}

resource "aws_rds_cluster" "aurora" {
  cluster_identifier = "${local.name}-aurora"
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned" # required for Serverless v2 scaling
  engine_version     = var.db_engine_version
  database_name      = var.db_name

  # Bootstrap/admin user. The app itself authenticates passwordless via IAM
  # (oracle_app_login, created by migration 0003); this master is only for
  # provisioning + running migrations. RDS manages the secret in Secrets Manager.
  master_username               = "oracle_admin"
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.main.arn

  iam_database_authentication_enabled = true

  db_subnet_group_name            = aws_db_subnet_group.aurora.name
  vpc_security_group_ids          = [aws_security_group.aurora.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.aurora.name

  storage_encrypted = true
  kms_key_id        = aws_kms_key.main.arn

  backup_retention_period         = 14
  preferred_backup_window         = "07:00-09:00"
  copy_tags_to_snapshot           = true
  deletion_protection             = true
  enabled_cloudwatch_logs_exports = ["postgresql"]

  serverlessv2_scaling_configuration {
    min_capacity = var.db_min_acu
    max_capacity = var.db_max_acu
  }

  final_snapshot_identifier = "${local.name}-aurora-final"

  # Pre-created log group carries our retention + KMS; RDS writes into it.
  depends_on = [aws_cloudwatch_log_group.pgaudit]

  # Re-evaluate engine_version drift on minor auto-upgrades.
  lifecycle { ignore_changes = [engine_version] }
}

resource "aws_rds_cluster_instance" "aurora" {
  count                = 2
  identifier           = "${local.name}-aurora-${count.index}"
  cluster_identifier   = aws_rds_cluster.aurora.id
  instance_class       = "db.serverless"
  engine               = aws_rds_cluster.aurora.engine
  engine_version       = aws_rds_cluster.aurora.engine_version
  db_subnet_group_name = aws_db_subnet_group.aurora.name
  # Tighten the publicly_accessible default to false (private subnets anyway).
  publicly_accessible = false
}
