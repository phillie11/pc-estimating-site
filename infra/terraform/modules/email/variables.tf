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

variable "domain" {
  description = "Domain emails are sent from (e.g. app.pc-estimating.co.uk sends as no-reply@app.pc-estimating.co.uk)"
  type        = string
}

variable "mail_from_subdomain" {
  description = "Subdomain used as the bounce (MAIL FROM) address, e.g. mail -> mail.<domain>"
  type        = string
  default     = "mail"
}

variable "dmarc_report_email" {
  description = "Address for DMARC aggregate reports in the suggested DMARC record"
  type        = string
}

variable "secret_recovery_days" {
  type    = number
  default = 7
}
