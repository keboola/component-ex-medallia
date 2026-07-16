"""Medallia Experience Cloud — Query API extractor.

Thin orchestrator: it loads and validates configuration, builds the separated GraphQL
client, pages feedback records oldest→newest into a single output table, and advances the
per-row incremental watermark in ``state.json`` only after a successful table write.
"""

import csv
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import BaseType, ColumnDefinition, TableDefinition
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement, ValidationResult

from client import (
    MedalliaClient,
    MedalliaClientError,
    MedalliaQueryBuilder,
    MedalliaTokenManager,
    Watermark,
    flatten_node,
    watermark_from_node,
)
from configuration import Configuration, FinishDateFieldType, RowConfiguration

# Sanitizer helpers (module level so they exist even when keboola.vcr is absent in the
# production image). Synthetic date/datetime/epoch values are generated relative to this
# base so they are strictly increasing in node order and their lexicographic order matches
# time. The base is chosen so synthetic watermark values sort AFTER the incremental seeds
# used by the functional tests (which sit before it) and so a first page's rows are never
# dropped as boundary duplicates on replay.
_SANITIZER_EPOCH = datetime(2026, 8, 1, tzinfo=UTC)
_SANITIZER_EPOCH_SECONDS = int(_SANITIZER_EPOCH.timestamp())
# An all-digit value this long is treated as an epoch timestamp (seconds since 1970 are
# 10 digits); shorter all-digit values are treated as ordinary small integers.
_EPOCH_MIN_DIGITS = 9
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# VCR sanitizers — picked up automatically by the keboola.datadirtest scaffolder while
# RECORDING cassettes. keboola.vcr ships only inside keboola.datadirtest (a dev-only
# dependency), so it is absent from the production image (`uv sync --no-dev`); the guard
# keeps the production import clean. DefaultSanitizer already redacts client_id/
# client_secret/access_token/token/password; the extra fields cover this component's own
# credential/tenant parameter names so no real value can leak into a recorded cassette.
try:
    from keboola.vcr import BaseSanitizer, DefaultSanitizer, UrlPatternSanitizer

    class MedalliaResponseBodySanitizer(BaseSanitizer):
        """Overwrite Query API response payloads with deterministic synthetic data.

        Feedback ``e_comment`` (and any free-text verbatim field) can contain arbitrary
        customer PII, so a denylist is unsafe: this REPLACES every value rather than
        redacting known-bad ones, making a recorded cassette clean BY CONSTRUCTION and
        verifiable by allowlist. Two response shapes are handled, everything else passes
        through untouched:

        * ``data.<object>.nodes[]`` feedback rows — the node ``id`` becomes ``RESP-####``
          and every ``fieldData`` alias's ``values`` are overwritten with a synthetic value
          chosen from the alias name (no customer specifics are hard-coded).
        * ``data.fields.nodes[]`` field catalogue (``listFields``) — the real catalogue is
          replaced wholesale with a small generic field set, dropping any company-substring
          field IDs.

        Stateful: node numbering is sequential across every response of one recording, so
        the synthetic finish-date / survey-id watermark stays monotonic and keyset
        pagination still terminates naturally.
        """

        SYNTHETIC_COMMENT = "Synthetic feedback comment."
        _SYNTHETIC_FIELD_CATALOG = [
            {"id": "a_surveyid", "name": "Survey ID", "dataType": "STRING"},
            {"id": "e_creationdate", "name": "Creation Date", "dataType": "DATE"},
            {"id": "e_nps", "name": "NPS Score", "dataType": "INTEGER"},
            {"id": "e_comment", "name": "Comment", "dataType": "STRING"},
            {"id": "a_survey_channel", "name": "Survey Channel", "dataType": "STRING"},
        ]

        def __init__(self) -> None:
            self._node_counter = 0

        def before_record_response(self, response: dict) -> dict:
            body = response.get("body")
            if not isinstance(body, dict) or "string" not in body:
                return response
            raw = body["string"]
            is_bytes = isinstance(raw, bytes)
            text = raw.decode("utf-8", "ignore") if is_bytes else raw
            # Cheap pre-filter: only Query API payloads with node lists are of interest.
            if not isinstance(text, str) or '"data"' not in text or '"nodes"' not in text:
                return response
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):  # fmt: skip
                return response
            if not self._synthesize(payload):
                return response
            new_text = json.dumps(payload)
            body["string"] = new_text.encode("utf-8") if is_bytes else new_text
            return response

        def _synthesize(self, payload: dict) -> bool:
            data = payload.get("data")
            if not isinstance(data, dict):
                return False
            changed = False
            for obj in data.values():
                if not isinstance(obj, dict):
                    continue
                nodes = obj.get("nodes")
                if not isinstance(nodes, list) or not nodes:
                    continue
                if self._is_field_catalog(nodes):
                    obj["nodes"] = [dict(field) for field in self._SYNTHETIC_FIELD_CATALOG]
                    if "totalCount" in obj:
                        obj["totalCount"] = len(obj["nodes"])
                    changed = True
                else:
                    for node in nodes:
                        if isinstance(node, dict):
                            self._synthesize_node(node)
                            changed = True
                    # Scrub the real aggregate feedback volume: the recorded totalCount is the
                    # customer's true matching-record count. Overwrite it with the page node
                    # count (as the field-catalog branch does) — a value consistent with the
                    # self-terminating pagination logic, where a page whose totalCount is below
                    # page_size is the last page. A full page keeps totalCount == page_size, so
                    # ``total_count < page_size`` stays False and pages still stitch on replay.
                    if "totalCount" in obj:
                        obj["totalCount"] = len(nodes)
            return changed

        @staticmethod
        def _is_field_catalog(nodes: list) -> bool:
            head = nodes[0]
            return isinstance(head, dict) and "dataType" in head and "values" not in head

        def _synthesize_node(self, node: dict) -> None:
            self._node_counter += 1
            n = self._node_counter
            for key, value in node.items():
                if isinstance(value, dict) and "values" in value:
                    original = value.get("values") or []
                    value["values"] = self._synthetic_values(key, n, original)
                elif key == "id":
                    node[key] = f"RESP-{n:04d}"

        def _synthetic_values(self, alias: str, n: int, original: list) -> list[str]:
            """Replace a field's values with synthetic ones, preserving ARITY and SHAPE.

            One synthetic value is emitted per real value (arity preserved), so a genuinely
            multi-value field stays multi-value and the ``flatten_node`` join/JSON path is
            exercised. Each value's SHAPE is inferred from the real value it replaces — an
            epoch-integer field stays integer, a date/datetime field stays date-shaped — so
            a watermark field keeps a usable, correctly-typed value. No real value is copied.
            """
            count = max(len(original), 1)
            return [self._synthetic_scalar(alias, n, i, original) for i in range(count)]

        def _synthetic_scalar(self, alias: str, n: int, i: int, original: list) -> str:
            a = alias.lower()
            # Ordinal unique + monotonic in node order; sub-index i separates multiple
            # values within one field so a multi-value column has distinct entries.
            ordinal = n * 100 + i
            if alias == "surveyId" or a.endswith("surveyid"):
                # Zero-padded so lexicographic order tracks node order (keyset secondary sort).
                return f"SURVEY-{n:06d}"
            sample = str(original[i]) if i < len(original) else (str(original[0]) if original else "")
            shape = self._shape_of(sample)
            if shape == "epoch":
                # Monotonic epoch seconds so an epoch watermark advances numerically and
                # sorts after the tests' epoch seeds (which sit before _SANITIZER_EPOCH).
                return str(_SANITIZER_EPOCH_SECONDS + ordinal)
            if shape == "int":
                return str(ordinal)
            if shape == "datetime":
                # Strictly increasing, zero-padded → lexicographic == chronological order.
                return (_SANITIZER_EPOCH + timedelta(seconds=n)).strftime("%Y-%m-%d %H:%M:%S")
            if shape == "date":
                return (_SANITIZER_EPOCH + timedelta(days=n)).strftime("%Y-%m-%d")
            if "comment" in a or "verbatim" in a or "text" in a:
                return self.SYNTHETIC_COMMENT
            if "email" in a:
                return f"user{ordinal:06d}@example.com"
            return f"synthetic-{alias}-{ordinal:06d}"

        @staticmethod
        def _shape_of(sample: str) -> str:
            """Classify a real value's shape so the synthetic replacement matches its type."""
            if _DATETIME_RE.match(sample):
                return "datetime"
            if _DATE_RE.match(sample):
                return "date"
            if sample.isdigit():
                return "epoch" if len(sample) >= _EPOCH_MIN_DIGITS else "int"
            return "text"

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
        # Response-body PII scrub: overwrite every feedback value / field-catalogue entry
        # with synthetic data so a recording can never carry real customer feedback.
        MedalliaResponseBodySanitizer(),
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
        # MEDALLIA_MAX_PAGES (optional): hard page cap, unset in production. Used to bound
        # live-instance blast radius while recording VCR cassettes; also a defensive stop.
        max_pages_env = os.environ.get("MEDALLIA_MAX_PAGES")
        max_pages = int(max_pages_env) if max_pages_env else None
        return MedalliaClient(
            api_host=config.api_host,
            token_manager=token_manager,
            query_builder=query_builder,
            max_pages=max_pages,
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
        # A MedalliaClientError (transport/5xx) would otherwise escape as an exit-2 crash;
        # convert it to a UserException so the button shows a clean failure Alert instead.
        try:
            client.run_metadata_query("query { fields(first: 1) { totalCount } }", compute_cost_only=True)
        except MedalliaClientError as exc:
            raise UserException(f"Connection to Medallia Query API failed: {exc}") from None
        return ValidationResult("Connection to Medallia Query API succeeded.")

    @sync_action("listFields")
    def list_fields(self) -> list[SelectElement]:
        """Query the ``fields`` metadata node to populate/validate field-ID selection."""
        config = self._get_config()
        client = self._build_metadata_client(config)
        # first: 1000 — the fields node otherwise defaults to Medallia's 30-record page,
        # truncating the catalog offered for UI selection. 1000 matches MAX_PAGE_SIZE.
        # As in test_connection, a transport/5xx MedalliaClientError is converted to a
        # UserException so the dropdown surfaces a clean error rather than crashing with exit 2.
        try:
            result = client.run_metadata_query("query { fields(first: 1000) { nodes { id name dataType } } }")
        except MedalliaClientError as exc:
            raise UserException(f"Could not load Medallia fields: {exc}") from None
        nodes = ((result.get("fields") or {}).get("nodes")) or []
        return [SelectElement(value=node["id"], label=self._field_label(node)) for node in nodes if node.get("id")]

    @staticmethod
    def _field_label(node: dict[str, Any]) -> str:
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
