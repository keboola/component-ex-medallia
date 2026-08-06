# keboola.ex-medallia — Design Spec

> Type: extractor
> Component ID: keboola.ex-medallia
> Status: draft
> Date: 2026-07-15

Grounded in the Phase 2 research summary (`docs/superpowers/research/medallia-query-api-research.md`)
and the `keboola-context` platform references. Where a Keboola convention decides a design point, it is
applied and stated (Tier A), not re-litigated. The per-reference grounding reconciliation is in §10.

## 1. Overview & source system

`keboola.ex-medallia` is an extractor that pulls record-level survey data out of **Medallia Experience
Cloud** via the **Query API** — a GraphQL API over a single HTTP endpoint (`POST /data/v0/query`) — and
lands it in Keboola Storage tables with incremental load. It targets the BI/data-warehouse extract use
case Medallia positions the Query API for: feedback responses, invitations, and customer profiles pulled
record-by-record for downstream analytics.

- Source docs: `https://docs.medallia.com/en/medallia-experience-cloud/integration/apis/query-api` and
  `https://developer.medallia.com/medallia-apis/reference/query-api-overview`.
- Official reference implementation (Apache-2.0, Java): `github.com/medallia/query-api-data-extract`
  (mirrors the query shape, watermark filter, and pagination this spec adopts).
- Primary use case: scheduled incremental extract of Experience Cloud raw survey records into a warehouse.

**Platform prerequisite:** Experience programs must already be configured in the target instance — they
create the referenceable Feedback / Invitations / Record-field schema. An empty instance has no queryable
schema. This is a customer-onboarding precondition, not something the component provisions.

## 2. Keboola mapping

How the Query API maps onto how Keboola runs a component.

- **Objects → output tables.** Each supported data-source node maps to one output table:
  `feedback` → `feedback.csv`, `invitations` → `invitations.csv`, `customer` (profiles) → `profiles.csv`.
  v1 ships **feedback only** (see §4 / decisions in §9); invitations and profiles are added as further
  config rows later without code changes.
- **Config rows, one row per object** (Tier A convention: multiple independent objects → config rows,
  not a multipick). One row = one data-source object = one output table = its own incremental cursor.
  Connection/auth lives at **config (root) level**; object selection, field list, filters, and load type
  live at **row level**. The component always receives a single platform-merged `config.json` — it never
  sees the root/row split.
- **Row execution is sequential by default.** Rows run one at a time in `rowsSortOrder`; row N's output is
  committed before row N+1 starts. Parallelism is opt-in (`parallelism`) and is **not** enabled in v1 —
  the per-row `state.json` is not thread-safe under concurrent runs of the same config, and sequential
  keeps watermark handling simple. (This corrects the loose "rows run in parallel" phrasing; see §10.)
- **Incremental → output-mapping incremental + PK + per-row state.** Output tables are written with
  `incremental=True` and a **primary key of `surveyId`** (feedback/invitations) so a boundary re-read
  upserts rather than duplicates. The watermark is a composite `(finishDate epoch seconds, surveyId)`
  persisted in the row's `state.json`: `{"last_finish_date_epoch": <int>, "last_survey_id": "<string>"}`.
  With config rows this state is automatically row-scoped — the root `state` node is unused.
- **Secrets → `#`-prefixed.** `#client_secret` is `#`-prefixed and encrypted (`KBC::ProjectSecure`).
  `client_id`, instance host, and tenant/company name are non-secret plain config keys.
- **Sync actions.** `testConnection` (mint a token + one cheap metadata / `compute_cost_only` call) and
  `listFields` (live `fields` metadata query, to populate/validate field IDs). Both live in the schema/UI
  layer (`component-build-ui`).
- **Output bucket / table naming.** Follow default-bucket behaviour rather than hardcoding a destination.
  Whether `default_bucket` is on is a Dev Portal setting (Phase 6); the component sets the table *name*
  (per object) and lets the platform route the bucket. Do not hardcode a `destination`.

## 3. Authentication & connection

- **Auth method: OAuth 2.0 client-credentials** (the headless M2M path). Chosen over the
  authorization-code + OpenID flow, which only exists for APIs needing a user/ID token — not applicable to
  a server-side extract. No refresh token is used or needed: re-minting is a cheap Basic-auth call.
- **Credential transport:** `client_id` + `client_secret` sent as **HTTP Basic** (username=client_id,
  password=client_secret) to the token endpoint with `grant_type=client_credentials`.
