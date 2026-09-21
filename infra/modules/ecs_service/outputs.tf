output "service_name" {
  value = aws_ecs_service.this.name
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.this.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.this.name
}

output "autoscaling_resource_id" {
  description = "null when the service does not scale."
  value       = try(aws_appautoscaling_target.this[0].resource_id, null)
}
