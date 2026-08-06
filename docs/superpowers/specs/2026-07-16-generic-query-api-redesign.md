# keboola.ex-medallia — Generic Query API Redesign — Design Spec

> Type: extractor
> Component ID: keboola.ex-medallia
> Status: draft (supersedes the v1 feedback-only design `2026-07-15-medallia-query-api-design.md`)
> Date: 2026-07-16

This is a **major redesign** of the current pre-GA feedback-only extractor into a **generic,
introspection-driven Medallia Query API extractor** with a metadata-driven UX and a raw-GraphQL
escape hatch. The root (connection/auth) config is unchanged; the **config row is redesigned**.
The nine LOCKED design decisions from the maintainer brief are treated as settled and are the
authority for this spec — they are not re-litigated here. Platform grounding is reconciled per
reference in §12.

**Approved decisions (2026-07-16), locked here:** (A) **Clean design, NO legacy shim** — the component
is **pre-GA** (never published; the only real config is our own cf-dev smoke test), so there is **no
install base to protect**. There is a single clean generic code path — no dual v1/generic branches, no
v1 config markers, no legacy state-key handling. The existing feedback-only code, config, and tests are
**replaced**, not wrapped. (B) Raw mode is always full-load, no state. (C) `data_object` allows manual
entry (tags-style) as the graceful introspection-disabled fallback. (D) Row-hash PK = SHA-256 hex in a
`_row_hash` column for the 3 id-less objects.

Ground truth is the live introspection already performed (§4). No new live calls were made writing
this spec.

---

## 1. Overview & source system

`keboola.ex-medallia` extracts record-level data out of **Medallia Experience Cloud** via the
**Query API** — a GraphQL API over a single HTTP endpoint (`POST /data/v0/query`) — and lands it in
Keboola Storage tables. v1 shipped a single hardwired object (`feedback`) with a low-level row UX
(single-option object dropdown, raw field-ID typing, `k_…_epoch_int`/"epoch" jargon, an uneditable
filter object, a user-picked survey-ID primary key). This redesign makes the component **generic over
every paginated connection the instance exposes**, driven by GraphQL introspection and per-object field
metadata, plus a **raw-GraphQL mode** for anything the guided UX doesn't cover.

- Source docs: `https://docs.medallia.com/en/medallia-experience-cloud/integration/apis/query-api` and
  `https://developer.medallia.com/medallia-apis/reference/query-api-overview`.
- Official reference implementation (Apache-2.0, Java): `github.com/medallia/query-api-data-extract`.
- Primary use case: scheduled incremental (or full) extract of Experience Cloud objects into a warehouse.

**Platform prerequisite (unchanged):** Experience programs must already be configured in the target
instance — they create the referenceable schema. An empty instance has no queryable objects/fields.

## 2. Keboola mapping

- **Connection (object) → output table.** Each extractable paginated connection maps to one output
  table named `<data_object>.csv` (`feedback.csv`, `customers.csv`, `programs.csv`, …) by default. An
  **optional per-row `output_table` override** (§5) renames the table in BOTH modes: it defaults to the
  object name in structured mode, and is required in raw mode (no object name to derive from). The
  override is what lets several rows extract the *same* object different ways in one configuration
  without colliding on `<object>.csv` (`tableAlreadyExists`).
- **Config rows, one row per object** (Tier A convention). One row = one object (or one raw query) =
  one output table = its own `state.json`. Connection/auth is **config (root) level**; object choice,
  field selection, incremental settings, filters, mode, and raw query are **row level**. The component
  always receives a single platform-**merged** `config.json`; it never sees the root/row split.
- **Rows execute sequentially by default** in `rowsSortOrder`; row N is committed before row N+1.
  Parallelism is opt-in (`parallelism`) and **not** enabled — sequential keeps watermark advance and
  commit ordering trivial and bounds the blast radius on the live Medallia instance (many objects
  hammering the cost/rate quota at once). (Per-row `state.json` is isolated per row even under
  `parallelism`; the real concurrency hazard is two overlapping job runs of the *same* config, which
  the row execution model does not change.)
- **Incremental → output-mapping incremental + PK + per-row state.** When a row is incremental, the
  table is written with `incremental=True` and its primary key (node `id`, or a deterministic row-hash
  for id-less objects — §7), so a boundary re-read upserts rather than duplicates. The watermark is a
  **single scalar** persisted per-row: `{"last_incremental_value": <int|str>}` (§8). Full-load rows are
  written with `incremental=False` (truncate-and-replace) and still declare their PK.
- **Secrets → `#`-prefixed.** `#client_secret` only (unchanged). `client_id`, hosts, and tenant are
  plain non-secret keys.
- **Sync actions.** `testConnection` (kept), `listObjects` (new), object-aware `listFields` and
  `listDateFields` (new / redesigned). All live in the schema/UI layer (`component-build-ui`).
- **Output bucket naming.** Set the table *name* per object; let default-bucket routing (a Dev Portal
  setting, already on) place the bucket. Never hardcode a `destination`.

## 3. Authentication & connection

