# Application secrets, KMS-encrypted. TF creates the container + a PLACEHOLDER
# version so the task definition can reference each JSON key; the operator then
# fills the real values out-of-band (so live secrets never sit in TF state or
# tfvars). lifecycle.ignore_changes keeps `terraform apply` from clobbering them.
#
#   aws secretsmanager put-secret-value --secret-id <name> \
#     --secret-string '{"ORACLE_SECRET_KEY":"...","ORACLE_ENCRYPTION_MASTER_KEY":"...",...}'
#
# config.validate_or_die() refuses to boot until ORACLE_SECRET_KEY +
# ORACLE_ENCRYPTION_MASTER_KEY are real, so populate before the service stabilizes.
resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.name}/app"
  description             = "${local.name} backend runtime secrets"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    ORACLE_SECRET_KEY            = "REPLACE_ME"
    ORACLE_ENCRYPTION_MASTER_KEY = "REPLACE_ME"
    ORACLE_ADMIN_PASSPHRASE      = "REPLACE_ME"
    STRIPE_SECRET_KEY            = ""
    STRIPE_WEBHOOK_SECRET        = ""
    RENTCAST_API_KEY             = ""
    RUNPOD_API_KEY               = ""
  })

  lifecycle { ignore_changes = [secret_string] }
}
