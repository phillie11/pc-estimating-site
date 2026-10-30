variable "aws_account_id" {
  description = "Your 12-digit AWS account number (top right of the AWS console)"
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be your 12-digit AWS account number. Edit terraform.tfvars."
  }
}

variable "app_domain" {
  type = string
}

variable "alert_email" {
  type = string

  validation {
    condition     = can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", var.alert_email)) && !endswith(var.alert_email, "@example.com")
    error_message = "alert_email must be your real email address. Edit terraform.tfvars."
  }
}

variable "support_email" {
  type = string
}

variable "admin_path" {
  type    = string
  default = "platform-admin"
}

variable "files_bucket_name" {
  description = "Name of your existing uploaded-files bucket (S3 console, or STORAGE_BUCKET in the PythonAnywhere .env)"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.files_bucket_name)) && var.files_bucket_name != "change-me"
    error_message = "files_bucket_name must be the name of your existing files bucket. Edit terraform.tfvars."
  }
}

variable "email_enabled" {
  type    = bool
  default = true
}

variable "hsts_seconds" {
  type    = number
  default = 31536000
}

variable "stripe_price_starter" {
  type    = string
  default = ""
}

variable "stripe_price_team" {
  type    = string
  default = ""
}

variable "stripe_price_business" {
  type    = string
  default = ""
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "web_desired_count" {
  type    = number
  default = 2
}

variable "worker_desired_count" {
  type    = number
  default = 0
}

variable "enable_schedules" {
  type    = bool
  default = true
}

variable "github_repository" {
  type    = string
  default = "phillie11/pc-estimating-site"
}
