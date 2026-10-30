# Prod settings. Nothing in this file is secret (keys and passwords live in AWS Secrets Manager).
# Change the lines marked CHANGE, save, then run terraform plan / apply (infra/README.md).

aws_account_id = "000000000000"    # CHANGE: your 12-digit AWS account number
alert_email    = "you@example.com" # CHANGE: your email address
support_email  = "support@pc-estimating.co.uk"

# Your EXISTING files bucket (STORAGE_BUCKET in the PythonAnywhere .env). Terraform never changes it.
files_bucket_name = "change-me" # CHANGE

app_domain = "app.pc-estimating.co.uk"

# Same value as ADMIN_PATH in the PythonAnywhere .env
admin_path = "platform-admin"

# Stripe LIVE price IDs (price_...), the same as STRIPE_PRICE_* in the PythonAnywhere .env
stripe_price_starter  = ""
stripe_price_team     = ""
stripe_price_business = ""

# Keep schedules off until the day you switch over, so AWS and PythonAnywhere never both send reminders
enable_schedules = false

web_desired_count    = 2
worker_desired_count = 0 # set 1 once a deploy includes the jobs app (run_worker); see infra/README.md "Turn the background worker on"
db_instance_class    = "db.t4g.small"
db_multi_az          = false # true adds a standby database in a second zone (about +£25/month)
