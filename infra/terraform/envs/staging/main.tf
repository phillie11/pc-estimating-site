# Staging: a smaller, cheaper copy of prod for trying things (and rehearsing the data move) first.
# Values you are likely to change are in terraform.tfvars; the rest are fixed here.

locals {
  env = "staging"
}

module "siteline" {
  source = "../../modules/siteline"

  env        = local.env
  app_domain = var.app_domain
  vpc_cidr   = "10.30.0.0/16"

  alert_email   = var.alert_email
  support_email = var.support_email
  admin_path    = var.admin_path
  email_enabled = var.email_enabled
  hsts_seconds  = 3600

  # Staging gets its own new files bucket; it never touches the live one
  create_files_bucket = true
  files_malware_scan  = var.files_malware_scan

  stripe_price_starter  = var.stripe_price_starter
  stripe_price_team     = var.stripe_price_team
  stripe_price_business = var.stripe_price_business

  # Small and disposable
  db_instance_class        = "db.t4g.micro"
  db_backup_retention_days = 7
  db_deletion_protection   = false
  db_final_snapshot        = false
  db_apply_immediately     = true
  alb_deletion_protection  = false
  secret_recovery_days     = 0 # deleted secrets go at once, so staging can be rebuilt with the same names

  web_desired_count    = var.web_desired_count
  worker_desired_count = var.worker_desired_count
  enable_schedules     = var.enable_schedules

  github_repository = var.github_repository
}
