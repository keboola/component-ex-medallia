# Research Summary — `keboola.ex-medallia` (Medallia Experience Cloud, Query API)

Phase 2 (RESEARCH) output for the Component Factory build. This is research only — not the spec
(Phase 3) and nothing is committed. All facts below are grounded in the maintainer's verified list
and deepened against Medallia's official docs and the official reference repo. Where I found the
published numbers differ from the verified list, I flag it explicitly (item 5) rather than silently
overriding.

## Sources (all public, no login)

- Developer portal (verified anchors): `https://developer.medallia.com/medallia-apis/reference/query-api-overview`,
  `.../reference/authentication` (JS/MDX-rendered — content read via the product docs mirror below).
- Product docs (Heretto, render cleanly), all under `https://docs.medallia.com/en/medallia-experience-cloud/integration/apis/`:
  - `query-api` (overview, endpoint, auth, request/response shape, data sources list)
  - `query-api/recommended-methodology` (explore-fields → query → optimize; cost preview)
  - `query-api/restrictions-and-limits` (rate/cost limits, timeout, headers)
  - `query-api/data-queries` (feedback / invitations / profiles root nodes; aggregations; translations)
  - `query-api/data-queries/pagination` (Relay cursor pagination)
  - `query-api/data-queries/filter-and-sort-data` (filter tree operators, orderBy, filterable types)
  - `query-api/metadata-queries` (field discovery via `fields`)
  - `authenticate-apis-with-oauth` (client-credentials flow, token lifetime)
  - `api-sandbox` (sandbox host pattern + 100 calls/month test guidance)
- Official reference implementation (Apache-2.0, Java): `github.com/medallia/query-api-data-extract`
  — read the full source via `gh api` (README, `SyncService.java`, `RecordProcessingService.java`,
  `WebClientConfig.java`, `RetryConfig.java`, `application.properties.template`,
  `QueryApiResponse.java`). The `recommended-methodology` doc page names this repo as *the* official
  reference (explicitly "not intended for productive use").
- Fivetran markets a managed Medallia connector (OAuth client-id/secret + access token, domain/
  subdomain config); its detailed table/incremental docs are login-gated, so it only corroborates
  the auth model, not internals.

---

## 1. API style confirmed — Query API / GraphQL is the right target

