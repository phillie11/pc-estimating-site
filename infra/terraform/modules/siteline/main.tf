# One complete Siteline environment (staging or prod). envs/staging and envs/prod call this with their own values.

data "aws_caller_identity" "current" {}

locals {
  name         = "siteline-${var.env}"
  email_domain = coalesce(var.email_domain, var.app_domain)
  from_email   = coalesce(var.default_from_email, "${var.product_name} <no-reply@${local.email_domain}>")

  files_bucket_name = var.create_files_bucket ? aws_s3_bucket.files[0].bucket : var.files_bucket_name
}

module "network" {
  source = "../network"

  name       = local.name
  region     = var.region
  cidr_block = var.vpc_cidr
}

module "database" {
  source = "../database"

  name                  = local.name
  env                   = var.env
  subnet_ids            = module.network.private_subnet_ids
  security_group_id     = module.network.db_security_group_id
  instance_class        = var.db_instance_class
  allocated_storage_gb  = var.db_allocated_storage_gb
  multi_az              = var.db_multi_az
  backup_retention_days = var.db_backup_retention_days
  deletion_protection   = var.db_deletion_protection
  final_snapshot        = var.db_final_snapshot
  apply_immediately     = var.db_apply_immediately
  secret_recovery_days  = var.secret_recovery_days
}

module "email" {
  source = "../email"

  name                 = local.name
  env                  = var.env
  region               = var.region
  domain               = local.email_domain
  dmarc_report_email   = var.alert_email
  secret_recovery_days = var.secret_recovery_days
}

# --- App secrets ------------------------------------------------------------------

resource "random_password" "django_secret_key" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "django" {
  name                    = "siteline/${var.env}/django"
  description             = "Siteline ${var.env} Django SECRET_KEY"
  recovery_window_in_days = var.secret_recovery_days
}

resource "aws_secretsmanager_secret_version" "django" {
  secret_id     = aws_secretsmanager_secret.django.id
  secret_string = jsonencode({ SECRET_KEY = random_password.django_secret_key.result })
}

# Stripe keys are typed in by the owner (infra/README.md), never stored in these files.
# Terraform only creates the secret with empty values and then leaves its contents alone.
resource "aws_secretsmanager_secret" "stripe" {
  name                    = "siteline/${var.env}/stripe"
  description             = "Siteline ${var.env} Stripe keys (filled in by hand)"
  recovery_window_in_days = var.secret_recovery_days
}