- **Two distinct hosts (do not derive one from the other):**
  - Token host (per-instance): `https://<instance>.medallia.com/oauth/<companyName>/token`
  - API gateway host: `https://<api-host>.apis.medallia.com/data/v0/query`
  Both are supplied as config (instance host, tenant/company name, and the API host), because per-instance
  host-derivation rules vary (the company segment is sometimes doubled; sandbox hosts differ again).
- **Token lifetime:** access token default **3600s**, configurable per instance; response carries
  `token_type`, `access_token`, `expires_in`. Token-manager re-mints when the token is missing or expires
  within a **5-minute** safety window, and also re-mints once on a `401` and retries the call.

**Provisioning (NOT headless):** OAuth credentials are **admin-provisioned by Medallia**. A Medallia
expert / customer program-management team must enable API access for the instance and issue the
`client_id`, `client_secret`, and OAuth endpoint. The component cannot self-provision.

**Blockers / access:** Everything is **per-instance / per-customer** — reporting-instance host, tenant,
data-center/region, and credentials all vary, and **prod / sandbox / dev do not share credentials**. There
is **no shared public sandbox**. Sandbox testing budget is tiny (~100 API calls/month). Consequence: live
testing, VCR recording, and the cf-dev smoke test are **BLOCKED pending a customer instance + OAuth creds**
(§7, §8, §9). The component and its unit/datadir tests are fully buildable now against hand-authored
fixtures.

## 4. Data model & endpoints

- **In scope for v1: `feedback`** (completed surveys — statuses COMPLETED, EXCLUDED, AUTOEXCLUDED).
  **Deferred (add as config rows later):** `invitations` (superset of feedback) and `customer`/profiles.
  **Out of scope:** aggregation-only sources (`FEED_FILES`, `SSO_EVENT_STATS`, `SURVEY_EXPORT_STATS`,
  `USER_ACTIVITIES`) and `aggregate`/`aggregateTable` — this is a record-level extractor.
- **Field selection is manual/advanced.** The API has no universal schema; fields are customer-defined and
  addressed by ID via `fieldData(fieldId: <ID>) { values }` with documented prefixes (`a_` system,
  `e_` survey/experience, `k_` computed K-field). `values` is always a **list of strings**. The user
  supplies the field-ID list per row. v1 extracts `{ values }` (string lists); inline-fragment typed
  expansion (`... on <Type>` for Text Analytics / enumerated detail) is deferred.
- **Field discovery** via the `fields` metadata node (filters: `ids`, `dataType`, `q` substring,
  `filterable:true`); queried **once in its own top-level query**, never inline per record (inline inflates
  cost). This backs the `listFields` sync action.
- **Pagination — keyset/watermark (chosen), not Relay cursor.** Sort `orderBy:[initialFinishDate ASC,
  surveyId ASC]`, filter `(finishDate, surveyId) > last-seen`, loop by re-seeding the filter with the last
  processed record; decide "fetch another page" by `totalCount >= pageSize`. This unifies pagination with
  incremental state and survives Keboola's short-lived run boundaries (an in-run Relay `after` cursor is
  useless across runs). **Page size default 100, max 1000** — clamp the configured value to 1000 (default
  matches the reference extractor; gentler on the live instance than the ceiling).
- **Watermark value format — epoch OR ISO date/datetime.** The finish-date field is not required to be the
  epoch-int K-field. It may be a DATETIME / ISO-date field (e.g. `e_creationdate`, `e_responsedate` with
  values like `2026-05-01`). The watermark value is therefore stored and compared in its **native format**,
  selected by a `finish_date_field_type` knob (`epoch` | `datetime`, default `epoch` for backward
  compatibility): epoch values are parsed to `int` and compared numerically; ISO strings are kept verbatim
  and compared lexicographically (ISO 8601 sorts correctly as strings). The keyset filter bounds and the
  run's upper bound (`now`) are emitted in that same format — never hardcoded to epoch — so pointing the
  watermark at a date field neither crashes (`int()` is not forced) nor stalls (the cursor still advances).
- **Node identity.** Each node's direct scalar `id` is selected alongside the configured survey field (the
  reference extractor selects a bare `id` on feedback/invitations nodes) and carried as an output column,
  giving every record a stable, format-independent identity in addition to the `surveyId` primary key.
