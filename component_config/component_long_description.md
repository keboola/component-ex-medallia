# Medallia Experience Cloud Extractor

Pulls data from Medallia Experience Cloud into Keboola through Medallia's Query API. Add one row per thing you want to load — feedback, invitations, customers, programs, and other collections your instance exposes — or write your own query for advanced cases.

## Features

- **Any object, one table per row.** Pick what to extract from a list loaded from your instance (or type its name), and it lands in its own table.
- **Pick your fields.** Choose the fields to load by name, or leave it empty to take everything available. Each field becomes a column.
- **Load only new records.** For an object with a date field, pick it and the component remembers where it left off, so each run brings in only newer records. Objects without a date field load in full.
- **Date range.** Limit a load to a Start/End date window.
- **Optional filters.** Narrow a load further with a simple JSON filter.
- **Raw query mode.** For advanced use, write your own GraphQL query and the component handles paging through the results.

## Authentication

Sign in with the API credentials from your Medallia administrator: your instance address, company (account) name, API address, and a client ID and secret.

## Output

One table per row. Rows are matched by the record's own id where Medallia provides one, so re-runs update existing rows instead of duplicating them; for records without an id, a stable row fingerprint is used instead.
