# Medallia Experience Cloud Extractor (`keboola.ex-medallia`)

Extracts data from **Medallia Experience Cloud** through the Medallia **Query API** — a single
GraphQL endpoint. The component is introspection-driven: it discovers the queryable objects on
your instance and extracts any of them (feedback, invitations, customers, programs, and other
collections) into tables, or runs a raw GraphQL query you author.

## Functionality

- **Any object, one table per row.** Each configuration row extracts one Medallia object. The
  object is picked from a list populated by live schema introspection, or entered manually.
- **Metadata-driven field selection.** Fields are chosen by name from the object's catalog, or
  left empty to take all scalar fields. The different Medallia node shapes (`fieldData`, `data`,
  plain scalar) are flattened automatically.
- **A date window you control.** Start Date and End Date bound every run on a date/int field of
  your choice (epoch or ISO format auto-detected). A rolling Start Date such as `5 days ago`
  re-reads the last five days each run, so records that reach Medallia late are still picked up;
  rows are matched on the primary key, so re-reading never duplicates. Objects without a suitable
  date field load in full.
- **Raw GraphQL mode** for advanced use: supply a query returning a single paginated connection
  and the component handles cursor pagination and node flattening.
- **Optional business filters** — a Medallia filter tree (as JSON) merged into each structured
  query.

## Prerequisites

Obtain an **OAuth 2.0 client-credentials** application from your Medallia administrator with
access to the Query API. You will need:

- the **reporting instance host** (OAuth token endpoint),
- the **company / tenant name** (`<companyName>` OAuth path segment),
- the **API gateway host** (`*.apis.medallia.com`),
- the OAuth **client ID** and **client secret**.

## Configuration

### Connection (config level)

| Parameter          | Key               | Description                                              |
|--------------------|-------------------|---------------------------------------------------------|
| Instance Host      | `instance_host`   | Reporting instance host for the OAuth token endpoint.   |
| Company Name       | `company_name`    | The `<companyName>` OAuth path segment (tenant).        |
| API Host           | `api_host`        | The `*.apis.medallia.com` gateway host for the Query API. |
| Client ID          | `client_id`       | OAuth client id (non-secret).                           |
| Client Secret      | `#client_secret`  | OAuth client secret (encrypted).                        |

Use **Test Connection** to validate the credentials before saving.

### Data extraction (one row per object)

Each row runs in one of two **modes**.

**Guided (`structured`)**

| Parameter          | Key                   | Description                                                                                  |
|--------------------|-----------------------|----------------------------------------------------------------------------------------------|
| Data Object        | `data_object`         | Medallia connection to extract (**Load Objects** lists what your instance exposes).          |
| Fields             | `fields`              | Field IDs to extract (**Load Fields**); leave empty to take all scalar fields.               |
| Field picker scope | `only_program_fields` | Picker-only: `true` offers only fields used on a survey program; `false` (default) offers the whole catalog. |
| Load Type          | `load_type`           | `incremental_load` (default) or `full_load`.                                                 |
| Date Field         | `incremental_field`   | Date/int field the dates below apply to (**Load Date Fields**). Prefer an *arrival* field such as `e_initialfinishdate` — see [Choosing the date field](#choosing-the-date-field). |
| Start Date         | `initial_start`       | Lower bound, applied on **every** run: ISO date, epoch seconds, or a relative expression (`5 days ago`). Empty = no start limit. |
| End Date           | `end_date`            | Upper bound (same formats); empty = now.                                                      |
| Filters            | `filters`             | Optional Medallia filter tree as a JSON string.                                              |
| Output Table       | `output_table`        | Optional; defaults to the data object name.                                                  |
| Page Size          | `page_size`           | Records per API page; clamped to **25–500** (default **100**).                                |

**Raw GraphQL query (`raw`)**

| Parameter    | Key            | Description                                                                                       |
|--------------|----------------|--------------------------------------------------------------------------------------------------|
| Query        | `raw_query`    | GraphQL query returning exactly one paginated connection (declare `$first`/`$after`, select `pageInfo`). |
| Output Table | `output_table` | **Required** — there is no object name to derive it from.                                         |
| Page Size    | `page_size`    | Records per API page; clamped to **25–500** (default **100**).                                    |

Raw mode always performs a full load. Use **Validate & Preview Query** to check it.

### Choosing the date field

Pick a field that reflects when a record became **available** in Medallia, not when it was
originally created. Medallia
[recommends `e_initialfinishdate`](https://docs.medallia.com/en/medallia-experience-cloud/integration/apis/query-api/data-queries/pull-incremental-data)
and rules out `e_lastupdated` as too volatile.

A creation date such as `e_creationdate` is the trap. `feedback` is a view over records whose
`e_status` is `COMPLETED` / `EXCLUDED` / `AUTOEXCLUDED`, so a survey created on the 1st and
answered on the 20th only enters that view on the 20th — still stamped the 1st. Your Start Date
therefore has to reach back past the *whole answering delay*, not just past the last run, or that
record is never loaded. An arrival-ordered field has no such gap, so a much shorter window works.

Either way the Start Date is your safety margin: Medallia advises allowing
[~10 hours for a period to settle](https://docs.medallia.com/en/medallia-experience-cloud/integration/apis/query-api/data-queries/filter-by-date-ranges),
so treat that as the floor and size the rest from your own data.

### Sync actions

| Button                   | Action           | Purpose                                                      |
|--------------------------|------------------|-------------------------------------------------------------|
| Test Connection          | `testConnection` | Validate the OAuth credentials.                             |
| Load Objects             | `listObjects`    | List the queryable objects on the instance.                |
| Load Fields              | `listFields`     | List a selected object's fields as `Name (api_id)`.        |
| Load Date Fields         | `listDateFields` | Date/int fields for the date window, as `Name (api_id)`.   |
| Validate & Preview Query | `validateQuery`  | Validate a raw GraphQL query.                              |

## Output

Writes **one table per configuration row**. The primary key is the object's own record `id`
where the API exposes one; for objects without a stable id, a deterministic `_row_hash` is used.
Incremental runs upsert on the primary key rather than duplicating records. The component keeps
**no state between runs** — every run's date range comes from the Start Date and End Date as they
are configured at that moment, so what the configuration says is what gets loaded. A correct run
that returns no rows is treated as a success.

## Development

The local data folder path can be customized by replacing the `CUSTOM_FOLDER` placeholder in
`docker-compose.yml`:

```yaml
volumes:
  - ./:/code
  - ./CUSTOM_FOLDER:/data
```

Clone the repository and run the component locally:

```bash
git clone https://github.com/keboola/component-ex-medallia.git
cd component-ex-medallia
docker-compose build
docker-compose run --rm dev
```

Run the test suite (pytest) and lint/type checks (ruff + ty):

```bash
docker-compose run --rm test
```

The project targets Python 3.14 and uses `uv` for dependency management.

## Integration

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer documentation](https://developers.keboola.com/extend/component/deployment/).
