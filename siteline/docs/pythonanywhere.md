# Running Siteline on PythonAnywhere

This replaces the AWS move for now. You end up with:

- **Prod** at `app.pc-estimating.co.uk`, on PostgreSQL instead of MySQL, with a background worker.
- **Staging** at `staging.pc-estimating.co.uk`, with its own database full of demo data. This is where you try each new module before it goes to customers. It never emails anyone.
- Daily database backups, kept for seven days.

Everything is done in the PythonAnywhere website: the **Bash console**, **Files**, **Web**, **Tasks** and **Databases** tabs. Nothing runs on your PC except a browser.

Replace `yourusername` everywhere with your PythonAnywhere username. Wherever a command has `NNNN` or `PORT`, use the values from the Databases tab (part 2).

Do parts 1 to 4 now. They don't touch prod, so customers notice nothing. Part 5 is the move of your live data; it needs a quiet hour.

---

## 1. Plan (once)

**Account** tab → change your plan so it includes:

| You need | Why |
|---|---|
| **2 web apps** | prod and staging |
| **1 always-on task** | the background worker |
| **Postgres** | it's a separate add-on on the same page |

PythonAnywhere's prices change, so check the Account page. Expect roughly £20–40 a month in total. If two web apps pushes you up a whole tier, skip staging for now and come back to part 3 later; it is the only part that needs the second app.

The address you're on decides where the database lives: www.pythonanywhere.com is in the US, eu.pythonanywhere.com is in Germany. Germany is the better answer for UK customers' data questions. Moving between them means a new account, so if you're on www and have few customers, now is the cheapest time to switch.

## 2. Create the Postgres databases (once)

1. **Databases** tab → **Postgres** → set a **superuser password**. Save it in your password manager.
2. Note the **Address** and **Port** shown there. They look like `yourusername-NNNN.postgres.pythonanywhere-services.com` and a five-digit number.
3. Click **Start a console on: postgres**. Paste this, after putting two different long passwords in place of the two `CHANGE-ME`s:
   ```sql
   CREATE USER siteline WITH PASSWORD 'CHANGE-ME-PROD';
   CREATE DATABASE siteline OWNER siteline;
   CREATE USER siteline_staging WITH PASSWORD 'CHANGE-ME-STAGING';
   CREATE DATABASE siteline_staging OWNER siteline_staging;
   ```
   Each prints `CREATE ROLE` or `CREATE DATABASE`. Type `\q` to leave. Save both passwords. Prod and staging have separate users, so staging can't touch prod's data by mistake.

## 3. Staging (once)

Open a **Bash console**.

1. **Code and Python packages:**
   ```bash
   git clone https://github.com/phillie11/pc-estimating-site.git ~/siteline-staging
   cd ~/siteline-staging/siteline
   python3.13 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   mkdir -p ~/siteline-staging-files
   python -c "import secrets; print(secrets.token_urlsafe(50))"
   ```
   Copy the long line the last command prints. It's staging's secret key.

2. **Settings.** **Files** tab → `/home/yourusername/siteline-staging/siteline/` → new file `.env`, paste, fill in the three marked values, save:
   ```
   DEBUG=0
   SECRET_KEY=PASTE-THE-LONG-LINE
   ALLOWED_HOSTS=staging.pc-estimating.co.uk
   CSRF_TRUSTED_ORIGINS=https://staging.pc-estimating.co.uk
   SITE_URL=https://staging.pc-estimating.co.uk
   ADMIN_PATH=platform-admin
   CLIENT_IP_HEADER=X-Real-IP
   SECURE_HSTS_SECONDS=0

   DB_ENGINE=postgres
   DB_NAME=siteline_staging
   DB_USER=siteline_staging
   DB_PASSWORD=THE-STAGING-PASSWORD
   DB_HOST=yourusername-NNNN.postgres.pythonanywhere-services.com
   DB_PORT=PORT

   MEDIA_ROOT=/home/yourusername/siteline-staging-files

   # Staging never sends email: every email is written to the server log instead
   EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
   EMAIL_SYNC=1
   ```
   Stripe and file storage are left out on purpose: uploads stay on PythonAnywhere's disk and nothing can be charged.

3. **Build it and load the demo company:**
   ```bash
   cd ~/siteline-staging/siteline && source .venv/bin/activate
   python manage.py migrate
   python manage.py collectstatic --noinput
   python manage.py seed_demo
   python manage.py createsuperuser
   ```
   `seed_demo` creates Northbank Interiors with six logins, all with the password `DemoPass!2026`. **Never run it on prod**: anyone could log in with that password.

