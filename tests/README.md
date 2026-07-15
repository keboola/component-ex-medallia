# Tests

## Layout

| Path | Kind | Network | Notes |
|------|------|---------|-------|
| `test_unit.py` | Unit | none | Token manager, query builder, keyset paginator, backoff/rate-limit, GraphQL error handling, response helpers, config parsing/validation. All against an in-memory stub session. |
| `test_component.py` | Datadir (functional) | none | Full component run against a hand-authored `KBC_DATADIR` fixture with `requests.Session.post` monkeypatched (success: table + manifest + advanced state; failure: invalid config → exit 1). |
| `test_functional.py` | VCR replay | none (replay) | Replays recorded cassettes. Skips cleanly while no cassettes exist. |
| `data/` | Fixtures | — | Datadir fixtures for `test_component.py`. |
| `setup/configs.json` | VCR definitions | — | Test-case definitions for the `keboola.datadirtest` scaffolder. |

Run everything with `uv run pytest`. No credentials are needed for the committed suite.

## VCR recording — DEFERRED / BLOCKED

VCR recording exercises a **customer's live production Medallia instance**. It has NOT been
performed and the recorded cassettes are **not committed**.

Recording is blocked on two non-secret Medallia hostnames that were **not** present in
`secrets.json`: `instance_host` (OAuth token endpoint) and `api_host` (Query API gateway).
`secrets.json` currently provides only `parameters.username`, `parameters.#password`, and
`parameters.company` (→ `client_id`, `#client_secret`, `company_name`). Do not guess the
hosts — obtain them before recording.

When the hosts are available, record gently against the live instance:

1. Fill the real hosts into the `PLACEHOLDER_*` values in `tests/setup/configs.json`
   (hosts are non-secret and are recorded as-is). Real credentials stay in `secrets.json`.
2. Keep the blast radius tiny: `page_size = 5`, a recent `initial_start_epoch`
   (~7 days ago) so few records match, and cap recording at **≤ 2 feedback pages**.
   `testConnection` uses `compute_cost_only` (free); `listFields` is a single metadata call.
3. Record:

   ```bash
   uv run python -m keboola.datadirtest scaffold --secrets secrets.json
   ```

4. **Verify sanitization** before anything leaves your machine — grep the cassettes for
   the real credential/host values and confirm no matches. Sanitizers live in
   `src/component.py` (`VCR_SANITIZERS`) and redact `client_id`, `#client_secret`,
   `company`/`company_name`, `username`, `#password`, `access_token`, `token`, and the
   `Authorization` header.

### Cassettes are customer data — never commit them

`tests/functional/**/cassettes/` is gitignored. Recorded `.yaml`/`.json` cassettes contain
real customer feedback and must be **manually reviewed** and then deleted or kept strictly
local. Do not `git add` them. Commit only the test code and scaffolding in this directory.
