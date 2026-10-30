output "dns_records_certificate_and_email" {
  description = "Add these at your DNS host first (step A of the runbook)"
  value       = concat(module.app.certificate_dns_records, module.email.dns_records)
}

output "dns_record_app" {
  description = "Add this last: it sends app_domain to AWS"
  value = {
    purpose = "Siteline itself - add when you are ready to switch over"
    type    = "CNAME"
    name    = var.app_domain
    value   = module.app.alb_dns_name
  }
}

output "alb_dns_name" {
  value = module.app.alb_dns_name
}

output "ecr_repository_url" {
  value = module.app.ecr_repository_url
}

output "cluster_name" {
  value = module.app.cluster_name
}

output "log_group_name" {
  value = module.app.log_group_name
}

output "files_bucket_name" {
  value = local.files_bucket_name
}

output "task_role_arn" {
  value = module.app.task_role_arn
}

output "github_deploy_role_arn" {
  value = module.app.github_deploy_role_arn
}

output "stripe_secret_name" {
  value = aws_secretsmanager_secret.stripe.name
}

output "database_instance_id" {
  value = module.database.instance_id
}

output "ses_configuration_set" {
  value = module.email.configuration_set_name
}