**Unchanged from v1.** OAuth 2.0 client-credentials (headless M2M): `client_id`+`client_secret` sent as
HTTP Basic to the per-instance token endpoint `https://<instance_host>/oauth/<company_name>/token` with
`grant_type=client_credentials`; the bearer token is used against the API gateway
`https://<api_host>/data/v0/query`. Two distinct hosts, both supplied as config (not derived). Token
default 3600 s; the token manager re-mints on a 5-minute pre-expiry window and once on a 401.

**Provisioning (NOT headless):** OAuth credentials are admin-provisioned by Medallia per instance;
prod/sandbox/dev do not share credentials; there is no shared public sandbox. This gates live testing
and VCR recording (§9, §10) — but the customer LIVE-instance creds already used for v1 recording remain
available (gitignored `secrets.json`), so the new gentle recordings in §9 are unblocked under the same
hard rules (never print secret values; do not commit cassettes carrying real data; be gentle on the
live instance).

## 4. Capability inventory & scope

Ground truth from live introspection: **29 Query-root fields**, of which **11 are paginated
connections**. Every connection is enumerated below with an explicit verdict. The 18 non-connection
root fields are aggregate/utility nodes and are collectively out of scope for a record-level extractor.

| Capability (Query-root connection) | Node shape | Node `id`? | `totalCount`? | Verdict | Rationale |
|---|---|---|---|---|---|
| `feedback` | (a) `fieldData(fieldId){values}` + `filter`/`first`/`after`/`orderBy` | yes | yes | **In scope** | core survey feedback; incremental-capable |
| `invitations` | (a) same as feedback | yes | yes | **In scope** | superset of feedback; incremental-capable |
| `customers` (Contact) | (b) scalar fields + `data(fieldId){value,values}` | yes | **no** (page via `pageInfo.hasNextPage`) | **In scope** | customer profiles |
| `programs` | (c) plain scalar nodes | yes | — | **In scope** | program metadata records |
| `missingSocialURLs` | (c) plain scalar nodes | yes | — | **In scope** | social operational data |
| `socialURLs` | (c) plain scalar nodes | **no** → row-hash PK | — | **In scope** | social operational data |
| `socialUrlsHealth` | (c) plain scalar nodes | **no** → row-hash PK | — | **In scope** | social operational data |
| `unitWarnings` | (c) plain scalar nodes | **no** → row-hash PK | — | **In scope** | operational warnings |
| `fields` | metadata catalog | n/a | — | **Excluded** | schema catalog, not an extraction target; powers `listFields` |
| `eventSchemas` | metadata catalog | n/a | — | **Excluded** | schema catalog; powers object-aware field metadata for events |
| `programRecordSchemas` | metadata catalog | n/a | — | **Excluded** | schema catalog; powers object-aware field metadata for program records |
| 18 non-connection root fields (`aggregate*`, `wordcloud`, `me`, `rateLimit`, `customerSchema`, `customer` singular, …) | not a connection | n/a | — | **Excluded** | aggregate/utility/singleton nodes — not record-level list extraction (aggregation is out of scope for a record extractor; `customerSchema`/`customer` are metadata/singleton, not list connections) |

**Scope decision:** all 8 extractable connections are In scope, per LOCKED decision 1 (support ALL
paginated connections generically). The 3 metadata catalogs and 18 non-connection nodes are Excluded
with the reasons above; the metadata catalogs are still *consumed* internally (field pickers), just not
extracted as tables. Classification is done **at runtime** by `listObjects` (§6), not hardcoded — the
table above is the expected result of that classification for this instance, and the fixed extractable
set doubles as the introspection-disabled fallback allowlist.

**Mechanics for the in-scope surface:**
- **Pagination:** Relay cursor, **universally via `pageInfo{hasNextPage endCursor}`** (LOCKED
  decision 3) — never `totalCount` (absent on `customers`). `first`/`after` are supplied as GraphQL
  **variables**, so the same paginator drives all objects and both modes.
- **Rate/cost limits:** header-driven throttle (`X-RateLimit-*`), exponential backoff on 429/5xx
  honouring `Retry-After`, optional `compute_cost_only` pre-flight — all kept from v1.
- **Response shapes → one generic flatten with shape detection** (LOCKED decision 2): (a) `fieldData`
  → `{values:[…]}` (1→scalar, 0→null, >1→JSON); (b) `data(fieldId)` → `{value, values}` (scalar via
  `value`, multi via `values`); (c) bare scalar node fields → copied through. Nested objects/lists →
  JSON-encoded. No child tables.

## 5. Configuration & schema

Root schema is **unchanged** (see below). The row schema is **redesigned**; every row field is
enumerated with its widget, dependencies, and feeding sync action. The actual `configSchema.json` /
`configRowSchema.json` are authored by `component-build-ui` — this section is the contract, not the JSON.

### 5.1 Root (config) level — unchanged

| Field | Type / widget | Req | Notes |
|---|---|---|---|
| `instance_host` | string text | yes | token-endpoint host |
| `company_name` | string text | yes | `<companyName>` OAuth path segment |
| `api_host` | string text | yes | `.apis.medallia.com` gateway host |
| `client_id` | string text | yes | OAuth client id (non-secret) |
| `#client_secret` | string password (encrypted) | yes | OAuth client secret |
| `test_connection` | `button`, `format: test-connection` → `testConnection` | — | inline Alert |

