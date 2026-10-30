output "smtp_host" {
  value = "email-smtp.${var.region}.amazonaws.com"
}

output "smtp_secret_arn" {
  value = aws_secretsmanager_secret.smtp.arn
}

output "configuration_set_name" {
  value = aws_sesv2_configuration_set.this.configuration_set_name
}

# DNS records to add at the domain's DNS host (the owner's DNS is not in AWS)
output "dns_records" {
  value = concat(
    [for token in aws_sesv2_email_identity.domain.dkim_signing_attributes[0].tokens : {
      purpose = "Email signing (DKIM) - required"
      type    = "CNAME"
      name    = "${token}._domainkey.${var.domain}"
      value   = "${token}.dkim.amazonses.com"
    }],
    [
      {
        purpose = "Email bounces (MAIL FROM) - required"
        type    = "MX"
        name    = "${var.mail_from_subdomain}.${var.domain}"
        value   = "10 feedback-smtp.${var.region}.amazonses.com"
      },
      {
        purpose = "Email sender check (SPF) - required"
        type    = "TXT"
        name    = "${var.mail_from_subdomain}.${var.domain}"
        value   = "v=spf1 include:amazonses.com ~all"
      },
      {
        purpose = "Email policy (DMARC) - recommended; skip if the domain already has a _dmarc record"
        type    = "TXT"
        name    = "_dmarc.${var.domain}"
        value   = "v=DMARC1; p=none; rua=mailto:${var.dmarc_report_email}"
      },
    ],
  )
}
