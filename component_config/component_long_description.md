# Medallia Experience Cloud Extractor

Extracts customer feedback and experience data from Medallia Experience Cloud using the Medallia Query API, a GraphQL interface for feedback and analytics.

## Features

- Retrieves feedback records with a configurable set of output columns, each selected by its Medallia field ID.
- Field selection is metadata-driven: field IDs can be picked from the live instance catalog or entered manually.
- Incremental loading keeps a per-configuration watermark on a finish-date field and fetches only records added since the last run; a full-load mode is also available.
- Supports both epoch-seconds and ISO 8601 datetime finish-date fields.
- Optional business filters (a raw Medallia filter tree) can be merged into each query.

## Authentication

Uses OAuth 2.0 with the client-credentials grant. Provide the reporting instance host, the company (tenant) name, the API gateway host, and the OAuth client ID and client secret issued by your Medallia administrator.

## Output

Writes one table per configuration row, keyed by the survey identifier so incremental runs upsert rather than duplicate records.
