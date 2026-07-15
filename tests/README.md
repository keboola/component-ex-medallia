# Tests

## Layout

| Path | Kind | Network | Notes |
|------|------|---------|-------|
| `test_unit.py` | Unit | none | Token manager, query builder, keyset paginator, backoff/rate-limit, GraphQL error handling, response helpers, config parsing/validation. All against an in-memory stub session. |
| `test_component.py` | Datadir (functional) | none | Full component run against a hand-authored `KBC_DATADIR` fixture with `requests.Session.post` monkeypatched (success: table + manifest + advanced state; failure: invalid config → exit 1). |
| `test_functional.py` | VCR replay | none (replay) | Replays recorded cassettes. Skips cleanly when no cassettes exist (the committed / CI state). |
| `data/` | Fixtures | — | Datadir fixtures for `test_component.py`. |
| `setup/configs.json` | VCR definitions | — | Test-case definitions for the `keboola.datadirtest` scaffolder. |

Run everything with `uv run pytest`. No credentials are needed for the committed suite, and
because cassettes are never committed, `test_functional.py` skips in CI.

## VCR recording — DONE (cassettes local-only, NOT committed)

Minimal cassettes were recorded once against the customer's **live production Medallia
instance** and left on disk under `tests/functional/` (gitignored) for manual review. They
are **customer data and must never be committed**.

Recorded cases (see `setup/configs.json`):

- `01_testConnection` — `compute_cost_only` pre-flight (free). Replays green.
- `02_listFields` — one metadata call returning the field catalog. Replays green.
- `03_feedback_incremental` — a small live feedback extract for this customer's **datetime**
  watermark schema (`finish_date_field_id=e_creationdate`, `finish_date_field_type=datetime`,
  `survey_id_field_id=a_surveyid`), `page_size=5`, `initial_start_value` ~7 days ago.
  **Excluded from replay** (see below); kept on disk only as a proof-of-life artifact.

### How feedback extraction is verified (two layers)

1. **Deterministic regression test** — `tests/test_component.py` runs the full component
   with the HTTP transport monkeypatched, exercising multi-page/keyset pagination,
   incremental watermark advance, output table + manifest, and `state.json`. This is the
   authoritative, always-run feedback test.
2. **One-time live proof-of-life** — `03_feedback_incremental` was recorded once against the
   live instance (exit 0, real rows) to prove the datetime schema works end-to-end. It is
   **not** a replayable regression fixture and is **skipped by `test_functional.py`** (with a
   `reason=`), so both local and CI `pytest` are fully green.

### Why the feedback recording is hard-capped and not replayable

The live 7-day window holds ~2300 feedback records, so recording was hard-capped to **2
pages** (10 records) via a temporary `MEDALLIA_MAX_PAGES=2` env guard applied **only during
recording** (the guard is not part of the shipped code). Medallia's date filter for this
field accepts only **day granularity** (`YYYY-MM-DD`), so no bounded, terminating cassette is
possible for a tenant at this volume: any gentle (<= 2 page) capture stops mid-pagination,
and an un-capped replay would request a page that was deliberately never recorded. Hence
`test_functional.py` replays only `01_testConnection` and `02_listFields` and skips any
feedback recording. To obtain a replayable feedback cassette you would need a tenant/window
that naturally yields fewer than `page_size` records on the last page.

### To re-record

`secrets.json` (gitignored, never committed) supplies the real `instance_host`, `api_host`,
`company_name`, `client_id`, and `#client_secret`; keep the generic `*.medallia.com` dummies
in `setup/configs.json` as-is (they are overlaid at record time and rewritten to fixed
placeholders by the URL sanitizer so replay still matches). Then:

```bash
# Full window is huge — cap pages while recording so the live instance is not hammered.
MEDALLIA_MAX_PAGES=2 uv run python -m keboola.datadirtest scaffold --secrets secrets.json
```

`testConnection` uses `compute_cost_only` (free); `listFields` is a single metadata call.

### Sanitization

Sanitizers live in `src/component.py` (`VCR_SANITIZERS`): `DefaultSanitizer` redacts
`client_id` / `#client_secret` / `access_token` / `token` / the `Authorization` header, and a
generic `UrlPatternSanitizer` rewrites any `*.medallia.com` host and the `/oauth/<company>/`
path segment to fixed placeholders. Verified after recording: no client id, client secret,
bearer token, host, or full company name appears in any cassette. Note the `02_listFields`
cassette necessarily contains the customer's **field catalog**, and some of the customer's
own field IDs embed the company name — that is the data `listFields` returns, not a
credential leak, and the cassette is not committed.

### Cassettes are customer data — never commit them

The entire `tests/functional/` tree is gitignored. Recorded cassettes contain real customer
feedback and field schema; **manually review then delete or keep strictly local**. Do not
`git add` them. Commit only the test code and scaffolding.
