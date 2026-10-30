output "alb_dns_name" {
  value = aws_lb.this.dns_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.this.repository_url
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.app.name
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "github_deploy_role_arn" {
  value = one(aws_iam_role.github_deploy[*].arn)
}

output "certificate_dns_records" {
  value = [for o in aws_acm_certificate.app.domain_validation_options : {
    purpose = "HTTPS certificate validation (ACM) - required, keep it forever (renewals use it)"
    type    = o.resource_record_type
    name    = trimsuffix(o.resource_record_name, ".")
    value   = trimsuffix(o.resource_record_value, ".")
  }]
}
