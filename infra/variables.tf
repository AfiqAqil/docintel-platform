# ---------------------------------------------------------------------------------------
# Identity of the deployment
# ---------------------------------------------------------------------------------------
variable "project" {
  description = "Short name used as a prefix for every resource. Must match the bootstrap stack."
  type        = string
  default     = "docintel"
}

variable "environment" {
  description = "Environment name, for example dev or prod."
  type        = string
}

variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "aws_account_id" {
  description = "The only account this stack may be applied to."
  type        = string
}

# ---------------------------------------------------------------------------------------
# What gets deployed
# ---------------------------------------------------------------------------------------
variable "image_tag" {
  description = "The git SHA the three images were pushed under. Deploying the application is a change to this one variable."
  type        = string
}

# ---------------------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------------------
variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "allowed_cidrs" {
  description = "Who may reach the load balancer. The platform has no end-user authentication, so this list is the access control. Set it in a gitignored *.local.tfvars."
  type        = list(string)

  validation {
    condition     = length(var.allowed_cidrs) > 0
    error_message = "allowed_cidrs is empty, so nobody could reach the load balancer."
  }
  validation {
    condition     = !contains(var.allowed_cidrs, "0.0.0.0/0")
    error_message = "0.0.0.0/0 would open an unauthenticated upload endpoint, and the model bill behind it, to the internet."
  }
}

variable "interface_endpoint_az_count" {
  description = "How many AZs get an interface endpoint network interface. 1 in dev to save cost, 2 in production."
  type        = number
  default     = 1

  validation {
    condition     = contains([1, 2], var.interface_endpoint_az_count)
    error_message = "Must be 1 or 2."
  }
}

variable "enable_nat" {
  description = "Creates one NAT gateway and a default route on the WORKER route table only. Exists for the OpenAI fallback and nothing else."
  type        = bool
  default     = false

  # Fails at plan time. The alternative is a worker that deploys cleanly and then cannot
  # reach its model.
  validation {
    condition     = var.llm_provider != "openai" || var.enable_nat
    error_message = "llm_provider = \"openai\" needs enable_nat = true, or the worker has no route to the OpenAI API."
  }
}

variable "private_dns_namespace" {
  description = "The Cloud Map private DNS namespace. The API is reachable at api.<namespace>."
  type        = string
  default     = "docintel.internal"
}

# ---------------------------------------------------------------------------------------
# LLM provider
# ---------------------------------------------------------------------------------------
variable "llm_provider" {
  description = "bedrock is the target design. openai is the documented fallback."
  type        = string
  default     = "bedrock"

  validation {
    condition     = contains(["bedrock", "openai"], var.llm_provider)
    error_message = "llm_provider must be bedrock or openai."
  }
}

variable "llm_model_id" {
  description = "A Bedrock inference profile id in bedrock mode, an OpenAI model name in openai mode."
  type        = string
  default     = "apac.amazon.nova-pro-v1:0"
}

# ---------------------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------------------
variable "services" {
  description = "CPU units, memory in MiB, and task counts per service."
  type = map(object({
    cpu           = number
    memory        = number
    desired_count = number
    max_count     = number
  }))
  default = {
    frontend = { cpu = 256, memory = 512, desired_count = 1, max_count = 1 }
    backend  = { cpu = 256, memory = 512, desired_count = 1, max_count = 3 }
    worker   = { cpu = 512, memory = 1024, desired_count = 1, max_count = 3 }
  }
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "log_retention_days" {
  type    = number
  default = 14
}

# ---------------------------------------------------------------------------------------
# Queue behaviour. These mirror the worker's own settings, and the worker is told the same
# values through its environment so the two can never disagree.
# ---------------------------------------------------------------------------------------
variable "sqs_visibility_timeout_seconds" {
  type    = number
  default = 120
}

variable "sqs_max_receive_count" {
  description = "Deliveries before a message goes to the dead letter queue."
  type        = number
  default     = 3
}

variable "worker_scale_out_backlog" {
  description = "Visible messages at which one more worker task is added."
  type        = number
  default     = 5
}

# ---------------------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------------------
variable "destroyable" {
  description = "True for a throwaway environment: the bucket can be destroyed with objects in it, and RDS skips its final snapshot and has no deletion protection. False in production."
  type        = bool
  default     = false
}
