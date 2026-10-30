# ADR 0001: A shared record base (`core`), proven on RFIs

- **Status:** accepted for RFIs; variations, RAMS, sub variations, snags and documents to follow
- **Date:** 2026-10-30
- **Code:** `core/` (models, registry, workflow, access, forms), `rfis/` (first adopter), migrations `core 0001`–`0002`, `rfis 0003`–`0005`

## Context

Every register in Siteline (RFIs, client variations, subcontractor variations, RAMS, snags, documents) grew its own version of the same things: a per-project number (`save_with_number`, which locks the whole project row), a status field changed by hand in views, a ball-in-court field with a different name each time (`assigned_to`, `assignee`), a visibility flag (`is_shared` on RFIs, `visibility` on documents), and no history beyond the activity feed. The next features ("Needs me", global search, linking a variation to the RFI it came from, retention and legal hold for disputes) all need to treat records of every type the same way. Building them against six different shapes would mean six copies of each feature.

## Decision

A `core` app with:

| Piece | What it is |
|---|---|
| `RecordBase` | Abstract `TenantModel` every register subclasses: `project`, `number`, `ref` ("RFI-007", stored), `title`, `status`, `visibility` (`INTERNAL`/`SHARED`), `due_date`, `ball_in_court_user`, `ball_in_court_company` (nullable FK to `directory.Subcontractor`), `created_by/at`, `updated_by/at`, `deleted_at/by`, `retain_until`, `legal_hold`, `version`. |
| `NumberSequence` | `(organization, project, key, next_value)`, unique on `(project, key)`. `next_number()` takes the row with `select_for_update()` inside the record's save transaction, so a failed save gives the number back and only that project's numbering for that type waits. The first record of a type on a project creates the row, starting after the highest number already used; two requests racing to create it are handled by the unique constraint and a savepoint. |
| `RecordIndex` | One row per record of any type (`content_type`, `object_id`, `record_type`, `project`, `ref`, `title`, `status`, `status_label`, `is_open`, both ball-in-court fields, `due_date`, `visibility`, `url`, timestamps, `deleted_at`, `purged_at`). Written by `RecordBase.save()` in the same transaction. "Needs me", search and links read this. |
| `RecordLink` | `(from_idx, to_idx, kind)` between index rows in one organisation. |
| `AuditEntry` | `(record_idx, actor, action, changes, note, at)`, `changes` as `{"field": [old, new]}` for the type's audited fields. Append-only. |
| `core.workflow` | `Transition(name, source, target, label, allowed, sets_ball_in_court, on_apply)` declared in code per type, and `apply_transition(record, name, user, **params)`. No configurable engine. |
| `core.registry` | `register_record_type(Model, prefix=..., transitions=..., open_statuses=..., audit_fields=...)`, called from the app's `AppConfig.ready()`. An unregistered subclass refuses to save. |

`RecordBase.save(actor=..., audit_action=..., audit_note=...)` does, in one transaction: number it (first save), fill `ref`, set `created_by`/`updated_by`, bump `version` with the UPDATE limited to the version the copy was loaded at (else `RecordConflict`), write the index row, and write an `AuditEntry` (`created`, `updated` when an audited field changed, or the transition/action name). The diff is taken against a snapshot made when the row was loaded (`from_db`), so it costs no extra query.

Everything is a `TenantModel`: scoped to the current organisation, fails closed without one, refuses writes into another company. The index, link and audit rows always carry the record's organisation explicitly, so record saves also work inside `unscoped()`.

### RFIs adopt it

`rfis.RFI` now subclasses `RecordBase`. The data migration keeps every RFI's id and number.

- `rfis 0003`: renames `subject → title`, `assigned_to → ball_in_court_user`, `raised_by → created_by` (data stays in place), adds `ref`, `visibility` and the new base columns.
- `rfis 0004`: copies `is_shared` into `visibility`, fills `ref`, creates a `NumberSequence` per project (next = highest + 1), a `RecordIndex` row per RFI and a back-filled `created` `AuditEntry` (actor = who raised it, at = when, empty diff, a note saying it was back-filled). Reverse copies `visibility` back into `is_shared` and removes the RFIs' index, link, audit and sequence rows.
- `rfis 0005`: drops `is_shared`.

