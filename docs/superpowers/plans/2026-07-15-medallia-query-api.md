# Implementation Plan — keboola.ex-medallia (Medallia Query API)

> Source spec: `docs/superpowers/specs/2026-07-15-medallia-query-api-design.md`
> Branch: `initial-implementation`  ·  Component: `keboola.ex-medallia` (extractor)
> Execution engine: `superpowers:subagent-driven-development` — one fresh subagent per task,
> reviewed between tasks. Each task names the component skill that owns it so the subagent stays
> Keboola-aware. Lifecycle gating lives in `docs/superpowers/keboola.ex-medallia-lifecycle.md`.

This plan covers Phases 4 (implement) and 5 (tests) of the lifecycle. Phase 6 (Dev Portal),
Phase 7 (cf-dev smoke — BLOCKED on creds), and Phase 8 (review/PR) are tracked there, not here.

## Ground rules for every task

- Read the referenced spec section before coding; do not re-derive from the API.
- `ruff check` + `ruff format` clean before a task is done. Typed with Pydantic; validate config early.
- Secrets only via `#client_secret`; never log token or secret values.
- Scratch to `/tmp`, never `data/out/tables/`. `UserException` (exit 1) for user-fixable errors.
- Tests use hand-authored fixtures / stub transport — **no live API, no fabricated cassettes.**

---

## Phase 4 — Implementation (owner: `component-develop`, UI/schema via `component-build-ui`)

### Task 4.1 — Typed Pydantic config models
**Owner:** `component-develop`. **Spec:** §5, §6.
Replace the scaffold `src/configuration.py` (`print_hello`/`api_token`) with:
- Root `Configuration`: `instance_host`, `company_name`, `api_host`, `client_id`,
  `client_secret` (alias `#client_secret`).
- Row `RowConfiguration`: `data_object` (enum, `feedback` for v1), `fields: list[str]`,
  `finish_date_field_id` (default `k_initialfinishdate_epoch_int`), `survey_id_field_id`,
  `filters` (optional), `page_size` (default 1000, clamp ≤1000), `initial_start_epoch` (optional),
  `load_type` (`incremental_load`|`full_load`, default incremental) + computed `incremental` bool.
