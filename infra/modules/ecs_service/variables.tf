variable "name" {
  description = "Service name, for example frontend. Used in resource names and the log stream prefix."
  type        = string
}

variable "name_prefix" {
  description = "Project and environment, for example docintel-dev."
  type        = string
}

variable "cluster_arn" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "image" {
  description = "Full image reference including the tag."
  type        = string
}

variable "cpu" {
  type = number
}

variable "memory" {
  type = number
}

variable "container_port" {
  description = "Port the container listens on. null for a service with no listener, which is how the worker is unreachable by construction."
  type        = number
  default     = null
}

variable "environment" {
  description = "Plain environment variables. Nothing secret belongs here."
  type        = map(string)
  default     = {}
}

variable "health_check_command" {
  description = "Container health check. ECS ignores a Dockerfile HEALTHCHECK, so it has to be restated in the task definition."
  type        = list(string)
  default     = null
}

variable "health_check_start_period" {
  type    = number
  default = 60
}

variable "stop_timeout" {
  description = "Seconds between SIGTERM and SIGKILL. 120 is the Fargate maximum."
  type        = number
  default     = 30
}

variable "execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}

variable "desired_count" {
  type = number
}

variable "max_count" {
  description = "Upper bound for autoscaling. Equal to desired_count means the service does not scale."
  type        = number
}

variable "cpu_target_percent" {
  description = "Target tracking on average CPU. null for none."
  type        = number
  default     = null
}

# Whether a service has a target group or a DNS name is decided by a plain boolean, not by
# testing the ARN or id for null. Those values are unknown until apply, and Terraform has to
# know how many instances of a resource exist while it is still planning.
variable "attach_to_load_balancer" {
  type    = bool
  default = false
}

variable "target_group_arn" {
  description = "Required when attach_to_load_balancer is true."
  type        = string
  default     = null
}

variable "register_in_dns" {
  description = "Register the service's tasks in Cloud Map. Only a service that others call by name needs this."
  type        = bool
  default     = false
}

variable "discovery_namespace_id" {
  description = "Required when register_in_dns is true."
  type        = string
  default     = null
}

variable "discovery_name" {
  description = "The DNS label registered in the namespace. Defaults to the service name."
  type        = string
  default     = null
}

variable "region" {
  type = string
}

variable "log_retention_days" {
  type = number
}
