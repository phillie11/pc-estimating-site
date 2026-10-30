# Amazon SES (London) for outbound email: a verified domain with DKIM, a custom MAIL FROM
# subdomain (so SPF and DMARC line up), a configuration set, and SMTP credentials for Django.
#
# Nothing here is verified until the DNS records in the `dns_records` output are added at the
# domain's DNS host. New SES accounts start in the "sandbox" (only verified addresses can receive);
# infra/README.md explains how to request production access.

resource "aws_sesv2_configuration_set" "this" {
  configuration_set_name = var.name

  reputation_options {
    reputation_metrics_enabled = true
  }

  sending_options {
    sending_enabled = true
  }

  suppression_options {
    suppressed_reasons = ["BOUNCE", "COMPLAINT"]
  }
}

resource "aws_sesv2_email_identity" "domain" {
  email_identity = var.domain

  # Every email sent as this domain uses the configuration set, including over SMTP
  configuration_set_name = aws_sesv2_configuration_set.this.configuration_set_name

  dkim_signing_attributes {
    next_signing_key_length = "RSA_2048_BIT"
  }
}

resource "aws_sesv2_email_identity_mail_from_attributes" "domain" {
  email_identity         = aws_sesv2_email_identity.domain.email_identity
  mail_from_domain       = "${var.mail_from_subdomain}.${var.domain}"
  behavior_on_mx_failure = "USE_DEFAULT_VALUE"
}

# --- SMTP credentials --------------------------------------------------------
# SES SMTP needs an IAM user's access key; the SMTP password is derived from it.
# The user can only send email as this domain through this configuration set.

resource "aws_iam_user" "smtp" {
  name = "${var.name}-ses-smtp"
  path = "/siteline/"
}

resource "aws_iam_user_policy" "smtp" {
  name = "send-email-as-${replace(var.domain, ".", "-")}"
  user = aws_iam_user.smtp.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "SendAsSitelineDomainOnly"
      Effect = "Allow"
      Action = ["ses:SendRawEmail", "ses:SendEmail"]
      Resource = [
        aws_sesv2_email_identity.domain.arn,
        aws_sesv2_configuration_set.this.arn,
      ]
    }]
  })
}

resource "aws_iam_access_key" "smtp" {
  user = aws_iam_user.smtp.name
}

resource "aws_secretsmanager_secret" "smtp" {
  name                    = "siteline/${var.env}/smtp"
  description             = "Siteline ${var.env} SES SMTP username and password"
  recovery_window_in_days = var.secret_recovery_days
}

resource "aws_secretsmanager_secret_version" "smtp" {
  secret_id = aws_secretsmanager_secret.smtp.id
  secret_string = jsonencode({
    username = aws_iam_access_key.smtp.id
    password = aws_iam_access_key.smtp.ses_smtp_password_v4
  })
}
