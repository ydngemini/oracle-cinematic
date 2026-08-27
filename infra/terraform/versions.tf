terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  # Remote state with locking + encryption. Bucket + DynamoDB table created out of
  # band (neoh-tfstate-151105438863 / neoh-tflock), with versioning, AES256 and a
  # public-access block. Holds the Aurora master-secret ARN + resource ids, so it
  # stays remote + encrypted.
  #
  # Re-pointed 2026-08-27 from account 404870839825. That account's credentials
  # were lost, which took its state bucket with them — so this is a fresh state,
  # not a migrated one, and `terraform apply` here BUILDS rather than adopts.
  # Anything still running in the old account is invisible to this state and has
  # to be dealt with there.
  backend "s3" {
    bucket         = "neoh-tfstate-151105438863"
    key            = "neoh/prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "neoh-tflock"
    encrypt        = true
  }
}
