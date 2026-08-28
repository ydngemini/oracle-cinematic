# Application secrets, KMS-encrypted. TF creates the container + a PLACEHOLDER
# version so the task definition can reference each JSON key; the operator then
# fills the real values out-of-band (so live secrets never sit in TF state or
# tfvars). lifecycle.ignore_changes keeps `terraform apply` from clobbering them.
#
#   aws secretsmanager put-secret-value --secret-id <name> \
#     --secret-string '{"ORACLE_SECRET_KEY":"...","ORACLE_ENCRYPTION_MASTER_KEY":"...",...}'
#
# ORDER MATTERS: put the real values in AFTER `terraform apply`, not before.
# ignore_changes only suppresses UPDATES, so on first create Terraform writes
# the placeholders below as the AWSCURRENT version regardless of what is already
# there — a value set beforehand is silently superseded.
#
# config.validate_or_die() refuses to boot on these placeholders. It did NOT
# until 2026-08-28: it asked only whether each value was non-empty, and
# "REPLACE_ME" is not empty. A fresh deploy would have come up signing JWTs
# with the literal string, encrypting PII with it, and serving a public
# platform_admin account (ecs.tf hardcodes ORACLE_ADMIN_ID) whose password was
# "REPLACE_ME" — while logging "Config validated for production".
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
    GOOGLE_CLIENT_ID             = ""
    GOOGLE_CLIENT_SECRET         = ""
    TWILIO_ACCOUNT_SID           = ""
    TWILIO_AUTH_TOKEN            = ""
    TWILIO_FROM_NUMBER           = ""
    ORACLE_TWILIO_TWIML_URL      = ""
    ACS_CONNECTION_STRING        = ""
    ACS_FROM_NUMBER              = ""
    ORACLE_ACS_WEBHOOK_SECRET    = ""
    REDIS_URL                    = ""
    RUNPOD_API_KEY               = ""
  })

  lifecycle { ignore_changes = [secret_string] }
}
