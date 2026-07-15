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
from configuration import Configuration, RowConfiguration

# state.json keys for the composite keyset watermark.
STATE_LAST_FINISH_DATE_EPOCH = "last_finish_date_epoch"
STATE_LAST_SURVEY_ID = "last_survey_id"

# Sentinel survey id used for the first-run lower bound (reference repo convention).
FIRST_RUN_SURVEY_ID = "-1"

# Field IDs whose flattened values are the epoch watermark → INTEGER in the manifest.
_INTEGER_COLUMNS = {"k_initialfinishdate_epoch_int"}


class Component(ComponentBase):
    """Extractor for Medallia Query API feedback records."""

    def __init__(self):
        super().__init__()

    def run(self):
        """Orchestrate a single config-row extract."""
        config = self._get_config()
        row = self._get_row_config()
        client = self._build_client(config, row)

        end_epoch = int(datetime.now(tz=UTC).timestamp())
        lower_bound = self._seed_lower_bound(row)

        table = self._build_table_definition(row)
        final_watermark = self._write_table(table, client, row, lower_bound, end_epoch)
        self.write_manifest(table)

        self._save_state(final_watermark)

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
        )
        return MedalliaClient(
            api_host=config.api_host,
            token_manager=token_manager,
            query_builder=query_builder,
        )

    # -- incremental state -------------------------------------------------------------

    def _seed_lower_bound(self, row: RowConfiguration) -> Watermark | None:
        """Lower bound = stored watermark (incremental) or ``initial_start_epoch`` on first run."""
        if row.incremental:
            state = self._load_state()
            if state is not None:
                return state
        if row.initial_start_epoch is not None:
            return Watermark(finish_date_epoch=row.initial_start_epoch, survey_id=FIRST_RUN_SURVEY_ID)
        logging.info("No stored watermark and no initial_start_epoch; loading full history.")
        return None

    def _load_state(self) -> Watermark | None:
        state = self.get_state_file() or {}
        epoch = state.get(STATE_LAST_FINISH_DATE_EPOCH)
        survey_id = state.get(STATE_LAST_SURVEY_ID)
        if epoch is None or survey_id is None:
            return None
        logging.info("Resuming from stored watermark (finish_date_epoch=%s).", epoch)
        return Watermark(finish_date_epoch=int(epoch), survey_id=str(survey_id))

    def _save_state(self, watermark: Watermark | None) -> None:
        if watermark is None:
            logging.info("No records processed and no prior watermark; leaving state unchanged.")
            return
        self.write_state_file(
            {
                STATE_LAST_FINISH_DATE_EPOCH: watermark.finish_date_epoch,
                STATE_LAST_SURVEY_ID: watermark.survey_id,
            }
        )
        logging.info("Persisted watermark (finish_date_epoch=%s).", watermark.finish_date_epoch)

    # -- output ------------------------------------------------------------------------

    def _build_table_definition(self, row: RowConfiguration) -> TableDefinition:
        columns = row.output_columns
        schema = {name: self._column_definition(name) for name in columns}
        return self.create_out_table_definition(
            f"{row.data_object.value}.csv",
            primary_key=["surveyId"],
            incremental=row.incremental,
            schema=schema,
            has_header=True,
        )

    @staticmethod
    def _column_definition(name: str) -> ColumnDefinition:
        if name in _INTEGER_COLUMNS:
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
        end_epoch: int,
    ) -> Watermark | None:
        """Stream feedback nodes into the output CSV, returning the advanced watermark."""
        columns = row.output_columns
        watermark = lower_bound
        record_count = 0
        with open(table.full_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=columns)
            writer.writeheader()
            for node in client.fetch_feedback(lower_bound, end_epoch, row.page_size):
                writer.writerow(flatten_node(node, columns))
                watermark = watermark_from_node(node, row.finish_date_field_id, watermark)
                record_count += 1
        logging.info("Wrote %s record(s) to %s.", record_count, table.name)
        return watermark

    # -- sync actions ------------------------------------------------------------------

    @sync_action("testConnection")
    def test_connection(self) -> ValidationResult:
        """Mint a token and run a cheap metadata query to validate connectivity."""
        config = self._get_config()
        client = self._build_metadata_client(config)
        client.run_metadata_query("query { fields(first: 1) { totalCount } }")
        return ValidationResult("Connection to Medallia Query API succeeded.")

    @sync_action("listFields")
    def list_fields(self) -> list[SelectElement]:
        """Query the ``fields`` metadata node to populate/validate field-ID selection."""
        config = self._get_config()
        client = self._build_metadata_client(config)
        result = client.run_metadata_query("query { fields { nodes { id name dataType } } }")
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
