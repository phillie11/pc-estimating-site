variable "env" {
  description = "staging or prod"
  type        = string

  validation {
    condition     = contains(["staging", "prod"], var.env)
    error_message = "env must be staging or prod."
  }
}

variable "region" {
  type    = string
  default = "eu-west-2"
}

variable "app_domain" {
  description = "Where Siteline is served, e.g. app.pc-estimating.co.uk (never the www marketing site)"
  type        = string

  validation {
    condition     = !startswith(var.app_domain, "www.") && var.app_domain != "pc-estimating.co.uk"
    error_message = "app_domain must be a subdomain such as app.pc-estimating.co.uk: www and the bare domain belong to the GitHub Pages marketing site."
  }
}

variable "email_domain" {
  description = "Domain to send email from; empty means the same as app_domain"
  type        = string
  default     = ""
}

variable "default_from_email" {
  description = "From address; empty means \"<product_name> <no-reply@<email_domain>>\""
  type        = string
  default     = ""
}

variable "email_enabled" {
  description = "Send real email through SES. false prints emails to the logs instead (use while staging holds a copy of live data)"
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Your email address (DMARC reports)"
  type        = string
}

variable "product_name" {
  type    = string
  default = "Siteline"
}

variable "support_email" {
  type = string
}

variable "admin_path" {
  description = "URL path of the platform admin; use the same value as ADMIN_PATH on PythonAnywhere"
  type        = string
  default     = "platform-admin"
}

variable "hsts_seconds" {
  description = "How long browsers must insist on HTTPS for app_domain (SECURE_HSTS_SECONDS)"
  type        = number
  default     = 3600
}

variable "trial_days" {
  type    = number
  default = 14
}

variable "payment_grace_days" {
  type    = number
  default = 7
}

variable "stripe_price_starter" {
  description = "Stripe price ID (price_...); not secret"
  type        = string
  default     = ""
}

variable "stripe_price_team" {
  type    = string
  default = ""
}

variable "stripe_price_business" {
  type    = string
  default = ""
}

variable "extra_environment" {
  description = "Any other non-secret environment variables for the app"
  type        = map(string)
  default     = {}
}

# --- Files -----------------------------------------------------------------------

variable "create_files_bucket" {
  description = "true: create a new private bucket for this environment (staging). false: use files_bucket_name (prod)"
  type        = bool
  default     = false
}

variable "files_bucket_name" {
  description = "Name of the EXISTING uploaded-files bucket (siteline/README.md step 9). Only read when create_files_bucket is false"
  type        = string
  default     = ""
}

variable "files_malware_scan" {
  description = "Only hand out files GuardDuty has tagged clean (STORAGE_MALWARE_SCAN). Needs GuardDuty Malware Protection on the bucket"
  type        = bool
  default     = true
}

# --- Network ------------------------------------------------------------------------

variable "vpc_cidr" {
  type = string
}

# --- Database -------------------------------------------------------------------------

variable "db_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 20
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "db_backup_retention_days" {
  type    = number
  default = 7
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

variable "db_final_snapshot" {
  type    = bool
  default = true
}

variable "db_apply_immediately" {
  type    = bool
  default = false
}

# --- Containers ---------------------------------------------------------------------------

variable "cpu_architecture" {
  type    = string
  default = "X86_64"
}

variable "web_cpu" {
  type    = number
  default = 512
}

variable "web_memory" {
  type    = number
  default = 1024
}

variable "web_desired_count" {
  type    = number
  default = 2
}

variable "worker_command" {
  type    = list(string)
  default = ["python", "manage.py", "run_worker"]
}

variable "worker_desired_count" {
  type    = number
  default = 0
}

variable "enable_schedules" {
  type    = bool
  default = true
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "alb_deletion_protection" {
  type    = bool
  default = true
}

variable "secret_recovery_days" {
  type    = number
  default = 7
}

variable "github_repository" {
  description = "owner/repo that may deploy through GitHub Actions; empty for none"
  type        = string
  default     = ""
}