### 5.2 Row level — redesigned

| # | Field | Widget | Depends on | Fed by | Notes |
|---|---|---|---|---|---|
| 1 | `mode` | select, enum `["structured","raw"]`, `enum_titles ["Guided (pick an object)","Raw GraphQL query"]`, default `structured` | — | — | top-level branch |
| 2 | `data_object` | select, `enum: []`, `format: select`, `tags: true` (manual fallback), async `listObjects` (`autoload:true`,`cache:true`) | `mode = structured` | `listObjects` | auto-populated connection list; `tags:true` allows manual entry when introspection is disabled |
| 3 | `fields` | **array** multi-select: `type:"array"`, `format:"select"`, `uniqueItems:true`, `items:{type:"string"}`, async `listFields` (`autoload:["data_object"]`,`cache:true`) | `mode = structured` | object-aware `listFields` | shows field **names**; empty ⇒ all scalar fields for shape (c). See §5.3 |
| 4 | `load_type` | select, enum `["incremental_load","full_load"]`, `enum_titles ["Incremental Load","Full Load"]`, default `incremental_load` | `mode = structured` | — | component falls back to full-load with a logged warning if incremental isn't possible for the object |
| 5 | `incremental_field` | select, `enum: []`, `format: select`, async `listDateFields` (`autoload:["data_object"]`) | `mode = structured` **and** `load_type = incremental_load` | object-aware `listDateFields` | date/datetime (and sortable-int) fields **by name**; the value format ("epoch" vs "datetime") is auto-detected at runtime — never shown |
| 6 | `initial_start` | string text | `mode = structured` **and** `load_type = incremental_load` | — | first-run lower bound; interpreted by the field's detected type (ISO date `2026-01-01`, or epoch seconds). Empty ⇒ full history on first run |
| 7 | `filters` | string, `format: "editor"`, `options.editor.mode: "application/json"` | `mode = structured` | — | **real editable JSON** filter tree; validated by the component (§6). Empty ⇒ no extra filter |
| 8 | `raw_query` | string, `format: "editor"` (GraphQL/text) | `mode = raw` | — | see the raw contract §6.4 |
| 9 | `output_table` | string text ("Storage Table Name") | — (both modes) | — | **optional** per-row override; output written to `<output_table>.csv`. Structured mode defaults to `<data_object>.csv`; **required** in raw mode (no object name to derive from). Validated as a filesystem-safe slug (`[A-Za-z0-9_-]+`, no path separators). Set a distinct name to extract the same object multiple ways in one configuration |
| 10 | `validate_query` | `button`, `format: sync-action` → `validateQuery` | `mode = raw` | `validateQuery` | inline pre-flight: compile + single-connection + cost check |
| 11 | `page_size` | integer, default 100, min 1, max 1000 | — (both modes) | — | the `first` variable; clamped to 1000 |

**Dropped from the current row schema** (replaced outright — no compatibility branch): `survey_id_field_id`
(PK is now node `id`/row-hash), `finish_date_field_id` + `finish_date_field_type` (replaced by
`incremental_field` + runtime dataType auto-detect), `initial_start_epoch` + `initial_start_value`
(merged into a single `initial_start`). `data_object` is no longer a single-option `["feedback"]` enum.

**UI hygiene (per `component-build-ui`):** one-sentence `description` per field, detail in
`options.tooltip`; `enum_titles` on every enum; English labels; the row schema (≥6 mixed fields) is
grouped into `type:object` sections — e.g. **Source** (`mode`, `data_object`, `fields`, `raw_query`,
`output_table`), **Incremental** (`load_type`, `incremental_field`, `initial_start`), **Advanced**
(`filters`, `page_size`, `validate_query`) — with gap-spaced `propertyOrder`.

### 5.3 The multi-select `fields` construct (LOCKED decision 6 — verified form)

The known-good Keboola sibling is the **Salesforce extractor row schema** (bundled in
`component-build-ui/references/sync-actions.md` → "Row Schema with Dynamic Dropdowns"): an `object`
async dropdown feeding an async multi-select `fields` array feeding a PK dropdown. The canonical
array+async form is:

```json
"fields": {
  "type": "array",
  "title": "Fields",
  "format": "select",
  "uniqueItems": true,
  "items": { "type": "string" },
  "options": { "async": { "label": "Load Fields", "action": "listFields", "autoload": ["data_object"], "cache": true } }
}
```

The async "Load" button requires `format: "select"` present on the array. **Verification step (Phase
`component-build-ui`):** confirm in the Ctrl+D schema sandbox that the Load button renders and the
multi-select populates; if the renderer needs it, add `items.enum: []` — this is the exact defect the
brief flags ("verify the correct array+async schema form"). `autoload: ["data_object"]` makes the field
list reload when the chosen object changes (v1 used a bare `autoload: true`, which does not react to
`data_object`).

## 6. Sync actions & client / query-builder changes

### 6.1 `testConnection` (kept)

