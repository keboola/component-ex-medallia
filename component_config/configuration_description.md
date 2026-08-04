### Connection

Enter your Medallia API credentials — instance address, company (account) name, API address, client ID, and client secret. Use **Test Connection** to check them before saving.

### Data Extraction

Add one row per object you want to load. Choose a **Mode**:

- **Guided** — pick a **Data Object** (**Load Objects** lists what your instance exposes, or type a name), then the **Fields** to load (**Load Fields** shows them by name; leave empty to take everything). To load only new records, pick a **Date Field** (**Load Date Fields**) — the component remembers the latest value it has loaded and continues from there next time; objects without a date field load in full. You can also set a Start/End date or a JSON **Filters** clause.
- **Raw GraphQL query** — write a query that returns one paginated result; the component handles paging for you. Use **Validate Query** to check it. Raw mode always loads everything.

Rows are matched by the record's own `id` where available, otherwise by a stable row fingerprint.
