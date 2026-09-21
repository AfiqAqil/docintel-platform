# ---------------------------------------------------------------------------------------
# Terraform state bucket for the main stack
# ---------------------------------------------------------------------------------------
resource "aws_s3_bucket" "tfstate" {
  bucket        = "${var.project}-tfstate-${var.aws_account_id}"
  force_destroy = var.destroyable
}

# Versioning is what makes a bad apply recoverable: every state write keeps the previous one.
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------------------
# Container registries
#
# These live here and not in the main stack because the ECS services need an image to exist
# before their first deployment. With the repositories in the main stack, the first apply
# would start three services with nothing to pull, the deployment circuit breaker would
# trip, and the apply would fail.
# ---------------------------------------------------------------------------------------
resource "aws_ecr_repository" "service" {
  for_each = var.services

  name = "${var.project}/${each.key}"
  # A tag, once pushed, can never point at different bytes. Images are tagged with the git
  # SHA, so "what is running" always maps to exactly one commit.
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.destroyable

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "service" {
  for_each   = aws_ecr_repository.service
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 15 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 15
      }
      action = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------------------------------
# The OpenAI API key, as an empty secret
#
# Only used when llm_provider = "openai". Terraform creates the container and never the
# value, so the key is absent from state, from code and from the task definition. It is set
# once, out of band:
#
#   aws secretsmanager put-secret-value --secret-id docintel/openai-api-key \
#     --secret-string "$OPENAI_API_KEY" --profile <profile>
#
# It lives in bootstrap for the same reason ECR does: the key must exist before the worker
# first starts, or the worker crashes, the circuit breaker trips and the apply fails.
# ---------------------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "openai_api_key" {
  name        = "${var.project}/openai-api-key"
  description = "OpenAI API key for the worker's fallback LLM provider. Value is set out of band."
  # Without this, a destroyed secret lingers for 30 days and blocks recreation by name.
  recovery_window_in_days = var.destroyable ? 0 : 30
}

# ---------------------------------------------------------------------------------------
# GitHub Actions: OIDC, so no AWS key is ever stored in the repository
# ---------------------------------------------------------------------------------------
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  github_owner = split("/", var.github_repository)[0]
  github_repo  = split("/", var.github_repository)[1]

  # repo:<owner>@<owner id>/<repo>@<repo id>, the form the token actually carries.
  github_sub_prefix = "repo:${local.github_owner}@${var.github_owner_id}/${local.github_repo}@${var.github_repository_id}"
}

# Read only, assumable from a pull request. It can plan, and it cannot change anything.
data "aws_iam_policy_document" "ci_plan_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${local.github_sub_prefix}:pull_request"]
    }
  }
}

resource "aws_iam_role" "ci_plan" {
  name               = "${var.project}-ci-plan"
  assume_role_policy = data.aws_iam_policy_document.ci_plan_trust.json
}

resource "aws_iam_role_policy_attachment" "ci_plan_readonly" {
  role       = aws_iam_role.ci_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# A plan takes the state lock, which is an object write next to the state file.
data "aws_iam_policy_document" "ci_plan_state_lock" {
  statement {
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.tfstate.arn}/*.tflock"]
  }
}

resource "aws_iam_role_policy" "ci_plan_state_lock" {
  name   = "state-lock"
  role   = aws_iam_role.ci_plan.id
  policy = data.aws_iam_policy_document.ci_plan_state_lock.json
}

# The deploy role is assumable only by a job running in the `dev` GitHub environment, which is
# restricted to the main branch. The only workflow that targets that environment is started
# by hand (workflow_dispatch), so a push to main on its own cannot assume this role, and
# neither can a pull request. A required reviewer on the environment would add a second
# person; GitHub offers that for private repositories only on paid plans.
data "aws_iam_policy_document" "ci_deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${local.github_sub_prefix}:environment:${var.github_deploy_environment}"]
    }
  }
}

resource "aws_iam_role" "ci_deploy" {
  name               = "${var.project}-ci-deploy"
  assume_role_policy = data.aws_iam_policy_document.ci_deploy_trust.json
}

# ponytail: AdministratorAccess. Terraform for this stack creates IAM roles, a VPC, RDS, ECS
# and more, and a hand-built least-privilege policy for all of that is its own project. The
# control here is who can assume the role (one environment, one branch, a manual trigger),
# not what it can do. Production: a permissions boundary plus a policy scoped to the project's resource
# prefix and tags.
resource "aws_iam_role_policy_attachment" "ci_deploy_admin" {
  role       = aws_iam_role.ci_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