Mint a token and run a cheap metadata query with `compute_cost_only=true` (validates auth + query at the
gateway without consuming quota). `MedalliaClientError` → `UserException` so the button shows a clean
Alert. Unchanged from v1.

### 6.2 `listObjects` (new)

One GraphQL **introspection** query (`__schema` → Query type fields with their return-type kinds and
field names). Classify each root field **at runtime**:
1. **Is it a connection?** Its return OBJECT type exposes a `nodes` field (Relay list). Non-connections
   (aggregate/utility/singleton) are dropped.
2. **Is it a metadata catalog?** Name in the denylist `{fields, eventSchemas, programRecordSchemas}`
   (schema catalogs). Dropped from the extraction dropdown (still used internally).
3. Everything else → **extractable**; returned as `SelectElement(value=<field>, label=<Title Case>)`.

Returns the extractable connections (expected: the 8 In-scope objects). **Fallback:** if introspection
is disabled on the instance (some tenants block `__schema`), catch the error and return the static
allowlist of the 8 known extractable connections; `data_object` also has `tags:true` so the user can
type an object name manually. Autoloaded.

### 6.3 `listFields` / `listDateFields` (object-aware — redesigned)

`listFields` reads `data_object` from the current form values and routes to the correct per-object field
metadata source, returning `SelectElement(value=<field id>, label=<field name>)` (**names**, machine id
preserved):

| Object / shape | Metadata source |
|---|---|
| `feedback`, `invitations` (shape a) | `fields` catalog (`Field{id,name,dataType,filterable,sortable,multivalued,…}`) |
| `customers` (shape b) | `customerSchema` |
| event connections (shape a/b) | `eventSchemas` |
| program-record connections | `programRecordSchemas` |
| `programs` + social connections (shape c, no field catalog) | introspected scalar fields of the connection's node type (from `__type`) |

`listDateFields` is the same lookup **filtered to** `dataType ∈ {DATE, DATETIME}` **plus** sortable
`INT` fields (to surface an epoch K-field where one exists), returned by name. It feeds
`incremental_field`. The user never sees the value format; the component reads the chosen field's
`dataType` at run start and formats the watermark bound accordingly (DATE/DATETIME → ISO string; INT →
numeric seconds — the v1 "epoch" path, now invisible). If the lookup returns nothing, the object has no
incremental cursor → the row is full-load only (§8).

**Pydantic/sync-action safety:** every async-fed field (`data_object`, `fields`, `incremental_field`,
`raw_query`, `output_table`, `filters`) must have a safe default (`""`/`None`/`[]`) with **no**
`@field_validator` rejecting empties, or every sync action (which instantiates the config on an
half-filled form) crashes. Presence is validated inside `run()` / the action method, not on the model.

### 6.4 Client & generic query builder

The separated client (`src/client/medallia_client.py`) keeps `MedalliaTokenManager`, the cost-aware
throttle/backoff, and the 401 re-mint. The v1 `MedalliaQueryBuilder` (feedback-specific) is generalized;
`flatten_node`, `watermark_from_node`, and `Watermark` are refactored.

**`GenericQueryBuilder(object_name, node_shape, selected_fields, scalar_fields, incremental_field,
filter_tree, supports_filter, supports_order)`** emits one connection query using **GraphQL variables**
for pagination (uniform across objects and modes):

```graphql
query ($first: Int, $after: String) {
  <object>(first: $first, after: $after [, filter: {…}] [, orderBy: [{fieldId:"<f>", direction: ASC}]]) {
    nodes { <selection> }
    pageInfo { hasNextPage endCursor }
  }
}
```

Selection per shape:
- **(a) fieldData:** `id` + for each selected field `f`: `<f>: fieldData(fieldId: "<f>") { values }`.
- **(b) data (customers):** `id` + known Contact scalar fields + for each selected field `f`:
  `<f>: data(fieldId: "<f>") { value values }`.
- **(c) scalar:** `id` (only if the node type has one) + each scalar field name directly.

Field IDs are validated against the GraphQL-name pattern before interpolation (kept from v1 — the only
values interpolated into the query string; `first`/`after`/`filter` scalars travel as JSON variables or
JSON-escaped literals, so they cannot break out).

**Filter:** the user's `filters` JSON is parsed and its object keys validated against the GraphQL-name
pattern (kept from v1 — `_validate_filter_keys`). For an incremental row the builder AND-joins
`{fieldIds:["<incremental_field>"], gte: "<watermark>"}` with the user filter. **`gte` (not `gt`)** is
deliberate: pagination no longer depends on the watermark (cursor-driven), so re-reading the boundary
record is harmless and is deduped by the PK upsert — `gte` guarantees no same-value record is ever
missed. Objects that don't support `filter` (introspected) get no watermark filter → full-load.

**Pagination loop** (`fetch(object, variables)`):
```
after = None
while True:
    conn = post(query, variables={"first": page_size, "after": after})[object]
    for node in conn["nodes"]: yield node
    if not conn["pageInfo"]["hasNextPage"]: break
    after = conn["pageInfo"]["endCursor"]
    if max_pages and pages >= max_pages: break   # defensive / MEDALLIA_MAX_PAGES cap (kept)
```
Never inspects `totalCount`. The `MEDALLIA_MAX_PAGES` env cap (v1) is retained as the recording/blast-
radius guard and a runaway stop.