- **Rate / cost limits — header-driven, not hardcoded.** Published Query-API limits: 70 req/s,
  975,000 req/24h, 3,000,000 cost units/query, 90s gateway timeout, configurable max query depth. The
  platform-wide baseline (~60k/24h) is a conservative floor. Real limits are per-contract and reported
  live in `X-RateLimit-*` response headers — read them and slow as they approach zero; do **not** hardcode
  a cap. Retry with **exponential backoff** on 429/5xx, honouring `Retry-After`. Optionally call
  `compute_cost_only=true` (a free URI param that returns a query's cost without executing or consuming
  quota) once at startup to validate the configured query fits under the cost ceiling.
- **Response shape:** `{data:{<object>:{totalCount, nodes:[{<alias>:{values:[...]}}]}}, errors:[...]}`.
  Each node is one record; each requested field is an alias whose `values` is a string list. Flattening:
  one output row per node; a single-element `values` list → scalar column; a multi-element list →
  JSON-encoded string in that column (documented; no child tables in v1).

## 5. Configuration & schema

Fields described here; the actual `configSchema.json` / `configRowSchema.json` are built by
`component-build-ui` (Phase 6).

**Config (root) level — connection/auth, entered once:**
- `instance_host` (string, required) — reporting instance host for the token endpoint.
- `company_name` / tenant (string, required) — the `<companyName>` OAuth path segment.
- `api_host` (string, required) — the `.apis.medallia.com` gateway host (supplied, not derived).
- `client_id` (string, required) — OAuth client id (non-secret).
- `#client_secret` (string, required, encrypted) — OAuth client secret.

**Row level — per object:**
- `data_object` (enum, required) — `feedback` (v1); `invitations` / `customer` reserved for later rows.
- `fields` (array of field-ID strings, required) — the `fieldData(fieldId)` selection; manual/advanced,
  optionally backed by the `listFields` sync-action dropdown.
- `finish_date_field_id` (string, default `k_initialfinishdate_epoch_int`) — the watermark field; may be an
  epoch-seconds K-field or a DATETIME/ISO field (`e_creationdate`, `e_responsedate`, `e_initialfinishdate`).
- `finish_date_field_type` (enum `epoch` | `datetime`, default `epoch`) — declares the watermark field's
  value format so the value is stored/compared and the filter bounds are emitted in the right shape, and the
  output column is typed correctly (epoch → INTEGER, datetime → STRING).