Data and table changes are in separate migrations because PostgreSQL refuses to alter a table with row changes pending in the same transaction. `core/tests/test_rfi_migration.py` runs forward and backward on seeded data from two companies.

RFI status changes now go through `rfis/workflow.py`: `respond`, `respond_and_close`, `close`, `reopen`, `void`. The views, URLs, templates and CSV are unchanged for users. `Pass ball` is an ordinary audited save (`reassigned`), not a transition, because it doesn't change status.

## Deviations from the reference design, and why

1. **Shared field names, so RFI fields were renamed.** The alternative was a per-type map ("RFI's title is called `subject`"), which every reader of the index, search and audit would have to go through forever. Renaming once, in the first adopter, is cheaper. The RFI form keeps "Subject" as its label and keeps the *Visible to project collaborators* tick box (a form field over `visibility`). `RecordBase.is_shared` is a read/write property over `visibility`, so `RFI(is_shared=False)` and `{{ rfi.is_shared }}` still work. ORM lookups must use the new names. Eleven test lines that built RFIs or posted the form were updated to the new names (`tenancy/tests.py`, `tenancy/test_fail_closed.py`, `tenders/tests.py`).
2. **`NumberSequence.project` is required.** An organisation-wide sequence (`project` null) can't be made unique portably: `nulls_distinct=False` is PostgreSQL 15+ only and conditional unique constraints are ignored on MySQL, which PythonAnywhere still runs. Every record type so far is per project. If one isn't, give it a separate key on an organisation-level table.
3. **The sequence key is the model label (`rfis.rfi`), not the prefix.** Prefixes are display text and could change; the label doesn't. `ref` is stored, so changing a prefix later needs a re-save or data migration for old rows.
4. **`RecordLink` is immutable, not strictly append-only.** A wrong link has to be removable. Links are never updated (save on an existing row, `update()` and queryset `delete()` are refused). `unlink()` is the only way to remove one, and both `link()` and `unlink()` write an `AuditEntry` on both ends, so the history of links is append-only even though the link table isn't.
5. **Append-only is enforced in the model, not the database.** `AuditEntry.save()` refuses to change an existing row and always INSERTs (`force_insert`), so `AuditEntry(pk=<existing>).save()` is an IntegrityError rather than an overwrite; `RecordLink.save()` does the same. `delete()`, queryset `update()`, `delete()`, `bulk_update()` and `bulk_create(update_conflicts=True)` all raise, on both `objects` and `all_objects`, and a `pre_save` guard stops `loaddata`/`import_data` (which save raw, bypassing `save()`) overwriting a row whose pk already exists with different values (re-loading an identical row, as the export round-trip test does, changes nothing and is allowed). Two writes are let through: Django's delete collector setting a deleted user's FKs to NULL (only user FKs, only to NULL; `actor_name` keeps the name), and `redact()` (point 10). Django's delete cascade (deleting a whole workspace, `seed_demo --reset`) still removes them, because it uses `_raw_delete`/delete batches rather than those methods, and `AuditEntry → RecordIndex` is `RESTRICT`, which allows the delete only when the index row goes in the same cascade. Raw SQL and migrations can still remove rows. **With Postgres:** add a trigger that raises on `UPDATE` and on `DELETE` unless a session setting (set by the workspace-deletion path) allows it, and revoke `UPDATE`/`DELETE` on `core_auditentry` from the app's database role.
6. **`RecordIndex` has `is_open`, `status_label`, `url` and `record_type` as well.** "Needs me" needs to know if the record is waiting on someone (a closed RFI still has a ball-in-court user but isn't waiting on them), and lists across types need a label and link without loading each record. Each type declares `open_statuses` when it registers.
7. **Purge keeps the index row and the audit trail.** `purge()` needs an owner, the record soft-deleted, no legal hold and its keep-until date passed (point 11); it then deletes the record row, marks the index row `purged_at` and writes a `purged` entry. A bare `delete()` on a record is refused. Because index rows outlive the record, `Project.has_records()` now counts index rows and soft-deleted records, so such a project is archived, not deleted.
8. **Optimistic locking reaches the forms.** `core.forms.RecordFormMixin` adds a hidden `version` and refuses a save from a stale form ("Someone else changed this..."). Without it, the lock would only catch races inside one request.
9. **`ball_in_court_company` exists but RFIs don't set it yet.** No RFI screen picks a company today.
10. **Redaction is the one sanctioned change to audit rows.** `core.models.redact(record_or_user, actor, reason)` replaces personal values with `[redacted]`: for a record, every value in its audit diffs and its title (record and index); for a user, the actor link and `actor_name` on entries they made, user-FK values that pointed at them, and their name or email inside diff strings and titles. It writes a `redacted` entry with the reason and no values. Free text on the live record (an RFI's question) is not touched; edit or purge the record for that.
11. **Retention lives in `core.RetentionPolicy`, not on `Organization`.** One row per organisation (`retention_years`); with no row the default is `settings.RECORD_RETENTION_YEARS`, or 12 if that isn't set. A record's effective keep-until date is its own `retain_until` if set, else its project's completion date plus that period, so it follows the project if completion moves. With no completion date and no `retain_until`, a record can't be purged. Kept out of `tenancy` so the policy can grow (per type, per sector) and so this change doesn't collide with other work on `tenancy` migrations.

### Fixed after the independent review

- Deleting a user with audit entries or links failed (the collector's SET_NULL update was refused): allowed for user FKs set to NULL, and `AuditEntry.actor_name` snapshots the name. `core 0002` adds it and fills it for existing entries.
- `AuditEntry(pk=existing).save()`, `RecordLink(pk=existing).save()`, upserts and `loaddata` could overwrite rows: refused (point 5).
- A stale copy of a purged record was re-INSERTed by Django when its version-guarded UPDATE matched nothing: `_do_update` now always raises `RecordConflict` in that case.
- First number on a project: `next_number()` reads without a lock first; only when the sequence row is missing does it lock the project row, then re-read with a locking read, so first-RFI races serialise on the project and can't deadlock on MySQL gap locks. A deadlock is still retried once from a savepoint (on MySQL a deadlock rolls back the whole transaction, so there the retry fails loudly rather than recovering). Not verified on MySQL.
- A failed first save puts `pk`, `number` and `ref` back so a retry is clean; a number set by hand moves the sequence past it; `save(update_fields=...)` audits only the fields saved (the rest show up when they are saved); link notes name the other record by ref only.

## Alternatives considered

- **Leave each register as it is and build "Needs me" as a union of per-type queries.** No migration, but every cross-type feature (search, links, retention, audit) is N implementations, and the union gets slower and harder to secure (each half needs its own tenancy and visibility rules) with every type.
- **Multi-table inheritance (a concrete `Record` table with a one-to-one per type).** One table to query, but every read of a register joins it, every save writes two rows, and Django's MTI makes per-type ordering, constraints and admin awkward. The index gives the same single table for cross-type reads without making each register pay for it.
- **A third-party audit/history package (django-simple-history, django-reversion) and a workflow package (django-fsm, viewflow).** New dependencies the brief rules out, and they don't know about tenancy: history tables would need the same fail-closed scoping added by hand, and a configurable workflow engine is more than five-status registers need.
- **Database triggers for the index and audit.** Can't be bypassed by a careless `update()`, but they'd need writing for SQLite, MySQL and PostgreSQL, and wouldn't know the actor. Revisit for audit immutability once on Postgres (point 5).
- **Keep locking the project row for numbering.** Works, but serialises every numbered save on a project across all types. One row per (project, type) is no more code.

## Consequences

- New registers get numbering, ball in court, visibility, soft delete, retention, audit, index rows and transitions by subclassing and registering. The README section *Building a new record type* is the checklist.
- `queryset.update()` and `bulk_create()` on a record bypass `save()`, so they skip the version bump, audit and index. Use them only where that's intended (test set-up does), and call `core.models.index_record(record)` afterwards if the index must follow.
- Every record save writes one more row (the index) and usually one audit row. Fine at our size; the index has the two indexes "Needs me" and registers use.
- `RFI.objects` hides soft-deleted RFIs everywhere, including `project.rfis`. There's no UI to delete or restore an RFI yet, so nothing changes for users. A variation linked to a soft-deleted RFI keeps the link (the FK follows the base manager) but the RFI page 404s, and the variation edit form won't offer that RFI.
- Purging an RFI leaves its comments (generic FK, no cascade) and its attachment file in storage. Purge has no UI or scheduled job yet; decide both before one exists (see open questions).
- `save_with_number` stays for variations, RAMS, sub variations and snags until they move.
- **Rolling back the RFI migration loses history.** Reversing `rfis 0004` deletes every RFI's index, link and audit rows, including everything recorded after deploy, and the old schema has nowhere to keep soft-delete state, versions, `updated_by` or legal hold, so those are lost too (soft-deleted RFIs reappear). Take an `export_data` dump before rolling back if the trail matters. A dump taken **before** deploy has the old RFI columns, so restore it into the old release's schema with `import_data` and then migrate forward; don't load it into a migrated database.
- The `core` → `projects`, `directory` dependency is one way; `projects` must never import `core` at module level (`Project.has_records` deliberately checks for a `with_deleted` manager instead of importing).

## Migration plan for the other types

One type per change, each with the same three-step migration as RFIs and a forward/backward test copied from `core/tests/test_rfi_migration.py`:

1. **Client variations (`variations.Variation`, `VO-`).** Already has `title`, `status`, `project`, `number`. Rename `raised_by → created_by`; add `visibility`: drafts are `INTERNAL` (the data migration sets drafts internal and everything else shared), and the `submit` transition's `on_apply` sets it to `SHARED` (recommended default, pending owner confirmation). Transitions: `submit` (staff), `approve` / `reject` (client, via `can_decide_variations` rules moved into a predicate), `withdraw`. Ball in court: the client while submitted. Replace the `Variation.rfi` FK with a `RecordLink(DERIVED_FROM)` in a later step, keeping the FK until every reader uses links.
2. **Subcontractor variations (`delivery.SubVariation`, `SV-`).** Ball-in-court company = the subcontractor. Their visibility rule (a subcontractor sees only their own) is narrower than `visibility`, so before externals read them from the index, add an audience column to `RecordIndex` (e.g. `audience_company`) and teach `core.access.visible_index` about it.
3. **RAMS (`delivery.Rams`, `RAMS-`).** Numbered per project but revised (`unique (project, number, revision)`). Either each revision is a record sharing a number (sequence taken only for revision 1), or the RAMS is the record and revisions are child rows. Prefer the second; decide before migrating.
4. **Snags (`projects.Issue`, `IS-`).** `assignee → ball_in_court_user`, `created_by` already matches. Visibility today is "assigned to them" for externals, so the same audience work as step 2 applies.
5. **Documents (`projects.Document`).** Already has `visibility`. Not numbered per project today and revisioned like RAMS; adopt last, once the revision question is settled for RAMS.

After the last one, remove `save_with_number`.

## Recommended defaults, pending owner confirmation

These are implemented so work can carry on, but the product owner hasn't confirmed them. Each lives in one place so it is a small change either way.

1. **Answering an RFI passes the ball back to whoever raised it**, to accept and close or reopen; responded RFIs count as open ("Needs me") for that person. Change: `BALL_AFTER_RESPONSE` in `rfis/workflow.py` (`core.workflow.KEEP` to leave it with the responder) and `open_statuses` in `rfis/apps.py`.
2. **Retention: project completion + 12 years**, configurable per organisation (`core.RetentionPolicy`), default `settings.RECORD_RETENTION_YEARS`. **Only owners and admins set or clear legal hold** (`core.access.can_set_legal_hold`). **Nothing purges automatically**; `purge()` is owners only (`core.access.can_purge`), and an owner-only purge action will come later.
3. **GDPR erasure overrides append-only, by redaction** (`core.models.redact`, owners, admins or a superuser: `core.access.can_redact`).
4. **Staff soft-delete their own drafts; admins soft-delete and restore anything** (`core.access.can_soft_delete`, `can_restore`; drafts are each type's `draft_statuses`). No UI yet.
5. **Voiding a closed RFI requires reopening it first** (the `void` transition's `source` in `rfis/workflow.py`).
6. **Draft variations are internal and become shared automatically on submit** (migration plan step 1).

## Open questions for the product owner

Confirm or change the six defaults above. Still open beyond them: does purge also need to remove an RFI's comments and attachment file, and should redaction also scrub free text on the live record (an RFI's question and response)?
