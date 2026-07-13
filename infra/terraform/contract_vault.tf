# Private S3 vault for generated legal PDFs.
#
# The backend uploads assignment contracts here with SSE-S3 (AES256) and returns
# one-hour presigned URLs. The bucket itself stays private: no ACLs, no public
# bucket policy, and a deny on non-TLS access.

resource "aws_s3_bucket" "contract_vault" {
  bucket = "${local.name}-contract-vault-${local.account_id}"
}

resource "aws_s3_bucket_ownership_controls" "contract_vault" {
  bucket = aws_s3_bucket.contract_vault.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "contract_vault" {
  bucket                  = aws_s3_bucket.contract_vault.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "contract_vault" {
  bucket = aws_s3_bucket.contract_vault.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "contract_vault" {
  bucket = aws_s3_bucket.contract_vault.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "contract_vault" {
  bucket = aws_s3_bucket.contract_vault.id

  rule {
    id     = "abort-incomplete-contract-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  rule {
    id     = "expire-old-contract-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

data "aws_iam_policy_document" "contract_vault_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.contract_vault.arn,
      "${aws_s3_bucket.contract_vault.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "DenyUnencryptedContractUploads"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.contract_vault.arn}/clients/*/contracts/*.pdf"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["AES256"]
    }
  }
}

resource "aws_s3_bucket_policy" "contract_vault" {
  bucket = aws_s3_bucket.contract_vault.id
  policy = data.aws_iam_policy_document.contract_vault_bucket.json
}

data "aws_iam_policy_document" "app_contract_vault" {
  statement {
    sid = "ContractVaultObjectRW"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.contract_vault.arn}/clients/*/contracts/*.pdf"]
  }
}

resource "aws_iam_role_policy" "app_contract_vault" {
  name   = "${local.name}-app-contract-vault"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.app_contract_vault.json
}

output "contract_vault_bucket" {
  description = "CONTRACT_VAULT_BUCKET — private SSE-S3 bucket for generated legal PDFs."
  value       = aws_s3_bucket.contract_vault.bucket
}
