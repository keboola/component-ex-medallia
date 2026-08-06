# Implementation Plan — keboola.ex-medallia (Generic Query API Redesign)

> Source spec: `docs/superpowers/specs/2026-07-16-generic-query-api-redesign.md`
> Branch: `initial-implementation`  ·  Component: `keboola.ex-medallia` (extractor, pre-GA)
> Execution engine: `superpowers:subagent-driven-development` — one fresh subagent per task, reviewed
> between tasks. Each task names the component skill that owns it so the subagent stays Keboola-aware.
> Lifecycle gating lives in `docs/superpowers/keboola.ex-medallia-lifecycle.md`.

This plan covers **Phase 4 (implement)** and **Phase 5 (tests)**. Phase 6 (Dev Portal re-patch),
Phase 7 (cf-dev regenerate + smoke), and Phase 8 (review/PR) are tracked in the lifecycle file.

**Clean-replacement mandate (spec §11, approved decision A):** the component is pre-GA with no install
base. There is **no legacy shim, no dual code path, no v1 markers/state migration.** The existing
feedback-only `src/`, `configRowSchema.json`, and feedback tests are **replaced** by the generic design.
Delete the feedback-specific keyset/composite-watermark/`surveyId`-PK code — do not wrap it.

## Ground rules for every task

- Read the referenced spec section before coding; do not re-derive from the API. Use the §4 introspection
  ground truth, not new live calls, for design.
- `ruff check` + `ruff format` clean; `ty` clean; typed with Pydantic; validate config early.
- Secrets only via `#client_secret`; never log or print token/secret values. Keep the v1 `ValidationError`
  no-echo guard (`from None` + `errors(include_input=False)`) — it is a secret-leak protection.
- Scratch to `/tmp`, never `data/out/tables/`. `UserException` (exit 1) for user-fixable errors; unexpected
  → exit 2. Never `sys.exit(2)` for a user error.
- Async-fed config fields (`data_object`, `fields`, `incremental_field`, `raw_query`, `output_table`,
  `filters`) must have safe defaults and **no** `@field_validator` rejecting empties (sync actions
  instantiate the config on a half-filled form). Validate presence inside `run()` / the action method.
- **Live-instance discipline (creds exist, gitignored `secrets.json`):** only Tasks **5.3** and **Phase 7**
  touch the live instance. Be gentle — tiny `page_size`, hard `MEDALLIA_MAX_PAGES` cap, `compute_cost_only`
  pre-flight, no retry storms, report exact live-call count. **Cassettes carrying real customer data are
  NOT committed** (gitignored, manual inspection first).

---

## Phase 4 — Implementation (owner: `component-develop`; UI via `ui-developer`)

### Task 4.1 — Rewrite the Pydantic config models
**Owner:** `component-develop`. **Spec:** §5.
Keep root `Configuration` (hosts, tenant, `client_id`, `#client_secret`) unchanged. **Replace**
`RowConfiguration` with the redesigned row (spec §5.2): `mode` (`structured`|`raw`, default structured),
`data_object`, `fields: list[str]`, `load_type` (`incremental_load`|`full_load` + computed `incremental`),
`incremental_field`, `initial_start`, `filters` (JSON string, parsed+validated), `raw_query`,
`output_table`, `page_size` (default 100, clamp ≤1000). All async-fed fields default empty, no
empty-rejecting validators. Keep the GraphQL-name validation for field IDs and `filters` object keys
(injection guard). Drop `survey_id_field_id`, `finish_date_field_id`, `finish_date_field_type`,
`initial_start_epoch`, `initial_start_value`. `ValidationError` → `UserException` (no value echo).
**Done:** models import; a merged structured sample and a merged raw sample both validate; bad config +
malformed `filters` JSON raise `UserException`; no v1 field names remain.

