# Siteline

A multi-tenant project portal for small fit-out and build contractors. Each company gets its own workspace (`/company-name/`), runs bids and live jobs in it, and invites clients, consultants and subcontractors in for free.

`Siteline` is a placeholder name. Change it with `PRODUCT_NAME` in `.env`.

## What's in it

| Area | What it does |
|---|---|
| **Bid pipeline** | Drag-and-drop board from Lead → Tendering → Submitted → Won. Weighted pipeline, 12-month win rate, tender return deadlines. |
| **Subcontract tenders (portal)** | Invite subcontractors to price a trade package. They get a notification with a link, never attachments: scope, tender documents, addenda (with acknowledgement tracking) and tender queries are all in the portal, and they submit quotes there. Compare quotes side by side, appoint one, and the rest are told and lose access. Losing the main tender closes every open package. |
| **Notifications** | A bell for everyone, staff and external. Every alert (tender invitations, addenda, queries, quotes, RFI and task assignments, variation decisions) is an in-app notification plus a short email linking to the item. Reply in the portal, not by email. |
| **Tender analytics** | Win rate by number and by value, results by month, and breakdowns by sector, size, client, lead source and project lead. Recent losses with feedback. 12 months, 24 months or all time. |
| **Programmes** | A Gantt chart per project: activities with dates, progress, milestones and a "follows" link that flags anything out of sequence or behind. Lines can be internal or shared with the client and subcontractors. The Workload page has two company-wide Gantt charts: **Tenders** (every open bid from enquiry received to tender return, with site visit, queries close and subcontractor quotes due as milestones, filterable by estimator) and **Secured work** (every won and live job from start on site to completion). |
| **Projects** | One record from first lead to completion. Overview with commercial position (contract sum + approved variations = forecast final account). |
| **Client & subcontractor portal** | Invite external people per project by email. They get free logins and only see what's shared with them. |
| **RFIs** | Numbered per project (RFI-001), ball-in-court assignment, due dates, responses, threaded comments, internal notes, CSV register. Company-wide RFI register. |
| **Variations** | Draft → submit to client → client approves or rejects in the portal (with PO ref). Linked to RFIs. CSV export. |
| **Snags & tasks** | Assign to staff or subcontractors; subs mark snags "ready for inspection" from their phone. |
| **Documents** | Revision-controlled drawings, specs, RAMS. Internal or shared per document. Externals can upload (e.g. RAMS). |
| **Supply chain** | Subcontractor directory with trades, ratings, accreditations, PL/EL insurance expiry. Trade packages per project with quotes and appointment, and a one-click portal invite. |
| **Billing** | 14-day trial, then Stripe subscription (Starter / Team / Business). Plan limits on staff seats and active projects. External users are never charged. |
| **Daily digest** | Overdue RFIs, tender returns in 3 days, insurance expiring, trial ending. |

### Who sees what

| | Owner / Admin | Staff | Client / consultant | Subcontractor |
|---|---|---|---|---|
| All projects, pipeline, supply chain | ✓ | ✓ | – | – |
| Their invited projects | ✓ | ✓ | ✓ | ✓ |
| Commercial figures, packages | ✓ | ✓ | – | – |
| Shared documents / RFIs | ✓ | ✓ | ✓ | ✓ |
| Internal documents, RFIs, comments | ✓ | ✓ | – | – |
| Variations | ✓ | ✓ | submitted ones; **client approves** | – |
| Tasks & snags | all | all | assigned to them | assigned to them |
| Programme | all lines | all lines | shared lines, read only | shared lines, read only |
| Tender analytics, workload charts | ✓ | ✓ | – | – |

A **tendering subcontractor** (invited to price a package, not yet appointed) sees only their tender page: their own package and quote, documents issued to tenderers, addenda, and query answers issued to everyone. They can't see other bidders, the client's documents, RFIs, the programme or anything else on the project. Appointing them turns them into a normal subcontractor on the project.
| Team, settings, billing | ✓ | team list only | – | – |

## How tenant separation works

This is the part to keep intact as you add features.

1. Every company-owned model inherits `tenancy.models.TenantModel`, which adds an `organization` field.
2. `TenantMiddleware` resolves `/<org_slug>/`, checks the user has an active `Membership`, and sets the current organisation for the request. Non-members get a 404, so workspace names can't be probed.
3. `TenantModel.objects` filters to the current organisation automatically, and `save()` refuses to write a row into a different organisation. So a view that forgets to filter still can't read another company's data. Use `Model.all_objects` only in admin, webhooks and management commands.
4. `User` is global (one login can belong to several companies). Any form field that picks a user must use `projects.access.assignable_users()` or `staff_users()`, never `User.objects.all()`.
5. Uploaded files sit outside the web root and are only served by views that check permissions. **Never add a `/media/` static mapping.**

`tenancy/tests.py` covers all of this (54 tests, including regression tests from an independent access-control review; `projects/tests.py` adds 17 more for programmes, workload and analytics, and `tenders/tests.py` 18 for the tender portal). Run them after every change:

```bash
DEBUG=1 python manage.py test
```

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt     # mysqlclient is only needed for MySQL; skip it if it fails locally
DEBUG=1 python manage.py migrate
DEBUG=1 python manage.py seed_demo  # demo company with data
DEBUG=1 python manage.py runserver
```

Open http://localhost:8000/login/. Demo logins (password `DemoPass!2026`):

- `owner@demo.test`: owner, sees everything
- `pm@demo.test`: staff project manager
- `client@demo.test`: client on the Leeds bar job (try approving VO-001)
- `sub@demo.test`: joinery subcontractor (appointed on Leeds; invited to price joinery on Kingsway)
- `bids@demo.test`: drylining subcontractor pricing the Kingsway tender (sees only their tender)

Emails print to the console while `EMAIL_BACKEND` is unset.

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
| 02:00 daily | `mysqldump -u yourusername -h yourusername.mysql.pythonanywhere-services.com --set-gtid-purged=OFF 'yourusername$siteline' \| gzip > ~/backups/siteline-$(date +\%a).sql.gz` |

Create `~/backups` first. The backup keeps a rolling seven days. Copy them off-site too, and back up `~/siteline-files`.

### 9. Updating
```bash
cd ~/siteline && git pull && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate && python manage.py collectstatic --noinput
```
Then reload on the Web tab.

## Running the platform

- Admin is at `/<ADMIN_PATH>/` (default `platform-admin`). From there you can see every company, its plan and status, extend a trial (`trial_ends_at`) or suspend a workspace (`is_suspended`).
- Login is rate-limited (6 failures locks that email + IP for an hour). Unlock with `python manage.py axes_reset`.

## Before you take money: open items

- **Terms, privacy policy and a data processing agreement.** You'll be a data processor for your customers' client and subcontractor data under UK GDPR. Register with the ICO.
- **Email verification** on sign-up isn't built yet. Invite links do prove the email address.
- **Storage.** PythonAnywhere disk quotas are modest. If customers upload a lot of drawings, move files to S3-compatible storage (`django-storages`); the permission-checked download views stay the same.
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
templates/    shared templates; app-specific ones live in each app
static/       app.css (single stylesheet; --accent is set from each company's brand colour), app.js
```
