output "dns_records_certificate_and_email" {
  description = "Add these at your DNS host (runbook step 12)"
  value       = module.siteline.dns_records_certificate_and_email
}

output "dns_record_app" {
  description = "Add this when you are ready for the domain to point at AWS"
  value       = module.siteline.dns_record_app
}

output "alb_dns_name" {
  value = module.siteline.alb_dns_name
}

output "ecr_repository_url" {
  value = module.siteline.ecr_repository_url
}

output "files_bucket_name" {
  value = module.siteline.files_bucket_name
}

output "stripe_secret_name" {
  value = module.siteline.stripe_secret_name
}

output "github_deploy_role_arn" {
  value = module.siteline.github_deploy_role_arn
}

output "database_instance_id" {
  value = module.siteline.database_instance_id
}

output "log_group_name" {
  value = module.siteline.log_group_name
}