### Task 4.2 — Client: introspection + object classification + shape resolution
**Owner:** `component-develop`. **Spec:** §4, §6.2, §6.3.
Add to `src/client/`: `run_introspection()` (POST an `__schema`/`__type` query), and pure classifiers —
`list_extractable_objects(schema)` (connection = OBJECT type exposing `nodes`; exclude the metadata-catalog
denylist `{fields, eventSchemas, programRecordSchemas}` and non-connections) with a **static allowlist
fallback** for introspection-disabled instances; and `resolve_object_shape(object, schema)` → one of
`fielddata` / `data` / `scalar`, whether it has a node `id`, and whether it accepts `filter`/`orderBy`.
**Done:** unit tests turn a captured introspection JSON into the 8-object extractable set (metadata catalogs
+ non-connections excluded) and classify each object's shape/id/filter-support; introspection-disabled →
fallback allowlist.

### Task 4.3 — Generic query builder (shape-aware, variable pagination)
**Owner:** `component-develop`. **Spec:** §6.4.
Replace the feedback-specific `MedalliaQueryBuilder` with `GenericQueryBuilder` emitting
`<object>(first:$first, after:$after [,filter:{…}] [,orderBy:[…]]) { nodes { <selection> } pageInfo
{ hasNextPage endCursor } }` with `$first`/`$after` as **GraphQL variables**. Selection per shape: (a)
`id` + `<f>: fieldData(fieldId:"<f>"){values}`; (b) `id` + Contact scalars + `<f>: data(fieldId:"<f>")
{value values}`; (c) `id?` + bare scalar field names. Incremental → AND-join `{fieldIds:["<f>"], gte:
"<watermark>"}` + `{…, lt:"<now>"}` with the user filter; only when the object supports `filter`.
**Done:** unit tests assert the emitted query + variables per shape, filter injection (gte lower + lt
upper), user-filter AND-join, orderBy, and field-ID/filter-key validation.

### Task 4.4 — Client: cursor paginator via `pageInfo` (keep token/throttle/backoff)
**Owner:** `component-develop`. **Spec:** §4, §6.4.
Replace the keyset/`totalCount` loop with a **Relay cursor** loop: post with `variables={first,after}`,
yield `nodes`, stop on `pageInfo.hasNextPage == false`, else `after = endCursor`; keep the
`MEDALLIA_MAX_PAGES` defensive cap. **Never read `totalCount`.** Keep `MedalliaTokenManager` (5-min
re-mint, 401 re-mint+retry), `X-RateLimit-*` throttle, exponential backoff + `Retry-After`, and the
non-fatal `Invalid field id:` tolerance. `run_metadata_query`/`compute_cost_only` retained.
**Done:** unit tests for single page, multi-page `hasNextPage` walk, `max_pages` cap, 401 re-mint+retry,
429 backoff, GraphQL `errors[]` → exit 1 — none depending on `totalCount`.

### Task 4.5 — Generic shape-detecting flatten + row-hash PK
**Owner:** `component-develop`. **Spec:** §4, §7.
Rewrite `flatten_node` to detect per-value shape at runtime: dict-with-`values` (fieldData) → 1→scalar /
0→None / >1→JSON; dict-with-`value(+values)` (customers) → scalar via `value`, multi via `values`; bare
scalar → copy; other dict/list → JSON. Add `row_hash(row)` = hex SHA-256 of canonical JSON (sorted keys,
stringified values, excluding `_row_hash`). PK strategy helper: node `id` when the shape has one, else
`_row_hash` column.
**Done:** unit tests cover all three shapes + multivalued→JSON + empty→None; row_hash determinism,
key-order independence, cross-run stability, self-exclusion.

### Task 4.6 — Incremental capability detection + single-scalar watermark + state
**Owner:** `component-develop`. **Spec:** §6.3, §8.
A row is incremental iff `load_type=incremental_load` AND the object supports `filter`/`orderBy` AND an
`incremental_field` is set — else auto full-load with a logged reason. At run start read the chosen
field's `dataType` (cheap metadata query) → DATE/DATETIME formats the bound as ISO (`YYYY-MM-DD`; Medallia
rejects `…Z`), INT as numeric seconds — the word "epoch" never surfaces. Watermark = single scalar
`{"last_incremental_value": …}`; lower bound = state → `initial_start` → none; upper bound = run-start
`now()`; advance to max seen; **persist only after a successful write**; write on every successful run
except raw mode.
**Done:** unit tests for capability detection, INT vs ISO auto-detect from dataType, gte boundary + max
advance, persist-after-write, and no-date-field → full-load fallback.

