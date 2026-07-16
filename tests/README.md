# Tests

Run everything with `uv run pytest`. No credentials are needed to run the suite — the
committed VCR cassettes contain only deterministic **synthetic** data and replay with no
network access.

## Layout

| Path | Kind | Network | Notes |
|------|------|---------|-------|
| `test_unit.py` | Unit | none | Fine-grained logic: token manager, query builder, keyset paginator, backoff/rate-limit, GraphQL error handling, response helpers, config parsing/validation, sync actions. In-memory stub session. |
| `test_recording_support.py` | Unit | none | The two recording-support mechanisms: the `MEDALLIA_MAX_PAGES` page cap and the sanitizer's arity/shape preservation. |
| `test_security.py` | Unit (subprocess) | none | Secret-leak guard: a config-validation failure must never leak the decrypted secret to stdout/stderr (asserts absence of the sentinel / `input_value` / `ValidationError`). |
| `test_functional.py` | VCR replay | none (replay) | Auto-discovers and replays every `functional/*` case; plus an explicit resume-advances-from-seed assertion. Skips cleanly if no cassettes exist. |
| `conftest.py` | — | — | Sets `MEDALLIA_MAX_PAGES=3` for replay (see below). |
| `functional/` | VCR cases | none (replay) | The recorded test matrix (see below). |
| `setup/configs.json` | Definitions | — | Human-readable matrix of the sync/data/error cases + the failure cases. |
| `setup/configs.failure.json` | Definitions | — | The HTTP-free failure cases (scaffoldable with the standard CLI, no secrets). |
| `setup/record_matrix.py` | Recorder | live | The canonical, reproducible recorder for the data/sync/error cases (see "Re-recording"). |

## Functional case matrix (`functional/`)

**Sync actions**
- `01_testConnection` — token mint + `compute_cost_only` pre-flight (no quota consumed).
- `02_listFields` — one metadata call; the catalogue is replaced by a synthetic field set.

**HTTP-free failure / validation cases** (config validation fails before any HTTP → empty cassette, exit 1)
- `03_run_missing_instance_host` — missing required root field.
- `04_run_missing_client_secret` — missing credentials.
- `05_testConnection_missing_creds` — sync action, missing creds → clean exit 1 (never exit 2).
- `06_listFields_missing_creds` — sync action, missing creds → clean exit 1.
- `07_config_error_secret_leak_guard` — sentinel secret + missing field → exit 1 (the leak assertion itself lives in `test_security.py`).

**Live-recorded data matrix** (real recording → auto-sanitized → replayed)
- `08_epoch_watermark` — epoch finish field (`a_initial_finish_timestamp`); INTEGER typing + watermark advance.
- `09_incremental_resume` — seeded `in/state.json` (via `source/set_up.py`); watermark advances from the seed.
- `10_full_load` — `load_type=full_load`; non-incremental manifest + boundary-deduped output.
- `11_empty_result` — impossible filter → 0 rows, no lower bound → state left unchanged.
- `12_multi_value_field` — includes `a_recognition_sentiment_types` → JSON-encoded multi-value column.
- `13_business_filters` — `filters` applied and injected into the keyset query.
- `14_pagination_multi_page` — 3 pages (`page_size=5`, dense field, `MEDALLIA_MAX_PAGES=3`).
- `15_auth_401` — one deliberately-wrong `#client_secret` → 401 → actionable `UserException`, exit 1.
- `16_bad_request_4xx` — an unknown tenant in the token URL → HTTP 400 → actionable `UserException`, exit 1.

> The documented default epoch field `k_initialfinishdate_epoch_int` is **absent** on this
> instance; `a_initial_finish_timestamp` is its real epoch-finish equivalent and is used by
> the epoch cases.
>
> 5xx cannot be forced against a live instance, so it stays at the unit level
> (`test_unit.py`: retryable-status and non-retryable-5xx paths).

## Sanitization — REPLACE, not redact (`VCR_SANITIZERS` in `src/component.py`)

Free-text feedback can hold arbitrary PII, so a denylist is unsafe. The sanitizers run **at
record time** so cassettes are clean by construction and verifiable by allowlist:

- `DefaultSanitizer` — redacts `client_id` / `#client_secret` / `access_token` / `token` /
  `password` / `company` etc. and strips the `Authorization` header.
- `UrlPatternSanitizer` — rewrites any `*.medallia.com` host and the `/oauth/<company>/`
  path segment to fixed placeholders (`instance-host.redacted`, `api-host.redacted`,
  `/oauth/company/`), on both record and replay so replay still matches.
- `MedalliaResponseBodySanitizer` — overwrites every feedback node `id` (`RESP-####`) and
  every `fieldData` value with synthetic data, and replaces the `listFields` catalogue with a
  small generic field set. It **preserves each value's arity and shape** (a multi-value field
  stays multi-value; an epoch-integer field stays integer; a date/datetime field stays
  date-shaped) and keeps values monotonic in node order so keyset watermarks still advance and
  pagination terminates. Synthetic date/datetime/epoch values are anchored at `2026-08-01`
  (after the tests' seeds) so first-page rows are never dropped as boundary duplicates.

Because keboola.vcr returns the **real** response to the component while recording (only the
cassette is sanitized), each case is then **replayed against its own sanitized cassette** to
regenerate the synthetic `expected/` tables, `logs.json`, `sync_action_result.json` and
`expected_status.json`. `record_matrix.py` does both steps.

## `MEDALLIA_MAX_PAGES`

An optional hard page cap read from the environment (unset in production). It bounded the
live-recording blast radius and is set to `3` in `conftest.py` so replay stops at the end of
the recorded pages for the dense cases. Non-paginating cases ignore it.

## Re-recording

Live credentials go in a gitignored `secrets.json` at the repo root:

```json
{ "parameters": { "instance_host": "...", "api_host": "...", "company_name": "...",
                  "client_id": "...", "#client_secret": "..." } }
```

- **Data / sync / error cases** (record real → auto-sanitize → replay-regenerate):

  ```bash
  uv run python tests/setup/record_matrix.py both ALL        # or: both 08_epoch_watermark,12_multi_value_field
  ```

  This hits the **live** instance — be gentle (page_size=5, `MEDALLIA_MAX_PAGES=3`, one
  deliberately-wrong call each for 401/4xx).

- **HTTP-free failure cases** (no secrets, no network):

  ```bash
  uv run python -m keboola.datadirtest scaffold \
      --definitions tests/setup/configs.failure.json --output tests/functional --component src/component.py
  ```

After recording, **validate before committing**: `uv run pytest` must be green, and grep the
`functional/` tree for any real host / company / token / customer identifier — there must be
zero (every recorded value is synthetic by construction).
