# Siteline house rules for screens

These are the rules every page follows. They describe what the app already does well, so a new page
can match it without restyling anything. When in doubt, copy `rfis/` (the worked example) and look at
`/<org>/_ui/` (*Screen parts*, staff only), which shows each shared part live.

Don't add new colours, fonts or button styles. `static/css/app.css` and `templates/partials/` are owned by
the usability stream. If your app needs a few extra rules, add one scoped block at the end of `app.css`
(`/* Quality (quality/) */`, using your own class prefix) and use the existing tokens (`var(--bad)`,
`var(--line)` and so on).

## 1. Page layout

```
crumbs (staff only)        Projects / LDS-001
h1                         Leeds bar fit-out                      [Export CSV] [Raise RFI]
tabs (8 at most), then sub-navigation chips
filters / search
card(s)
```

- One `<h1>` per page, in `.page-head`. Headings never skip a level. Cards directly on the page use `<h2>`
  in `.card-head`; a heading inside a card is `<h3>`. (Hardening found 44 pages that jumped from h1 to h3.)
- The project pages extend `project_base.html` and fill `{% block project_actions %}` and
  `{% block project_content %}`. Tab and sub-tab wiring is in `actions/tabs.py` and `contracts/commercial.py`.
- Every page has a way back. That can be the crumbs, the tabs or a Cancel button. A dead end is a bug.

## 2. Buttons

| What it does | Markup | Example |
|---|---|---|
| The main job on the page (**one per page**, and one per form) | `btn primary` | *Raise RFI*, *Give payment notice* |
| Anything else | `btn` | *Export CSV*, *Edit* |
| Cancel, back, close | `btn ghost` | *Cancel* |
| Something you can't easily undo | `btn danger` | *Withdraw*, *Delete* |
| A small action inside a table row | `linkbtn` | *Void this RFI*, the remove ✕ |

- **Order.** In the page head, secondary buttons come first and the primary button comes last, on the right:
  `Export CSV`, then `Raise RFI`. In a form, the primary button comes first and Cancel follows it, both on the left:
  ```html
  <div class="form-actions"><button class="btn primary">Raise RFI</button>
    <a class="btn ghost" href="{% url 'rfi_list' org.slug project.pk %}">Cancel</a></div>
  ```
- **Labels** are a verb plus the thing: *Raise RFI*, *Add line*, *Approve variation*. Don't use *Submit*, *OK* or
  *Go*. After an edit the label is *Save*. A button that only has an icon needs an `aria-label`.
- Icons come only from `{% icon %}`: `plus` for adding, `download` for exports.

## 3. Words

- Use plain UK construction English: *programme*, *snag*, *practical completion*, *pay less notice*,
  *subcontractor*. Write in sentence case. Use *we* for the contractor and *you* for the reader.
- Never show an internal code. Show `get_status_display`, not `OPEN`, and *Approved with comments*, not `B`,
  unless the code is the industry term (RFI-004, *Code A*, S2). Refs are shown in `class="ref"`.
- Dates are `j M Y` (3 Mar 2026), or `D j M` (Tue 3 Mar) for something due soon. Money goes through
  `{{ v|money:currency }}`, `money_exact` (anything someone approves or pays) or `money_short` (dashboard tiles).
- Anything statutory or contractual carries `{% badge 'Advisory' 'warn' %}` and says *check your contract*.
- Externals (clients, consultants, subs) get plain words with no jargon about our internals. Say *the site
  team*, not *staff*, and *shared with you*, not *visibility SHARED*.
- **One-way actions say what happens next**, in the button's confirmation and in the message after it
  (see section 7). For example: *"Your application SA-003 has been sent. Northbank must give a payment notice
  by 14 Mar."*

## 4. Forms

- Render fields with `{% include "partials/form_fields.html" %}`. It puts the label on every input, marks
  required fields with `*`, shows help text, and shows each error under its field. Base the form on
  `StyledModelForm` (`projects/forms.py`) so dates get the native picker.
- **Ask only for what the record can't exist without.** Everything else is optional and can be added later.
  Fill in defaults: today, the signed-in person, this project, the company's usual terms.
- A label says what to type (*Required by*, *Gross value to date*). `help_text` covers the one thing people
  get wrong (*"Excluding VAT"*).
