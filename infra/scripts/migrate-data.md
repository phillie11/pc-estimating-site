# Moving the data from PythonAnywhere to AWS

This moves Siteline's **database** from PythonAnywhere's MySQL to the new AWS PostgreSQL database, then points the domain at AWS. You do it twice: a **rehearsal** on staging (nobody notices), then the **real move** on prod during a short freeze.

Do [infra/README.md](../README.md) steps 1 to 18 first, so staging and prod are both built, deployed and smoke-tested.

Commands marked **PythonAnywhere** go in a PythonAnywhere *Bash console*. Everything else goes in PowerShell on your PC, in the `pc-estimating-site` folder, signed in as in README step 2.

## What moves and what doesn't

| | What happens |
|---|---|
| Database (companies, users, projects, RFIs, everything you see in Siteline) | Exported on PythonAnywhere, loaded into AWS. Passwords move too (as scrambled hashes), so everyone keeps their login. |
| Uploaded files in your S3 bucket | **Don't move.** Prod on AWS uses the same bucket, so every file link keeps working. |
| Files still on PythonAnywhere's disk | Only if `STORAGE_BUCKET` was never set in the PythonAnywhere `.env`. Copy them into the bucket first (step 1). |
| Who is logged in (sessions) | Not moved. Everyone logs in again once. |
| Login lockouts (django-axes) | Not moved. Any locked-out email starts fresh. |

