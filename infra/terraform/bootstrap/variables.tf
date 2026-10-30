variable "aws_account_id" {
  description = "Your 12-digit AWS account number"
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be your 12-digit AWS account number. Edit terraform.tfvars."
  }
}

variable "budget_usd" {
  description = "Monthly AWS spend (USD) that triggers an email: at 80% spent, and when forecast or actual passes 100%"
  type        = number
  default     = 250
}

variable "budget_email" {
  description = "Who gets the budget emails"
  type        = string

  validation {
    condition     = can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", var.budget_email)) && !endswith(var.budget_email, "@example.com")
    error_message = "budget_email must be your real email address. Edit terraform.tfvars."
  }
}

variable "create_github_oidc_provider" {
  description = "Let GitHub Actions sign in to this account (set false if the account already has the GitHub provider)"
  type        = bool
  default     = true
}
