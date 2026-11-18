# Zendesk MCP — Config-Write Tools (custom fields, forms, macros, triggers)

- **Date:** 2026-06-24
- **Repo:** `zendesk-mcp-server` (Haven-Services fork)
- **Tracker:** `DEHMANI-2/01-projects/zendesk-mcp-toolkit/index.md`
- **Driving use case:** `DEHMANI-2/01-projects/zendesk-equipment-request` — currently blocked by the
  "read + ticket-write only" constraint; this batch unblocks building its field/form/macro/trigger
  from Claude instead of by hand in Admin Center.

## Objective

Extend the single-file MCP fork with a **config-write tier** so Claude can create and update the
four Zendesk business-configuration objects the toolkit roadmap and the equipment-request project
need: **ticket fields, ticket forms, macros, and triggers**. Scope is **create + update + list/get;
no delete**.

## Scope

### In scope — 12 new tools

| Object | New tools | Read already shipped |
|---|---|---|
| Ticket fields | `create_ticket_field`, `update_ticket_field` | `list_ticket_fields` ✅ |
| Ticket forms | `create_ticket_form`, `update_ticket_form` | `list_ticket_forms` ✅ |
| Macros | `list_macros`, `get_macro`, `create_macro`, `update_macro` | — (new) |
| Triggers | `list_triggers`, `get_trigger`, `create_trigger`, `update_trigger` | — (new) |

`get_macro`/`get_trigger` exist so the full `actions`/`conditions` array can be fetched before an
update — the safe path around the PUT-replace semantics (below).

### Out of scope

- **Delete / deactivate-as-delete** — excluded per decision and the vault's never-delete ethos.
  (`active: false` is reachable through the normal update tools as an ordinary field.)
- Automations, views, user/organization fields, macro categories — not in this batch.
- Routing config writes through `zendesk-scripts` (the separate IaC repo). The MCP writes live and
  directly; the scripts repo is a parallel path and is not touched here.

## Approach — direct REST

Add a `_send_json(method, path, payload, *, error_context, timeout=30)` helper to `ZendeskClient`
mirroring the existing `_get_json`:

