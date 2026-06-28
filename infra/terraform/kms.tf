# Customer-managed KMS key encrypting Aurora storage, the app Secrets Manager
# secret, and the CloudWatch log groups (incl. the tamper-evident pgaudit trail).
resource "aws_kms_key" "main" {
  description             = "${local.name} encryption (Aurora, Secrets, Logs)"
  deletion_window_in_days = 14
  enable_key_rotation     = true

  # Allow CloudWatch Logs to use the key for the encrypted log groups.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableRoot"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "AllowCloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action = [
          "kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*",
          "kms:GenerateDataKey*", "kms:DescribeKey"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:*"
          }
        }
      }
    ]
  })
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.main.key_id
}
