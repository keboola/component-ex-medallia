### Connection

Set the reporting instance host, company (tenant) name, API gateway host, OAuth client ID, and client secret. Use **Test Connection** to validate the credentials before saving.

### Data Extraction

Add one row per object you want to extract. Choose a **Mode**:

- **Guided** — pick a **Data Object** (**Load Objects** lists what your instance exposes, or type a name manually), then the **Fields** to extract (**Load Fields** shows them by name; leave empty to take all scalar fields). For incremental loading, pick an **Incremental Field** (**Load Date Fields**) — the component tracks its value as a watermark and auto-detects the format; objects without a date field load in full. Optionally add a first-run start bound or a JSON **Filters** tree.
- **Raw GraphQL query** — write a query returning exactly one paginated connection (declaring `$first`/`$after` and selecting `pageInfo`); the component injects pagination and flattens the nodes. Use **Validate Query** to check it. Raw mode always performs a full load.

The primary key is the record's own `id` where available, otherwise a deterministic row hash.
