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

Recording is blocked on the **OAuth client secret** (`#client_secret`). A usable plaintext
secret is not available — the customer's stored value is a `KBC::ProjectSecure` blob that
only decrypts inside their project. Recording cannot run until a usable secret is provided.

The gitignored, local-only `secrets.json` now provides the non-secret connection values
under the component's own parameter keys — `instance_host`, `api_host`, `company_name`,
`client_id` — so only `#client_secret` needs to be added before recording. `secrets.json`
is never committed and contains no client secret.

When a usable client secret is available, record gently against the live instance:

1. Add `#client_secret` to `secrets.json`. The connection hosts/company/client_id are
   already there; keep the `PLACEHOLDER_*`/`DUMMY_*` values in `tests/setup/configs.json`
   as-is (secrets.json overlays the real values at record time). Never commit real values.
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