### Task 4.7 — `run()` orchestrator + typed manifest (dataType→BaseType)
**Owner:** `component-develop`. **Spec:** §2, §6, §9.
Thin `run()`: `_get_config` → branch on `mode` → build client → `_resolve_object` (shape/PK/incremental)
→ seed lower bound → page nodes writing rows → `create_out_table_definition` (PK = `id`/`_row_hash`,
authoritative `schema`, `incremental=<computed>`, `has_header=True`) → write manifest → persist watermark.
Manifest typing via the Medallia `dataType`→`BaseType` map (spec §9 table: INT→integer, FLOAT→numeric,
DATE→date, DATETIME→timestamp, others→string, multivalued→string; scalar-type map for shape (c); default
string when dataType absent). Private methods `_run_structured`, `_run_raw`, `_write_table`, `_load_state`,
`_save_state`, `_resolve_object`. **No legacy branch.**
**Done:** `ruff`/`ty` clean; runs against fixture datadirs for a fieldData object, a data object, a scalar
object, and an id-less object producing the right table + typed manifest + PK + state.

### Task 4.8 — Raw-GraphQL mode: execution + contract validation
**Owner:** `component-develop`. **Spec:** §6.5.
`_run_raw`: static pre-check (`raw_query` contains `$first`, `$after`, `pageInfo`); execute with
`variables={first,after}`; response validation — `data` must have exactly one key whose value has a list
`nodes` + a `pageInfo` (else precise UserException: zero / multi-connection / missing pageInfo); flatten
via §4, PK = node `id` or `_row_hash`; write to `<output_table>.csv`; **always full-load, never write
state**. `compute_cost_only` pre-flight at run start.
**Done:** unit tests for the static check, single-connection detection, zero/multi/missing-pageInfo
rejections; a datadir case runs a paginated raw query to a table with no state file written.

### Task 4.9 — Schema redesign: configSchema + configRowSchema
**Owner:** `ui-developer`. **Spec:** §5.
Keep `configSchema.json` (root) as-is. Rebuild `configRowSchema.json` per §5.2: `mode` select; sectioned
`type:object` groups (Source / Incremental / Advanced) with gap-spaced `propertyOrder`;
`options.dependencies` for every conditional (`mode`, `load_type`); `data_object` async `listObjects`
(`autoload`, `cache`, **`tags:true`** manual fallback, `enum:[]`); **`fields` multi-select fixed against
the verified Salesforce sibling** (`type:array`, `format:select`, `items:{type:string}`, `uniqueItems`,
async `listFields`, `autoload:["data_object"]`) — verify the Load button renders in the Ctrl+D sandbox and
add `items.enum:[]` if the renderer needs it; `incremental_field` async `listDateFields`; `filters` as a
JSON `format:editor`; `raw_query` as an editor; `output_table`; `validate_query` button; `page_size`.
One-sentence descriptions, detail in tooltips, `enum_titles` on every enum, English labels.
**Done:** both schemas validate in the schema tester; the object dropdown, dependent field multi-select,
JSON filter editor, and mode-gated raw fields all render correctly; no v1 row fields remain.

### Task 4.10 — Sync actions
**Owner:** `component-develop` (backend) + `ui-developer` (wiring). **Spec:** §6.1–§6.3, §6.5.
Implement/keep: `testConnection` (unchanged); `listObjects` (introspection → extractable connections,
static-allowlist fallback, humanized labels, autoload); object-aware `listFields` (route per object to
`fields`/`customerSchema`/`eventSchemas`/`programRecordSchemas` or node-type scalars; value=id, label=name)
and `listDateFields` (same, filtered to DATE/DATETIME + sortable INT); `validateQuery` (raw pre-flight:
static contract check + `compute_cost_only` + single-connection check → inline `success`/`error`).
**Done:** each action returns the documented shape; `MedalliaClientError`→`UserException` for clean Alerts;
sync-action unit tests green (Task 5.4).

## Phase 5 — Tests (owner: `component-test` / `generate-vcr-tests`)