Confirmed. The Query API is a **GraphQL** API over a **single HTTP POST endpoint** (`/data/v0/query`).
It is described by Medallia as "the primary way for apps to access Experience Cloud raw survey data"
and is explicitly positioned for the BI/data-warehouse extract use case (Power BI, Tableau, "pull
Experience Cloud record-level data into your BI tool, data warehouse"). It is self-documenting via
GraphQL introspection.

This is the correct surface for a Keboola extractor versus the other Medallia APIs: Import/Feed
(`/inbound/...`) is write/ingest, Users/Admin (`/admin/v1`) is provisioning, Agile Research and Speech
are separate products. Query API is the read/analytics surface. **No change to the verified target.**

Note the platform prerequisite: Experience programs must already be set up in the target instance —
they create the referenceable schema for Feedback, Invitations, and Record fields. An empty/unconfigured
instance has no queryable schema.

## 2. Auth — client-credentials OAuth2, 1-hour bearer, token-manager design

Confirmed and deepened.

- **Grant:** OAuth 2.0 **client-credentials**. (An authorization-code + OpenID flow also exists but is
  only for APIs needing a user/ID token — not needed here. Use client-credentials, the headless M2M path.)
- **Credential transport:** client ID + client secret passed as **HTTP Basic** (username=client_id,
  password=client_secret) to the token endpoint with `grant_type=client_credentials`.
- **Token endpoint (per-instance):** `https://<instance>.medallia.com/oauth/<companyName>/token`.
  (Reference repo template: `token-uri = https://YOURINSTANCE.medallia.com/oauth/YOURCOMPANY/token`.)
- **API calls:** go to a *different* host — the API gateway: `https://<instance-tenant>.apis.medallia.com/data/v0/query`
  (reference repo: `https://YOURCOMPANY.apis.medallia.com/data/v0/query`). Sandbox host differs again
  (see item 8). Bearer token sent as `Authorization: Bearer <access_token>`.
- **Lifetime:** access token **default 3600s (1 hour)**, configurable per instance; response returns
  `token_type`, `access_token`, `expires_in`. For client-credentials there is **no need for a refresh
  token** — re-minting a token is a cheap Basic-auth call, and refresh tokens are a feature of the
  authorization-code flow only.

**Token-manager design (recommendation):** a small `MedalliaTokenManager` that (a) mints a token via
Basic-auth POST to the instance token endpoint, (b) stores `access_token` + computed expiry from
`expires_in`, (c) re-mints when the token is missing or expires within a 5-minute safety window
(Medallia's own docs recommend exactly this "refresh if expiring in the next 5 minutes" pattern), and
(d) also re-mints on a 401 and retries once. Token host and API host are **separate** config-derived
values — do not assume one from the other.

## 3. GraphQL query shape — pagination args, `fieldData(fieldId)`, filter tree

Confirmed against both docs and the reference repo.

- **Field selection is `fieldData(fieldId: <ID>) { values }`.** Fields are customer-defined and
  addressed by ID with the documented prefixes (`a_` administrative/system, `e_` experience/survey,
  `k_` computed "K-field"). `values` is always a list of strings. Example from the reference repo's
  query: `surveyId: fieldData(fieldId: $surveyIdField) { values }` and
  `finishDate: fieldData(fieldId: $initialFinishDateField) { values }`, plus a `%s` slot for the
  user's custom field list.
- **Typed expansion:** because each Medallia data type has its own schema, `fieldData` combines with
  GraphQL inline fragments (`... on <Type>`) to pull type-specific detail (e.g. enumerated values,
  comment/Text-Analytics data). For a first version, `{ values }` (string list) is sufficient and is
  what the reference implementation uses.
- **Filter tree:** logical operators `{and:[...]}`, `{or:[...]}`, `{not:[...]}`, and `isNull:true|false`;
  comparison operators `{in:[String]}`, `{gt|gte|lt|lte: String}`. A filter node targets fields via
  `fieldIds: [<ID>]`. Date filtering is done by comparing a date/epoch field with `gt/gte/lt/lte`
  (see item 6). **Text-search filtering is not supported.**
- **Sorting:** `orderBy: [{ direction: ASC|DESC, fieldId: <ID> }]`.
- **Aggregation:** `aggregate` / `aggregateTable` exist (AVG/SUM/COUNT, 2D tables) — out of scope for a
  record-level extractor v1 but available. NPS/CSAT are just fields, not special endpoints.

## 4. Pagination mechanics + max page size

Two supported models — pick deliberately:

- **Relay cursor connection (documented default):** args `first` (page size) + `after` (cursor);
  response exposes `pageInfo { endCursor hasNextPage }` and `totalCount`. Loop: query with `after:null`,
  then set `after = endCursor` while `hasNextPage == true`. Must be sequential — cursors cannot be
  precomputed.
- **Keyset/watermark (what the official reference repo actually uses):** instead of `after`, it sorts by
  `orderBy:[finishDate ASC, surveyId ASC]` and filters `finishDate/surveyId > last-seen`, then loops
  by re-seeding the filter with the last processed record; it decides "fetch another page" by checking
  `totalCount >= pageSize`. This model *is* the incremental checkpoint (item 6), which is why the
  reference chose it.
- **Page size:** **default 30** if `first` is omitted; **max 1000**. The reference repo hardcodes a
  `MAX_RECORDS_PER_REQUEST = 1000` ceiling and clamps the configured value to it.

**Recommendation:** use the reference repo's keyset approach — it unifies pagination and incremental
state and survives run boundaries cleanly (Keboola runs are short-lived, so an in-run `after` cursor is
useless across runs; a persisted watermark is exactly right).

## 5. Rate limits + cost model + throttling/backoff strategy

Published Query-API-specific limits (from the Query API `restrictions-and-limits` page):

- **70 requests / second**
- **975,000 requests / 24-hour period**
- **3,000,000 cost units per query** (query rejected above this)
- **90-second gateway timeout** per request (longer requests are discarded)
- **Configurable max query depth** (per-instance; "consult your Medallia expert")

> **Discrepancy flag (does not contradict the verified list, reconciles it):** the maintainer's
> "~60k calls/24h" matches Medallia's *general* cross-API baseline ("at least 60,000 per 24h") stated
> on the platform-wide `api-restrictions-and-limits` page, whereas the *Query-API-specific* page
> publishes the higher 70/s + 975k/24h figures. Real limits are **per-contract/per-instance** and are
> reported live in response headers (`X-RateLimit-Limit-day`, `-Limit-second`,
> `-Remaining-day`, `-Remaining-second`, `-Remaining-credits-minute` — currently documented as
> deprecated in favour of a newer header set). Treat 60k/24h as a safe conservative floor; do not
> hardcode either number.

**Cost preview (important, free):** append `compute_cost_only=true` as a URI query param to get a
query's cost **without executing it and without consuming quota**. Cost is driven mainly by page size
(`first`) and number of fields requested.

**Throttling/backoff strategy (recommendation):**
- Be **header-driven, not hardcoded**: read the `X-RateLimit-Remaining-*` headers and slow down as they
  approach zero rather than assuming a fixed cap.
- Retry with backoff on 429 and 5xx; honour `Retry-After` if present. Reference repo uses a simple
  fixed-backoff retry (2 attempts, 2s) — for Keboola prefer exponential backoff with a few attempts.
- Keep page size high (up to 1000) to minimise call count against the 24h quota, but watch the 3M
  cost limit and 90s timeout — if a page of 1000 with the selected fields exceeds cost/timeout, reduce
  page size. Optionally call `compute_cost_only=true` once at startup to validate the configured
  query fits under the cost ceiling before the real run.
- **Live-test budget is tiny** (sandbox ~100 calls/month) — see item 8.

## 6. Incremental strategy — state shape, ordering, dedup

The official pattern (README "Theory of Operation" + `SyncService`/`RecordProcessingService`):

- **Track two values from the last record pulled:** the **survey id** and the **initial finish date**
  (the moment the record first became available in Reporting), stored as a **Unix epoch seconds**
  integer. Medallia recommends materialising the epoch as a computed **K-field**
  (`k_initialfinishdate_epoch_int`) via a small transform of `e_initialfinishdate`
  (`Math.floor(date.getTime()/1000)`) so it is filterable/sortable as an integer.
- **Ordering: oldest → newest**, `orderBy:[initialFinishDate ASC, surveyId ASC]`.
- **The exact filter (from the reference repo) for a run window is:**
  ```
  and:[
    <business filters...>,
    { fieldIds:[finishDateField], lt: endTimestamp },          # run's "now" upper bound
    { or:[
      { fieldIds:[finishDateField], gt: startTimestamp },      # strictly newer finish date
      { and:[                                                   # same finish date, newer surveyId
        { fieldIds:[finishDateField], gte: startTimestamp },
        { fieldIds:[surveyIdField],  gt: startSurveyId }
      ]}
    ]}
  ]
  ```
  This `(finishDate, surveyId)` composite watermark is what prevents both gaps and duplicates when
  many records share the same finish-date second.
- **Dedup/overlap:** the `> (finishDate, surveyId)` composite predicate makes the boundary exclusive,
  so records already seen are not re-fetched. Because Keboola loads should still be idempotent, set
  `surveyId` (or a `surveyId`+field composite) as the **table primary key** and use **incremental
  load** on output so any boundary re-read upserts rather than duplicates.

**Keboola state shape (recommendation):** persist per config row in `state.json`, e.g.
`{"last_finish_date_epoch": <int>, "last_survey_id": "<string>"}`. On run start, read state → seed
`startTimestamp`/`startSurveyId` (fall back to a configurable initial start epoch + `-1` survey id on
first run); page oldest→newest updating the in-memory watermark; write the final watermark back to
state at the end. `endTimestamp = now()` at run start bounds the window.

## 7. Data objects to support + field discovery

Root data-source nodes (from `data-queries`):

- **`feedback`** — completed surveys only (statuses COMPLETED, EXCLUDED, AUTOEXCLUDED); a subset of
  invitations. **This is the core object for v1.**
- **`invitations`** — all invitations *including* completed feedback (a superset of feedback).
- **`customer` / `customerSchema`** — customer **profiles** (known vs anonymous; demographics, loyalty).
- **Aggregation-only sources** (`FEED_FILES`, `SSO_EVENT_STATS`, `SURVEY_EXPORT_STATS`,
  `USER_ACTIVITIES`) — not record-level, out of scope for v1.
- **Metadata / field definitions** — see below.

**Field discovery (the answer to "instance-specific schema"):** query the **`fields`** metadata node.
Filters: `ids:[...]` (exact IDs; takes priority, disables pagination), `dataType:<T>`
(`DATE|DATETIME|TIME|EMAIL|ENUM|STRING|UNIT|URL|INT|FLOAT`), `q:"substr"` (name contains), and
`filterable:true` (only fields usable in filter/sort). Returns id, name, dataType, options. Filterable
alt-set types: ENUMERATED, AUTOINDEX_TEXT, DATE, TIME, DATETIME, INTEGER, UNIT (TEXT/COMMENT/FRACTIONAL/
EMAIL are **not** filterable). Best practice: query field metadata **once in its own top-level query**,
not inline per record (inline repeats it per row and inflates cost).

**Keboola mapping (recommendation, to be finalised in the spec):**
- **Config rows, one per data object** (feedback / invitations / profiles) per Keboola convention —
  each row a table with its own incremental state. v1 can ship feedback only and add rows later.
- **Field selection is manual/advanced** (the API has no universal schema): let the user paste/select
  the list of `fieldData(fieldId)` fields per row. A **sync action** can back this with a live
  `fields` metadata call to populate a dropdown / validate IDs (nice-to-have; also doubles as
  test-connection). UI leans advanced, matching the verified note.
- Secrets `client_secret` (and arguably `client_id`) as `#`-prefixed encrypted config values.
- `test-connection` sync action = mint a token + a cheap `compute_cost_only` or 1-field metadata call.

## 8. Feasibility & provisioning verdict

**Live testing + VCR recording are BLOCKED pending a customer's Medallia instance and OAuth
credentials.** This is inherent to Medallia's model and confirmed in the docs:

- **Everything is per-instance/per-customer:** the reporting-instance host, tenant/company name,
  data-center/region, and client_id/secret all vary per customer, and prod/sandbox/dev **do not share
  credentials**. Token host (`<instance>.medallia.com/oauth/<company>/token`), API host
  (`<instance-tenant>.apis.medallia.com`), and sandbox host
  (`<sandbox>-<company>.apis.sbx.<data-center>.medallia.com/data/v0/query`, sometimes with the company
  name doubled) are all distinct and instance-specific. There is **no shared public sandbox**.
- **Credentials are admin-provisioned, not headless:** a Medallia expert / customer program-management
  team must enable API access for the instance and create the OAuth client (issuing client_id, secret,
  and the OAuth endpoint). We cannot self-provision.
- **Sandbox budget is tiny:** Medallia recommends **≤100 API calls/month** for testing — enough to
  record a handful of VCR cassettes, not to iterate against live.

**But the whole component + unit/datadir tests CAN be built now without live access.** The query shape,
pagination, filter tree, auth flow, and incremental logic are fully specified above and mirrored in the
official reference repo, which gives us concrete request/response structures
(`QueryApiResponse`: `{data:{feedback:{totalCount, nodes:[{<alias>:{values:[...]}}]}}, errors:[...]}`).
So: build against **mocked/hand-authored fixtures** now (datadir tests + unit tests with a stub
transport), and defer **VCR recording + live smoke test (Phase 7)** until a customer instance + OAuth
creds are provisioned. Recommend gating the lifecycle tracker's Phase 7 on "creds available" and noting
the blocker in the spec's Open Risks section.

## 9. Open unknowns

None that block writing the spec. Minor items to resolve during implementation or first customer
onboarding (all have safe defaults):

- **Exact per-instance rate/cost/depth limits** — configurable per contract; handle via live
  response headers + backoff rather than a fixed constant. (Not a blocker.)
- **Precise host-derivation rules** — whether `<company>` is doubled in a given API/sandbox host varies
  by instance; the robust design is to make the user supply the full instance/API host (and token host)
  as config rather than deriving them. (Not a blocker — config captures instance host, tenant, id, secret
  as the verified list requires.)
- **Whether the target instance exposes `k_initialfinishdate_epoch_int`** (or an equivalent epoch
  K-field) for the incremental watermark — this is a per-instance setup step the reference repo calls a
  hard dependency. Fallback: filter on a DATETIME field (`e_initialfinishdate`/`e_responsedate`) with an
  ISO/date value if the epoch K-field is absent. Document as a config field for the watermark field ID.
- **`fieldData` typed expansion depth for v1** — start with `{ values }` (string lists, as the reference
  does); add inline-fragment typed extraction (Text Analytics on comments, enumerated expansion) only if
  a customer needs it. (Scope choice, not an unknown.)
- **New vs deprecated rate-limit response headers** — docs note the `X-RateLimit-*` set is being
  superseded; read whichever the live instance returns. (Implementation detail.)

---

### One-file appendix — key reference-repo facts (github.com/medallia/query-api-data-extract)

- `SyncService.performQuery`: builds the exact watermark filter above, POSTs GraphQL, recurses to next
  page while `totalCount >= numRecordsPerRequest`, `MAX_RECORDS_PER_REQUEST=1000`.
- `RecordProcessingService`: persists each node, tracks last `(surveyId, finishDate)`; on cold start
  reads the max `(finishDate DESC, surveyId DESC)` from storage to seed the next query.
- `WebClientConfig`: Spring OAuth2 client-credentials registration named `medallia`.
- `RetryConfig`: fixed backoff, 2 attempts, 2000ms (network-error retry only).
- `application.properties.template`: token-uri, `.apis.medallia.com/data/v0/query`, page size 1000,
  default start epoch + start survey id, business-logic filters, `k_initialfinishdate_epoch_int`.
