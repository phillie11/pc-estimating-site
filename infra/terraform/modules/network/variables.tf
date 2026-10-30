variable "name" {
  description = "Name prefix, e.g. siteline-prod"
  type        = string
}

variable "region" {
  description = "AWS region"
  type        = string
}

variable "cidr_block" {
  description = "VPC address range"
  type        = string
}

variable "az_count" {
  description = "Number of availability zones (the ALB and RDS subnet group need at least 2)"
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "app_port" {
  description = "Port gunicorn listens on inside the task"
  type        = number
  default     = 8000
}