resource "aws_secretsmanager_secret_version" "stripe" {
  secret_id = aws_secretsmanager_secret.stripe.id
  secret_string = jsonencode({
    STRIPE_SECRET_KEY      = ""
    STRIPE_PUBLISHABLE_KEY = ""
    STRIPE_WEBHOOK_SECRET  = ""
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# --- Staging-only files bucket ---------------------------------------------------------
# Prod uses the existing bucket from siteline/README.md step 9 (referenced by name, never changed here).
# Staging gets its own bucket so tests can never touch live customer files.

resource "aws_s3_bucket" "files" {
  count = var.create_files_bucket ? 1 : 0

  bucket = "${local.name}-files-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "files" {
  count = var.create_files_bucket ? 1 : 0

  bucket                  = aws_s3_bucket.files[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "files" {
  count = var.create_files_bucket ? 1 : 0

  bucket = aws_s3_bucket.files[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "files" {
  count = var.create_files_bucket ? 1 : 0

  bucket = aws_s3_bucket.files[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "files" {
  count = var.create_files_bucket ? 1 : 0

  bucket = aws_s3_bucket.files[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "files" {
  count = var.create_files_bucket ? 1 : 0

  bucket = aws_s3_bucket.files[0].id
  rule {
    id     = "cleanup"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "files" {
  count = var.create_files_bucket ? 1 : 0

  bucket = aws_s3_bucket.files[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "HttpsOnly"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.files[0].arn, "${aws_s3_bucket.files[0].arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.files]
}

# --- The app -----------------------------------------------------------------------

module "app" {
  source = "../app"

  name                  = local.name
  env                   = var.env
  region                = var.region
  vpc_id                = module.network.vpc_id
  public_subnet_ids     = module.network.public_subnet_ids
  alb_security_group_id = module.network.alb_security_group_id
  app_security_group_id = module.network.app_security_group_id
  app_domain            = var.app_domain
  cpu_architecture      = var.cpu_architecture

  files_bucket_name = local.files_bucket_name

  web_cpu              = var.web_cpu
  web_memory           = var.web_memory
  web_desired_count    = var.web_desired_count
  worker_command       = var.worker_command
  worker_desired_count = var.worker_desired_count
  enable_schedules     = var.enable_schedules
  scheduled_jobs = merge(
    {
      # The same jobs as the PythonAnywhere Tasks tab (siteline/README.md steps 8 and 9.12).
      # The 02:00 mysqldump backup is not needed: RDS takes automated backups.
      send-reminders = {
        command  = ["python", "manage.py", "send_reminders"]
        schedule = "cron(0 6 * * ? *)" # 06:00 UTC daily
      }
      check-file-scans = {
        command  = ["python", "manage.py", "check_file_scans"]
        schedule = "cron(0 * * * ? *)" # hourly, on the hour
      }
    },
    # With no worker service, an hourly burst still retries failed emails and prunes old jobs
    { for k, job in {
      run-worker-burst = {
        command  = ["python", "manage.py", "run_worker", "--burst"]
        schedule = "cron(30 * * * ? *)" # hourly, at half past
      }
    } : k => job if var.worker_desired_count == 0 },
  )

  log_retention_days      = var.log_retention_days
  alb_deletion_protection = var.alb_deletion_protection
  github_repository       = var.github_repository

  app_environment = merge(
    {
      DEBUG                = "0"
      ALLOWED_HOSTS        = var.app_domain
      CSRF_TRUSTED_ORIGINS = "https://${var.app_domain}"
      SITE_URL             = "https://${var.app_domain}"
      PRODUCT_NAME         = var.product_name
      SUPPORT_EMAIL        = var.support_email
      ADMIN_PATH           = var.admin_path
      SECURE_HSTS_SECONDS  = tostring(var.hsts_seconds)
      LOG_LEVEL            = "INFO"
      AWS_DEFAULT_REGION   = var.region

      DB_ENGINE  = "postgres"
      DB_HOST    = module.database.address
      DB_PORT    = tostring(module.database.port)
      DB_NAME    = module.database.db_name
      DB_SSLMODE = "require"

      STORAGE_BUCKET       = local.files_bucket_name
      STORAGE_REGION       = var.region
      STORAGE_USE_IAM_ROLE = "1"
      STORAGE_MALWARE_SCAN = var.files_malware_scan ? "1" : "0"

      EMAIL_BACKEND      = var.email_enabled ? "django.core.mail.backends.smtp.EmailBackend" : "django.core.mail.backends.console.EmailBackend"
      EMAIL_HOST         = module.email.smtp_host
      EMAIL_PORT         = "587"
      EMAIL_USE_TLS      = "1"
      DEFAULT_FROM_EMAIL = local.from_email

      # With a worker running, emails go through the outbox; without one, they're sent during the request
      EMAIL_SYNC = var.worker_desired_count > 0 ? "0" : "1"

      STRIPE_PRICE_STARTER  = var.stripe_price_starter
      STRIPE_PRICE_TEAM     = var.stripe_price_team
      STRIPE_PRICE_BUSINESS = var.stripe_price_business
      TRIAL_DAYS            = tostring(var.trial_days)
      PAYMENT_GRACE_DAYS    = tostring(var.payment_grace_days)
    },
    var.extra_environment,
  )

  app_secrets = {
    SECRET_KEY             = "${aws_secretsmanager_secret.django.arn}:SECRET_KEY::"
    DB_USER                = "${module.database.secret_arn}:username::"
    DB_PASSWORD            = "${module.database.secret_arn}:password::"
    EMAIL_HOST_USER        = "${module.email.smtp_secret_arn}:username::"
    EMAIL_HOST_PASSWORD    = "${module.email.smtp_secret_arn}:password::"
    STRIPE_SECRET_KEY      = "${aws_secretsmanager_secret.stripe.arn}:STRIPE_SECRET_KEY::"
    STRIPE_PUBLISHABLE_KEY = "${aws_secretsmanager_secret.stripe.arn}:STRIPE_PUBLISHABLE_KEY::"
    STRIPE_WEBHOOK_SECRET  = "${aws_secretsmanager_secret.stripe.arn}:STRIPE_WEBHOOK_SECRET::"
  }

  secret_arns = [
    aws_secretsmanager_secret.django.arn,
    aws_secretsmanager_secret.stripe.arn,
    module.database.secret_arn,
    module.email.smtp_secret_arn,
  ]
}
