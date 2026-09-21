terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Local state, on purpose. This stack creates the bucket that holds every other stack's
  # state, so it cannot keep its own state there. It is applied once, by hand, and the
  # resulting terraform.tfstate stays on the machine that ran it (it is gitignored).
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
