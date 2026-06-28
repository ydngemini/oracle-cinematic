terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  # Remote state with locking + encryption. Bucket + DynamoDB table created out of
  # band (neoh-tfstate-404870839825 / neoh-tflock). Holds the Aurora master-secret
  # ARN + resource ids, so it stays remote + encrypted.
  backend "s3" {
    bucket         = "neoh-tfstate-404870839825"
    key            = "neoh/prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "neoh-tflock"
    encrypt        = true
  }
}