- `survey_id_field_id` (string, default the instance's survey-id field) — second watermark component + PK.
- `filters` (optional) — business filter tree passed into the GraphQL filter (`and`/`or`/`not`, `in`,
  `gt/gte/lt/lte`, `isNull`).
- `page_size` (int, default 100, clamped to 1000).
- `initial_start_epoch` (int, optional) — first-run lower bound for an `epoch` watermark field when state is
  empty.
- `initial_start_value` (string, optional) — first-run lower bound (ISO date/datetime) for a `datetime`
  watermark field when state is empty.
- `load_type` (enum `incremental_load` | `full_load`, default `incremental_load`) — exposed as a dropdown,
  not a bare boolean.

**Sync actions:** `testConnection` (mint token + cheap metadata/`compute_cost_only` call) and `listFields`
(live `fields` metadata query for the dropdown / ID validation).

## 6. Code architecture

- **Separated GraphQL API client module** (e.g. `src/client/medallia_client.py`) distinct from
  `component.py`, containing: (a) `MedalliaTokenManager` (Basic-auth token mint, expiry tracking, 5-min
  pre-expiry re-mint, 401 re-mint+retry); (b) a GraphQL **query builder** (assembles the `fieldData`
  selection, keyset watermark filter, `orderBy`, page-size args, optional `compute_cost_only`);
  (c) a **paginator** that loops keyset pages updating the in-memory `(finishDate, surveyId)` watermark;
  (d) **cost/throttle awareness** reading `X-RateLimit-*` + `Retry-After` with exponential backoff.
- **Typed Pydantic config** — one model per parameter group (root connection model + row model),
  validated early; validation errors raised as `UserException`.
- **`run()` is a thin orchestrator:** load & validate config → build client → read state watermark →
  page oldest→newest writing rows → write manifest (PK, schema, incremental) → persist final watermark to
  state. Logic lives in well-named private methods (`_get_config`, `_fetch_records`, `_write_table`,
  `_load_state` / `_save_state`).
- **Incremental via Keboola state:** capture `end_timestamp = now()` at run start (upper bound); seed
  lower bound from `state.json` (or `initial_start_epoch` + sentinel survey id on first run); **persist the
  advanced watermark only after a successful table write** so a failed run safely retries from the old
  watermark.
- **Error handling:** `UserException` (exit 1) for user-fixable problems — bad/missing config, auth failure
  after re-mint (401), query cost over the 3M ceiling, missing watermark field on the instance, GraphQL
  `errors[]` indicating a bad query/field. Unexpected failures bubble up as exit 2. Never `sys.exit(2)` for
  a user-actionable error (exit 2 hides the message from the user). **Exception — non-fatal per-field
  errors:** Medallia returns `Invalid field id: <x>` in `errors[]` when a selected/auto-discovered field is
  not valid for the entity, yet still returns the valid data. Such errors are logged as a WARNING and the
  response is processed; only OTHER (real) errors raise `UserException` (mirrors the reference extractor).
- **Output manifest:** emit the **authoritative `schema` manifest** (`data_type.base.type`) — the CF
  default for a new component. Because `fieldData.values` is always strings, most columns are STRING;
  `surveyId` is the PK; the watermark field is INT **only when `finish_date_field_type=epoch`** — a
  `datetime` watermark field stays STRING (forcing INTEGER would corrupt an ISO value). This requires the
  Dev Portal `dataTypeSupport` property to be `authoritative` (set in Phase 6) — until flipped, the platform
  silently downgrades to legacy hints. If the CSV is written **with** a header row, pass `has_header=True` so
  Storage skips it.
- **Scratch files go to `/tmp`**, never `data/out/tables/` (everything under `data/out/tables/` is uploaded
  to Storage as a table).
- **Key dependencies:** `keboola.component` (common interface, state, manifests, `UserException`);
  `requests` for HTTP (token + GraphQL); `pydantic` for config. No Medallia SDK exists for Python — the
  GraphQL client is hand-rolled per the reference repo's shape.

## 7. Testing

- **Datadir tests (buildable now, no live API):**
  - Happy path — feedback page of N records → expected `feedback.csv` + manifest (PK, schema, incremental).
  - Pagination — multi-page keyset walk (`totalCount >= pageSize` triggers next page) stitched into one table.
  - Incremental — seeded `state.json` watermark → filter uses `(finishDate, surveyId) >` bound; output
    state advances to the last record.
  - First run — empty state → falls back to `initial_start_epoch`.
  - Error cases → exit code 1: invalid config (missing required field), GraphQL `errors[]` response,
    cost-limit-exceeded response, auth failure. Unexpected transport error → exit 2.
- **Unit tests:** token manager (mint, pre-expiry re-mint, 401 re-mint+retry), query builder (watermark
  filter tree + field selection), paginator loop, response flattening (scalar vs JSON-list column),
  backoff/`Retry-After` handling — all against a **stub transport / hand-authored fixtures**.
- **Sync action tests:** `testConnection` and `listFields` against stubbed responses.
- **Fixtures:** hand-authored from the documented + reference-repo response shape
  (`{data:{feedback:{totalCount, nodes:[{alias:{values:[...]}}]}}, errors:[...]}`) — no live payload
  captured (no creds).
- **VCR: scaffold only — RECORDING DEFERRED / BLOCKED.** Stand up the VCR test structure and secret
  sanitizers (redact `Authorization`, `client_secret`, token responses), but **record NO cassettes** and
  **fabricate NONE** — recording is blocked on a customer Medallia instance + OAuth creds. This is an
  explicit gate, not an omission (§9).

## 8. Deployment & validation (cf-dev)

- **Phase 7 smoke test is BLOCKED pending a customer Medallia instance + OAuth creds** — per-instance
  endpoint, no shared sandbox, so there is nothing to authenticate against in cf-dev yet.
- Planned flow once creds exist: build the `initial-implementation` image, create a cf-dev config via
  `kbagent` with the branch image tag (`runtime.tag` override), point it at the customer instance/API host
  + OAuth creds, run one feedback row, and confirm a successful job that lands `feedback.csv` with a
  plausible row count and an advanced `state.json` watermark.
- Until then: do not stub a fake endpoint or fabricate a run — pause and report the blocker.

## 9. Open risks & blockers

Ranked.

1. **BLOCKER — no credentials / no shared sandbox.** Live test, VCR recording, and cf-dev smoke test are
   blocked on customer instance + admin-provisioned OAuth creds. Mitigation: full build + unit/datadir
   tests now on fixtures; gate Phase 7 + VCR recording on "creds available". Owner: maintainer/customer.
2. **Per-instance schema variability.** Fields, hosts, and the presence of the epoch K-field
   (`k_initialfinishdate_epoch_int`) all vary per instance. Mitigation: manual/advanced field selection,
   configurable watermark field IDs with a DATETIME fallback, hosts supplied as config (not derived).
3. **Rate / cost limits + tiny sandbox budget.** 3M cost units/query, 90s timeout, ~100 sandbox
   calls/month. Mitigation: header-driven throttling, exponential backoff, optional `compute_cost_only`
   pre-flight, page size clamped to 1000.
4. **Token expiry mid-run.** 1h default. Mitigation: 5-min pre-expiry re-mint + 401 re-mint+retry in the
   token manager.
5. **Incremental overlap / duplicates.** Many records can share a finish-date second. Mitigation: composite
   `(finishDate, surveyId)` exclusive-boundary watermark + `surveyId` primary key + incremental output
   (upsert) makes boundary re-reads idempotent.
6. **Native-type manifest mismatch.** Authoritative `schema` output is silently downgraded until the Dev
   Portal `dataTypeSupport` is flipped to `authoritative`; a header row without `has_header=True` fails the
   Storage load. Mitigation: flip the property in Phase 6 and keep the write path + manifest in agreement;
   verify on the (blocked) smoke run.

## 10. Grounding reconciliation (keboola-context)

Per the Phase 3 done-bar, one line per behaviour-relevant reference; every `corrected:` item is folded into
the sections above.

- **architecture-conventions.md** → correct. Config rows per object, config-vs-row param split, `#` secrets,
  incremental + PK + state, test-connection sync action, enumerable→dropdown (`listFields`), authoritative
  schema manifest, `UserException`/exit-code split, client separated from `run()`, default-bucket naming —
  all applied and stated (§2, §5, §6).
- **config-rows.md** → corrected: the loose "rows run in parallel" reading is wrong. Rows execute
  **sequentially by default** in `rowsSortOrder`; parallelism is opt-in and **not** enabled in v1. Spec now
  states this (§2) and relies on automatic **per-row state** (root `state` unused) with the component seeing
  a single platform-**merged** `config.json`; test fixtures are single merged `config.json` + row-scoped
  `state.json` (§7).
- **incremental-state.md** → corrected/clarified: watermark captured before fetch, **persisted only after a
  successful write** (failed run retries from old watermark); `incremental=True` **with** `surveyId` PK =
  upsert; `load_type` is a row-level `full_load`/`incremental_load` **dropdown** (default incremental), not
  a bare boolean (§2, §5, §6). Our cursor is a composite `(finishDate, surveyId)` rather than a single
  `last_run` timestamp — the same pattern, adapted to the source's keyset order. **Refined for real customer
  usage:** the finish-date component is format-aware — an epoch-int field stores/compares as an `int`
  (numeric, output INTEGER), a DATETIME/ISO field stores/compares as a `str` (lexicographic, output STRING),
  selected by `finish_date_field_type`; the filter bounds are emitted in that same native format so the
  cursor advances for both. Separately, Medallia's non-fatal `Invalid field id:` `errors[]` entries are
  tolerated (logged, data still processed) so a single bad/auto-discovered field ID does not abort the load.
- **native-data-types.md** → corrected: v1 emits the **authoritative `schema`** manifest (not legacy
  `column_metadata`); this needs the Dev Portal `dataTypeSupport=authoritative` flip (Phase 6) or output is
  silently downgraded; if a header row is written, pass `has_header=True` (§6, §9).
- **encryption.md** → correct. Only `#client_secret` is `#`-prefixed/encrypted (`KBC::ProjectSecure`);
  `client_id`/hosts/tenant are plain non-secret keys; runtime receives the decrypted value (§2, §5).
- **output-mapping.md** → corrected/reinforced: scratch files go to **`/tmp`**, never `data/out/tables/`
  (everything there is uploaded); `incremental + PK = upsert`; no header rows in sliced tables (§6). v1
  writes a single (non-sliced) table per object, so the header/`has_header` rule from native-types governs.
- **exit-codes.md** → correct. User-actionable errors → `UserException` (exit 1) with a visible message;
  unexpected → exit 2; never `sys.exit(2)` for user errors (§6).
- **default-bucket.md** → corrected: do **not** hardcode a `destination` — set the table name per object and
  let default-bucket routing (a Dev Portal setting) place the bucket; a hardcoded destination would be
  silently overridden (§2).
- **environment-variables.md** → correct. `KBC_DATA_TYPE_SUPPORT` is **absent** (not empty) without the
  feature gate — default to legacy handling via the library's auto-detect; `KBC_CONFIGROWID` present per
  row; no `forward_token` needed (the extractor calls Medallia, not the Storage API) (§6).
- **telemetry.md** → not applicable — no telemetry querying in this component.
