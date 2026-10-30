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

variable "email_enabled" {
  type    = bool
  default = true
}

variable "files_malware_scan" {
  type    = bool
  default = false
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

variable "web_desired_count" {
  type    = number
  default = 1
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
