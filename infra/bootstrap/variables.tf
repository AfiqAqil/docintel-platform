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

# GitHub's OIDC subject claim names the owner and the repository by their immutable numeric
# ids as well as by name: repo:<owner>@<owner id>/<repo>@<repo id>:<context>. A trust policy
# written against the older repo:<owner>/<repo>:<context> form matches nothing, and AWS
# answers "not authorized". The ids also close a real hole: a name can be released and
# re-registered by someone else, an id cannot.
#   gh api repos/<owner>/<repo> --jq '.owner.id, .id'
variable "github_owner_id" {
  type    = string
  default = "152358148"
}

variable "github_repository_id" {
  type    = string
  default = "1378307019"
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