How it works: `export_data` (Django's `dumpdata`, run across every company) writes every row to one compressed file; `import_data` (`loaddata`) reads it into a freshly built, empty PostgreSQL database in one go (if anything fails, nothing is loaded); then the ID counters are reset so new rows don't clash. `model_counts` prints how many rows each table has, on both sides, and `compare-counts.ps1` checks they match.

The export uses `--all`, which reads every company's rows. Without it, Siteline's company-separation safety net refuses to run outside a company workspace. The row counts use the unscoped `all_objects` managers for the same reason.

## 1. Before you start (once)

1. **Same code on both sides.** The export only loads into a database with the same tables. On **PythonAnywhere**, update to the latest code exactly as in `siteline/README.md` step 10 (Updating):
   ```bash
   cd ~/siteline && git pull && source .venv/bin/activate
   pip install -r requirements.txt
   python manage.py migrate && python manage.py collectstatic --noinput
   ```
   Then **Reload** on the Web tab. Deploy the same commit to AWS (`.\infra\scripts\deploy.ps1 staging` and `prod`). Don't let either side get new code between the rehearsal and the real move.

2. **Files still on PythonAnywhere's disk.** Open the PythonAnywhere `.env`. If `STORAGE_BUCKET` is set, skip this. If it is empty, finish `siteline/README.md` step 9 (S3 bucket and keys) first, then on **PythonAnywhere**:
   ```bash
   cd ~/siteline && source .venv/bin/activate
   python manage.py move_files_to_storage --dry-run
   python manage.py move_files_to_storage
   ```
   It copies every uploaded file from the disk into the bucket under the same name, skips ones already there, and deletes nothing. It is safe to run again, and you will run it again during the freeze (step 3.3) for anything uploaded since. Afterwards, run `python manage.py check_file_scans` (README step 9.13) until "still being scanned" is 0.

3. **A safety copy of MySQL.** On **PythonAnywhere** (use your username):
   ```bash
   mkdir -p ~/backups
   mysqldump -u yourusername -h yourusername.mysql.pythonanywhere-services.com --set-gtid-purged=OFF 'yourusername$siteline' | gzip > ~/backups/before-aws-$(date +%F).sql.gz
   ```
   Download it from the Files tab and keep it somewhere safe.

## 2. Rehearsal on staging

Staging is about to hold a copy of real customer data. First stop it emailing anyone or running the daily reminders.

1. Edit `infra\terraform\envs\staging\terraform.tfvars`: set `email_enabled = false` and `enable_schedules = false`. Save, then:
   ```powershell
   cd infra\terraform\envs\staging
   terraform apply
   cd ..\..\..\..
   .\infra\scripts\deploy.ps1 staging
   ```
   (`terraform apply` changes the settings; the deploy makes sure the running site picks them up.)

2. **Empty the staging database** (it holds your smoke-test data). This deletes every row in staging, never prod:
   ```powershell
   .\infra\scripts\run-command.ps1 staging "flush --noinput"
   ```

3. **Export on PythonAnywhere** (**PythonAnywhere** console). Note the time you start:
   ```bash
   cd ~/siteline && source .venv/bin/activate
   python manage.py model_counts --exclude contenttypes auth.permission sessions axes > ~/counts-pythonanywhere.txt
   python manage.py export_data --output ~/siteline-data.json.gz
   ls -lh ~/siteline-data.json.gz
   ```
   `--natural-foreign` writes links to Django's internal table list by name instead of number, because the numbers differ in the new database. IDs are kept, so links between rows don't change.

4. **Download** both files from the PythonAnywhere **Files** tab (home folder) into your PC's Downloads folder.

5. **Load into staging** (type `staging` when asked):
   ```powershell
   .\infra\scripts\load-data.ps1 staging "$HOME\Downloads\siteline-data.json.gz" | Tee-Object counts-aws.txt
   ```
   It ends with the row counts on AWS. A failure prints the reason and loads nothing; fix it and run steps 2 and 5 again.

6. **Compare:**
   ```powershell
   .\infra\scripts\compare-counts.ps1 "$HOME\Downloads\counts-pythonanywhere.txt" counts-aws.txt
   ```
   It must say **MATCH**.

7. **Look around** at https://staging.app.pc-estimating.co.uk with your normal login: companies, projects, RFIs, variations, the pipeline. Documents won't open, because staging has its own empty files bucket. That is expected.

8. **How long did steps 3 to 6 take?** That, plus 30 minutes, is your freeze window for the real move.

9. **Clean up.** Customer data must not linger in staging or on your PC:
   ```powershell
   .\infra\scripts\run-command.ps1 staging "flush --noinput"
   Remove-Item "$HOME\Downloads\siteline-data.json.gz", "$HOME\Downloads\counts-pythonanywhere.txt", counts-aws.txt
   ```
   On **PythonAnywhere**: `rm ~/siteline-data.json.gz ~/counts-pythonanywhere.txt`. Set `email_enabled = true` and `enable_schedules = true` in the staging `terraform.tfvars` again, then `terraform apply` in `infra\terraform\envs\staging`, then `.\infra\scripts\deploy.ps1 staging`. The copy in the transfer bucket deletes itself after 7 days.

If anything went wrong, ask an engineer before the real move, with the error text.

## 3. The real move (prod)

### A week before

1. **Shorten the DNS cache time.** At your DNS host, edit the record for the address customers use today (for example `app.pc-estimating.co.uk`) and set its **TTL** to **300** seconds (5 minutes). Then the switch takes effect within minutes, and so would a rollback. Don't touch `www` or the bare `pc-estimating.co.uk` (the GitHub Pages site).
2. **SES production access granted** (README step 17). Without it, prod can only email addresses you verified.
3. **Tell customers**, e.g. "Siteline will be unavailable on Saturday from 07:00 to 09:00 for an upgrade. You'll need to log in again afterwards."

**If PythonAnywhere is on a different address** than `app.pc-estimating.co.uk` (for example `yourname.pythonanywhere.com`), links in old emails and bookmarks will stop working after the move, and the Stripe webhook needs a new address (step 3.8). Decide that before the day.

### The day before

- Prod is deployed with the same commit as PythonAnywhere, and its database is empty: the deploy only created the tables.
- Check prod answers, before DNS points at it (use the `alb_dns_name` output from `terraform output` in `infra\terraform\envs\prod`):
  ```powershell
  $ip = (Resolve-DnsName PASTE-alb_dns_name -Type A)[0].IPAddress
  curl.exe --resolve "app.pc-estimating.co.uk:443:$ip" https://app.pc-estimating.co.uk/health/
  ```
  It should print `ok`.

### On the day

1. **Freeze PythonAnywhere.** Web tab → **Disable** the web app. Tasks tab → disable the `send_reminders` and `check_file_scans` tasks (keep the backup task). If there is a `run_worker` always-on task, run `python manage.py run_worker --burst` in a Bash console first so queued emails go out, then disable it too. Emails still queued are copied with the data and sent by the AWS worker. Nobody can now change data. Stripe holds back webhooks it can't deliver and retries them for up to three days, so payments aren't lost.

2. **Final safety copy** of MySQL (step 1.3 again, with a new file name).

3. **Files uploaded since** (only if you did step 1.2): run `python manage.py move_files_to_storage` again on **PythonAnywhere**.

4. **Export** on **PythonAnywhere**: exactly step 2.3. Then **download** both files (step 2.4).

5. **Load into prod** (type `prod` when asked) and **compare**:
   ```powershell
   .\infra\scripts\load-data.ps1 prod "$HOME\Downloads\siteline-data.json.gz" | Tee-Object counts-aws-prod.txt
   .\infra\scripts\compare-counts.ps1 "$HOME\Downloads\counts-pythonanywhere.txt" counts-aws-prod.txt
   ```
   **Only continue if it says MATCH.** If not: re-enable the PythonAnywhere web app and tasks (you are back where you started), and ask an engineer.

6. **Turn on the scheduled jobs in AWS.** In `infra\terraform\envs\prod\terraform.tfvars` set `enable_schedules = true`, then:
   ```powershell
   cd infra\terraform\envs\prod
   terraform apply
   cd ..\..\..\..
   ```

7. **Switch DNS.** At your DNS host, change the `app.pc-estimating.co.uk` record to a **CNAME** whose value is the `alb_dns_name` (also shown by `terraform output dns_record_app`). If it was an A record or a CNAME to PythonAnywhere, edit or replace that one record only. Check:
   ```powershell
   Resolve-DnsName app.pc-estimating.co.uk
   ```
   Once it shows the `...elb.amazonaws.com` name (5 minutes or so), open https://app.pc-estimating.co.uk.

8. **Stripe.** If the address is the same as before, the webhook (`https://app.pc-estimating.co.uk/billing/webhook/`) and its signing secret stay as they are. If the address changed: Stripe Dashboard → Developers → Webhooks → **Add endpoint** with the new address and the same events as `siteline/README.md` step 7.3, copy its signing secret into the prod Stripe secret (README step 12, prod), and run `.\infra\scripts\deploy.ps1 prod` so the app picks it up. Delete the old endpoint later. Then on the endpoint page use **Send test event** and check it shows a 200 response.

9. **Smoke test prod** (README step 16 list): log in, open a project, open a document (the address bar briefly shows `s3.eu-west-2.amazonaws.com`), raise and delete a test RFI, send yourself a password reset email, open the Billing page.

10. **Tell customers** it's done.

11. **Clean up** your PC and PythonAnywhere as in step 2.9 (the export contains customer data). Keep the MySQL safety copies.

## 4. Rollback (first two weeks)

PythonAnywhere stays exactly as you froze it for two weeks: web app disabled, database untouched. If something serious goes wrong on AWS:

1. At your DNS host, point `app.pc-estimating.co.uk` back to PythonAnywhere (the record you had before).
2. PythonAnywhere: Web tab → **Enable** and **Reload**; Tasks tab → re-enable the two tasks.
3. Prod: set `enable_schedules = false` in `infra\terraform\envs\prod\terraform.tfvars` and `terraform apply`, so both don't send reminders.

Anything customers changed on AWS after the switch is **not** on PythonAnywhere. Decide within the first few hours if you can; after that, fixing forward on AWS is usually better than rolling back. The AWS database keeps its own backups whatever you decide.

## 5. After two weeks

1. PythonAnywhere: take one last `mysqldump` (step 1.3) and keep it offline. Delete the web app, the tasks and the MySQL database, then downgrade or cancel the plan.
2. AWS: IAM → Users → `siteline-app` → Security credentials → **Deactivate** the access key. A week later, if nothing broke, **Delete** it (and the user). The AWS app uses its own role, not this key.
3. Put the DNS TTL back to 3600 if you like.