**Per-object incremental capability detection:** a row is incremental iff `load_type=incremental_load`
**and** the object's connection accepts `filter`+`orderBy` (introspected) **and** an `incremental_field`
is chosen (from `listDateFields`). If any is missing, the component logs `"<object> has no incremental
cursor; loading full."` and writes a full load. This is auto — the user is never asked to reason about it.

### 6.5 Raw GraphQL mode (LOCKED decision 8) — exact contract

**Contract the user's `raw_query` MUST satisfy:**
1. Declare the variables `$first: Int` and `$after: String` and pass them to **exactly one** top-level
   Relay connection field.
2. On that connection, select `nodes { … }` and `pageInfo { hasNextPage endCursor }`.
3. Return exactly **one** connection under `data` (one key whose value is an object with `nodes` +
   `pageInfo`).

**Execution:** the component supplies `variables={"first": page_size, "after": <cursor>}` and drives the
same cursor loop as structured mode. It **injects nothing into the query string** — pagination flows
through the declared variables, so there is no fragile string surgery.

**Flattening & PK:** the generic shape-detecting flatten (§4) runs on each node; PK = node `id` if
present on every node, else a deterministic row-hash (§7). Output → `<output_table>.csv`.

**Load type:** raw mode is **always full-load** from the component's perspective — it does not manage a
watermark and does not advance `state.json` (there is no reliable field to track generically). Users who
want incremental raw extraction embed their own `filter` bound in the query (managed by them, e.g. a
literal date). Documented in the `raw_query` tooltip.

**Validation & failure modes** (surfaced as `UserException` → exit 1, clean message):
- Static pre-check (in `run()` and in `validateQuery`): the query text must contain `$first`, `$after`,
  and `pageInfo` — else "Raw query must declare $first/$after and select pageInfo{hasNextPage endCursor}".
- `compute_cost_only` pre-flight (in `validateQuery`, and once at run start): compile + cost check;
  GraphQL compile errors → the GraphQL error message; cost over the 3M ceiling → a cost message.
- **Row preview (`validateQuery`, Keboola standard):** after the pre-flight passes, the button fetches
  ONE small page (`first = min(page_size, 5)`) and renders the flattened rows as a Markdown table in the
  `ValidationResult` (capped columns/cell width). If the pre-flight passes but the preview can't be
  fetched (no connection / transient error), it returns a WARNING ("valid, preview unavailable"), never
  an ERROR. Button label: "Validate & Preview Query".
- Response validation: `data` must have exactly one key; its value must contain a list `nodes` and a
  `pageInfo`. Zero connections → "Raw query returned no Relay connection"; more than one → "Raw query
  must return exactly one connection (found N: …)"; missing `pageInfo` → "…must select pageInfo…".
- Auth/transport/429/5xx → identical handling to structured mode.
- Defensive `max_pages` cap applies.

## 7. Primary-key strategy (LOCKED decision 4)

- **Node `id` where present** → `primary_key=["id"]`, `id` carried as a column. Objects: `feedback`,
  `invitations`, `customers`, `programs`, `missingSocialURLs` (and any raw-mode query whose every node
  has an `id`).
- **Row-hash for id-less objects** → `socialURLs`, `socialUrlsHealth`, `unitWarnings` (and raw-mode
  queries with no node `id`). The component adds a `_row_hash` column = hex **SHA-256** of the canonical
  JSON of the flattened row (keys sorted, values stringified, excluding `_row_hash` itself), and sets
  `primary_key=["_row_hash"]`. Deterministic ⇒ an identical source row yields an identical hash ⇒
  upsert dedupes across runs. **Documented caveats:** a mutated field on an id-less record produces a
  *new* hash (no update-in-place — it appears as a new row), so id-less objects are best consumed as
  full-load append-style operational data; SHA-256 collision risk is negligible.
- **`survey_id_field_id` is dropped entirely** — the user no longer picks a PK.

The current feedback code wrote `primary_key=["surveyId"]`; the generic path uses node `id`. Because the
component is pre-GA with no install base (§11), this is a clean replacement — there is no existing table
whose PK must be preserved.

## 8. Incremental strategy & state (per object)

- **Watermark = a single scalar** in the row's `state.json`: `{"last_incremental_value": <int|str>}`
  (stored in the incremental field's native format — int for an epoch/INT field, ISO string for a
  DATE/DATETIME field). No composite `(value, id)` cursor is needed anymore: cursor pagination
  (`hasNextPage`) reads the whole window regardless of order, and `gte` + PK upsert makes the boundary
  idempotent. This is simpler than the v1 composite keyset and correct given the PK upsert.
- **Lower bound** at run start: stored `last_incremental_value` (incremental resume) → else
  `initial_start` (first run) → else no lower bound (full history). **Upper bound** = run start `now()`
  emitted in the field's format (date-only `YYYY-MM-DD` for DATE/DATETIME — Medallia rejects a `…Z`
  timestamp, a v1 live-verified fact — numeric seconds for INT), AND-joined as `{…, lt: <now>}`.
