# Staging settings. Nothing in this file is secret (keys and passwords live in AWS Secrets Manager).
# Change the lines marked CHANGE, save, then run terraform plan / apply (infra/README.md).

aws_account_id = "000000000000"    # CHANGE: your 12-digit AWS account number
alert_email    = "you@example.com" # CHANGE: your email address
support_email  = "support@pc-estimating.co.uk"

app_domain = "staging.app.pc-estimating.co.uk"

# Same value as ADMIN_PATH in the PythonAnywhere .env
admin_path = "platform-admin"

# Set to false BEFORE loading a copy of live data into staging (data move rehearsal),
# so staging cannot email real customers. Emails then appear in the logs instead.
email_enabled = true
# Also set to false during the rehearsal, so the daily reminders do not run against live data
enable_schedules = true

# Staging's own files bucket has no GuardDuty scanning unless you switch it on (README step 9.7) and set this to true
files_malware_scan = false

# Stripe TEST mode price IDs (price_...) for staging
stripe_price_starter  = ""
stripe_price_team     = ""
stripe_price_business = ""

web_desired_count    = 1
worker_desired_count = 0 # the background worker does not exist in the app yet
