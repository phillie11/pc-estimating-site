variable "name" {
  description = "Name prefix, e.g. siteline-prod"
  type        = string
}

variable "env" {
  description = "staging or prod"
  type        = string
}

variable "subnet_ids" {
  description = "Private subnets for the database"
  type        = list(string)
}

variable "security_group_id" {
  description = "Security group that only lets the ECS tasks in"
  type        = string
}

variable "engine_version" {
  description = "PostgreSQL major version; AWS applies minor upgrades in the maintenance window"
  type        = string
  default     = "16"
}

variable "instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "allocated_storage_gb" {
  type    = number
  default = 20
}

variable "max_allocated_storage_gb" {
  description = "Storage grows automatically up to this size"
  type        = number
  default     = 100
}

variable "multi_az" {
  description = "A standby copy in a second zone (roughly doubles the database cost)"
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  description = "Days of automated backups and point-in-time restore (7 to 14 recommended)"
  type        = number
  default     = 7

  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35
    error_message = "Keep at least 7 days of backups (35 is the RDS maximum)."
  }
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "final_snapshot" {
  description = "Take a final snapshot if the database is ever deleted"
  type        = bool
  default     = true
}

variable "apply_immediately" {
  description = "Apply database changes now instead of in the weekly maintenance window"
  type        = bool
  default     = false
}

variable "secret_recovery_days" {
  description = "Days a deleted secret can still be restored"
  type        = number
  default     = 7
}