- Wrap `ValidationError` → `UserException` (keep the scaffold's pattern).
**Done:** models import; a merged sample config validates; bad config raises `UserException`.

### Task 4.2 — Token manager
**Owner:** `component-develop`. **Spec:** §3, §6.
`src/client/medallia_client.py` → `MedalliaTokenManager`: Basic-auth POST to
`https://<instance_host>/oauth/<company_name>/token` with `grant_type=client_credentials`; store
`access_token` + expiry from `expires_in`; `get_token()` re-mints when missing or within a 5-min
pre-expiry window; `invalidate()` for 401-driven re-mint. Never log the secret/token.
**Done:** unit-tested (mint, pre-expiry re-mint, forced invalidate) against a stub transport.

### Task 4.3 — GraphQL query builder
**Owner:** `component-develop`. **Spec:** §4, §6.
Build the `feedback` query: `fieldData(fieldId)` selection from `fields` + watermark fields +
`surveyId`; keyset watermark filter tree `(finishDate, surveyId) > bound` AND `finishDate < end`
plus optional business `filters`; `orderBy:[finishDate ASC, surveyId ASC]`; `first: page_size`;
`pageInfo`/`totalCount`. Support the `compute_cost_only=true` URI param variant.
**Done:** unit tests assert the emitted query/variables for seeded and first-run bounds.

### Task 4.4 — HTTP client: POST, throttling, backoff, pagination
**Owner:** `component-develop`. **Spec:** §4, §6.
Client method that POSTs GraphQL to `https://<api_host>/data/v0/query` with
`Authorization: Bearer`, re-minting once on 401; exponential backoff on 429/5xx honouring
`Retry-After`; reads `X-RateLimit-*` headers and slows as remaining approaches zero. Keyset
paginator loops while `totalCount >= page_size`, advancing the in-memory `(finishDate, surveyId)`
watermark. Raise `UserException` on GraphQL `errors[]`, cost-limit, and post-re-mint auth failure.
**Done:** unit tests for single page, multi-page walk, 401 re-mint+retry, 429 backoff, error→exit 1.

### Task 4.5 — Response flattening
**Owner:** `component-develop`. **Spec:** §4.
Map each `node` to one output row; each field alias's `values` → scalar column when single-element,
JSON-encoded string when multi-element. Derive column set from the configured `fields` + `surveyId`
+ watermark field.
**Done:** unit test covers scalar, empty, and multi-value fields.

### Task 4.6 — `run()` orchestrator + incremental state + manifest
**Owner:** `component-develop`. **Spec:** §2, §6.
Thin `run()`: load/validate config → build client → capture `end_timestamp=now()` → seed lower
bound from row `state.json` (`last_finish_date_epoch`/`last_survey_id`) or `initial_start_epoch` on
first run → page oldest→newest writing rows → `create_out_table_definition` with `primary_key=
["surveyId"]`, authoritative `schema`, `incremental=<load_type>` → write manifest → persist advanced
watermark to `state.json` **only after successful write**. Private methods: `_get_config`,
`_fetch_records`, `_write_table`, `_load_state`, `_save_state`.
**Done:** `ruff` clean; runs against a fixture datadir producing table + manifest + state.

### Task 4.7 — Schema + sync actions (UI)
**Owner:** `component-build-ui`. **Spec:** §5.
`configSchema.json` (root: hosts, tenant, `client_id`, `#client_secret`) and
`configRowSchema.json` (object, fields, watermark field IDs, filters, page_size,
initial_start_epoch, load_type dropdown). Sync actions: `testConnection` (token + cheap
metadata/`compute_cost_only`) and `listFields` (`fields` metadata → dropdown/validation).
Manual/advanced field selection leaning. **Done:** schema validates in the tester; sync actions wired.

## Phase 5 — Tests (owner: `component-test` / `generate-vcr-tests`)

### Task 5.1 — Datadir tests
**Owner:** `component-test`. **Spec:** §7.
Cases: happy path, multi-page pagination, incremental (seeded state → advanced state), first run
(empty state → `initial_start_epoch`), and error cases (invalid config, GraphQL `errors[]`,
cost-limit, auth failure → exit 1; unexpected transport → exit 2). Fixtures are single merged
`config.json` + row-scoped `state.json`, hand-authored from the documented response shape.
**Done:** all datadir tests green with no network.

### Task 5.2 — Unit tests
**Owner:** `component-test`. **Spec:** §7.
Consolidate/verify unit coverage for token manager, query builder, paginator, flattening, backoff,
and sync actions against the stub transport. **Done:** full non-VCR `pytest` suite green.

### Task 5.3 — VCR scaffolding (RECORDING DEFERRED)
**Owner:** `generate-vcr-tests`. **Spec:** §7, §9.
Stand up the VCR test structure + secret sanitizers (redact `Authorization`, `#client_secret`,
token response bodies). **Record NO cassettes and fabricate NONE** — recording is BLOCKED on a
customer Medallia instance + OAuth creds. Leave a clear README/marker that recording resumes when
creds arrive. **Done:** structure + sanitizers committed; no cassette files; suite still green.

---

## Deferred / blocked (tracked in the lifecycle file, not this plan)

- **Phase 6 — Dev Portal** (`component-dev-portal`): flip `dataTypeSupport=authoritative`, confirm
  `default_bucket` intent, publish schema/sync-actions/descriptions after the `0.0.1` bootstrap.
- **Phase 7 — cf-dev smoke test** (`component-test`): BLOCKED pending customer instance + OAuth
  creds — do not stub or fabricate; pause and report.
- **Phase 8 — Review/PR** (`component-checklist-review` + `babysit-pr`).