- **Errors say how to fix it**, in the field's own terms: *"Enter the gross amount paid."*, *"Pick a date that
  has not passed."*, *"That is more than the £1,200.00 still to pay."* Don't write *"Invalid value"* or
  *"This field is required"* where you can name what's missing.
- Uploads: `accept` lists the types. Site photos use `accept="image/*" capture="environment"`.
- Long forms go in a card with `max-width:900px`. Side forms go in `.grid.split`.

## 5. Lists and registers

- **Filters** are chips (`<a class="chip on">`) that set query-string values, so every filtered list has a
  URL people can share. `{% qs status='OPEN' %}` keeps the other filters. Over about 20 rows, add a
  `<form class="search" method="get"><input type="search" name="q" aria-label="Search ...">`.
- Tables: `<div class="table-wrap"><table class="t">`. Every `<th>` has text; an action column gets
  `<th><span class="sr-only">Actions</span></th>`. Money columns use `class="num"`. The first link in a row
  is `class="rowlink"`. Use `partials/register_table.html` when the rows fit it.
- **Status** is words plus a tone, never colour alone: `{% status_badge obj %}` or
  `{% badge label tone %}`. The tones mean: `good` done or approved, `warn` waiting or due soon, `bad`
  overdue, rejected or blocked, `info` with someone else, `accent` needs your decision, `muted` closed or draft.
- **Paging** at 50 rows: `{% include "partials/pager.html" %}` with a Django `page` object
  (`Paginator(qs, 50).get_page(request.GET.get('page'))`).
- **CSV export** for every register: `{% icon 'download' %} Export CSV` as a secondary button in the page
  head. Every cell goes through `core.exports.safe`, or use `core.exports.csv_response(filename, header, rows)`,
  which does it for you. (Until that is on main, use `levelling.exports.safe`.)

## 6. Empty states

Every empty list says what goes here and gives **one** next step.
```django
{% include "partials/empty_state.html" with title="No inspections yet" message="Run a checklist on an area to record pass, fail or N/A, with photos." cta_url=new_url cta_label="Start an inspection" %}
```
- When a filter emptied the list, say so and offer the way out: *"No RFIs waiting on you. Show every live
  RFI"*. Don't offer *Add*.
- If the person can't add one (an external, or read only on an archived job), leave out `cta_url` and say who
  does: *"The site team adds these."*
- Inside a card body, pass `left=True`.

## 7. Confirmations and messages

- A one-way or destructive action is a POST form with `data-confirm="..."`. The question names the thing,
  says what will happen and who will be told, and says whether it can be undone:
  `data-confirm="Withdraw SA-003? It can't be used again, and the subcontractor will be told."`
- Records with history are archived or voided, never deleted (see the README, *Archiving*). Deleting an
  empty project means typing its reference.
- After a POST, redirect and set one message. `messages.success` names the ref and the next step:
  *"RFI-004 raised."*, *"VO-002 sent to the client. They'll approve or reject it in their portal."*
  `messages.error` says how to put it right.

## 8. Phones and site use

- Every page works at 375px with no sideways scroll (`scrollWidth == innerWidth`). Wide tables scroll inside
  `.table-wrap`.
- Touch targets are at least 44px. `.btn` and `.tabs a` already are on phones. Add `min-height:44px` to
  anything custom.
- Site-entry screens (inspections, permits, incidents, sign-in, defects, dayworks, diary) should be usable
  one-handed. Put the primary button full width (`btn primary block`) at the bottom of the form.

## 9. Accessibility checklist

Every input has a label (`form_fields.html` handles this). Headings go in order. Table headers have text.
Icon-only controls have an `aria-label`. The current tab has `aria-current="page"`. Drop-downs are
`<details data-menu>` (Escape closes them). Run `scratchpad/hard/axe_crawl.js` over your pages: no serious
or critical findings.

## 10. Help, search and Needs me

- **Help.** Put `help/content/<topic>.md` in place. It starts with:
  ```
  ---
  title: Inspections
  pages: inspection_list inspection_detail inspection_create
  ---
  ```
  `pages` are URL names. The **?** button shows the panel on those pages by itself. Write 5 to 10 lines:
  what the page is for, then how to do the main job. Customers can edit these files later.
- **Search** covers projects, RFIs, variations, drawings, subcontractors and people. To add a record type,
  add a source in `help/search.py` that applies the same access rule as the record's own list.
- Anything waiting on a person goes on **Needs me** through a hook module (README, *Needs me*), not a
  dashboard card.