- **Advance** the in-memory watermark to the **max** incremental-field value seen across the run, and
  **persist only after a successful table write** (a failed run retries from the old watermark). Write
  state on every successful run, including full loads (so a later switch to incremental just works),
  except raw mode which never writes state.
- **Value-format auto-detect:** at run start the component fetches the chosen `incremental_field`'s
  metadata (one cheap `fields(ids:[…])`/schema query) to read its `dataType`; DATE/DATETIME → ISO
  string compare & bound, INT → numeric compare & bound. The word "epoch" and the type knob never reach
  the user (LOCKED decision 5).

## 9. Code architecture

- **Separated client** (`src/client/`): `MedalliaTokenManager` (unchanged), `GenericQueryBuilder`
  (§6.4), `MedalliaClient` (cursor paginator via `pageInfo`, cost/throttle/backoff, 401 re-mint,
  `run_metadata_query`, `run_introspection`), and pure helpers `flatten_node` (shape-detecting),
  `row_hash`, `watermark_from_value`. `Watermark` collapses to a single scalar.
- **Typed Pydantic config.** `Configuration` (root) unchanged. `RowConfiguration` redesigned per §5.2
  with safe defaults on every async-fed field; `ValidationError` → `UserException` with **no** value
  echo (the `from None` + `errors(include_input=False)` guard from v1 is kept — it is a
  secret-leak protection and must not be weakened).
- **`run()` thin orchestrator:** `_get_config` → branch on `mode` (structured | raw) → build client →
  resolve object shape + PK strategy + incremental capability → seed lower bound from state/`initial_start`
  → page nodes writing rows → write manifest (PK, typed schema, incremental) → persist watermark. Logic
  in private methods (`_run_structured`, `_run_raw`, `_write_table`, `_load_state`, `_save_state`,
  `_resolve_object`). There is **no** legacy branch — one generic path only.
- **Error handling:** `UserException` (exit 1) for user-fixable problems — bad/missing config, invalid
  `filters` JSON, raw-query contract violations, auth failure after re-mint, cost over ceiling, GraphQL
  `errors[]` (except the non-fatal `Invalid field id:` which is logged and tolerated — kept from v1),
  introspection disabled with no fallback object. Unexpected/transport → `MedalliaClientError` → exit 2.
  Never `sys.exit(2)` for a user-actionable error.
- **Output manifest — authoritative typed schema** with a **Medallia `dataType` → Keboola BaseType map**
  (LOCKED decision 9):

  | Medallia `dataType` | Keboola `BaseType` |
  |---|---|
  | `INT` / `INTEGER` | `integer()` |
  | `FLOAT` | `numeric()` |
  | `DATE` | `date()` |
  | `DATETIME` | `timestamp()` |
  | `STRING`,`EMAIL`,`ENUM`,`URL`,`UNIT`,`TIME` | `string()` |
  | `multivalued=true` (any type) | `string()` (JSON-encoded list) |

  For shape-(c) scalar objects, the GraphQL scalar type maps analogously (`Int→integer`, `Float→numeric`,
  `Boolean→boolean`, `String`/`ID→string`). When `dataType` is unavailable (raw mode, id-less objects
  with no metadata catalog) the column defaults to `string()`. `_row_hash` is `string()`. Requires the
  Dev Portal `dataTypeSupport=authoritative` (already flipped in v1 Phase 8). The CSV is written **with**
  a header row, so `has_header=True` is passed (kept) — the two must stay in agreement.
- **Scratch files → `/tmp`**, never `data/out/tables/`.
- **Key dependencies:** `keboola.component`, `requests`, `pydantic`. No Medallia Python SDK exists.

## 10. Testing

### 10.1 Unit tests (no live API — stub transport / hand-authored fixtures)

- Generic **flatten** across all three shapes: `fieldData{values}` (1→scalar, 0→None, >1→JSON), `data
  {value,values}` (scalar via `value`, multi via `values`), bare scalar nodes; nested → JSON.
- **row_hash** — determinism, key-order independence, cross-run stability, dedup behaviour, exclusion of
  `_row_hash` from its own input.
- **GenericQueryBuilder** — selection per shape, variable-based `first/after`, `gte` watermark + `lt`
  upper bound injection, user-filter AND-join, `orderBy`, field-ID + filter-key validation (injection
  guard).
- **Object classification** (`listObjects`) — introspection JSON → the 8 extractable set, metadata-catalog
  exclusion, non-connection exclusion, introspection-disabled → static-allowlist fallback.
- **Object-aware `listFields` / `listDateFields`** — routing per object to the right metadata source;
  `listDateFields` filtering to DATE/DATETIME + sortable-INT; names shown, ids preserved.
- **Watermark** — INT vs ISO auto-detect from `dataType`, max-advance, `gte` boundary, persist-after-write.
- **Cursor paginator** — multi-page walk via `hasNextPage`/`endCursor`, `max_pages` cap, no `totalCount`
  dependency.
- **Raw-mode validation** — static `$first/$after/pageInfo` check; single-connection detection;
  zero/multi-connection rejection; missing-`pageInfo` rejection.
