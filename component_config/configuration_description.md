### Connection

Set the reporting instance host, company (tenant) name, API gateway host, OAuth client ID, and client secret. Use **Test Connection** to validate the credentials before saving.

### Data Extraction

Add one row per data object. Select the field IDs to extract (use **Load Fields** to pull them from the instance metadata, or type them manually), the survey-identifier field used as the primary key, and the finish-date field that drives the incremental watermark. Choose incremental or full load, and optionally set a first-run start bound or business filters.
