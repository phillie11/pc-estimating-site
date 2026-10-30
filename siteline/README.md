# Siteline

A multi-tenant project portal for small fit-out and build contractors. Each company gets its own workspace (`/company-name/`), runs bids and live jobs in it, and invites clients, consultants and subcontractors in for free.

`Siteline` is a placeholder name. Change it with `PRODUCT_NAME` in `.env`.

## Two halves: estimating and projects

Siteline is two connected systems in one workspace:

- **Estimating**: bids from lead to result, subcontract tenders run through the portal, the bid pipeline, tender workload and tender analytics.
- **Projects**: won and live jobs, with procurement, programme, RFIs, client and subcontractor variations, RAMS, snags and drawings.

Each staff member is set to *Estimating*, *Projects* or both on the Team page; owners and admins always see both and switch with the toggle at the top of the sidebar. A project's stage decides which half it lives in, and `projects.access.visible_projects` enforces it, so an estimator cannot open a live job and the projects team cannot open a bid.

The join is the **handover**. When a bid is won, estimating picks a project manager, confirms the contract sum and dates, and writes handover notes. The project manager is notified, and the job arrives in Projects with its documents and the subcontract quotes from the tender. Every route to *Won* (pipeline drag, stage select, edit form) goes through the handover.

## What's in it