4. **Web app.** **Web** tab → *Add a new web app* → your own domain `staging.pc-estimating.co.uk` → *Manual configuration* → Python 3.13. Then on its page:
   - **Virtualenv:** `/home/yourusername/siteline-staging/siteline/.venv`
   - **Static files:** URL `/static/` → directory `/home/yourusername/siteline-staging/siteline/staticfiles`
   - **Force HTTPS:** on
   - **WSGI configuration file:** replace everything in it with
     ```python
     import os, sys
     path = '/home/yourusername/siteline-staging/siteline'
     if path not in sys.path:
         sys.path.insert(0, path)
     os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
     from django.core.wsgi import get_wsgi_application
     application = get_wsgi_application()
     ```

5. **DNS.** At your domain host, add a **CNAME** record `staging` pointing at the value the Web tab shows (`webapp-NNNN.pythonanywhere.com`). When the Web tab's DNS check goes green, turn on the free **Let's Encrypt** certificate there. Then **Reload**.

6. **Try it.** `https://staging.pc-estimating.co.uk/health/` should say `ok`. Then sign in as each of these:

   | Login | Sees |
   |---|---|
   | `owner@demo.test` | everything; asked to set up two-factor sign-in first (use any authenticator app) |
   | `estimator@demo.test` | bids, sub tenders, pipeline |
   | `pm@demo.test` | live jobs, drawings, variations, RAMS |
   | `client@demo.test` | the client portal on the Leeds job |
   | `sub@demo.test` | the subcontractor portal |
   | `bids@demo.test` | a subcontractor pricing a tender |

   Emails that would have been sent appear in the web app's **server log** (Web tab, Log files).

No scheduled tasks run on staging, so it never sends round-ups or chases anyone.

### Updating staging when a release is ready

```bash
cd ~/siteline-staging/siteline && git pull && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate && python manage.py collectstatic --noinput
```
Then **Reload** on the staging web app. `python manage.py seed_demo --reset` puts the demo company back to its starting state.

## 4. Backups and password file (once)

So that the scheduled backup can reach Postgres without typing a password, put prod's password in a file only you can read. In a Bash console:
```bash
echo "yourusername-NNNN.postgres.pythonanywhere-services.com:PORT:siteline:siteline:THE-PROD-PASSWORD" > ~/.pgpass
chmod 600 ~/.pgpass
mkdir -p ~/backups
```

The backup task itself is added in part 5, once prod is on Postgres. Until then keep your existing MySQL backup task.

## 5. Move prod from MySQL to Postgres

You do this twice. First a **rehearsal** that loads a copy of your data into the empty prod Postgres database, while prod carries on running on MySQL. Then the **real move**, with prod switched off for the length of the rehearsal plus half an hour.

Uploaded files don't move. They are in your S3 bucket (or `~/siteline-files`) and stay where they are.

Throughout, `~/siteline/siteline` is prod's folder: the one holding `manage.py` and `.env`. If you're not sure where yours is, run `ls ~/*/manage.py ~/*/siteline/manage.py` in a Bash console and use that folder instead, everywhere below.

### 5.1 Rehearsal

1. **Up to date first.** Prod must run the latest code before the export, so both databases have the same tables:
   ```bash
   cd ~/siteline/siteline && git pull && source .venv/bin/activate
   pip install -r requirements.txt
   python manage.py migrate && python manage.py collectstatic --noinput
   ```
   **Reload** the prod web app.

2. **Safety copy of MySQL:**
   ```bash
   mysqldump -u yourusername -h yourusername.mysql.pythonanywhere-services.com --set-gtid-purged=OFF 'yourusername$siteline' | gzip > ~/backups/before-postgres-$(date +%F).sql.gz
   ```

3. **Export from MySQL.** Note the time:
   ```bash
   cd ~/siteline/siteline && source .venv/bin/activate
   python manage.py model_counts --exclude contenttypes auth.permission sessions axes > ~/counts-mysql.txt
   python manage.py export_data --output ~/siteline-data.json.gz
   ```

4. **Point this console, and only this console, at Postgres.** These settings win over `.env` until you close the console. `.env` is not changed, so prod keeps running on MySQL:
   ```bash
   export DB_ENGINE=postgres DB_NAME=siteline DB_USER=siteline DB_PORT=PORT
   export DB_HOST=yourusername-NNNN.postgres.pythonanywhere-services.com
   export DB_PASSWORD='THE-PROD-PASSWORD'
   python manage.py migrate
   ```

5. **Load and count:**
   ```bash
   python manage.py import_data ~/siteline-data.json.gz
   python manage.py model_counts --exclude contenttypes auth.permission sessions axes > ~/counts-postgres.txt
   diff ~/counts-mysql.txt ~/counts-postgres.txt && echo MATCH
   ```
   It must print **MATCH**. In the rehearsal only, prod is still in use, so a line or two can differ where someone did something between the count and the export (sign-in counters, activity). Anything else that differs, or a load that fails, send me the text. A failed load prints the reason and loads nothing.

