variable "project" {
  description = "Short name used as a prefix for every resource."
  type        = string
  default     = "docintel"
}

variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "aws_account_id" {
  description = "The only account this stack may be applied to."
  type        = string
  default     = "277707137200"
}

variable "github_repository" {
  description = "owner/name of the repository whose workflows may assume the CI roles."
  type        = string
  default     = "AfiqAqil/docintel-platform"
}

variable "github_deploy_environment" {
  description = "The GitHub environment the deploy jobs run in. The deploy role trusts only this environment's OIDC subject, and the environment is restricted to the main branch."
  type        = string
  default     = "dev"
}

variable "services" {
  description = "One ECR repository per independently deployed service."
  type        = set(string)
  default     = ["frontend", "backend", "worker"]
}

variable "destroyable" {
  description = "True for a throwaway environment: buckets, repositories and the secret can be destroyed even when they hold data. False in production."
  type        = bool
  default     = true
}