| Area | What it does |
|---|---|
| **Bid pipeline** | Drag-and-drop board from Lead → Tendering → Submitted → Won, with a "Move to" control on every card for touch and keyboard users (moving to Won still opens the handover). Weighted pipeline, 12-month win rate, tender return deadlines. |
| **Subcontract tenders (portal)** | Invite subcontractors to price a trade package. They get a notification with a link, never attachments: scope, tender documents, addenda (with acknowledgement tracking) and tender queries are all in the portal, and they submit quotes there. Compare quotes side by side, appoint one, and the rest are told and lose access. Losing the main tender closes every open package. |
| **Live-job portal for subcontractors** | Appointed subcontractors submit RAMS for review (approve, approve with comments, or revise and resubmit, with revision history) and submit variations, which the projects team approves in full or part, rejects, or passes straight on to the client as a draft variation. Each subcontractor sees only their own. Issuing or revising a shared drawing notifies the client and subcontractors. The overview shows each subcontract's order value, approved variations and RAMS status. |
| **Notifications** | A bell for everyone, staff and external. Every alert (tender invitations, addenda, queries, quotes, RFI and task assignments, variation decisions) is an in-app notification plus a short email linking to the item. Reply in the portal, not by email. |
| **Tender analytics** | Win rate by number and by value, results by month, and breakdowns by sector, size, client, lead source and project lead. Recent losses with feedback. 12 months, 24 months or all time. |
| **Programmes** | A Gantt chart per project: activities with dates, progress, milestones and a "follows" link that flags anything out of sequence or behind. Lines can be internal or shared with the client and subcontractors. The Workload page has two company-wide Gantt charts: **Tenders** (every open bid from enquiry received to tender return, with site visit, queries close and subcontractor quotes due as milestones, filterable by estimator) and **Secured work** (every won and live job from start on site to completion). |
| **Projects** | One record from first lead to completion. Overview with commercial position (contract sum + approved variations = forecast final account). **Archived, not deleted:** once a project has anything on it (documents, RFIs, variations, RAMS, packages, snags, tasks, programme, downloads) an owner or admin can only archive it. See [Archiving](#archiving-instead-of-deleting). |
| **Client & subcontractor portal** | Invite external people per project by email. They get free logins and only see what's shared with them. |
| **RFIs** | Numbered per project (RFI-001), ball-in-court assignment, due dates, responses, threaded comments, internal notes, CSV register. Company-wide RFI register. |
| **Variations** | Draft → submit to client → client approves or rejects in the portal (with PO ref). Linked to RFIs. CSV export. |
| **Snags & tasks** | Assign to staff or subcontractors; subs mark snags "ready for inspection" from their phone. |
| **Documents** | Revision-controlled drawings, specs, RAMS. The current revision is the highest one (P01 → P10 → C01 → C02), not the last uploaded: filing an old P03 after C01 doesn't replace it unless you tick "make current". Internal or shared per document. Externals can upload (e.g. RAMS). |
| **Supply chain** | Subcontractor directory with trades, ratings, accreditations, PL/EL insurance expiry. Trade packages per project with quotes and appointment, and a one-click portal invite. A subcontractor with quotes, RAMS or variations on file is archived rather than deleted. |
| **Billing** | 14-day trial, then Stripe subscription (Starter / Team / Business). Plan limits on staff seats and active projects (archived projects don't count). External users are never charged. |
| **Daily digest** | Overdue RFIs, tender returns in 3 days, insurance expiring, trial ending. |

### Who sees what

| | Owner / Admin | Staff | Client / consultant | Subcontractor |
|---|---|---|---|---|
| All projects, pipeline, supply chain | ✓ | ✓ | – | – |
| Their invited projects | ✓ | ✓ | ✓ | ✓ |
| Commercial figures, packages | ✓ | ✓ | – | – |
| Shared documents / RFIs | ✓ | ✓ | ✓ | ✓ |
| Internal documents, RFIs, comments | ✓ | ✓ | – | – |
| Client variations | ✓ | ✓ | submitted ones; **client approves** | – |
| Subcontractor variations, RAMS | ✓ | ✓ | – | their own; submit and resubmit |
| Tasks & snags | all | all | assigned to them | assigned to them |
| Programme | all lines | all lines | shared lines, read only | shared lines, read only |
| Tender analytics, workload charts | ✓ | ✓ | – | – |

A **tendering subcontractor** (invited to price a package, not yet appointed) sees only their tender page: their own package and quote, documents issued to tenderers, addenda, and query answers issued to everyone. They can't see other bidders, the client's documents, RFIs, the programme or anything else on the project. Appointing them turns them into a normal subcontractor on the project.
| Team, settings, billing | ✓ | team list only | – | – |

### Archiving instead of deleting

Deleting a project used to take its RFIs, variations, RAMS, sub variations, packages, documents, the activity trail and the download log with it. Now:

- **Project with records:** *Edit project → Archive project* (owners and admins). It disappears from the bid and project lists, the pipeline, workload charts, dashboards, tender analytics, reminders and the plan's active-project count, and collaborators lose access. Nothing is deleted. Owners and admins find it under the **Archived** filter on Bids or Projects, can open every tab read only, and can **Restore** it (if the plan has room). Because it no longer counts against the plan, nothing can be changed on it until it is restored: `projects.access.get_project()`, which every project view uses, refuses any request other than GET/HEAD for an archived project, except archive/restore itself and deleting an empty project. Its events and alerts drop out of the activity feed, the notifications page and the bell for anyone who can't open it, and a pending portal invite to it can't be accepted. The activity feed also only shows staff events for the sections they work in.
- **Project with nothing on it** (a lead added by mistake): still deleted outright, by typing its reference.
- **Subcontractors** work the same way from *Edit subcontractor*: archive if they have quotes, RAMS, variations or addendum receipts (they drop out of the list and tender invitations; owners and admins see them under *Status → Archived*), delete if not.
- The database backs this up: those records' links to their project and subcontractor are `RESTRICT`, so even Django admin or the shell can't delete a project or subcontractor out from under them. Deleting a whole workspace still removes everything.

## How tenant separation works

This is the part to keep intact as you add features.

1. Every company-owned model inherits `tenancy.models.TenantModel`, which adds an `organization` field.
2. `TenantMiddleware` resolves `/<org_slug>/`, checks the user has an active `Membership`, and sets the current organisation for the request. Non-members get a 404, so workspace names can't be probed.
3. `TenantModel.objects` filters to the current organisation automatically, and `save()` refuses to write a row into a different organisation. So a view that forgets to filter still can't read another company's data.
4. It **fails closed**. With no current organisation (management commands, webhooks, scheduled jobs, the shell, login/signup/invite pages), running a `TenantModel.objects` query raises `TenantContextMissing` instead of returning every company's rows. That covers reverse and many-to-many managers (`project.rfis.all()`), `count()`, `exists()`, `pk__in=queryset` subqueries and `get_or_create()` too. Building a queryset is fine; running it is what needs an organisation, so a form's class-level `queryset=` is scoped to whichever organisation is current when the form is used. The filter is a `TenantFilter` expression in the query's WHERE clause that reads the current organisation when the SQL is compiled, so it also holds when querysets are combined (`|`, `&`, `^`, `union()`, `intersection()`, `difference()`) or wrapped in `Subquery()` / `Exists()`. A queryset made while an organisation is current stays pinned to it and raises `PermissionError` if it runs inside another. Combining a `TenantModel.objects` queryset with an `all_objects` one is refused, since that half has no filter.
5. `update()`, `delete()` and `bulk_create()` on `TenantModel.objects` stay inside the current organisation: update and delete only reach its rows, `update(organization=...)` can't move rows elsewhere, and `bulk_create()` fills in a missing organisation and refuses any object that belongs to another one.
6. Code outside a request picks one of these, in this order of preference:
   - `with org_context(org):` (from `tenancy.context`), looping over organisations where the job covers several, as `send_reminders` does.
   - `with unscoped():` when the code really means every company, as the platform admin (`tenancy.admin.TenantModelAdmin`) does. Inside it `TenantModel.objects` behaves like `all_objects` and `save()` needs an explicit organisation. The scope ends with the block: a queryset made inside it and run after it is filtered to whichever organisation is current then (or raises with none), and rows already fetched into a queryset's cache under one organisation, or inside `unscoped()`, raise `PermissionError` if read again inside another. Render anything that lists every company inside the block, as the admin does.
   - `Model.all_objects` for a lookup that is pinned some other way: a pk or token (invite links), a known organisation, or a file path.

   Say why in a comment, and grep for `unscoped()` and `all_objects` when reviewing. Following a foreign key (`rfi.project`), `refresh_from_db()` and delete cascades use `all_objects` as the base manager; each is pinned to a row the code already holds, so it can't list another company's data. For whole-database exports use `manage.py export_data` and `import_data`, which run `dumpdata`/`loaddata` inside `unscoped()` (plain `dumpdata`, even with `--all`, stops with `TenantContextMissing` on many-to-many fields).
7. `User` is global (one login can belong to several companies). Any form field that picks a user must use `projects.access.assignable_users()` or `staff_users()`, never `User.objects.all()`.
8. Uploaded files sit outside the web root and are only served by views that check permissions. **Never add a `/media/` static mapping.**

`tenancy/tests.py` covers all of this (45 tests, including regression tests from an independent access-control review; `tenancy/test_fail_closed.py` adds 30 for the fail-closed rule, combined querysets and subqueries, the bulk-write guard, the admin and every management command run against two companies; `projects/tests.py` adds 65 more for programmes, workload, analytics, files and archived projects, `tenders/tests.py` 18 for the tender portal, and `delivery/tests.py` 17 for the estimating/projects split and the live-job portal). Run them after every change:

```bash
DEBUG=1 python manage.py test
```

## Background jobs and email

Slow or unreliable work (sending email, for now) runs in the background from a queue kept in the app's own database. There is no Redis or Celery, and it works the same on SQLite, MySQL (PythonAnywhere) and PostgreSQL (AWS). The design and its trade-offs are in the docstring of `jobs/__init__.py`.

- **Writing a task.** In any app's `tasks.py`:
  ```python
  import jobs

  @jobs.task(name='rfis.chase_overdue', max_attempts=3)
  def chase_overdue(payload):
      rfi = RFI.objects.get(pk=payload['rfi_id'])   # runs inside org_context(the job's organisation)
      ...

  jobs.enqueue('rfis.chase_overdue', {'rfi_id': rfi.pk}, org=rfi.organization)
  ```
  Pass ids in the payload (it is JSON), not model instances. `enqueue()` writes the job in the caller's transaction, so it only exists if the request's changes commit. With `idempotency_key='...'`, a second enqueue with the same key returns the first job. A task runs with its job's organisation current, so `TenantModel.objects` works as in a request. A job with no organisation has none, and tenant queries in it fail closed. Only a task registered with `cross_company=True` runs inside `unscoped()`.
- **Delivery is at least once.** A failed job is retried with exponential backoff (30 seconds, doubling, up to an hour) and marked *dead* after `max_attempts` (5 unless the task says otherwise). A job whose worker died is retried after `JOBS_LOCK_TIMEOUT` (15 minutes). So a task can run twice: make a second run harmless.
- **Running jobs.** `python manage.py run_worker` runs until stopped; SIGTERM or Ctrl-C finish the current job first. `--burst` runs whatever is due and exits, `--once` runs one job. `python manage.py jobs_status` shows the queue, overdue jobs and recent dead ones. The platform admin (superusers only) has **Jobs** and **Outbox emails** pages with *Run again* and *Send again* actions. Where no worker runs all the time, schedule `run_worker --burst` hourly so failed emails are still retried and old rows pruned: PythonAnywhere step 8 lists it, and on AWS Terraform adds it whenever `worker_desired_count` is 0.
- **What is kept.** A sent email's body is blanked after `JOBS_EMAIL_BODY_DAYS` (7), because it holds invite links that still work; who it went to, the subject and when stay. Done jobs and sent emails are deleted after `JOBS_KEEP_DAYS` (30). Dead jobs and failed emails, which someone may need to look at or send again, are deleted after `JOBS_KEEP_FAILED_DAYS` (90). Stored errors and the worker's logs mask email addresses.
- **Email.** `tenancy.notify.send()` (every notification, invite and digest) renders the email in the request and stores it as an `OutboxEmail`. With `EMAIL_SYNC=0` the worker sends it, with retries, so a slow mail server never holds up a page. With `EMAIL_SYNC=1`, the default, it is sent during the request as before, and only a failed send is handed to the worker to retry. Set `EMAIL_SYNC=0` only where a worker is running, or emails wait in the outbox until one does. On AWS that happens by itself: Terraform sets `EMAIL_SYNC=0` whenever `worker_desired_count` is above 0. An email that can never be sent (a malformed header or address) is marked failed at once rather than retried. `EMAIL_TIMEOUT` (30 seconds per SMTP step) stops a mail server that has stopped answering from hanging the worker; keep it well under `JOBS_LOCK_TIMEOUT`, or a stuck send can be picked up by a second worker and the email sent twice (`manage.py check` warns, `jobs.W001`).
- **Password reset emails** are not queued. Django's reset form sends them itself, during the request: the person is waiting on the page for that email, it has no organisation, and a failure should show at once.
- **Local development and tests.** Leave `EMAIL_SYNC=1` and no worker is needed; emails print to the console. `JOBS_RUN_INLINE=1` runs every job in the web process as soon as its transaction commits. Tests keep the default, so `django.core.mail.outbox` fills as before; `jobs/tests.py`, `jobs/test_outbox.py` and `jobs/test_hardening.py` (43 tests) cover the queue and the worker path with `override_settings(EMAIL_SYNC=False)`.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt     # mysqlclient is only needed for MySQL; skip it if it fails locally
DEBUG=1 python manage.py migrate
DEBUG=1 python manage.py seed_demo  # demo company with data
DEBUG=1 python manage.py runserver
```

Open http://localhost:8000/login/. Demo logins (password `DemoPass!2026`):

- `owner@demo.test`: owner, sees estimating and projects and switches between them
- `estimator@demo.test`: estimating only (bids, sub tenders, pipeline, analytics)
- `pm@demo.test`: projects only (live jobs, procurement, RAMS, variations)
- `client@demo.test`: client on the Leeds bar job (try approving VO-001)
- `sub@demo.test`: joinery subcontractor (appointed on Leeds; invited to price joinery on Kingsway)
- `bids@demo.test`: drylining subcontractor pricing the Kingsway tender (sees only their tender)

Emails print to the console while `EMAIL_BACKEND` is unset.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every pull request and every push to `main`:

- **Tests**, on Python 3.13, one job per database: `Tests (sqlite)`, `Tests (postgres)` (PostgreSQL 16, the planned AWS RDS database) and `Tests (mysql)` (MySQL 8.0, what PythonAnywhere runs in production today). Each job runs `manage.py check`, `manage.py makemigrations --check --dry-run` (fails if a model change has no migration), then the full test suite under `coverage`, with the report printed in the log and the job summary. There is no coverage threshold yet.
  - The MySQL job uses the `mysql:8.0` service container with the same settings as production (`DB_ENGINE=mysql`: utf8mb4, `STRICT_TRANS_TABLES`, `CONN_MAX_AGE=280`). It connects as root so the test runner can create `test_siteline`. It installs `pkg-config`, `default-libmysqlclient-dev` and `build-essential` from apt and compiles `mysqlclient`. It also checks that the server is 8.0 with utf8mb4 and strict mode, and runs `manage.py check --database default` for the MySQL-specific checks. Warnings there are printed but do not fail the job; the one known warning, `mysql.W003` on `projects.FileScan.path`, is fine on MySQL 8.0 InnoDB. Any migration that relies on database-specific behaviour, such as `SELECT ... FOR UPDATE SKIP LOCKED`, must pass this job before it is deployed to PythonAnywhere.
  - The SQLite and PostgreSQL jobs leave `mysqlclient` out of the install because it has to be compiled and they do not use it.
  - Branch protection: once `Tests (mysql)` has passed on `main`, add it to the required status checks next to `Tests (sqlite)` and `Tests (postgres)`.
- **Security scan**: `pip-audit` on `requirements.txt` and `requirements-dev.txt`, and `bandit -r siteline -ll`. This is non-blocking for now (`continue-on-error`) while the existing findings are fixed. Findings show up as warnings on the run.

Dependabot (`.github/dependabot.yml`) opens weekly update PRs for the pip requirements and for the GitHub Actions, which are pinned to commit SHAs. The `@claude` workflow (`.github/workflows/claude.yml`) only runs for repository owners, members and collaborators.

To run the same checks locally:

```bash
pip install -r requirements.txt -r requirements-dev.txt
DEBUG=1 MEDIA_ROOT=$(mktemp -d) python -m coverage run manage.py test && python -m coverage report
pip-audit -r requirements.txt --no-deps --disable-pip   # checks the pins without building mysqlclient
bandit -r . -ll --exclude ./.venv
```

## Deploy on PythonAnywhere

You need a **paid plan** (Hacker or above) for a custom domain and unrestricted SMTP. MySQL is included.

### 1. Code and virtualenv
In a Bash console:
```bash
git clone <your-repo-url> ~/siteline     # or upload the zip and unzip it
cd ~/siteline
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p ~/siteline-files
```

### 2. Database
**Databases** tab → set a MySQL password → create a database called `siteline` (it becomes `yourusername$siteline`).

### 3. Environment
```bash
cp .env.example .env
nano .env    # fill in every value
python -c "import secrets; print(secrets.token_urlsafe(50))"   # paste into SECRET_KEY
```

### 4. Build
```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser        # this is YOU, the platform owner
```

### 5. Web app
**Web** tab → Add a new web app → *Manual configuration* → Python 3.13.

- **Virtualenv:** `/home/yourusername/siteline/.venv`
- **Static files:** URL `/static/` → Directory `/home/yourusername/siteline/staticfiles`
- **Do not** add a mapping for uploaded files.
- **Force HTTPS:** on.
- **WSGI configuration file**: replace its contents with:

```python
import os, sys
path = '/home/yourusername/siteline'
if path not in sys.path:
    sys.path.insert(0, path)
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
```

Reload. Visit `https://yourusername.pythonanywhere.com/health/`, which should say `ok`.

### 6. Custom domain
Web tab → add `yourportal.com` (and `www.`) → point a CNAME at the value PythonAnywhere gives you → enable the free Let's Encrypt certificate. Add the domain to `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` and `SITE_URL`.

### 7. Stripe
1. **Products**: create Starter, Team and Business, each with a monthly recurring price. Copy the `price_...` IDs into `.env`. The display prices are in `config/settings.py → PLANS`; keep them in step with Stripe.
2. **Settings → Billing → Customer portal**: enable it, and allow switching between your three prices, updating cards and cancelling.
3. **Developers → Webhooks → Add endpoint**: `https://yourportal.com/billing/webhook/` with these events:
   `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `customer.subscription.paused`, `customer.subscription.resumed`, `invoice.paid`, `invoice.payment_failed`.
   Copy the signing secret into `STRIPE_WEBHOOK_SECRET`.
4. Turn on **Stripe Tax** if you're VAT registered (Checkout already collects billing address and VAT numbers).
5. Test end to end with `sk_test_` keys and card `4242 4242 4242 4242` before switching to live keys.

Only the webhook changes a company's subscription status, so a customer can't unlock a workspace by hitting the success URL.

### 8. Scheduled tasks
**Tasks** tab:

| Time (UTC) | Command |
|---|---|
| 06:00 daily | `cd ~/siteline && .venv/bin/python manage.py send_reminders` |
| Always-on task (*Tasks* tab → *Always-on tasks*) | `cd ~/siteline && .venv/bin/python manage.py run_worker` |
| Hourly, at :30 (only while there is no always-on task) | `cd ~/siteline && .venv/bin/python manage.py run_worker --burst` |
| 02:00 daily | `mysqldump -u yourusername -h yourusername.mysql.pythonanywhere-services.com --set-gtid-purged=OFF 'yourusername$siteline' \| gzip > ~/backups/siteline-$(date +\%a).sql.gz` |

Create `~/backups` first. The backup keeps a rolling seven days. Copy them off-site too, and back up `~/siteline-files`.

The always-on task is the background worker (see [Background jobs and email](#background-jobs-and-email)). Once its log shows `Worker ... started`, add `EMAIL_SYNC=0` to `.env` and reload the web app, so emails are sent by the worker instead of during requests. Then remove the hourly `run_worker --burst` task, which the always-on worker replaces. Without an always-on task, leave `EMAIL_SYNC=1` and keep the hourly burst: it retries any email that failed during a request and deletes old job and email rows.

### 9. Cloud file storage and virus scanning (AWS S3 London)

PythonAnywhere's disk is too small for tender packs and drawings, so uploaded files go to a private S3 bucket in London, and GuardDuty scans every upload for malware. Siteline checks permissions on every download, logs it, then sends the browser a signed link that expires after five minutes. The bucket is never public. Until `STORAGE_BUCKET` is set, files stay on PythonAnywhere's disk as before. You need a paid PythonAnywhere plan: the free one blocks connections to AWS.

1. **Account.** aws.amazon.com → Create an AWS account. Choose the **Paid plan** (the Free plan closes the account when its credits run out). Basic support.
2. **Protect it.** IAM → *Add MFA for root user* → passkey or authenticator app.
3. **Region.** Top right → **Europe (London) eu-west-2**. Check it on every page.
4. **Spending alert.** Budgets → Create budget → template *Monthly cost budget* → 20 (USD) → your email.
5. **Bucket.** S3 → Create bucket: a unique lower-case name, region London, ACLs disabled, **Block all public access on** (never turn it off), **Bucket versioning enabled**, default encryption SSE-S3 with Bucket Key.
6. **Lifecycle.** Bucket → Management → Create lifecycle rule `cleanup`, all objects: permanently delete noncurrent versions after **30** days; delete incomplete multipart uploads after **7** days.
7. **Virus scanning.** GuardDuty → *Malware Protection for S3* (you don't need full GuardDuty) → Enable → your bucket, all objects → **Tag scanned objects: enabled** → *Create and use a new service role*.
8. **A key that can only touch this bucket.** IAM → Users → Create user `siteline-app` (no console access) → Attach policies directly → Create policy → JSON, replacing `BUCKET` twice:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {"Sid": "ListBucket", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": "arn:aws:s3:::BUCKET"},
       {"Sid": "Files", "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:GetObjectTagging"],
        "Resource": "arn:aws:s3:::BUCKET/*"}
     ]
   }
   ```
   Name it `siteline-files-access`, attach it, create the user. Then the user → Security credentials → Create access key → *Application running outside AWS*. The secret is shown once; keep it in a password manager.
9. **`.env`** (Files tab), then save:
   ```
   STORAGE_BUCKET=your-bucket-name
   STORAGE_REGION=eu-west-2
   STORAGE_ACCESS_KEY=AKIA...
   STORAGE_SECRET_KEY=...
   ```
10. **Install and move existing files** (Bash console):
    ```bash
    cd ~/pc-estimating-site/siteline && source .venv/bin/activate && pip install -r requirements.txt
    python manage.py move_files_to_storage --dry-run
    python manage.py move_files_to_storage
    ```
    Reload on the Web tab. The local copies in `MEDIA_ROOT` aren't deleted; remove them yourself once you've checked.
11. **Check.** Open a document in Siteline: the address bar briefly shows `s3.eu-west-2.amazonaws.com`. In S3, a file's Tags should show `GuardDutyMalwareScanStatus = NO_THREATS_FOUND`.

12. **Hourly scan check.** Siteline already refuses to hand out any file until GuardDuty has tagged it clean: people see "virus check in progress" for the minute or so that takes, and an infected or unscannable file is blocked for everyone, with the project team notified. The scheduled check catches bad uploads straight away and tells the uploader too. Tasks tab → add an **hourly** task (use your own folder name):
    ```
    cd ~/YOUR-FOLDER/siteline && .venv/bin/python manage.py check_file_scans
    ```
13. **Files uploaded before scanning was on.** GuardDuty only scans new uploads. Run this once and read the last line:
    ```bash
    python manage.py check_file_scans
    ```
    If "still being scanned" stays above 0 on a second run a few minutes later, those files predate scanning. Ask GuardDuty to scan them, wait five minutes, then run the plain command again until it reads 0:
    ```bash
    python manage.py check_file_scans --retrigger
    ```
14. **Make AWS enforce it too.** Once step 13 reads 0, add a bucket policy so S3 itself refuses any file not tagged clean, even if Siteline had a bug. Find the GuardDuty role first: IAM → Roles → search `Malware` or `GuardDuty` → open the role → copy its **ARN**. Then S3 → your bucket → Permissions → Bucket policy → Edit, paste, replace `BUCKET` and the role ARN, and save:
    ```json
    {
      "Version": "2012-10-17",
      "Statement": [{
        "Sid": "OnlyServeScannedCleanFiles",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:GetObject",
        "Resource": "arn:aws:s3:::BUCKET/*",
        "Condition": {
          "StringNotEquals": {
            "s3:ExistingObjectTag/GuardDutyMalwareScanStatus": "NO_THREATS_FOUND",
            "aws:PrincipalArn": "PASTE-THE-GUARDDUTY-ROLE-ARN"
          }
        }
      }]
    }
    ```
    The role is excluded so GuardDuty can still read new files to scan them. After this, `move_files_to_storage` and `--retrigger` can no longer read unscanned files, which is intended.

If scanning ever stops working (every download says "virus check in progress"), check Malware Protection is still enabled on the bucket in GuardDuty. `STORAGE_MALWARE_SCAN=0` in `.env` turns Siteline's check off, but only use it while the bucket policy in step 14 is not in place, and fix scanning rather than leaving it off.

Other S3-compatible providers work with the same settings plus `STORAGE_ENDPOINT_URL`: DigitalOcean Spaces London (`https://lon1.digitaloceanspaces.com`, region `lon1`) or Cloudflare R2 (`https://<account_id>.eu.r2.cloudflarestorage.com`, region `auto`). See `.env.example`.

Where data lives: files in London; the database where your PythonAnywhere account is (www.pythonanywhere.com is the US, eu.pythonanywhere.com is Germany). There is no UK PythonAnywhere.

### 10. Updating
```bash
cd ~/siteline && git pull && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate && python manage.py collectstatic --noinput
```
Then reload on the Web tab.

## Deploy on AWS (London)

The move from PythonAnywhere to AWS (ECS Fargate, RDS PostgreSQL, SES) is scripted in `infra/`. Follow `infra/README.md`; the data move is in `infra/scripts/migrate-data.md`.

## Running the platform

- Admin is at `/<ADMIN_PATH>/` (default `platform-admin`). From there you can see every company, its plan and status, extend a trial (`trial_ends_at`) or suspend a workspace (`is_suspended`).
- Login is rate-limited (6 failures locks that email + IP for an hour). Unlock with `python manage.py axes_reset`.

## Before you take money: open items

- **Terms, privacy policy and a data processing agreement.** You'll be a data processor for your customers' client and subcontractor data under UK GDPR. Register with the ICO.
- **Email verification** on sign-up isn't built yet. Invite links do prove the email address.
- **Storage.** Set up cloud file storage (step 9) before customers upload drawings. PythonAnywhere's disk fills quickly.
- **Two-factor login** isn't built yet, and security questionnaires ask about it first.
- **Scale.** PythonAnywhere suits the first few dozen customers. If you outgrow it, the code runs unchanged on Render, Railway or a VPS with Postgres (`DB_ENGINE=postgres`), and subdomain workspaces become possible.

## Project layout

```
config/       settings, root URLs
accounts/     User (email login), sign-up, invites, profile
tenancy/      Organization, Membership, tenant middleware/manager, notifications, dashboard, team, settings, digest, demo seed, tests
billing/      Stripe checkout, portal, webhook
projects/     projects, pipeline, team & portal invites, tasks, snags, documents, comments, access rules
rfis/         RFI register
variations/   variations & client approval
directory/    subcontractors, trades, trade packages and quote comparison
tenders/      portal tenders: invitations, addenda, tender queries, appointment rules
delivery/     live-job portal: RAMS review, subcontractor variations, subcontract summary
jobs/         background job queue and worker (run_worker, jobs_status), email outbox
templates/    shared templates; app-specific ones live in each app
static/       app.css (single stylesheet; --accent is set from each company's brand colour, --accent-ink to white or near-black, whichever contrasts more), app.js
```