### Task 5.1 — Unit tests (no live API — stub transport / fixtures)
**Owner:** `component-test`. **Spec:** §10.1.
Cover: generic flatten (3 shapes + multivalued/empty); row_hash; GenericQueryBuilder per shape + filter
injection + validation; object classification + fallback; object-aware listFields/listDateFields routing;
watermark (INT/ISO auto-detect, gte, persist-after-write); cursor paginator (hasNextPage, cap, no
totalCount); raw-mode validation; manifest typing map; config parsing + `mode` branch + invalid filters;
secret-leak guard. **Done:** full non-VCR `pytest` suite green; `ruff`/`ty` clean.

### Task 5.2 — Datadir fixtures per shape/mode (hand-authored — NO live)
**Owner:** `component-test`. **Spec:** §10.2.
Regenerate the feedback cases and add new ones under the new row shape: `feedback` (shape a, id PK);
`customers` (shape b, `data`, id PK, no totalCount); `programs` (shape c scalar, id PK); `socialURLs`
(shape c, id-less → `_row_hash` PK); incremental resume (gte + upsert, state advances); full-load object;
raw-mode happy path (no state written); raw-mode failures (multi-connection / missing pageInfo → exit 1);
error cases (401, 4xx, GraphQL errors, invalid filters JSON, cost-limit → exit 1; transport → exit 2).
Fixtures are single merged `config.json` + row-scoped `state.json`, hand-authored from the §4 shapes.
**Done:** all datadir tests green with no network; no v1-shaped fixtures remain.

### Task 5.3 — VCR: extend sanitizer + record NEW gentle live cassettes  ⚠️ NEEDS LIVE INSTANCE
**Owner:** `generate-vcr-tests`. **Spec:** §10.3.
**Extend `MedalliaResponseBodySanitizer`** to scrub the new shapes: `data(fieldId){value,values}`, bare
scalar node fields, `pageInfo.endCursor` (normalise to a deterministic placeholder), and id-less nodes —
keep the existing URL/host/secret sanitizers. Then record gentle cassettes (tiny page_size, hard page cap,
`compute_cost_only` pre-flight, report exact call count) for: `feedback`, `customers`, one scalar object
(`programs`), one id-less object (`socialURLs`) if it has data, and one raw-mode query. Where a high-volume
object is non-replayable (as feedback was in v1), keep the hand-authored datadir fixture as authoritative
and skip-with-reason the VCR case. **Cassettes with real data are NOT committed** (gitignored, manual
inspection first). **Done:** sanitizer extension unit-verified clean; cassettes recorded + inspected;
suite green on the no-cassette CI path.

### Task 5.4 — Sync-action tests
**Owner:** `component-test`. **Spec:** §10.4.
`testConnection`, `listObjects` (incl. introspection-disabled fallback), `listFields`/`listDateFields`
(per object), `validateQuery` — against stubbed introspection + metadata responses. **Done:** green, no live.

---

## Deferred / tracked in the lifecycle file (not this plan)

- **Phase 6 — Dev Portal re-patch** (`component-dev-portal` via `kbagent`): update the published
  `configurationSchema` + `configurationRowSchema` + `actions` (`listObjects`, `listFields`,
  `listDateFields`, `validateQuery`, `testConnection`) to the redesigned shape; `dataTypeSupport` stays
  `authoritative`; `defaultBucket` stays on. Dry-run + TTY-confirmed; confirm via fresh GET.
- **Phase 7 — cf-dev regenerate + smoke** (`component-test`, tier 4)  ⚠️ **NEEDS LIVE INSTANCE**:
  **regenerate** our cf-dev test config under the new row shape (discard the v1 test config), build the
  `initial-implementation` image, run one structured row (e.g. `feedback` or `customers`) and one raw-mode
  row end-to-end; confirm success, resolved branch image tag, correct PK/typed columns, and advanced state.
  Gentle-live rules apply.
- **Phase 8 — Review** (`component-checklist-review` + `babysit-pr`): full audit + Copilot loop on the
  `initial-implementation → main` PR; hand a clean PR to the maintainer (Factory never merges).
