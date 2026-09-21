terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # The backend is declared in backend.tf, which also explains the one time this stack has
  # to run on local state: the first apply, before the bucket it creates exists.
}

provider "aws" {
  region = var.region

  # No profile is named here. Locally the caller sets AWS_PROFILE, and in CI authentication
  # is OIDC, where no profile exists. This guard is what holds in both: an apply pointed at
  # any other account fails before it changes anything.
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Stack     = "bootstrap"
    }
  }
}
