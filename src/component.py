"""Medallia Experience Cloud — Query API extractor.

Thin orchestrator: it loads and validates configuration, builds the separated GraphQL
client, pages feedback records oldest→newest into a single output table, and advances the
per-row incremental watermark in ``state.json`` only after a successful table write.
"""

import csv
import logging
from datetime import UTC, datetime

from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import BaseType, ColumnDefinition, TableDefinition
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement, ValidationResult

from client import (
    MedalliaClient,
    MedalliaQueryBuilder,
    MedalliaTokenManager,
    Watermark,
    flatten_node,
    watermark_from_node,
)
from configuration import Configuration, FinishDateFieldType, RowConfiguration

# VCR sanitizers — picked up automatically by the keboola.datadirtest scaffolder while
# RECORDING cassettes. keboola.vcr ships only inside keboola.datadirtest (a dev-only
# dependency), so it is absent from the production image (`uv sync --no-dev`); the guard
# keeps the production import clean. DefaultSanitizer already redacts client_id/
# client_secret/access_token/token/password; the extra fields cover this component's own
# credential/tenant parameter names so no real value can leak into a recorded cassette.
try:
    from keboola.vcr import DefaultSanitizer, UrlPatternSanitizer

    VCR_SANITIZERS = [
        DefaultSanitizer(
            additional_sensitive_fields=[
                "company",
                "company_name",
                "username",
                "#password",
                "#client_secret",
                "client_secret",
            ]
        ),
        # The tenant host and <companyName> path segment are not secrets, but they identify
        # the customer instance and appear in request URIs (which DefaultSanitizer leaves
        # intact). These GENERIC patterns rewrite any Medallia host / oauth company segment
        # to fixed placeholders — no customer-specific value is hard-coded here. Applied on
        # both record and replay (before_record_request), so a placeholder-host replay config
        # still matches the recorded (rewritten) URIs. Order matters: the more specific
        # ``apis.medallia.com`` gateway host is rewritten before the broader instance host.
        UrlPatternSanitizer(
            patterns=[
                (r"https://[A-Za-z0-9._-]+\.apis\.medallia\.com", "https://api-host.redacted"),
                (r"https://[A-Za-z0-9._-]+\.medallia\.com", "https://instance-host.redacted"),
                (r"/oauth/[^/]+/token", "/oauth/company/token"),
            ]
        ),
    ]
except ImportError:  # pragma: no cover - production image has no dev dependencies.
    VCR_SANITIZERS = []

# state.json keys for the composite keyset watermark.
STATE_LAST_FINISH_DATE_EPOCH = "last_finish_date_epoch"
STATE_LAST_SURVEY_ID = "last_survey_id"

# Sentinel survey id used for the first-run lower bound (reference repo convention).
FIRST_RUN_SURVEY_ID = "-1"


