# Prod: the live Siteline. Values you are likely to change are in terraform.tfvars; the rest are fixed here.

locals {
  env = "prod"
}

module "siteline" {
  source = "../../modules/siteline"

  env        = local.env
  app_domain = var.app_domain
  vpc_cidr   = "10.20.0.0/16"

  alert_email   = var.alert_email
  support_email = var.support_email
  admin_path    = var.admin_path
  email_enabled = var.email_enabled
  hsts_seconds  = var.hsts_seconds

  # The EXISTING bucket from siteline/README.md step 9. Terraform only gives the app access to it.
  create_files_bucket = false
  files_bucket_name   = var.files_bucket_name
  files_malware_scan  = true

  stripe_price_starter  = var.stripe_price_starter
  stripe_price_team     = var.stripe_price_team
  stripe_price_business = var.stripe_price_business

  db_instance_class        = var.db_instance_class
  db_multi_az              = var.db_multi_az
  db_backup_retention_days = 14
  db_deletion_protection   = true
  db_final_snapshot        = true
  db_apply_immediately     = false
  alb_deletion_protection  = true
  secret_recovery_days     = 7

  web_desired_count    = var.web_desired_count
  worker_desired_count = var.worker_desired_count
  enable_schedules     = var.enable_schedules

  github_repository = var.github_repository
}
