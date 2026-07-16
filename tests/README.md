# Tests

## Layout

| Path | Kind | Network | Notes |
|------|------|---------|-------|
| `test_unit.py` | Unit | none | Token manager, query builder, keyset paginator, backoff/rate-limit, GraphQL error handling, response helpers, config parsing/validation. All against an in-memory stub session. |
| `test_component.py` | Datadir (functional) | none | Full component run against a hand-authored `KBC_DATADIR` fixture with `requests.Session.post` monkeypatched (success: table + manifest + advanced state; failure: invalid config → exit 1). |
| `test_functional.py` | VCR replay | none (replay) | Replays the committed cassettes under `tests/functional/`. Skips cleanly only if no cassettes are present. |
| `data/` | Fixtures | — | Datadir fixtures for `test_component.py`. |
| `setup/configs.json` | VCR definitions | — | Test-case definitions for the `keboola.datadirtest` scaffolder. |

Run everything with `uv run pytest`. No credentials are needed — the committed cassettes
contain only deterministic synthetic data and replay with no network access.

## VCR cassettes — committed, synthetic by construction

The `tests/functional/` tree **is committed**. Every recorded value is deterministic
synthetic data; no real customer feedback, field ID, host, or credential is present. Only
the per-run output dir (`source/data/out/`) is gitignored (it is regenerated on each replay),
and `secrets.json` is gitignored as always.

Committed cases (see `setup/configs.json`):

- `01_testConnection` — `compute_cost_only` pre-flight (free). Replays green.
- `02_listFields` — one metadata call; the returned catalogue is a small generic synthetic
  field set (`a_surveyid`, `e_creationdate`, `e_nps`, `e_comment`, `a_survey_channel`).
- `03_feedback_incremental` — a self-terminating two-page keyset feedback extract for the
  **datetime** watermark schema (`finish_date_field_id=e_creationdate`,
  `finish_date_field_type=datetime`, `survey_id_field_id=a_surveyid`), `page_size=5`. Page 1
  returns 5 nodes (`totalCount=8`) and page 2 returns a short final page of 3 nodes
  (`totalCount=3 < page_size`), so keyset pagination stops within the recorded pages and the
  case replays green as a regression fixture.

`test_component.py` remains the second, independent feedback regression layer (monkeypatched
transport, fully fabricated data).

## Sanitization — REPLACE, not redact

Free-text feedback (`e_comment` and any verbatim field) can hold arbitrary PII, so a denylist
is unsafe. `VCR_SANITIZERS` in `src/component.py` therefore **overwrites** every value with
synthetic data, so a recording is clean by construction and verifiable by allowlist:

- `DefaultSanitizer` — redacts `client_id` / `#client_secret` / `access_token` / `token` and
  strips the `Authorization` header.
- `UrlPatternSanitizer` — rewrites any `*.medallia.com` host and the `/oauth/<company>/` path
  segment to fixed placeholders (`instance-host.redacted`, `api-host.redacted`).
- `MedalliaResponseBodySanitizer` — overwrites every feedback node `id` (`RESP-####`) and
  every `fieldData` value with a synthetic value chosen from the alias name, and replaces the
  real `listFields` catalogue wholesale with a small generic field set. Node numbering is
  sequential across a recording's responses so the finish-date / survey-id watermark stays
  monotonic and pagination still terminates naturally.

## To re-record

`secrets.json` (gitignored) supplies the real `instance_host`, `api_host`, `company_name`,
`client_id`, and `#client_secret`; keep the generic `*.medallia.com` dummies in
`setup/configs.json` as-is (they are overlaid at record time and rewritten to placeholders by
the URL sanitizer so replay still matches). Then:

```bash
uv run python -m keboola.datadirtest scaffold --secrets secrets.json
```

The `MedalliaResponseBodySanitizer` runs automatically during recording, so a fresh recording
is synthetic on disk. For `03_feedback_incremental`, pick a tenant/window that yields fewer
than `page_size` records on the last page (or trim the recorded final page) so the cassette is
self-terminating and replayable.
