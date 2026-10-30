variable "name" {
  description = "Name prefix, e.g. siteline-prod"
  type        = string
}

variable "env" {
  type = string
}

variable "region" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "alb_security_group_id" {
  type = string
}

variable "app_security_group_id" {
  type = string
}

variable "app_domain" {
  description = "Domain the app is served on, e.g. app.pc-estimating.co.uk"
  type        = string
}

variable "app_port" {
  type    = number
  default = 8000
}

variable "health_check_path" {
  type    = string
  default = "/health/"
}

variable "cpu_architecture" {
  description = "X86_64 or ARM64; deploy.sh builds the image for this"
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be X86_64 or ARM64."
  }
}

variable "app_environment" {
  description = "Non-secret environment variables for every task"
  type        = map(string)
}

variable "app_secrets" {
  description = "Environment variable name -> Secrets Manager reference (arn:...:json-key::)"
  type        = map(string)
}

variable "secret_arns" {
  description = "Secrets the tasks may read (whole-secret ARNs)"
  type        = list(string)
}

variable "files_bucket_name" {
  description = "S3 bucket holding uploaded files; the task role gets least-privilege access to it"
  type        = string
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
  description = "Command for the background worker service"
  type        = list(string)
  default     = ["python", "manage.py", "run_worker"]
}

variable "worker_cpu" {
  type    = number
  default = 256
}

variable "worker_memory" {
  type    = number
  default = 512
}

variable "worker_desired_count" {
  description = "Background worker containers (manage.py run_worker). The siteline module sets EMAIL_SYNC=0 when this is above 0"
  type        = number
  default     = 0
}

variable "job_cpu" {
  description = "CPU for migrate, one-off and scheduled tasks"
  type        = number
  default     = 256
}

variable "job_memory" {
  type    = number
  default = 512
}

variable "scheduled_jobs" {
  description = "Scheduled manage.py commands: name -> { command, schedule (EventBridge Scheduler expression, UTC) }"
  type = map(object({
    command  = list(string)
    schedule = string
  }))
}

variable "enable_schedules" {
  description = "Turn the scheduled tasks on or off (off while rehearsing with a copy of live data)"
  type        = bool
  default     = true
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "ecr_keep_images" {
  type    = number
  default = 30
}

variable "alb_deletion_protection" {
  type    = bool
  default = true
}

variable "github_repository" {
  description = "owner/repo allowed to deploy through GitHub Actions (empty = no GitHub role)"
  type        = string
  default     = ""
}
