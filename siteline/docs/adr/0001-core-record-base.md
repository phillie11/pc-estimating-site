# ADR 0001: A shared record base (`core`), proven on RFIs

- **Status:** accepted for RFIs; variations, RAMS, sub variations, snags and documents to follow
- **Date:** 2026-10-30
- **Code:** `core/` (models, registry, workflow, access, forms), `rfis/` (first adopter), migrations `core 0001`, `rfis 0003`–`0005`

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
5. **Append-only is enforced in the model, not the database.** `AuditEntry.save()` refuses to change an existing row, and `delete()`, queryset `update()`, `delete()` and `bulk_update()` all raise, on both `objects` and `all_objects`. Django's delete cascade (deleting a whole workspace, `seed_demo --reset`) still removes them, because it uses `_raw_delete`/delete batches rather than those methods, and `AuditEntry → RecordIndex` is `RESTRICT`, which allows the delete only when the index row goes in the same cascade. Raw SQL and migrations can still remove rows. **With Postgres:** add a trigger that raises on `UPDATE` and on `DELETE` unless a session setting (set by the workspace-deletion path) allows it, and revoke `UPDATE`/`DELETE` on `core_auditentry` from the app's database role.
6. **`RecordIndex` has `is_open`, `status_label`, `url` and `record_type` as well.** "Needs me" needs to know if the record is waiting on someone (a responded RFI still has a ball-in-court user but isn't waiting on them), and lists across types need a label and link without loading each record. Each type declares `open_statuses` when it registers.
7. **Purge keeps the index row and the audit trail.** `purge()` needs the record soft-deleted, no legal hold and `retain_until` passed; it then deletes the record row, marks the index row `purged_at` and writes a `purged` entry. A bare `delete()` on a record is refused. Because index rows outlive the record, `Project.has_records()` now counts index rows and soft-deleted records, so such a project is archived, not deleted.
8. **Optimistic locking reaches the forms.** `core.forms.RecordFormMixin` adds a hidden `version` and refuses a save from a stale form ("Someone else changed this..."). Without it, the lock would only catch races inside one request.
9. **`ball_in_court_company` exists but RFIs don't set it yet.** No RFI screen picks a company today.

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
- The `core` → `projects`, `directory` dependency is one way; `projects` must never import `core` at module level (`Project.has_records` deliberately checks for a `with_deleted` manager instead of importing).

## Migration plan for the other types

One type per change, each with the same three-step migration as RFIs and a forward/backward test copied from `core/tests/test_rfi_migration.py`:

1. **Client variations (`variations.Variation`, `VO-`).** Already has `title`, `status`, `project`, `number`. Rename `raised_by → created_by`; add `visibility` (all `SHARED` except drafts, or keep "drafts are internal" as a rule in the transitions; product decision). Transitions: `submit` (staff), `approve` / `reject` (client, via `can_decide_variations` rules moved into a predicate), `withdraw`. Ball in court: the client while submitted. Replace the `Variation.rfi` FK with a `RecordLink(DERIVED_FROM)` in a later step, keeping the FK until every reader uses links.
2. **Subcontractor variations (`delivery.SubVariation`, `SV-`).** Ball-in-court company = the subcontractor. Their visibility rule (a subcontractor sees only their own) is narrower than `visibility`, so before externals read them from the index, add an audience column to `RecordIndex` (e.g. `audience_company`) and teach `core.access.visible_index` about it.
3. **RAMS (`delivery.Rams`, `RAMS-`).** Numbered per project but revised (`unique (project, number, revision)`). Either each revision is a record sharing a number (sequence taken only for revision 1), or the RAMS is the record and revisions are child rows. Prefer the second; decide before migrating.
4. **Snags (`projects.Issue`, `IS-`).** `assignee → ball_in_court_user`, `created_by` already matches. Visibility today is "assigned to them" for externals, so the same audience work as step 2 applies.
5. **Documents (`projects.Document`).** Already has `visibility`. Not numbered per project today and revisioned like RAMS; adopt last, once the revision question is settled for RAMS.

After the last one, remove `save_with_number`.

## Open questions for the product owner

1. **Should responding to an RFI pass the ball back to whoever raised it?** Today the ball stays with the responder, and only open RFIs count as waiting on someone, so a responded RFI is in nobody's "Needs me" until it's closed. `Transition.sets_ball_in_court` is there to do it in one line.
2. **Retention defaults.** What `retain_until` should a record get (e.g. contract completion + 6 or 12 years for deeds), who can set legal hold, and should purging be a scheduled job, a button for owners, or never automatic?
3. **What does purge remove?** Today the audit trail keeps the full old values of audited fields (question and response text included) after a purge. For a GDPR erasure request we may need to redact the diffs too, which conflicts with append-only. Which wins?
4. **Who may soft-delete and restore records?** Owners and admins only, or the person who raised it while it's still a draft?
5. **Voiding a closed RFI** was possible before (no source-status check) and isn't now: void is from draft, open or responded. Reopen first, then void. Is that right?
6. **Variation visibility:** are draft variations "internal" records (visibility) or a status rule? It decides how variations map onto `visibility` in step 1.
