output "app_url" {
  description = "Open this from an allowlisted IP."
  value       = "http://${aws_lb.main.dns_name}"
}

output "api_internal_name" {
  description = "Resolves only inside the VPC. The frontend proxies /api to it."
  value       = "api.${var.private_dns_namespace}:8000"
}

output "documents_bucket" {
  value = aws_s3_bucket.documents.bucket
}

output "processing_queue_url" {
  value = aws_sqs_queue.processing.url
}

output "dead_letter_queue_url" {
  value = aws_sqs_queue.dlq.url
}

output "database_endpoint" {
  description = "Private. Reachable from the API and worker security groups only."
  value       = aws_db_instance.main.endpoint
}

output "database_secret_arn" {
  description = "The RDS managed master password. The value is never output."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_services" {
  description = "For `aws ecs wait services-stable` in the deploy pipeline."
  value       = [module.frontend.service_name, module.backend.service_name, module.worker.service_name]
}

output "frontend_target_group_arn" {
  description = "For `aws elbv2 describe-target-health` in the deploy pipeline."
  value       = aws_lb_target_group.frontend.arn
}

output "log_groups" {
  value = {
    frontend = module.frontend.log_group_name
    backend  = module.backend.log_group_name
    worker   = module.worker.log_group_name
  }
}

output "llm_mode" {
  description = "Which provider this environment runs on, and whether the worker has internet egress."
  value       = "${var.llm_provider} (${var.llm_model_id}), nat=${var.enable_nat}"
}
