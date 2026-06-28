terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  # Remote state with locking + encryption. Create the bucket + DynamoDB table
  # once (out of band) then uncomment. Keeping state remote + encrypted matters
  # here: it holds the Aurora master-secret ARN and resource ids.
  # backend "s3" {
  #   bucket         = "neoh-tfstate-<account-id>"
  #   key            = "neoh/prod/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "neoh-tflock"
  #   encrypt        = true
  # }
}