class Component(ComponentBase):
    """Extractor for Medallia Query API feedback records."""

    def __init__(self):
        super().__init__()

    def run(self):
        """Orchestrate a single config-row extract."""
        config = self._get_config()
        row = self._get_row_config()
        client = self._build_client(config, row)

        end_bound = self._end_bound(row)
        lower_bound = self._seed_lower_bound(row)

        table = self._build_table_definition(row)
        final_watermark = self._write_table(table, client, row, lower_bound, end_bound)
        self.write_manifest(table)

        self._save_state(final_watermark)

    @staticmethod
    def _end_bound(row: RowConfiguration) -> int | str:
        """Upper watermark bound (this run's ``now``) in the field's native format."""
        now = datetime.now(tz=UTC)
        if row.finish_date_field_type == FinishDateFieldType.datetime:
            # Verified at live recording: Medallia's date/datetime range filter rejects a
            # full ISO-8601 timestamp with a 'Z' suffix ("Invalid date expression:
            # 2026-07-15T17:03:01Z"). A date-only YYYY-MM-DD literal is accepted. Using the
            # day boundary means today's records are picked up on the next run (bounded lag).
            return now.strftime("%Y-%m-%d")
        return int(now.timestamp())

    # -- configuration -----------------------------------------------------------------

    def _get_config(self) -> Configuration:
        return Configuration(**self.configuration.parameters)

    def _get_row_config(self) -> RowConfiguration:
        return RowConfiguration(**self.configuration.parameters)

    # -- client ------------------------------------------------------------------------

    @staticmethod
    def _build_client(config: Configuration, row: RowConfiguration) -> MedalliaClient:
        token_manager = MedalliaTokenManager(
            instance_host=config.instance_host,
            company_name=config.company_name,
            client_id=config.client_id,
            client_secret=config.client_secret,
        )
        query_builder = MedalliaQueryBuilder(
            data_object=row.data_object.value,
            survey_id_field_id=row.survey_id_field_id,
            finish_date_field_id=row.finish_date_field_id,
            fields=row.fields,
            business_filters=row.filters,
            finish_date_field_type=row.finish_date_field_type.value,
        )
        return MedalliaClient(
            api_host=config.api_host,
            token_manager=token_manager,
            query_builder=query_builder,
        )

    # -- incremental state -------------------------------------------------------------

    def _seed_lower_bound(self, row: RowConfiguration) -> Watermark | None:
        """Lower bound = stored watermark (incremental) or the first-run seed value."""
        if row.incremental:
            state = self._load_state(row)
            if state is not None:
                return state
        if row.initial_start is not None:
            return Watermark(finish_date_value=row.initial_start, survey_id=FIRST_RUN_SURVEY_ID)
        logging.info("No stored watermark and no first-run start value; loading full history.")
        return None

    def _load_state(self, row: RowConfiguration) -> Watermark | None:
        state = self.get_state_file() or {}
        raw_finish = state.get(STATE_LAST_FINISH_DATE_EPOCH)
        survey_id = state.get(STATE_LAST_SURVEY_ID)
        if raw_finish is None or survey_id is None:
            return None
        # Keep the value in the watermark field's native format (int epoch / ISO string);
        # an epoch field stored as a numeric string is normalised back to int.
        if row.finish_date_field_type == FinishDateFieldType.datetime:
            finish_value: int | str = str(raw_finish)
        else:
            finish_value = int(raw_finish)
        logging.info("Resuming from stored watermark (finish=%s).", finish_value)
        return Watermark(finish_date_value=finish_value, survey_id=str(survey_id))

    def _save_state(self, watermark: Watermark | None) -> None:
        if watermark is None:
            logging.info("No records processed and no prior watermark; leaving state unchanged.")
            return
        self.write_state_file(
            {
                STATE_LAST_FINISH_DATE_EPOCH: watermark.finish_date_value,
                STATE_LAST_SURVEY_ID: watermark.survey_id,
            }
        )
        logging.info("Persisted watermark (finish=%s).", watermark.finish_date_value)

    # -- output ------------------------------------------------------------------------

    def _build_table_definition(self, row: RowConfiguration) -> TableDefinition:
        columns = row.output_columns
        schema = {name: self._column_definition(name, row) for name in columns}
        # PK is the configured survey field alias (surveyId). The node ``id`` is also
        # captured as a column for a format-independent record identity.
        # TODO(verify-at-recording): confirm against a live feedback response whether the
        # node ``id`` (or surveyId) is the truly-unique record key, and promote it to the
        # primary key if surveyId is not unique for this instance's schema.
        return self.create_out_table_definition(
            f"{row.data_object.value}.csv",
            primary_key=["surveyId"],
            incremental=row.incremental,
            schema=schema,
            has_header=True,
        )

    @staticmethod
    def _column_definition(name: str, row: RowConfiguration) -> ColumnDefinition:
        # Type the finish-date watermark column by its declared format: epoch → INTEGER,
        # datetime/ISO → STRING (forcing INTEGER on a date field would corrupt the value).
        if name == row.finish_date_field_id and row.finish_date_field_type == FinishDateFieldType.epoch:
            data_type = BaseType.integer()
        else:
            data_type = BaseType.string()
        return ColumnDefinition(
            data_types=data_type,
            primary_key=(name == "surveyId"),
            nullable=(name != "surveyId"),
        )

    def _write_table(
        self,
        table: TableDefinition,
        client: MedalliaClient,
        row: RowConfiguration,
        lower_bound: Watermark | None,
        end_bound: int | str,
    ) -> Watermark | None:
        """Stream feedback nodes into the output CSV, returning the advanced watermark."""
        columns = row.output_columns
        watermark = lower_bound
        record_count = 0
        field_type = row.finish_date_field_type.value
        with open(table.full_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=columns)
            writer.writeheader()
            for node in client.fetch_feedback(lower_bound, end_bound, row.page_size):
                writer.writerow(flatten_node(node, columns))
                watermark = watermark_from_node(node, row.finish_date_field_id, watermark, field_type=field_type)
                record_count += 1
        logging.info("Wrote %s record(s) to %s.", record_count, table.name)
        return watermark

    # -- sync actions ------------------------------------------------------------------

    @sync_action("testConnection")
    def test_connection(self) -> ValidationResult:
        """Mint a token and run a cheap metadata query to validate connectivity."""
        config = self._get_config()
        client = self._build_metadata_client(config)
        # compute_cost_only validates auth + query at the gateway without consuming quota.
        client.run_metadata_query("query { fields(first: 1) { totalCount } }", compute_cost_only=True)
        return ValidationResult("Connection to Medallia Query API succeeded.")

    @sync_action("listFields")
    def list_fields(self) -> list[SelectElement]:
        """Query the ``fields`` metadata node to populate/validate field-ID selection."""
        config = self._get_config()
        client = self._build_metadata_client(config)
        # first: 1000 — the fields node otherwise defaults to Medallia's 30-record page,
        # truncating the catalog offered for UI selection. 1000 matches MAX_PAGE_SIZE.
        result = client.run_metadata_query("query { fields(first: 1000) { nodes { id name dataType } } }")
        nodes = ((result.get("fields") or {}).get("nodes")) or []
        return [SelectElement(value=node["id"], label=self._field_label(node)) for node in nodes if node.get("id")]

    @staticmethod
    def _field_label(node: dict) -> str:
        name = node.get("name") or node["id"]
        data_type = node.get("dataType")
        return f"{name} ({data_type})" if data_type else name

    @staticmethod
    def _build_metadata_client(config: Configuration) -> MedalliaClient:
        """Client for root-level sync actions (no row context needed)."""
        token_manager = MedalliaTokenManager(
            instance_host=config.instance_host,
            company_name=config.company_name,
            client_id=config.client_id,
            client_secret=config.client_secret,
        )
        # Metadata queries do not use the feedback query builder, but the client requires one.
        query_builder = MedalliaQueryBuilder(
            data_object="fields",
            survey_id_field_id="surveyId",
            finish_date_field_id="finishDate",
            fields=[],
        )
        return MedalliaClient(api_host=config.api_host, token_manager=token_manager, query_builder=query_builder)


"""
        Main entrypoint
"""
if __name__ == "__main__":
    try:
        comp = Component()
        # this triggers the run method by default and is controlled by the configuration.action parameter
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
