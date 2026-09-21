terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial configuration. The bucket, key and region come from envs/<env>.backend.hcl, so one
  # root module serves every environment without workspaces or copied directories:
  #
  #   terraform init -backend-config=envs/dev.backend.hcl
  #
  # Locking is S3 native (use_lockfile). The DynamoDB lock table is deprecated.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  # No profile is named here. Locally the caller sets AWS_PROFILE, and in CI authentication
  # is OIDC, where no profile exists. This guard is what holds in both: an apply pointed at
  # any other account fails before it changes anything.
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = "${var.project}-${var.environment}"
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)
}