6. **How long did steps 3 to 5 take?** That plus 30 minutes is your switch-off window for the real move.

7. **Empty Postgres again** (this console is still pointed at it) and tidy up:
   ```bash
   python manage.py flush --noinput
   rm ~/siteline-data.json.gz ~/counts-mysql.txt ~/counts-postgres.txt
   exit
   ```
   `exit` closes the console, which drops the Postgres settings from step 4.

### 5.2 The real move

1. **Tell customers** a few days before, e.g. "Siteline will be unavailable on Saturday from 07:00 for about an hour. You'll need to log in again afterwards."

2. **Switch prod off.** **Web** tab → **Disable** the prod web app. **Tasks** tab → disable every Siteline task except the MySQL backup. If there's a `run_worker` always-on task, first run `python manage.py run_worker --burst` in a console so queued emails go out, then disable it.

3. **Repeat rehearsal steps 2 to 5** in a fresh Bash console (new safety copy, export, point at Postgres, migrate, load, count). **Only continue on MATCH.** If it doesn't match: close the console, re-enable the web app and tasks, and you're back where you started.

4. **Switch `.env` to Postgres.** **Files** tab → prod's `.env`. Replace the five `DB_` lines with:
   ```
   DB_ENGINE=postgres
   DB_NAME=siteline
   DB_USER=siteline
   DB_PASSWORD=THE-PROD-PASSWORD
   DB_HOST=yourusername-NNNN.postgres.pythonanywhere-services.com
   DB_PORT=PORT
   ```
   Keep the old MySQL lines in your password manager. Putting them back is the rollback.

5. **Switch prod on.** **Web** tab → **Enable**, then **Reload**. Sign in and check companies, projects, the pipeline and a document. Everyone else signs in again once.

6. **Turn the tasks back on** and change the backup. **Tasks** tab:

   | Time (UTC) | Command |
   |---|---|
   | 06:00 daily | `cd ~/siteline/siteline && .venv/bin/python manage.py send_reminders` |
   | Hourly | `cd ~/siteline/siteline && .venv/bin/python manage.py check_file_scans` (only if you use S3 storage) |
   | 05:15 on the 1st | `cd ~/siteline/siteline && .venv/bin/python manage.py update_bank_holidays` |
   | 02:00 daily (replaces the MySQL one) | `pg_dump -h yourusername-NNNN.postgres.pythonanywhere-services.com -p PORT -U siteline siteline \| gzip > ~/backups/siteline-$(date +\%a).sql.gz` |

   Use your own folder path if it differs. If `pg_dump` says its version doesn't match the server, send me the message. The backup keeps a rolling seven days, one file per weekday. Download one now and then to keep off PythonAnywhere.

7. **Clean up:** `rm ~/siteline-data.json.gz ~/counts-*.txt`. Leave the MySQL database alone for a month as a fallback, then delete it on the Databases tab.

## 6. The background worker

After the move, and once prod has run on Postgres for a day:

1. **Tasks** tab → **Always-on tasks** → add:
   ```
   cd ~/siteline/siteline && .venv/bin/python manage.py run_worker
   ```
2. When its log shows `Worker ... started`, add `EMAIL_SYNC=0` to prod's `.env` (replace the `EMAIL_SYNC=1` line) and **Reload**. Emails are now sent by the worker, with retries, instead of during the page load.
3. If you added an hourly `run_worker --burst` task earlier, delete it. The always-on worker replaces it.

To check on it at any time: `python manage.py jobs_status` in a console, or **Jobs** and **Outbox emails** in the platform admin.

## Updating prod

Same as staging, in prod's folder, after the release has been tried on staging:
```bash
cd ~/siteline/siteline && git pull && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate && python manage.py collectstatic --noinput
```
Then **Reload** the prod web app, and restart the always-on task (Tasks tab, its restart button) so the worker runs the new code too.

## If something goes wrong

- **Prod won't start after the move:** put the old MySQL `DB_` lines back in `.env` and Reload. Nothing was deleted from MySQL, so you're back where you were. Anything people did on Postgres in between is lost, which is why to check straight away.
- **Error pages:** Web tab → **Error log**. Send me the last 30 lines.
- **Someone is locked out after too many attempts:** `python manage.py axes_reset` in prod's folder.

## When to move to AWS

When a customer's IT team asks where the data is hosted and needs the answer to be the UK, or you need a staging site that matches prod exactly, or you need to restore to a minute rather than a day. The scripts and guide are ready in `infra/README.md`, and the code runs the same on both.
