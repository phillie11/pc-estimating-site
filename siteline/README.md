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
| **Live-job portal for subcontractors** | Appointed subcontractors submit RAMS for review (approve, approve with comments, or revise and resubmit, with revision history) and submit variations, which the projects team approves in full or part, rejects, or passes straight on to the client as a draft variation. Each subcontractor sees only their own. Drawings reach them on transmittals (see Documents), and they acknowledge receipt in the portal. The overview shows each subcontract's order value, approved variations and RAMS status. |
| **Notifications** | A bell for everyone, staff and external. Every alert (tender invitations, addenda, queries, quotes, RFI and task assignments, variation decisions) is an in-app notification plus a short email linking to the item. Reply in the portal, not by email. |
| **Tender analytics** | Win rate by number and by value, results by month, and breakdowns by sector, size, client, lead source and project lead. Recent losses with feedback. 12 months, 24 months or all time. |
| **Programmes** | A Gantt chart per project: activities with dates, progress, milestones and a "follows" link that flags anything out of sequence or behind. Lines can be internal or shared with the client and subcontractors. The Workload page has two company-wide Gantt charts: **Tenders** (every open bid from enquiry received to tender return, with site visit, queries close and subcontractor quotes due as milestones, filterable by estimator) and **Secured work** (every won and live job from start on site to completion). |
| **Projects** | One record from first lead to completion. Overview with commercial position (contract sum + approved variations = forecast final account). **Archived, not deleted:** once a project has anything on it (documents, RFIs, variations, RAMS, packages, snags, tasks, programme, downloads) an owner or admin can only archive it. See [Archiving](#archiving-instead-of-deleting). |
| **Client & subcontractor portal** | Invite external people per project by email. They get free logins and only see what's shared with them. |
| **RFIs** | Numbered per project (RFI-001), ball-in-court assignment, due dates, responses, threaded comments, internal notes, CSV register. Company-wide RFI register. |
| **Variations** | Draft → submit to client → client approves or rejects in the portal (with PO ref). Linked to RFIs and change events. Extension of time requested and granted. CSV export. |
| **Commercial tab (staff only)** | Contract set-up per job (JCT MW/MWD/IC/ICD/DB/SBC 2016 or 2024, NEC4 ECC Options A–F, bespoke): damages, retention and release, defects period, payment terms pre-filled from the form's standard terms (advisory: check your contract), the client's contract administrator. Words follow the form (`contracts/vocabulary.py`). Change register: early warnings (EW-001) and change events (CE-001) priced as cost and revenue lines, linked to the RFI or early warning they came from, the client variation they become and the sub variations they create. Advisory NEC4 clocks (8-week notification time bar, 3-week quotation, 2-week reply, deemed acceptance) and a JCT notice-of-delay prompt. Company-wide change register at `/changes/`. |
| **Snags & tasks** | Assign to staff or subcontractors; subs mark snags "ready for inspection" from their phone. |
| **Documents** | A drawing register numbered to BS EN ISO 19650-2 (UK National Annex), with per-project code lists, revision status (work in progress, shared, published, superseded, withdrawn) and suitability. The current revision is the highest one (P01 → P10 → C01 → C02), not the last uploaded; superseded revisions stay viewable with a "Superseded by P03" banner. Bulk upload reads numbers and revisions from file names and previews each file before it's added. Numbered transmittals (TR-001) issue revisions to people or companies, who acknowledge receipt; that is what shares revisions with externals. CSV and PDF issue sheets. Externals can upload (e.g. RAMS). See [Document control](#document-control). |
| **Supply chain** | Subcontractor directory with trades, ratings, accreditations, PL/EL insurance expiry. Trade packages per project with quotes and appointment, and a one-click portal invite. A subcontractor with quotes, RAMS or variations on file is archived rather than deleted. |
| **Billing** | 14-day trial, then Stripe subscription (Starter / Team / Business). Plan limits on staff seats and active projects (archived projects don't count). External users are never charged. |
| **Needs me** | One list of everything waiting on you, across every project and register, with a count by the bell. The daily digest emails the same list (overdue, today, this week), plus tender returns in 3 days, insurance expiring and trial ending. |

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

`tenancy/tests.py` covers all of this (45 tests, including regression tests from an independent access-control review; `tenancy/test_fail_closed.py` adds 30 for the fail-closed rule, combined querysets and subqueries, the bulk-write guard, the admin and every management command run against two companies; `projects/tests.py` adds 65 more for programmes, workload, analytics, files and archived projects, `tenders/tests.py` 18 for the tender portal, `delivery/tests.py` 17 for the estimating/projects split and the live-job portal, and `core/tests/` 71 for the shared record base below). Run them after every change:

```bash
DEBUG=1 python manage.py test
```

## Document control

The Documents tab of a project (`documents/`, with `projects.Document` and `projects.DocumentRevision`).

- **Numbering.** `documents/naming.py` parses and checks ISO 19650 numbers (`SLN-ABC-ZZ-02-DR-A-1001`: project, originator, volume, level, type, role, number) as pure functions. Revision and suitability are separate fields on each revision. Each project has code lists (`CodeList`), starting from the UK defaults in `naming.DEFAULT_CODES` until someone changes one, a project code and a file-name pattern (`RegisterSettings`). A number that doesn't conform is a warning: staff tick "use this number anyway", externals' uploads are flagged for staff to check ("Numbers to check" on the register).
- **Statuses.** Making a revision current marks the previous one `SUPERSEDED` (with `superseded_by`); filing an older one marks it superseded by the current one; going back to an earlier revision supersedes the newer; withdrawing the current one hands over to the latest left. A revision's status otherwise follows its suitability (S0 work in progress, S1-S4 shared, A/B/CR published).
- **Who sees what.** `documents/access.py`. Staff see everything. An external sees a revision once it was on an issued transmittal to them or their company, or when its document is shared with their role and the revision has been shared at some point (`DocumentRevision.shared_at`, set when it leaves work in progress or is transmitted, and never cleared). So a revision that was only ever work in progress stays internal even after it is superseded or withdrawn. Documents shared before transmittals existed keep working (projects migration 0013 marked their revisions shared). If a revision was superseded by one the external hasn't been issued, they see "A newer revision is being prepared", not its number. Externals can see that a revision was withdrawn but can't download it; staff can, with a "do not use" banner. Text in PDF issue sheets is escaped before it reaches ReportLab's markup.
- **Transmittals** (`documents.Transmittal`, `TR-`, on `core.RecordBase`). Issuing one shares its revisions, notifies the recipients, puts the ball in their court until each acknowledges (any of a company's people can acknowledge for it), and queues the PDF issue sheet (`documents.transmittal_sheet` job). "Issue to usual recipients" (on uploads, the document page and bulk upload) uses the project's distribution rules for the document's discipline, else whoever it last went to, else every client, consultant and subcontractor on the project. Transmittals are only for their recipients, so their index rows stay internal: the "Needs me" registry should read `documents.models.pending_acknowledgements(user)` for externals.
- **Bulk upload.** Files are stored (and so virus-scanned) straight away, read against the project's pattern, and previewed as new document, new revision (and what it supersedes), older revision, conflict, already uploaded or needs details. A row can't be committed while its file is still being scanned or if the scan failed. Batches of more than 10 files get their checksum and page count from the `documents.file_facts` job, which waits for the scan. Uploads still pass through Django; direct browser-to-S3 upload is a follow-up.

## Building a new record type

Every numbered, per-project register (RFIs today; variations, RAMS, sub variations, snags and documents next) sits on `core.models.RecordBase`. Why it looks the way it does, and the order the existing registers move over, is in [docs/adr/0001-core-record-base.md](docs/adr/0001-core-record-base.md). `rfis/` is the worked example: copy it.

What you get by subclassing: a per-project number and stored `ref` (`RFI-007`) from a locked `NumberSequence`; `title`, `status`, `visibility` (`INTERNAL`/`SHARED`, with an `is_shared` property), `due_date`, `ball_in_court_user` and `ball_in_court_company`; `created_by/at`, `updated_by/at`; soft delete (`soft_delete()`, `restore()`, `purge()`) with `retain_until` and `legal_hold`; an optimistic-lock `version`; a `RecordIndex` row kept in step on every save; and an append-only `AuditEntry` per change with field-level diffs. It is a `TenantModel`, so everything above about tenant separation applies unchanged.

Checklist:

1. **Model.** Subclass `RecordBase`; give the subclass `class Meta(RecordBase.Meta)` so it keeps `unique (project, number)` and the managers. Declare `STATUS_CHOICES` and override `status` with your choices and default (max 20 characters). Override `visibility` only to change its default (internal unless the register is shared by default, as RFIs are). Don't add your own `number`, `title`, `project`, `created_at`, `updated_at` or ball-in-court fields: use the base ones, and label them in the form (`labels = {'title': 'Subject'}`). Add `get_absolute_url()`; the index stores it.
2. **Register** it in your `AppConfig.ready()`: `register_record_type(Model, prefix='VO', transitions=TRANSITIONS, open_statuses=(...), audit_fields=(...))`. `open_statuses` are the statuses in which the ball-in-court person has something to do ("Needs me"); `draft_statuses` are the ones in which staff may delete their own record. `audit_fields` are your own fields to diff on top of the base ones; leave out anything large or derived. `external_roles` says which external project roles see shared records of the type through the index (`('CLIENT', 'CONSULTANT')` for client variations, `()` for internal-only types); leave it out and every external role on the project does.
3. **Transitions.** In `yourapp/workflow.py`, declare a list of `core.workflow.Transition(name, source=(...), target=..., label=..., allowed=..., sets_ball_in_court=..., on_apply=...)`. `allowed(record, user)` decides who may make it (`core.access.record_staff` and `record_staff_or_ball_in_court` cover the common cases); `on_apply(record, user, **params)` sets the other fields (`responded_at`, `closed_at`). In views call `apply_transition(record, name, request.user, **params)`, never set `status` by hand. It raises `TransitionForbidden` (a `PermissionDenied`, so a 403) and `InvalidTransition` (show it as a message). Use `available_transitions(record, user)` to decide which buttons to show.
4. **Saving.** Always `record.save(actor=request.user)` so `created_by`/`updated_by` and the audit trail know who did it; pass `audit_action='reassigned'` for a named non-status change. Catch `core.exceptions.RecordConflict` where a save could race. Edit forms subclass `core.forms.RecordFormMixin` first (`class VariationForm(RecordFormMixin, StyledModelForm)`) so a stale form is refused. `queryset.update()` skips the audit, index and version: don't use it on records in app code.
   Who may soft-delete, restore, purge, set legal hold and redact is decided in `core.access` (`can_soft_delete`, `can_restore`, `can_purge`, `can_set_legal_hold`, `can_redact`), and the record methods ask it, so don't repeat those rules in views. Records are kept until their project's completion plus the organisation's `core.RetentionPolicy` years (default 12), and nothing purges automatically. For GDPR erasure use `core.models.redact(record_or_user, actor, reason)`, the only sanctioned change to audit rows.
5. **Access.** `Model.objects` hides soft-deleted rows (`with_deleted` shows them, `all_objects` is unscoped as everywhere). Views still fetch the project with `projects.access.get_project()` and filter externals to `visibility=VIS_SHARED`. Anything listing records across types reads `RecordIndex` through `core.access.visible_index(request)` (or `needs_me(request)`), never `RecordIndex.objects` directly. If externals' rule is narrower than visibility (a subcontractor sees only their own), the index can't serve them yet: see the ADR.
6. **Adopting an existing register** takes three migrations, as `rfis 0003`–`0005`: (a) rename fields to the base names and add the new columns, (b) a reversible `RunPython` that fills `ref`/`visibility` and creates the `NumberSequence`, `RecordIndex` and back-filled `created` `AuditEntry` rows from the historical models, (c) drop the old columns. Keep data changes and `ALTER TABLE`s in separate migrations (PostgreSQL). A new register only needs its `CreateModel`.
7. **Tests.** Copy `core/tests/base.py` for set-up and add your type's versions of: numbering per project (`test_records.NumberingTests`), index kept in step on create/update/transition/soft delete (`IndexTests`), audit diffs (`AuditTests`), each transition's permissions and source statuses through the views (`test_workflow`), another company can't see it and no organisation fails closed (`test_tenancy`), and, when adopting an existing register, the forward and backward data migration on seeded data (`test_rfi_migration`).

## Cost control and the cost ledger

*Commercial → Costs* on a won or live job (`costs/`): cost report, budget, orders, actual costs and the ledger. Only the job's project manager (its manager, or a PM or QS on its team), owners and admins see it (`costs/access.py`); externals never do, and archived jobs are read only. Owners and admins keep the company's cost code library (seeded with UK fit-out defaults on first use) and the margin warning (default 10%) under *Settings → Cost control*.

- **Ledger.** Every budget, commitment and actual movement is a `costs.LedgerEntry`. It is append-only: `save()` of an existing row, `delete()` and queryset `update()`/`delete()`/`bulk_update()` raise `PermissionError`. Other apps post through `costs.ledger`:
  ```python
  from costs import ledger
  entry = ledger.post(project, cost_code, ledger.ACTUAL, Decimal('1250.00'), source=payment, actor=user, note='Valuation 3')
  ledger.reverse(entry, actor=user, reason='Posted to the wrong job')   # equal and opposite entry, once
  ledger.totals(project)   # {cost_code_id: {'BUDGET': Decimal, 'COMMITMENT': Decimal, 'ACTUAL': Decimal}}
  ```
  Amounts are Decimal/int/str (floats raise), rounded half up to 1p. The source is any saved row (a generic link). Only ACTUAL entries can be reversed: budgets are corrected by a transfer or revision and orders by an adjustment (`costs.services`), and `reverse()` refuses anything else. New orders and order adjustments carry a one-time form key, so a double submit records once.
- **Budget** is set once from the tender (value less margin %, spread over codes; the lines must add up), then changes only by a transfer (between codes) or a revision (changes the total), each with a reason. **Orders** come from appointed packages (*Commit this order*) or are entered by hand, and change only through recorded adjustments, including *Add to order* for approved sub variations. **Actual costs** are invoices or timesheet totals with an optional attachment (validated and served by `projects/files.py`); a wrong one is reversed, never deleted.
- **Report** (`costs/report.py`) reads only the ledger: forecast final cost per code is the latest manual forecast, else the larger of budget, committed and actual; margin is against `contracts.commercial.commercial_position`'s forecast final account. Below the company threshold it shows red and lists the job on its PM's and admins' *Needs me* (`costs/actions.py`). The money rules and their fixed test table are in `costs/money.py` and `costs/tests/test_money.py`.

## Needs me, dashboards and the portal

Anything waiting on a person is listed through one registry, `actions` ("Needs me"). Don't add a dashboard card of your own for it: register a provider.

- **Registry.** `actions.register(provider)` from your `AppConfig.ready()`; `actions.for_user(request)` returns `actions.Item`s (`kind`, `title`, `ref`, `project`, `due`, `url`, `priority`, `action`, `status_label`, `tone`, `amount`) soonest due first, cached on the request; `actions.grouped(items)` splits them into Overdue / Today / This week / Later or no date. A provider has `kind`, `label`, `items(request)` and optionally `covers` (record types it already lists, so the record-index provider skips them). It must apply the same access rules as the page its items link to. The built-ins are listed at the top of `actions/providers.py`: open ball-in-court records from `core.access.needs_me`, client variations to approve, RAMS to revise (subcontractor) and to review (the job's team), sub variations to decide, unacknowledged addenda and tenders to price (tendering subcontractors), open snags, snags to inspect and tasks. `actions/tests.py` checks every provider against two companies and with no organisation.
- **Where it shows.** The *Needs me* page (`/<org>/needs-me/`, filter with `?kind=` and `?project=`), the count next to the bell, the staff dashboards' *Needs me* card, the client and subcontractor dashboard's *Decisions waiting* (with the money for clients), each project's *Decisions* tab for externals, and the round-up email (`send_reminders`). The page is paginated (50 a page) and the badge shows "99+" past 99. Amounts someone approves show to the penny (`money_exact`); dashboard tiles keep `money_short`.
- **Round-up email.** Sent only when something is overdue or due within 3 days (or, for staff, a tender return, insurance lapse or trial end within 3 days). Undated items never send it on their own; when it goes they are listed, capped at five (`actions/digest.py`). Each person, staff or external, picks daily (default), weekly on Mondays, or off on their account page (`actions.DigestPreference`, one row per login). Every email ends with a signed link (`/round-up/<token>/`) that changes it without logging in; opening the link changes nothing until a button is pressed.
- **Hooks for apps on other branches.** `actions/providers.py` `HOOKS` plugs in `tenders.actions.coverage_alerts(request)` (staff), `changes.actions.items(request)` and `documents.models.pending_acknowledgements(user)` (externals) by dotted path. Each lists nothing until its module exists; results (Items, dicts or model rows) on projects outside `portal_projects` are dropped.
- **Dashboards.** Every number on the estimating and projects dashboards links to the list behind it, using the filters in `actions/filters.py` (`/projects/?view=live&attention=rams`, `/bids/?view=open&due=14`, `?mine=1`; `/rfis/?status=open&scope=live&overdue=1`), so the number and the list always agree.
- **Layouts.** `templates/app.html` picks `layouts/staff.html` (sidebar) or `layouts/portal.html` (top bar with a project switcher, no sidebar) for external collaborators. Externals get at most five project tabs (Overview, Decisions, Documents, Programme, Snags); the registers under each (RFIs, Variations, *Your variations*, *Your RAMS*, tasks, who's who) show as a second row. On the overview they see *What's changed since your last visit*, from `actions.ProjectVisit` (one row per `ProjectMember`). Staff get at most eight tabs on a live job: seven in the row and a *More* menu; tab order and labels are in `actions/tabs.py`.
- **Screen parts.** `templates/partials/status_badge.html` (or `{% status_badge obj %}`), `empty_state.html`, `register_table.html` and `decision_panel.html`. Staff can see them all at `/<org>/_ui/`.

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

## Sign-in security

All in `security/`; the route lists are in `security/middleware.py` and `security/ratelimit.py`, so views elsewhere don't change.

- **Two-factor sign-in** (an authenticator app, RFC 6238 codes). Required for workspace owners and admins and platform staff: `TwoFactorEnforcementMiddleware` sends them to *Set up two-factor sign-in* wherever they go until it's on (only sign-out and the set-up pages are open). Optional for everyone else, from *Your account*. The password step doesn't sign anyone in when two-factor is on; only the right code does. Ten single-use recovery codes, stored as salted PBKDF2 hashes and shown once. *Remember this device for 30 days* sets a signed, `HttpOnly`, `Secure` cookie tied to the user, their password hash and their authenticator, so a password change or a reset ends it. Wrong codes count towards the django-axes lockout, and ten wrong codes in a row, from anywhere, lock codes for that account until a recovery code is used or an owner resets it (both are recorded in the admin under **Security → Security events**). The QR code is drawn on the server (`segno`), and the key is shown for typing in by hand. The key being set up is kept only in that person's session until a code from it is typed, so someone else who knows the password can't read it in advance. An owner can reset a member's two-factor from *Team → Two-factor sign-in* (it's logged in Activity and the member is emailed), unless that person also works in another company's workspace or is platform staff (then only platform support can, by deleting the device in the admin).
- **The risk of a reset.** After a reset, the next person to sign in with that member's password is asked to set up two-factor with *their* phone. So an owner should only reset after checking it's really the member asking (in person or by phone, not just by email), and the member is emailed straight away: *"Your two-factor sign-in was reset ... If this wasn't expected, contact your owner"*. If a reset wasn't expected, change the member's password before they next sign in.
- **Step-up.** Billing, company settings, inviting staff or external collaborators, role and member changes, CSV exports and archiving, restoring or deleting a project or subcontractor need a password or code in the last 10 minutes (`STEP_UP_SECONDS`); otherwise the person is asked to confirm it's them, then sent back.
- **Rate limits**, counted in their own table (`security.RateLimitCounter`, one atomic UPDATE per request, never culled like a cache) and answered with a "Too many tries" page (429): sign-up, password reset (per address and per email), login, invites, invite links, two-factor codes, uploads, exports, search and the Stripe webhook. IPv6 addresses count per /64. Rows for finished windows are deleted as new ones start. The site cache is the database too (`security/migrations/0002` makes its table on `migrate`, the same as `python manage.py createcachetable`).
- **Headers**: Content-Security-Policy (scripts only from this site; `CSP_REPORT_ONLY=1` to trial a change), Permissions-Policy, `Referrer-Policy: same-origin`, `nosniff`, HSTS for a year by default when `DEBUG` is off (`SECURE_HSTS_SECONDS` overrides), and a `__Host-` session cookie. Fonts are self-hosted in `static/fonts/`, so pages load nothing from Google. `manage.py check --deploy` reports only `security.W005` and `security.W021`, both deliberate (see `config/settings.py`).
- **The visitor's IP address** for limits and lockouts comes from `REMOTE_ADDR` unless `CLIENT_IP_HEADER` says otherwise: set `CLIENT_IP_HEADER=X-Real-IP` on PythonAnywhere. AWS needs nothing (`config/alb_wsgi.py`). **Only set it when the app can't be reached except through the proxy that sets that header**: if the server also answers directly (on its own IP address, say), anyone can send the header themselves and pick any address, dodging the limits and the login lockout. `check --deploy` warns (`security.W003`) if the header is set and `ALLOWED_HOSTS` contains a bare IP address.
- **Tests.** The test runner (`security/testing.py`) switches two-factor enforcement off so the other apps' tests can sign in with `force_login`; `security/tests/` switches it back on.

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

Owners and admins (`owner@demo.test`) are asked to set up two-factor sign-in with an authenticator app on first sign-in.

Emails print to the console while `EMAIL_BACKEND` is unset.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every pull request and every push to `main`:

- **Tests**, on Python 3.13, one job per database: `Tests (sqlite)`, `Tests (postgres)` (PostgreSQL 16, the planned AWS RDS database) and `Tests (mysql)` (MySQL 8.0, what PythonAnywhere runs in production today). Each job runs `manage.py check`, `manage.py makemigrations --check --dry-run` (fails if a model change has no migration), then the full test suite under `coverage`, with the report printed in the log and the job summary. There is no coverage threshold yet.
  - The MySQL job uses the `mysql:8.0` service container with the same settings as production (`DB_ENGINE=mysql`: utf8mb4, `STRICT_TRANS_TABLES`, `CONN_MAX_AGE=280`). It connects as root so the test runner can create `test_siteline`. It installs `pkg-config`, `default-libmysqlclient-dev` and `build-essential` from apt and compiles `mysqlclient`. It also checks that the server is 8.0 with utf8mb4 and strict mode, and runs `manage.py check --database default` for the MySQL-specific checks. Warnings there are printed but do not fail the job; the one known warning, `mysql.W003` on `projects.FileScan.path`, is fine on MySQL 8.0 InnoDB. Any migration that relies on database-specific behaviour, such as `SELECT ... FOR UPDATE SKIP LOCKED`, must pass this job before it is deployed to PythonAnywhere.
  - The SQLite and PostgreSQL jobs leave `mysqlclient` out of the install because it has to be compiled and they do not use it.
  - Branch protection: once `Tests (mysql)` has passed on `main`, add it to the required status checks next to `Tests (sqlite)` and `Tests (postgres)`.
- **Security scan**: `pip-audit` on `requirements.txt` and `requirements-dev.txt`, and `bandit -r siteline -ll`. Both are blocking: a known advisory in any pin, or a medium-or-higher Bandit finding, fails the job. A reviewed false positive gets `# nosec <ID>` and a comment on that line saying why.

Dependabot (`.github/dependabot.yml`) opens weekly update PRs for the pip requirements and for the GitHub Actions, which are pinned to commit SHAs. The `@claude` workflow (`.github/workflows/claude.yml`) only runs for repository owners, members and collaborators.

To run the same checks locally:

```bash
pip install -r requirements.txt -r requirements-dev.txt
DEBUG=1 MEDIA_ROOT=$(mktemp -d) python -m coverage run manage.py test && python -m coverage report
pip-audit -r requirements.txt --no-deps --disable-pip   # checks the pins without building mysqlclient
bandit -r . -ll --exclude ./.venv
```

## Deploy on PythonAnywhere

**Start with [docs/pythonanywhere.md](docs/pythonanywhere.md).** It sets up a staging site with demo data, moves prod from MySQL to PostgreSQL and adds the background worker. The steps below are the original first install on MySQL; steps 6, 7 and 9 (domain, Stripe, file storage) still apply as written.

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
| 05:15 on the 1st of each month | `cd ~/siteline && .venv/bin/python manage.py update_bank_holidays` (refreshes UK bank holidays from gov.uk for payment dates; the bundled table covers 2020–2030) |
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

Not needed yet: see the last section of [docs/pythonanywhere.md](docs/pythonanywhere.md) for when it is. The move from PythonAnywhere to AWS (ECS Fargate, RDS PostgreSQL, SES) is scripted in `infra/`. Follow `infra/README.md`; the data move is in `infra/scripts/migrate-data.md`.

## Running the platform

- Admin is at `/<ADMIN_PATH>/` (default `platform-admin`). From there you can see every company, its plan and status, extend a trial (`trial_ends_at`) or suspend a workspace (`is_suspended`).
- Login is rate-limited (6 failures locks that email + IP for an hour; wrong two-factor codes count too). Unlock with `python manage.py axes_reset`.
- Someone who has lost their phone and their recovery codes: an owner of their workspace resets it from *Team → Two-factor sign-in*. If they also work in another company's workspace, delete their device under **Security → Two-factor devices** in the platform admin instead (after checking it's really them).

## Before you take money: open items

- **Terms, privacy policy and a data processing agreement.** You'll be a data processor for your customers' client and subcontractor data under UK GDPR. Register with the ICO.
- **Email verification** on sign-up isn't built yet. Invite links do prove the email address.
- **Storage.** Set up cloud file storage (step 9) before customers upload drawings. PythonAnywhere's disk fills quickly.
- **Scale.** PythonAnywhere suits the first few dozen customers. If you outgrow it, the code runs unchanged on Render, Railway or a VPS with Postgres (`DB_ENGINE=postgres`), and subdomain workspaces become possible.

## Project layout

```
config/       settings, root URLs
accounts/     User (email login), sign-up, invites, profile
tenancy/      Organization, Membership, tenant middleware/manager, notifications, dashboard, team, settings, digest, demo seed, tests
billing/      Stripe checkout, portal, webhook
projects/     projects, pipeline, team & portal invites, tasks, snags, documents, comments, access rules
core/         shared record base: numbering, record index, links, audit trail, transitions (docs/adr/0001)
rfis/         RFI register (the first record type on core)
variations/   variations & client approval (on core.RecordBase)
contracts/    main contract set-up per project, standard terms (presets.py), contract vocabulary, the Commercial tab
changes/      early warnings, change events, change registers, advisory contract clocks (clocks.py), Needs-me items (actions.py)
costs/        cost codes, budgets, commitments, actual costs, the append-only cost ledger (ledger.py), cost report
holidays/     UK bank holidays by region (bundled gov.uk-format table, `manage.py update_bank_holidays`), Construction Act day counting
directory/    subcontractors, trades, trade packages and quote comparison
tenders/      portal tenders: invitations, addenda, tender queries, appointment rules
delivery/     live-job portal: RAMS review, subcontractor variations, subcontract summary
documents/    drawing register naming, code lists, bulk upload, transmittals, issue sheets
docs/adr/     architecture decision records
jobs/         background job queue and worker (run_worker, jobs_status), email outbox
actions/      "Needs me" registry and providers, project tab layout, portal visits, dashboard filters
security/     two-factor sign-in, step-up, rate limits, security headers
templates/    shared templates; app-specific ones live in each app
static/       app.css (single stylesheet; --accent is set from each company's brand colour, --accent-ink to white or near-black, whichever contrasts more), app.js
```