- **Manifest typing** — `dataType`→`BaseType` map incl. multivalued→string and scalar-type map.
- **Config parsing** — new row shape; `mode` branch selection; invalid `filters` JSON → exit 1.
- **Secret-leak guard** — `ValidationError` never echoes config values (kept from v1).

### 10.2 Datadir tests (hand-authored fixtures — primary functional coverage)

One case per node shape + PK strategy + mode, each a `source/data/config.json` + cassette/fixture →
`expected/` table + manifest + state:
- `feedback` happy path (shape a, id PK) — adapt the existing case.
- `customers` (shape b, `data(fieldId)`, id PK, no `totalCount`, page via `hasNextPage`) — new fixture.
- `programs` (shape c scalar, id PK) — new fixture.
- `socialURLs` (shape c scalar, **id-less → row-hash PK**) — new fixture.
- Incremental resume (feedback): seeded state → `gte` bound → PK upsert; state advances to max.
- Full-load object (no date field / `filter` unsupported) → `incremental=False`, no state advance drama.
- Raw-mode happy path (one connection, paginated via variables) → `<output_table>.csv`, no state written.
- Raw-mode failures (multi-connection; missing `pageInfo`; malformed) → exit 1 with the precise message.
- Error cases: 401, non-retryable 4xx, GraphQL `errors[]`, invalid `filters` JSON, cost-limit → exit 1;
  unexpected transport → exit 2.

The existing feedback datadir cases (`03/08/09/10/11/12/13/14` etc.) are **regenerated** under the new
row shape and generic output (node-`id` PK, redesigned config) — not kept as legacy fixtures.

### 10.3 VCR — new gentle live recordings needed

New cassettes must be recorded against the customer live instance (creds already available), because the
node shapes changed (`data(fieldId)`, bare scalars, `pageInfo` cursors) and v1 cassettes only cover
feedback's `fieldData` shape. Record under the v1 hard rules: tiny `page_size`, hard `MEDALLIA_MAX_PAGES`
cap, `compute_cost_only` pre-flight, no retry storms, report exact live-call count. Target one cassette
each for: `feedback`, `customers`, one scalar object (`programs`), one id-less object (`socialURLs`) if
it has data, and one raw-mode query. **The `MedalliaResponseBodySanitizer` must be EXTENDED** to also
scrub: shape-(b) `data(fieldId){value,values}` payloads, bare scalar node fields, `pageInfo.endCursor`
(normalise to a deterministic placeholder — cursors may encode offsets/PII), and id-less nodes. As in
v1, cassettes carrying real customer data are **not committed** (gitignored, manual inspection first);
where a high-volume object is non-replayable (as feedback was in v1), the hand-authored datadir fixture
in §10.2 is the authoritative coverage and the VCR case is skipped-with-reason.

### 10.4 Sync-action tests

`testConnection`, `listObjects` (incl. fallback), `listFields`/`listDateFields` (per object),
`validateQuery` — against stubbed introspection + metadata responses.

## 11. Migration (clean replacement — NO legacy shim)

**Approved decision A (2026-07-16): clean design, no back-compat shim.** The component is **pre-GA** —
never published to the marketplace, no customer install base. The only existing configuration is our own
**cf-dev smoke-test config** (created during v1 Phase 7). There is therefore nothing to protect and no
dual code path:

- The current feedback-only `src/` (component + client + configuration), `configRowSchema.json`, and the
  feedback datadir/VCR tests are **replaced** by the generic design — not wrapped, not detected, not
  branched. No `_run_legacy`, no v1 config markers (`survey_id_field_id`, `finish_date_field_type`), no
  legacy state keys (`last_finish_date_epoch`/`last_survey_id`).
- The new state shape is `{"last_incremental_value": <int|str>}` (§8); the old two-key state is simply
  gone. Because there are no production configs carrying old state, no state migration is needed.
- **Our own cf-dev smoke config is regenerated** under the new row shape (new `mode`/`data_object`/
  `incremental_field` fields, node-`id` PK) during the build's deploy phase; the previous test config is
  discarded/recreated. The **committed feedback cassettes and datadir fixtures are regenerated** under the
  new design (new node shapes, `pageInfo` pagination) — see §10.
- Net effect: a single clean generic code path. This is the simplest correct outcome given the pre-GA
  status and keeps the codebase free of dead compatibility branches.

## 12. Grounding reconciliation (keboola-context)

One line per behaviour-relevant reference; every `corrected:` item is folded into the sections above.
(A fresh-context subagent re-ran this against the written spec; its per-reference verdicts are recorded
in the lifecycle tracker Phase 3 evidence.)

- **architecture-conventions.md** → correct. Config rows per object, root/row split, `#client_secret`
  only, incremental+PK+per-row state, sync actions (`testConnection`/`listObjects`/`listFields`), typed
  Pydantic config, client separated from a thin `run()`, authoritative schema manifest, `UserException`/
  exit-code split, default-bucket naming — all applied and stated (§2, §5, §6, §9).
- **config-rows.md** → correct. Rows execute **sequentially by default** (parallelism opt-in, not
  enabled); per-row `state.json` is automatic (single merged `config.json` seen by the component); test
  fixtures are a single merged `config.json` + row-scoped `state.json` (§2, §8, §10).
