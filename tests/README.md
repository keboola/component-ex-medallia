# Tests

Run everything with `uv run pytest`. No credentials are needed to run the suite — the
committed VCR cassettes contain only deterministic **synthetic** data and replay with no
network access.

## Layout

| Path | Kind | Network | Notes |
|------|------|---------|-------|
| `test_unit.py` | Unit | none | Fine-grained pure logic: generic flatten (3 shapes), `row_hash`, `GenericQueryBuilder` (per-shape selection, variable pagination, `gte`/`lt` filter injection, field/type injection guards), object classification + shape resolution, `RowConfiguration`/`Configuration` + `parsed_filters`, watermark/`advance_watermark`, cursor paginator, raw-mode validation, `dataType`→`BaseType` map, secret-leak guard. In-memory stub session/token manager. |
| `test_datadir.py` | Functional (in-process) | none | Drives the real `Component.run()` end-to-end against a temp `KBC_DATADIR` with a stubbed `requests.Session` (a Python object — NOT a committed cassette). Covers each node shape + PK strategy + mode: fieldData incremental resume (node-id PK, `gte`, watermark advance, `Int!/ID` variable header), `data` shape full load, id-less `_row_hash`, empty result, multi-page pagination, raw mode (id PK + `_row_hash`, no state), and the HTTP-free contract failures. |
| `test_recording_support.py` | Unit | none | The `MedalliaResponseBodySanitizer` guarantee across all three node shapes + `pageInfo.endCursor` + the request `after` cursor: proves no real value can survive into a cassette (planted-sentinel allowlist). |
| `test_security.py` | Unit (subprocess) | none | Secret-leak guard: a config-validation failure must never leak the decrypted secret to stdout/stderr. |
| `test_functional.py` | VCR replay | none (replay) | Auto-discovers and replays every `functional/*` case. Skips cleanly if no cassettes exist. |
| `conftest.py` | — | — | Sets `MEDALLIA_MAX_PAGES=2` for replay (must match `record_matrix.py`). |
| `functional/` | VCR cases | none (replay) | The recorded test matrix (see below). |
| `setup/configs.failure.json` | Definitions | — | The HTTP-free failure cases (scaffoldable with the standard CLI, no secrets). |
| `setup/record_matrix.py` | Recorder | live | The canonical, reproducible recorder for the live data/sync/error cases. |

## Functional case matrix (`functional/`)

**HTTP-free failure / validation cases** (config/contract validation fails before any HTTP → empty cassette, exit 1; scaffolded from `setup/configs.failure.json`)
- `03_run_missing_instance_host` — missing required root field.
- `04_run_missing_client_secret` — missing credentials.
- `05_testConnection_missing_creds` — sync action, missing creds → clean exit 1 (never exit 2).
- `06_listObjects_missing_creds` — sync action, missing creds → clean exit 1.
- `07_config_error_secret_leak_guard` — sentinel secret + missing field → exit 1 (the leak assertion itself lives in `test_security.py`).
- `08_run_bad_raw_query` — raw mode, query missing `$first`/`$after`/`pageInfo` → static contract check → exit 1.

