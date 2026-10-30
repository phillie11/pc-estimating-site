# Siteline on AWS (London)

This folder moves Siteline from PythonAnywhere + MySQL to Amazon Web Services in London (`eu-west-2`). You run every step yourself, on your Windows PC, in your own AWS account. Nothing here contains a password or key: those are created in AWS, or typed into the AWS console by you.

There are two copies of Siteline:

- **staging** (`staging.app.pc-estimating.co.uk`): a smaller copy for trying things first. Nobody else uses it.
- **prod** (`app.pc-estimating.co.uk`): the live site your customers use.

Your marketing site (`www.pc-estimating.co.uk` and `pc-estimating.co.uk`, on GitHub Pages) is never touched. Only add the DNS records this guide lists.

Steps 1 to 7 are set-up and cost nothing. AWS starts charging at step 11 (staging, about £2 a day) and step 18 (prod, about £3.50 a day). The full list is in [What it costs](#what-it-costs).

## What gets built

```
Internet ──HTTPS──> Load balancer (ALB) ──> web: 2 x gunicorn containers (ECS Fargate) ──> PostgreSQL 16 (RDS, private)
                         │ HTTP is redirected to HTTPS      │
                         │                                  ├──> S3: your existing files bucket (no keys: the containers' own role)
                         │                                  ├──> SES: outgoing email
                         │                                  └──> Stripe
 Scheduled: send_reminders 06:00 UTC daily, check_file_scans hourly (EventBridge Scheduler -> one-off containers)
```

- Containers sit in public subnets with their own addresses, so there is no NAT gateway (saves about £30 a month per copy). Only the load balancer can reach them, and only they can reach the database.
- Passwords and keys live in **AWS Secrets Manager**: Django's `SECRET_KEY`, the database password, the email password and your Stripe keys.
- The database is encrypted, refuses unencrypted connections, keeps automatic backups you can restore to any second (7 days on staging, 14 on prod) and, on prod, can't be deleted by accident.
- Logs are kept 30 days in **CloudWatch**.
- Your existing S3 bucket and GuardDuty scanning (from `siteline/README.md` step 9) are reused as they are. Terraform never changes that bucket. Staging gets its own new, empty bucket.

## The files

```
infra/
  README.md                this guide
  terraform/
    bootstrap/             run once: storage for Terraform's own records, GitHub sign-in, budget alert
    envs/staging/          staging: terraform.tfvars holds the settings you edit
    envs/prod/             prod: terraform.tfvars holds the settings you edit
    modules/               the building blocks both use (network, database, email, app)
  scripts/
    deploy.ps1             build and roll out a new version (deploy.sh: same, for Mac/Linux/GitHub)
    run-command.ps1        run a manage.py command on AWS (run-command.sh)
    load-data.ps1          load a database export into AWS (load-data.sh)
    compare-counts.ps1     check two row-count lists match (compare-counts.sh)
    migrate-data.md        moving the data from PythonAnywhere, and switching over
```

**Terraform** is the program that builds everything from these files and remembers what it built. `terraform plan` shows what it would change; `terraform apply` shows the same and asks you to type `yes`.

---

## 1. Install the tools (once)

Open **PowerShell** as administrator: Start menu → type `PowerShell` → right-click **Windows PowerShell** → *Run as administrator*. Paste these lines one at a time, and say yes to any prompts:

```powershell
winget install --id Git.Git -e
winget install --id Hashicorp.Terraform -e
winget install --id Amazon.AWSCLI -e
winget install --id Amazon.SessionManagerPlugin -e
wsl --install --no-distribution
winget install --id Docker.DockerDesktop -e
```

**Restart the PC.** Then start **Docker Desktop** from the Start menu, accept its terms (it's free for businesses with fewer than 250 staff and under $10m turnover), and check **Settings → General → Use the WSL 2 based engine** is ticked. Docker Desktop must be running whenever you deploy.

If `winget` isn't found, install **App Installer** from the Microsoft Store, or use these installers instead: [Git](https://git-scm.com/download/win), [Terraform](https://developer.hashicorp.com/terraform/install#windows) (unzip `terraform.exe` into `C:\Program Files\Terraform` and add that folder to *Path*), [AWS CLI v2](https://awscli.amazonaws.com/AWSCLIV2.msi), [Session Manager plugin](https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe), [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/).

Check in a **new**, normal (not administrator) PowerShell window. Each line should print a version:

```powershell
git --version
terraform -version
aws --version
docker version
session-manager-plugin --version
```

Terraform must be 1.10 or newer; AWS CLI must start `aws-cli/2`.

## 2. Every time you open PowerShell for this

Use a normal PowerShell window (not administrator) and paste these three lines first. The first lets this window run the scripts in this folder. The second picks the AWS sign-in you make in step 5. The third signs you in through the browser, and lasts 4 hours.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
$env:AWS_PROFILE = "siteline-admin"
aws sso login
```

Then go to the code folder (step 3): `cd $HOME\pc-estimating-site`

## 3. Get the code (once)

```powershell
cd $HOME
git clone https://github.com/phillie11/pc-estimating-site.git
cd pc-estimating-site
```

Sign in to GitHub if asked. The commands in this guide assume you are in this `pc-estimating-site` folder unless they start with `cd`.

## 4. A safe admin sign-in for AWS (once)

Don't use the root login (the email address you opened the account with) for this. Instead make a personal admin sign-in that always asks for a code from your phone.

1. Sign in to https://console.aws.amazon.com as root. Top right, choose **Europe (London)**.
2. Search for **IAM Identity Center** → **Enable** → *Enable with AWS Organizations* → **Continue**. This is free.
3. **Users → Add user**: your email address as username and email, your name, then **Next** → skip groups → **Add user**.
4. **Permission sets → Create permission set** → *Predefined* → **AdministratorAccess** → **Next** → *Session duration* **4 hours** → **Next** → **Create**.
5. **AWS accounts** → tick your account → **Assign users or groups** → *Users* tab → tick yourself → **Next** → tick **AdministratorAccess** → **Next** → **Submit**.
6. **Settings → Authentication → Multi-factor authentication → Configure**: *Every time they sign in*, *Authenticator apps* and *Security keys*, and **Require them to register an MFA device at sign in**. **Save**.
7. **Dashboard**: copy the **AWS access portal URL** (it looks like `https://d-1234567890.awsapps.com/start`).
8. Open the invitation email AWS sent you, set a password, and register your phone's authenticator app (or a passkey). Sign out of root. From now on, sign in through the access portal URL.

## 5. Connect PowerShell to AWS (once)

```powershell
aws configure sso
```

Answer:

| Question | Answer |
|---|---|
| SSO session name | `siteline` |
| SSO start URL | the access portal URL from step 4.7 |
| SSO region | `eu-west-2` |
| SSO registration scopes | press Enter |

A browser opens: sign in, then **Allow**. Back in PowerShell, pick your account and **AdministratorAccess**, then answer `eu-west-2` (default client region), `json` (output format) and `siteline-admin` (profile name).

Check it works (after the step 2 lines):

```powershell
aws sts get-caller-identity
```

The `Account` number it prints is your **12-digit AWS account number**. You need it in step 6.

## 6. Fill in your settings (once)

Open these three files in Notepad and change every line marked `CHANGE` (your account number and email address). Nothing in them is secret.

```powershell
notepad infra\terraform\bootstrap\terraform.tfvars
notepad infra\terraform\envs\staging\terraform.tfvars
notepad infra\terraform\envs\prod\terraform.tfvars
```

In the **prod** file also set:

- `files_bucket_name`: your existing files bucket (the `STORAGE_BUCKET` value in the PythonAnywhere `.env`, or see S3 in the AWS console).
- `admin_path`: the same as `ADMIN_PATH` in the PythonAnywhere `.env`.
- The three `stripe_price_...` lines: your **live** Stripe price IDs (the `STRIPE_PRICE_...` values in the PythonAnywhere `.env`).

In the **staging** file, the `stripe_price_...` lines are your Stripe **test mode** price IDs (Stripe Dashboard → turn on *Test mode* → Products). You can leave them empty for now.

## 7. Bootstrap (once, about a minute, costs pennies)

This makes a private bucket where Terraform keeps its records of what it built for staging and prod, lets GitHub deploy without stored keys (optional, see [Deploying from GitHub](#deploying-from-github)), and sets a monthly budget alert. The alert emails you at 80% and 100% of `budget_usd` (250 USD; change it in the bootstrap `terraform.tfvars`).

```powershell
cd infra\terraform\bootstrap
terraform init
terraform apply
```

Type `yes`. At the end it prints `init_command`, a line like `terraform init "-backend-config=bucket=siteline-terraform-state-123456789012"`. Copy it: you use it in steps 8 and 18.

Keep the file `terraform.tfstate` that appears in this folder (it is ignored by git; copy it somewhere safe).

You set a 20 USD budget in `siteline/README.md` step 9.4. It will now email you every month, so raise it to 250 or delete it: AWS console → **Billing and Cost Management → Budgets**.

```powershell
cd ..\..\..
```

## 8. Staging: get Terraform ready

```powershell
cd infra\terraform\envs\staging
terraform init "-backend-config=bucket=siteline-terraform-state-123456789012"
```

Use the line you copied in step 7 (your account number, not `123456789012`). It downloads the AWS plug-in and says **Terraform has been successfully initialized**.

## 9. Staging: certificate and email first (free)

The HTTPS certificate and the email domain only work once you've added DNS records for them, so make those two first:

```powershell
terraform apply "-target=module.siteline.module.app.aws_acm_certificate.app" "-target=module.siteline.module.email"
```

Type `yes`. Terraform warns that `-target` is for exceptional cases: that's expected here. Then list the records to add:

```powershell
terraform output dns_records_certificate_and_email
```

If that prints nothing, find them in the AWS console instead: **Certificate Manager** → the certificate → *Domains* (CNAME name and value), and **Amazon SES → Identities** → the domain → *DomainKeys Identified Mail* (3 CNAMEs) and *Custom MAIL FROM domain* (MX and TXT).

## 10. Add the DNS records

Sign in where `pc-estimating.co.uk`'s DNS is managed (your domain registrar or DNS host: the place you added the GitHub Pages records). For each record in the list, add a new record with that **type**, **name** and **value**:

- **Name**: many DNS hosts add `.pc-estimating.co.uk` themselves. If yours does, enter only the part before it (`_3f2a...staging.app`, not `_3f2a...staging.app.pc-estimating.co.uk`). Check by looking at an existing record.
- **MX** records: priority `10`, value `feedback-smtp.eu-west-2.amazonses.com`.
- **TXT** values: paste them exactly, without extra quotes if the host adds its own.
- The DMARC record is recommended. Skip it if a `_dmarc` record for that name already exists.
- Don't change or delete any existing record, especially `www`, `@`/bare domain, and anything for email you already use.

Records usually work within 10 to 60 minutes. Check:

```powershell
aws acm list-certificates --query "CertificateSummaryList[].[DomainName,Status]" --output table
aws sesv2 get-email-identity --email-identity staging.app.pc-estimating.co.uk --query "{verified:VerifiedForSendingStatus,dkim:DkimAttributes.Status}"
```

Wait until the certificate says `ISSUED` and email says `true` / `SUCCESS`. Keep these records for good: the certificate renews itself every year using them.

## 11. Staging: build everything (about 15 minutes; charges start)

```powershell
terraform plan
```

Read the summary on the last line: it should say a number to add, `0 to change` and `0 to destroy`. Then:

```powershell
terraform apply
```

Type `yes`. The database takes the longest. **From now on staging costs about £2 a day** (see [What it costs](#what-it-costs)). Until step 13, the site shows an error page: there is no app to run yet.

## 12. Stripe keys (staging: test keys)

The keys go into AWS directly in your browser, so they're never in a file or your PowerShell history.

1. Stripe Dashboard → *Test mode* on → **Developers → API keys**. Keep the tab open.
2. AWS console (Europe London) → **Secrets Manager** → `siteline/staging/stripe` → **Retrieve secret value** → **Edit**.
3. Fill in `STRIPE_SECRET_KEY` (`sk_test_...`) and `STRIPE_PUBLISHABLE_KEY` (`pk_test_...`). For `STRIPE_WEBHOOK_SECRET`: Stripe → **Developers → Webhooks → Add endpoint** → `https://staging.app.pc-estimating.co.uk/billing/webhook/` with the events in `siteline/README.md` step 7.3 → copy its *Signing secret* (`whsec_...`). **Save**.

The app reads the keys when it starts, so do this before step 13, or run step 13 again afterwards.

## 13. Deploy staging

Docker Desktop must be running. Go back to the top folder and deploy:

```powershell
cd ..\..\..\..
.\infra\scripts\deploy.ps1 staging
```

The first time takes about 10 minutes (building the image); later ones 3 to 5. It builds the app, uploads it, updates the database tables, starts the new version and waits until it's healthy. It ends with **Deployed ... to staging**. If it stops with an error, the old version (if any) keeps running; see [When something goes wrong](#when-something-goes-wrong).

Deploy only from committed code: run `git pull` first to get the latest. Prod refuses uncommitted changes.

## 14. Point the staging address at AWS

```powershell
cd infra\terraform\envs\staging
terraform output dns_record_app
cd ..\..\..\..
```

At your DNS host add that **CNAME**: name `staging.app` (or the full name, as in step 10), value the `...eu-west-2.elb.amazonaws.com` address. After a few minutes, https://staging.app.pc-estimating.co.uk/health/ shows `ok`.

## 15. Make yourself the platform admin on staging

```powershell
.\infra\scripts\run-command.ps1 staging createsuperuser -Exec
```

It asks for your email and a password (at least 10 characters). `-Exec` opens a live session inside the running app, so the password never gets saved anywhere.

## 16. Smoke test staging

SES starts in a **sandbox**: it only delivers to addresses you have verified. Verify your own address first:

```powershell
aws sesv2 create-email-identity --email-identity you@yourcompany.co.uk
```

(use your address) and click the link in the email AWS sends. Then, in a browser:

1. https://staging.app.pc-estimating.co.uk/login/ → log in.
2. `https://staging.app.pc-estimating.co.uk/platform-admin/` (or your `admin_path`) → the admin opens.
3. **Sign up** a test company → create a project → upload a document → open it (the address bar briefly shows `s3.eu-west-2.amazonaws.com`).
4. Log out → *Forgot password* with your verified email → the email arrives from `no-reply@staging.app.pc-estimating.co.uk`.
5. Billing → start a subscription with card `4242 4242 4242 4242` → the plan shows as active (proves the webhook works).
6. The scheduled jobs work:
   ```powershell
   .\infra\scripts\run-command.ps1 staging send_reminders
   .\infra\scripts\run-command.ps1 staging check_file_scans
   ```
7. Logs from the last 15 minutes (Ctrl+C to stop):
   ```powershell
   aws logs tail /ecs/siteline-staging --since 15m --follow
   ```

## 17. Ask AWS to let you email anyone (once, 1 to 2 days)

Until this is done, Siteline can't email customers. It covers staging and prod (one request per AWS account).

1. AWS console (Europe London) → **Amazon SES → Get set up** (or *Account dashboard*) → **Request production access**.
2. *Mail type*: **Transactional**. *Website URL*: `https://www.pc-estimating.co.uk`.
3. *Use case description*, for example:
   > Siteline is a project portal for construction contractors. It sends transactional emails only: notifications about tenders, RFIs, variations and tasks to users of the portal, invitations they have been sent by their contractor, and password resets. No marketing email. Recipients are registered users or people invited by name. Bounces and complaints are suppressed automatically through an SES configuration set, and we expect under 2,000 emails a month.
4. Tick the acknowledgement → **Submit**. AWS replies by email, usually within a day. If they ask questions, answer in the AWS Support case.

## 18. Prod

The same as staging, in the prod folder. Charges for prod start at the `terraform apply` in this step, about £3.50 a day.

```powershell
cd infra\terraform\envs\prod
terraform init "-backend-config=bucket=siteline-terraform-state-123456789012"
terraform apply "-target=module.siteline.module.app.aws_acm_certificate.app" "-target=module.siteline.module.email"
terraform output dns_records_certificate_and_email
```

Add those records as in step 10 (they are for `app.pc-estimating.co.uk`). **Do not add or change the `app` record itself yet**: that's the switch-over in the data move. When the certificate is `ISSUED` and the email domain verified (step 10's check, with `app.pc-estimating.co.uk`):

```powershell
terraform plan
terraform apply
cd ..\..\..\..
```

Then:

1. **Stripe live keys** into `siteline/prod/stripe` as in step 12, with *Test mode* off: your **live** `sk_live_...` and `pk_live_...` keys (the same as in the PythonAnywhere `.env`), and the **same** `STRIPE_WEBHOOK_SECRET` as in the PythonAnywhere `.env` if the address stays `app.pc-estimating.co.uk`.
2. **Deploy**: `.\infra\scripts\deploy.ps1 prod`
3. Don't create a superuser on prod: your admin login arrives with the data.

The scheduled jobs on prod stay off (`enable_schedules = false`) until the switch-over, so AWS and PythonAnywhere never both send reminders.

## 19. Move the data and switch over

Follow [scripts/migrate-data.md](scripts/migrate-data.md): a rehearsal on staging, then the real move during a short freeze, the DNS switch, and how to roll back.

## 20. Two weeks later

The end of [scripts/migrate-data.md](scripts/migrate-data.md): close PythonAnywhere and switch off the old `siteline-app` access key.

---

## Everyday tasks

Start every session with the step 2 lines.

| To... | Run |
|---|---|
| Release new code to staging, then prod | `git pull`, then `.\infra\scripts\deploy.ps1 staging`, test, then `.\infra\scripts\deploy.ps1 prod` |
| Go back to an earlier version | `.\infra\scripts\deploy.ps1 prod -ImageTag 1a2b3c4d5e6f` (the tag from that deploy's last line). Database changes are not undone. |
| Run a manage.py command | `.\infra\scripts\run-command.ps1 prod "axes_reset"` (quotes round the command if it has several words) |
| Watch the logs | `aws logs tail /ecs/siteline-prod --since 1h --follow` |
| Change a setting in `terraform.tfvars` | `cd infra\terraform\envs\prod`, `terraform plan`, `terraform apply`, then deploy again so the app sees it |
| Change a Stripe key | Secrets Manager (step 12), then deploy again |
| Restore the database to a moment in time | RDS console → `siteline-prod` → **Actions → Restore to point in time**. It makes a *new* database; ask an engineer to swap it in. |
| Turn the background worker on (once the app has one) | `worker_desired_count = 1` in `terraform.tfvars`, `terraform apply` |
| Pause staging to save money | In the staging `terraform.tfvars` set `web_desired_count = 0`, `terraform apply`. The database and load balancer still cost about £1 a day. Remove staging completely with `terraform destroy` in `infra\terraform\envs\staging`. |

Never run `terraform destroy` in the prod folder. Prod's database and load balancer are protected against it.

## Deploying from GitHub

Optional: deploy with a button in GitHub instead of from your PC. Terraform has already made a deploy role that only trusts this repository's *Deploy Siteline to AWS* workflow, so no AWS keys are stored in GitHub.

1. GitHub → the repository → **Settings → Environments → New environment** `staging`. Add an environment variable `AWS_DEPLOY_ROLE_ARN` with the value from `terraform output github_deploy_role_arn` in `infra\terraform\envs\staging`.
2. The same for `prod` (from `infra\terraform\envs\prod`). In `prod`, also tick **Required reviewers** (add yourself) and set **Deployment branches** to *Selected branches* → `main`.
3. **Actions → Deploy Siteline to AWS → Run workflow** → pick the environment. Prod waits for your approval. It never runs by itself on a push.

## What it costs

Estimates for London (`eu-west-2`) at late-2026 list prices, in US dollars as AWS bills, converted at $1.30 = £1. AWS adds **20% UK VAT** unless your VAT number is in the account settings. Low traffic assumed; check with the [AWS Pricing Calculator](https://calculator.aws/) if in doubt. **Bills → Cost Explorer**, grouped by *Tag: Env*, shows actual spend once you activate the `Env` and `Project` tags under **Billing → Cost allocation tags** (do that once; it takes a day to appear).

| Item | Prod | Staging | Starts |
|---|---:|---:|---|
| Web containers (Fargate: 0.5 vCPU, 1 GB each; prod 2, staging 1) | $41.50 | $20.75 | step 11 / 18 |
| Scheduled jobs (a minute or two each hour) | $0.50 | $0.50 | same |
| Load balancer (fixed hourly charge + light traffic) | $25 | $22 | same |
| Public IP addresses (load balancer 2, plus 1 per container) | $14.60 | $11 | same |
| Database (prod db.t4g.small, staging db.t4g.micro, 20 GB, backups) | $30 | $16 | same |
| Secrets Manager (4 secrets) | $1.60 | $1.60 | same |
| CloudWatch logs (a few GB) | $2 | $1 | same |
| Container images (ECR) | $0.30 | $0.30 | step 13 |
| Email (SES, $0.10 per 1,000) | $1 | $0 | when sending |
| Files bucket and GuardDuty scanning | as now | under $1 | already running |
| Terraform state, budget, schedules, S3 endpoint, certificate | $0 | $0 | |
| **Total before VAT** | **about $116 (£90)** | **about $73 (£56)** | |
| **Total with VAT** | **about £108** | **about £67** | |

Options that change it: `db_multi_az = true` (a standby database in a second data centre, so prod survives one failing) adds about $30 (£23) a month. Busier traffic raises the load balancer and data transfer a little. Downloads of files go straight from S3: the first 100 GB a month leaving AWS is free, then about $0.09 per GB. The architect's planning figure of £120 to £180 a month for prod allows for Multi-AZ, VAT and growth.

## When something goes wrong

| Message or symptom | What to do |
|---|---|
| `... cannot be loaded because running scripts is disabled` | Paste `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` (step 2). |
| `Error loading SSO Token` / `ExpiredToken` | `aws sso login` |
| `could not read /siteline/staging/deploy` | You're signed in to the wrong account, or `terraform apply` hasn't finished for that environment. |
| `docker login failed` / `error during connect` | Start Docker Desktop and wait until it says *Engine running*. |
| `terraform apply` waits a long time at `aws_acm_certificate_validation` | The certificate DNS record (step 10) is missing or mistyped. It gives up after 45 minutes; fix the record and run `terraform apply` again. |
| `Error acquiring the state lock` | Another `terraform` command is running (maybe in another window). If you're sure none is, `terraform force-unlock` with the ID it shows. |
| Deploy says migrations failed | Read the lines above the error (they're Django's). The site is still on the previous version. |
| Deploy says web did not switch to the new version | The new version kept crashing and ECS put the old one back. `aws logs tail /ecs/siteline-prod --since 30m` shows why (often a setting or secret). |
| The site shows 503 | No healthy containers: check the logs as above, then deploy again. |
| Emails don't arrive | SES still in the sandbox (step 17), or the domain not verified (step 10's check). |
| `run-command.ps1 ... -Exec` says *SessionManagerPlugin is not found* | Install the plug-in (step 1) and open a new PowerShell window. |

If you're stuck, send an engineer the full error text and which step you were on. Never send the contents of Secrets Manager.

## For engineers

- **Image tags.** Terraform builds every task definition from the image tag in the SSM parameter `/siteline/<env>/image_tag`. `deploy.ps1` / `deploy.sh` push the image, register a new revision of the `migrate` family and run it, write the tag, register the other families, update the services and wait. Task definitions use `track_latest`, so Terraform accepts the deploy's revisions and never rolls the image back. Scheduled jobs reference the family without a revision, so they always run the latest. Old revisions are kept (`skip_destroy`) for rollbacks; ECR keeps the last 30 images.
- **Settings.** Non-secret settings are container environment variables set in `modules/siteline/main.tf`. Secrets are injected from Secrets Manager (`siteline/<env>/django`, `db`, `smtp`, `stripe`). `STORAGE_USE_IAM_ROLE=1` makes S3 use the task role. `DB_SSLMODE=require`.
- **Database password.** Generated by Terraform and stored in Secrets Manager, not the RDS-managed master password. RDS rotates its managed password every 7 days, and the app reads `DB_PASSWORD` only when a task starts, so running tasks would lose the database every week. The state bucket is private, encrypted and versioned because the password is in the state. To rotate: `terraform apply -replace="module.siteline.module.database.random_password.db"`, then deploy.
- **ALB and Django.** `config/alb_wsgi.py` (used only by the Docker image) answers ALB health checks on `/health/` (they come with the task IP as Host, over HTTP) and sets `REMOTE_ADDR` from the last `X-Forwarded-For` entry, so django-axes locks per real client IP.
- **Locking.** Terraform state uses S3 native locking (`use_lockfile`), not a DynamoDB table: Terraform deprecated DynamoDB locking in 1.11 and warns on every run.
- **Offline checks.** `terraform fmt -check -recursive`, then in each of `bootstrap`, `envs/staging` and `envs/prod`: `terraform init -backend=false` and `terraform validate`. In the two env folders, `terraform test` plans and applies against a mocked AWS provider.
- **macOS / Linux.** Install with `brew install git terraform awscli jq` plus `brew install --cask session-manager-plugin docker`, sign in the same way (`aws configure sso`, `export AWS_PROFILE=siteline-admin`), and use the `.sh` scripts: `infra/scripts/deploy.sh staging`, `infra/scripts/run-command.sh prod --exec createsuperuser`, `infra/scripts/load-data.sh`, `infra/scripts/compare-counts.sh`.