- **incremental-state.md** → correct/adapted. Watermark captured before fetch, persisted only after a
  successful write; `incremental=True` **with** a PK ⇒ upsert; `load_type` a row-level dropdown, default
  incremental; write state on every successful run (except raw). Adapted: the cursor is a single
  incremental-field scalar with **`gte`** boundary (idempotent via PK upsert) rather than a composite
  keyset — cursor pagination decouples paging from the watermark (§8).
- **native-data-types.md** → correct. Authoritative `schema` manifest with the `dataType`→`BaseType`
  map; needs `dataTypeSupport=authoritative` (already on); CSV written with a header ⇒ `has_header=True`
  (§9). The map is the mechanism that makes native typing real rather than all-STRING.
- **encryption.md** → correct. Only `#client_secret` `#`-prefixed/encrypted (`KBC::ProjectSecure`); the
  `ValidationError` no-echo guard is preserved as a secret-leak protection (§9).
- **output-mapping.md** → correct. Scratch → `/tmp`, never `data/out/tables/`; `incremental+PK=upsert`;
  full-load rows truncate-replace; single (non-sliced) table per row so the header/`has_header` rule
  governs; id-less objects use a row-hash PK so incremental upsert is still well-defined (§7, §9).
- **exit-codes.md** → correct. User-actionable → `UserException` (exit 1) with a visible message;
  unexpected/transport → exit 2; never `sys.exit(2)` for user errors (§6.5, §9).
- **default-bucket.md** → correct. No hardcoded `destination`; set the table name (`<object>.csv` /
  `<output_table>.csv`) and let default-bucket routing place the bucket (§2).
- **environment-variables.md** → correct. `KBC_DATA_TYPE_SUPPORT` drives the manifest format (auto-
  detected by the library); `KBC_CONFIGROWID` gives per-row state; `MEDALLIA_MAX_PAGES` (component-own)
  bounds pages while recording; no `forward_token` (the component calls Medallia, not Storage) (§6.4, §9).
- **telemetry.md** → correct (not applicable). The component does not query Keboola telemetry at runtime.
  A telemetry blast-radius check would normally gate a breaking change to a shipped component, but this
  one is **pre-GA with no published install base** (approved decision A, §11) — the only config is our own
  cf-dev test — so there is no customer usage to size and no back-compat decision to make. Clean
  replacement is confirmed safe without a usage count.

## 13. Open risks & blockers (ranked) + resolved maintainer decisions

1. **Introspection disabled on some instances** → `listObjects` can't enumerate. Mitigation: static
   allowlist fallback + `tags:true` manual object entry; `listFields` scalar-field path also falls back to
   node-type introspection, which shares the risk — document that manual field entry may be required.
2. **Objects without `filter`/`orderBy` support** (likely the scalar/operational connections) →
   incremental impossible → full-load only; a large operational table is fetched in full each run.
   Mitigation: auto full-load with a logged reason, `page_size`/`max_pages` tuning; document.
3. **No cross-run pagination resume.** A run must complete all cursor pages to capture the window; a very
   large full-load object could approach job limits. Mitigation: incremental narrows the window for
   date-capable objects; `page_size` up to 1000 minimises calls; defensive page cap.
4. **PK / row-shape change vs. our cf-dev test config.** No customer impact (pre-GA), but our own cf-dev
   smoke config and the committed feedback fixtures/cassettes must be **regenerated** under the new design
   (§10, §11) or the build's test/deploy phases will diff against stale v1 output. Mitigation: regenerate
   both in the test + deploy phases; there is no install base to migrate.
5. **Row-hash PK does not update mutated id-less records.** Documented; full-load recommended for those.
6. **`customers` `data(fieldId)` returns both `value` and `values`** — the flattener prefers `value` for
   scalars and `values` for multivalued; exact behaviour to confirm at recording.
7. **Cursor `endCursor` sanitization for VCR** — cursors may encode offsets/PII; the sanitizer must
   normalise them or replay/matching breaks.
8. **Raw-mode query cost/complexity** — arbitrary user queries. Mitigation: `compute_cost_only`
   pre-flight + cost ceiling + `validateQuery` button + static contract check + page cap.
9. **`listDateFields` INT inclusion heuristic** — surfacing epoch K-fields as INT could also surface a
   non-date INT (e.g. an NPS score). Mitigation: prefer DATE/DATETIME; include INT only when `sortable`;
   the runtime `lt` upper bound would simply return nothing useful if mis-picked (safe failure). Confirm
   the epoch K-field's actual `dataType` in metadata at recording.

**Maintainer decisions — all resolved (2026-07-16), no open points:**
- **A. Clean design, NO legacy shim** — pre-GA, no install base; single generic path; cf-dev config +
  fixtures/cassettes regenerated. §11.
- **B. Raw-mode = always full-load / no state** — confirmed. §6.5.
- **C. `data_object` tags-style manual entry** as the graceful introspection-disabled fallback —
  confirmed. §6.2.
- **D. Row-hash PK = SHA-256 hex in a `_row_hash` column** for the 3 id-less objects — confirmed. §7.
