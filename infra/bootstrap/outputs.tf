output "tfstate_bucket" {
  description = "Goes in infra/envs/<env>.backend.hcl as `bucket`."
  value       = aws_s3_bucket.tfstate.bucket
}

output "ecr_repository_urls" {
  description = "Where CI pushes each service's image."
  value       = { for name, repo in aws_ecr_repository.service : name => repo.repository_url }
}

output "openai_secret_name" {
  description = "Set its value out of band before deploying with llm_provider = openai."
  value       = aws_secretsmanager_secret.openai_api_key.name
}

output "ci_plan_role_arn" {
  description = "Assumed by the pull request workflow. Read only."
  value       = aws_iam_role.ci_plan.arn
}

output "ci_deploy_role_arn" {
  description = "Assumed by the deploy workflow, from the reviewer-gated environment only."
  value       = aws_iam_role.ci_deploy.arn
}