- same `Authorization` / `Content-Type` headers,
- `data=json.dumps(payload).encode()`, explicit `method` (`POST`/`PUT`),
- surfaces the `HTTPError` response **body** on failure (Zendesk's 422 reasons — e.g. "invalid
  action field", "options required" — live in the body and are essential for debugging),
- non-HTTP exceptions propagate with original type.

**Why not zenpy** (which backs `create_ticket`/`update_ticket`): these payloads are nested and
shape-sensitive (`{ticket_field: {…, custom_field_options:[…]}}`, action arrays, trigger
`conditions.{all,any}`), and the "PUT replaces the whole actions array" semantics must be explicit
and inspectable, not mediated by zenpy object-diffing. The metadata *reads* already use direct REST,
so this matches the existing read pattern.

## Tool specifications

Envelope: every create/update wraps the object under its singular key
(`{ticket_field: {…}}`, `{ticket_form: {…}}`, `{macro: {…}}`, `{trigger: {…}}`).

### Ticket fields

**`create_ticket_field`** → `POST /api/v2/ticket_fields.json`
- `type` (enum: `text`, `textarea`, `checkbox`, `date`, `integer`, `decimal`, `regexp`,
  `multiselect`, `tagger`) — required
- `title` (agent-facing) — required; `description?`
- End-user portal: `title_in_portal?`, `visible_in_portal?`, `editable_in_portal?`,
  `required_in_portal?`
- `required?`, `active?`
- Type-conditional: `custom_field_options?` `[{name, value}]` (tagger/multiselect), `tag?`
  (checkbox), `regexp_for_validation?` (regexp)
- **Client-side validation:** `tagger`/`multiselect` must include a non-empty `custom_field_options`.

**`update_ticket_field`** → `PUT /api/v2/ticket_fields/{id}.json`
- `ticket_field_id` (required) + any mutable field above.
- Note: a supplied `custom_field_options` **replaces** the option set (full-array discipline).

### Ticket forms

**`create_ticket_form`** → `POST /api/v2/ticket_forms.json`
- `name` (required), `ticket_field_ids` (**ordered** list — drives field display order),
  `display_name?`, `active?`, `end_user_visible?`, `default?`, `position?`

**`update_ticket_form`** → `PUT /api/v2/ticket_forms/{id}.json`
- `ticket_form_id` (required) + fields. `ticket_field_ids` **replaces** the list.
- Equipment-request workflow: `list_ticket_forms` → read current `ticket_field_ids` for form
  `49422106190107` → splice the new field ids into the desired order → send the full list.

### Macros

Action catalog, restriction shapes, and status constraints per
`DEHMANI-2/wiki/zendesk/macros-and-actions.md`.

**`create_macro`** → `POST /api/v2/macros.json`
- `title` (required), `actions` `[{field, value}]` (required), `description?`, `active?`
- `restriction?`: omit = All Agents; `{type:"Group", id}` or `{type:"Group", ids:[…]}`;
  `{type:"User", id}`
- Action notes surfaced in the tool description: `status` accepts only
  `open`/`pending`/`hold`/`solved` (not `new`/`closed`); use `custom_status_id` for Haven custom
  statuses; `current_tags` adds tags, `set_tags` replaces, `remove_tags` removes;
  comment via `comment_value` (+ optional `comment_value_html`).

**`update_macro`** → `PUT /api/v2/macros/{id}.json`
- `macro_id` (required) + `title?` / `actions` / `restriction?` / `active?`.
- **`actions` replaces the entire array** — omitting an action deletes it; there is no partial
  update. Tool description states this plainly; intended path is `get_macro` → edit → `update_macro`.

### Triggers

Triggers share the macro action catalog plus condition support.

**`create_trigger`** → `POST /api/v2/triggers.json`
- `title` (required), `conditions` `{all:[{field, operator, value}], any:[…]}` (required),
  `actions` `[{field, value}]` (required), `active?`, `category_id?`, `position?`

**`update_trigger`** → `PUT /api/v2/triggers/{id}.json`
- `trigger_id` (required) + full `conditions` / `actions` (both **replace**) / `title?` / `active?`.

### Reads

**`list_macros`** → `GET /api/v2/macros.json` — `page?`/`per_page?`, lean projection
(`_MACRO_FIELDS`). **`get_macro`** → `GET /api/v2/macros/{id}.json` — full object incl. `actions`.

**`list_triggers`** → `GET /api/v2/triggers.json` — `page?`/`per_page?`, lean projection
(`_TRIGGER_FIELDS`). **`get_trigger`** → `GET /api/v2/triggers/{id}.json` — full object incl.
`conditions` + `actions`.

## Data shapes / projections

Follow the existing `_project` + `_*_FIELDS` tuple pattern:

- `_MACRO_FIELDS = ('id', 'title', 'active', 'restriction', 'actions', 'created_at', 'updated_at')`
- `_TRIGGER_FIELDS = ('id', 'title', 'active', 'conditions', 'actions', 'category_id',
  'created_at', 'updated_at')`

Create/update tools return the created/updated object projected through the same tuple (fetch-back
not required — POST/PUT return the full object).

## Safety model

- **No delete path** ships.
- **Update = full replace, made explicit** for macros/triggers (`actions`, trigger `conditions`)
  and ticket-field options. Tool descriptions warn that omission = removal.
- **Live production writes** against `watermarkhomeservices` (multi-brand), consistent with the
  existing `create_ticket`. Guardrail is human-in-the-loop + the toolkit discipline:
  ship → `/code-review --effort high` → fix-up → `/mcp` reconnect → live verify. No separate
  dry-run flag (parity with existing write tools).

## Error handling

`_send_json` raises `Exception` carrying the Zendesk response body on HTTP error; the
`handle_call_tool` dispatcher wraps it into the standard `Error: <msg>` TextContent (existing
pattern). 422 reason text reaches the caller verbatim.

## Testing

- **Pure unit tests** (network-free, `tests/test_pure_helpers.py` style) for any extracted
  payload-builder/validator logic — at minimum the tagger/multiselect "options required" check and
  the singular-key envelope construction. Keep network methods out of unit tests.
- **Live verification battery** against `watermarkhomeservices` per toolkit norms.
- **Acceptance scenario (end-to-end):** build the equipment-request objects via the new tools —
  (1) `create_ticket_field` "Ship-to Address" (multi-line text), (2) `update_ticket_form` to add
  Job Type / Main Office Address / Ship-to / Supervisor Email / Due Date to Hardware form
  `49422106190107` in the specified order, (3) `create_macro` "Equipment Request – intake & reply",
  (4) `create_trigger` "Equipment email → Hardware form". This is the real proof the tier works.

## Docs & tracking

- Author `to-ingest/docs/config-write-tools.md` in the toolkit project (DEHMANI-2 wiki feed).
- Update `zendesk-mcp-toolkit/index.md`: mark shipped reads, add a "Config writes" tier listing the
  12 tools, append a Log line.
- This spec is committed to the repo; the vault `index.md` remains the project tracker.

## Open questions

- Equipment-request status action: built-in `pending` vs. a Haven custom
  "Pending – End User Response" `custom_status_id` — resolved at macro-build time, not a tool-design
  concern (the tools pass whichever value is supplied).
