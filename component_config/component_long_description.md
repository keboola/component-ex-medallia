# Medallia Experience Cloud Extractor

Extracts data from Medallia Experience Cloud through the Medallia Query API — a single GraphQL endpoint. The component is introspection-driven: it discovers the queryable objects on your instance and extracts any of them (feedback, invitations, customers, programs, and other collections) into tables, or runs a raw GraphQL query you author.

## Features

- **Any object, one table per configuration row.** The data object is picked from a list auto-populated by live schema introspection (or entered manually if introspection is disabled).
- **Metadata-driven field selection.** Fields are chosen by name from the object's catalog, or left empty to take all scalar fields. Different Medallia node shapes (`fieldData`, `data`, plain scalar) are handled automatically.
- **Incremental loading** where the object supports it: a per-configuration watermark on a date field fetches only newer records; the value format (epoch or ISO datetime) is auto-detected. Objects without a suitable date field load in full.
- **Raw GraphQL mode** for advanced use: supply a query returning a single paginated connection and the component handles cursor pagination and flattening.
- **Optional business filters** (a Medallia filter tree, as JSON) merged into each structured query.

## Authentication

Uses OAuth 2.0 with the client-credentials grant. Provide the reporting instance host, the company (tenant) name, the API gateway host, and the OAuth client ID and client secret issued by your Medallia administrator.

## Output

Writes one table per configuration row. The primary key is the object's own record `id` where the API exposes one; for objects without a stable id, a deterministic row hash is used. Incremental runs upsert on the primary key rather than duplicating records.