**Live-recorded matrix** (real recording → auto-sanitized at record time → replayed)
- `10_testConnection` — token mint + `compute_cost_only` pre-flight (no quota consumed).
- `11_listObjects` — one `__schema` introspection; classified into the 8 extractable connections.
- `12_listFields_feedback` — the field catalogue (replaced by a synthetic field set).
- `13_feedback` — shape (a) `fieldData`, node-`id` PK, incremental on the epoch finish field (`Int!/ID` header, INTEGER typing, watermark advance).
- `14_invitations` — shape (a) `fieldData`, node-`id` PK, incremental (feedback's superset).
- `17_raw` — raw mode, user Relay query paginated via `$first`/`$after`; full load, no state written.
- `18_auth_401` — one deliberately-wrong `#client_secret` → 401 → actionable `UserException`, exit 1.
- `19_bad_request_4xx` — an unknown tenant in the token URL → HTTP 4xx → actionable `UserException`, exit 1.

> **Skipped-with-reason live shapes** (per spec §10.3 "skip rather than force"): the `customers`
> (`data`) shape returns *"Unsupported Operation - Elasticsearch is not enabled"* on this
> instance, and the id-less operational connections (`socialURLs`/`socialUrlsHealth`/
> `unitWarnings`) declare a **non-null offset cursor** (`after: Int!`) the variable-based
> first-page request cannot satisfy. Both shapes are covered network-free by the hand-authored
> fixtures in `test_datadir.py` instead. See `record_matrix.py` for the exact reasons.

## Sanitization — REPLACE, not redact (`VCR_SANITIZERS` in `src/component.py`)

Free-text feedback can hold arbitrary PII, so a denylist is unsafe. The sanitizers run **at
record time** so cassettes are clean by construction and verifiable by allowlist:

- `DefaultSanitizer` — redacts `client_id` / `#client_secret` / `access_token` / `token` /
  `password` / `company` etc. and strips the `Authorization` header.
- `UrlPatternSanitizer` — rewrites any `*.medallia.com` host and the `/oauth/<company>/`
  path segment to fixed placeholders (`instance-host.redacted`, `api-host.redacted`,
  `/oauth/company/`), on both record and replay so replay still matches.
- `MedalliaResponseBodySanitizer` — overwrites **every node value across all three node
  shapes** — (a) `fieldData(fieldId){values}`, (b) `data(fieldId){value,values}`, and (c) bare
  scalar node fields, **including id-less nodes** — plus every node `id` (`RESP-####`), the
  `pageInfo.endCursor` (`SYNTHETIC-CURSOR-####`), and the request `after` cursor. It replaces
  the `fields` catalogue with a fixed synthetic field set (covering the field IDs the recorded
  cases use, so typing/watermark stay faithful). It **preserves each value's arity and shape**
  (multi-value stays multi-value; an epoch-integer field stays a long integer; a date/datetime
  field stays date-shaped) and keeps values monotonic so incremental watermarks advance and
  pagination terminates. Synthetic date/datetime/epoch values are anchored at `2026-08-01`.

Because keboola.vcr returns the **real** response to the component while recording (only the
cassette is sanitized), each case is then **replayed against its own sanitized cassette** to
regenerate the synthetic `expected/` tables, `logs.json`, `sync_action_result.json` and
`expected_status.json`. `record_matrix.py` does both steps.

## `MEDALLIA_MAX_PAGES`

An optional hard page cap read from the environment (unset in production). It bounded the
live-recording blast radius and is set to `2` in `conftest.py` so replay stops at the end of
the recorded pages for the dense cases. **It must match `REPLAY_MAX_PAGES` in `record_matrix.py`.**
Non-paginating cases ignore it.

## Re-recording

Live credentials go in a gitignored `secrets.json` at the repo root:

```json
{ "parameters": { "instance_host": "...", "api_host": "...", "company_name": "...",
                  "client_id": "...", "#client_secret": "..." } }
```

- **Live data / sync / error cases** (record real → auto-sanitize → replay-regenerate):

  ```bash
  uv run python tests/setup/record_matrix.py both ALL        # or: both 13_feedback,17_raw
  ```

  This hits the **live** instance — be gentle (`page_size=5`, `MEDALLIA_MAX_PAGES=2`, one
  deliberately-wrong call each for 401/4xx).

- **HTTP-free failure cases** (no secrets, no network):

  ```bash
  uv run python -m keboola.datadirtest scaffold \
      --definitions tests/setup/configs.failure.json --output tests/functional --component src/component.py
  ```

After recording, **validate before committing**: `uv run pytest` must be green, and grep the
`functional/` tree for any real host / company / token / customer identifier — there must be
zero (every recorded value is synthetic by construction).
