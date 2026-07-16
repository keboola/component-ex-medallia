"""Medallia Experience Cloud — generic Query API extractor.

Thin orchestrator (spec §9): it loads and validates configuration, builds the separated
GraphQL client, and — depending on the row ``mode`` — either extracts one introspection-
resolved object (structured mode) or drives a user-authored Relay query (raw mode). Every
object is paged via ``pageInfo.hasNextPage`` with ``first``/``after`` as GraphQL variables;
structured incremental rows persist a single-scalar watermark in ``state.json`` only after a
successful table write. There is a SINGLE generic code path — no feedback-specific branch.
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
from keboola.component.sync_actions import MessageType, SelectElement, ValidationResult

from client import (
    SHAPE_DATA,
    SHAPE_FIELDDATA,
    SHAPE_SCALAR,
    STATIC_EXTRACTABLE_OBJECTS,
    STATIC_OBJECT_SHAPES,
    GenericQueryBuilder,
    MedalliaClient,
    MedalliaClientError,
    MedalliaTokenManager,
    ObjectShape,
    advance_watermark,
    flatten_node,
    list_extractable_objects,
    resolve_object_shape,
    row_hash,
)
from configuration import Configuration, Mode, RowConfiguration

# Sanitizer helpers (module level so they exist even when keboola.vcr is absent in the
# production image). Synthetic date/datetime/epoch values are generated relative to this base
# so they are strictly increasing in node order and their lexicographic order matches time.
_SANITIZER_EPOCH = datetime(2026, 8, 1, tzinfo=UTC)
_SANITIZER_EPOCH_SECONDS = int(_SANITIZER_EPOCH.timestamp())
# An all-digit value this long is treated as an epoch timestamp (seconds since 1970 are 10
# digits); shorter all-digit values are treated as ordinary small integers.
_EPOCH_MIN_DIGITS = 9
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# VCR sanitizers — picked up automatically by the keboola.datadirtest scaffolder while
# RECORDING cassettes. keboola.vcr ships only inside keboola.datadirtest (a dev-only
# dependency), so it is absent from the production image (`uv sync --no-dev`); the guard keeps
# the production import clean. NOTE (Phase 5.3): the response-body sanitizer below still targets
# the v1 fieldData shape and is EXTENDED for the generic shapes (data(fieldId){value,values},
# bare scalars, pageInfo.endCursor, id-less nodes) in the dedicated recording phase.
try:
    from keboola.vcr import BaseSanitizer, DefaultSanitizer, UrlPatternSanitizer

    class MedalliaResponseBodySanitizer(BaseSanitizer):
        """Overwrite Query API response payloads with deterministic synthetic data.

        Free-text verbatim fields can contain arbitrary customer PII, so a denylist is unsafe:
        this REPLACES every value rather than redacting known-bad ones, making a recorded
        cassette clean BY CONSTRUCTION and verifiable by allowlist.
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
            """Replace a field's values with synthetic ones, preserving ARITY and SHAPE."""
            count = max(len(original), 1)
            return [self._synthetic_scalar(alias, n, i, original) for i in range(count)]

        def _synthetic_scalar(self, alias: str, n: int, i: int, original: list) -> str:
            a = alias.lower()
            ordinal = n * 100 + i
            if alias == "surveyId" or a.endswith("surveyid"):
                return f"SURVEY-{n:06d}"
            sample = str(original[i]) if i < len(original) else (str(original[0]) if original else "")
            shape = self._shape_of(sample)
            if shape == "epoch":
                return str(_SANITIZER_EPOCH_SECONDS + ordinal)
            if shape == "int":
                return str(ordinal)
            if shape == "datetime":
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
        UrlPatternSanitizer(
            patterns=[
                (r"https://[A-Za-z0-9._-]+\.apis\.medallia\.com", "https://api-host.redacted"),
                (r"https://[A-Za-z0-9._-]+\.medallia\.com", "https://instance-host.redacted"),
                (r"/oauth/[^/]+/token", "/oauth/company/token"),
            ]
        ),
        MedalliaResponseBodySanitizer(),
    ]
except ImportError:  # pragma: no cover - production image has no dev dependencies.
    VCR_SANITIZERS = []

# state.json key for the single-scalar incremental watermark (spec §8).
STATE_LAST_INCREMENTAL_VALUE = "last_incremental_value"

# Global field-catalogue query (shape-a metadata source; spec §6.3).
_FIELDS_CATALOG_QUERY = "query { fields(first: 1000) { nodes { id name dataType sortable multivalued } } }"


class Component(ComponentBase):
    """Generic extractor for the Medallia Query API."""

    def __init__(self):
        super().__init__()

    # -- orchestration -----------------------------------------------------------------

    def run(self):
        """Orchestrate a single config-row extract (structured or raw mode)."""
        config = self._get_config()
        row = self._get_row()
        client = self._build_client(config)
        if row.mode == Mode.raw:
            self._run_raw(client, row)
        else:
            self._run_structured(client, row)

    def _run_structured(self, client: MedalliaClient, row: RowConfiguration) -> None:
        if not row.data_object:
            raise UserException("No data object selected. Choose a Medallia object (structured mode).")
        shape = self._resolve_object(client, row.data_object)
        field_meta = self._field_metadata(client, shape)
        do_incremental, is_int = self._incremental_plan(row, shape, field_meta)

        lower_bound = self._seed_lower_bound(row) if do_incremental else None
        upper_bound = self._upper_bound(is_int) if do_incremental else None

        scalar_fields = list(shape.scalar_fields) if shape.shape in (SHAPE_DATA, SHAPE_SCALAR) else []
        builder = GenericQueryBuilder(
            object_name=row.data_object,
            node_shape=shape.shape,
            selected_fields=row.fields,
            scalar_fields=scalar_fields,
            incremental_field=row.incremental_field or None,
            filter_tree=row.parsed_filters(),
            supports_filter=shape.supports_filter,
            supports_order=shape.supports_order,
            has_id=shape.has_id,
        )
        query = builder.build_query(lower_bound, upper_bound)
        columns = builder.selection_columns()
        fieldnames = [*columns, "_row_hash"] if not shape.has_id else list(columns)

        table = self._build_table_definition(f"{row.data_object}.csv", fieldnames, shape, field_meta, do_incremental)
        nodes = client.fetch_object(row.data_object, query, row.page_size)
        max_watermark = self._write_rows(
            table.full_path, fieldnames, nodes, shape.has_id, row.incremental_field, is_int, lower_bound
        )
        self.write_manifest(table)
        self._save_incremental_state(row, max_watermark)

    def _run_raw(self, client: MedalliaClient, row: RowConfiguration) -> None:
        if not row.raw_query.strip():
            raise UserException("Raw mode requires a GraphQL query in 'raw_query'.")
        if not row.output_table.strip():
            raise UserException("Raw mode requires an 'output_table' name.")
        self._validate_raw_static(row.raw_query)
        # Cost pre-flight at run start (compile + price without consuming quota). $first/$after
        # travel as variables so the priced query is identical to the one that will run.
        client.run_metadata_query(
            row.raw_query, compute_cost_only=True, variables={"first": row.page_size, "after": None}
        )

        table_name = f"{row.output_table.strip()}.csv"
        nodes = client.paginate(row.raw_query, row.page_size, self._raw_connection)
        self._write_raw_table(table_name, nodes)

    # -- configuration -----------------------------------------------------------------

    def _get_config(self) -> Configuration:
        return Configuration(**self.configuration.parameters)

    def _get_row(self) -> RowConfiguration:
        return RowConfiguration(**self.configuration.parameters)

    def _build_client(self, config: Configuration) -> MedalliaClient:
        token_manager = MedalliaTokenManager(
            instance_host=config.instance_host,
            company_name=config.company_name,
            client_id=config.client_id,
            client_secret=config.client_secret,
        )
        # MEDALLIA_MAX_PAGES (optional): hard page cap, unset in production. Bounds live-instance
        # blast radius while recording VCR cassettes; also a defensive stop.
        max_pages_env = os.environ.get("MEDALLIA_MAX_PAGES")
        max_pages = int(max_pages_env) if max_pages_env else None
        return MedalliaClient(api_host=config.api_host, token_manager=token_manager, max_pages=max_pages)

    # -- object / shape resolution -----------------------------------------------------

    def _resolve_object(self, client: MedalliaClient, object_name: str) -> ObjectShape:
        """Resolve an object's shape via introspection, falling back to the static map.

        Some tenants disable ``__schema`` introspection; that surfaces as a GraphQL error
        (UserException) or a transport failure. In that case a known object is served from the
        static shape map so extraction still works; an unknown object is a user error.
        """
        introspection = None
        try:
            introspection = client.run_introspection()
        except (MedalliaClientError, UserException) as exc:
            logging.warning("Introspection unavailable (%s); using the static shape for '%s'.", exc, object_name)
        if introspection:
            shape = resolve_object_shape(object_name, introspection)
            if shape is not None:
                return shape
            logging.warning("Object '%s' not found in the introspected schema; using the static shape.", object_name)
        static = STATIC_OBJECT_SHAPES.get(object_name)
        if static is None:
            raise UserException(
                f"Object '{object_name}' could not be resolved via introspection and is not a known "
                "Medallia object. Check the object name."
            )
        return static

    # -- incremental planning + state --------------------------------------------------

    def _incremental_plan(self, row: RowConfiguration, shape: ObjectShape, field_meta: dict) -> tuple[bool, bool]:
        """Decide whether the row loads incrementally, and whether the cursor is numeric."""
        is_int = self._incremental_is_int(shape, row.incremental_field, field_meta)
        if not row.incremental:
            return False, is_int
        if not row.incremental_field:
            logging.warning("%s: incremental load requested but no incremental field set; loading full.", shape.name)
            return False, is_int
        if not (shape.supports_filter and shape.supports_order):
            logging.warning("%s has no incremental cursor (no filter/orderBy support); loading full.", shape.name)
            return False, is_int
        return True, is_int

    @staticmethod
    def _incremental_is_int(shape: ObjectShape, field_id: str, field_meta: dict) -> bool:
        """Auto-detect the watermark value format from metadata: INT → numeric, else ISO string."""
        if not field_id:
            return False
        meta = field_meta.get(field_id) or {}
        data_type = meta.get("dataType")
        if data_type:
            return str(data_type).upper() in {"INT", "INTEGER"}
        return shape.scalar_fields.get(field_id) == "Int"

    def _seed_lower_bound(self, row: RowConfiguration) -> int | str | None:
        """Lower bound = stored watermark → first-run ``initial_start`` → none (full history)."""
        state = self.get_state_file() or {}
        stored = state.get(STATE_LAST_INCREMENTAL_VALUE)
        if stored is not None and str(stored) != "":
            logging.info("Resuming from stored watermark (%s).", stored)
            return stored
        if row.initial_start.strip():
            logging.info("First run: seeding lower bound from initial_start.")
            return row.initial_start.strip()
        logging.info("No stored watermark and no initial_start; loading full history.")
        return None

    @staticmethod
    def _upper_bound(is_int: bool) -> int | str:
        """Run-start upper bound in the field's format (INT seconds, else date-only string).

        A DATE/DATETIME field is bounded with ``YYYY-MM-DD`` (Medallia rejects a ``…Z``
        timestamp — a v1 live-verified fact); today's records arrive on the next run.
        """
        now = datetime.now(tz=UTC)
        return int(now.timestamp()) if is_int else now.strftime("%Y-%m-%d")

    def _save_incremental_state(self, row: RowConfiguration, watermark: int | str | None) -> None:
        if row.incremental_field and watermark is not None:
            self.write_state_file({STATE_LAST_INCREMENTAL_VALUE: watermark})
            logging.info("Persisted watermark (last_incremental_value=%s).", watermark)
        else:
            logging.info("No watermark to persist; leaving state unchanged.")

    # -- field metadata ----------------------------------------------------------------

    def _field_metadata(self, client: MedalliaClient, shape: ObjectShape) -> dict[str, dict[str, Any]]:
        """Per-field metadata for typing + watermark detection, routed by node shape (spec §6.3).

        Shape (a) reads the global ``fields`` catalogue (id → dataType/sortable/multivalued).
        Shapes (b)/(c) derive types from the introspected scalar node fields (GraphQL scalar
        type); when no catalogue is available a column defaults to STRING.
        """
        if shape.shape == SHAPE_FIELDDATA:
            try:
                data = client.run_metadata_query(_FIELDS_CATALOG_QUERY)
            except MedalliaClientError as exc:
                logging.warning("Could not load field metadata (%s); columns default to STRING.", exc)
                return {}
            nodes = ((data.get("fields") or {}).get("nodes")) or []
            return {node["id"]: node for node in nodes if node.get("id")}
        return {name: {"scalar_type": scalar_type} for name, scalar_type in shape.scalar_fields.items()}

    # -- output ------------------------------------------------------------------------

    def _build_table_definition(
        self,
        table_name: str,
        fieldnames: list[str],
        shape: ObjectShape,
        field_meta: dict,
        incremental: bool,
    ) -> TableDefinition:
        primary_key = ["id"] if shape.has_id else ["_row_hash"]
        schema = {name: self._column_definition(name, shape, field_meta) for name in fieldnames}
        return self.create_out_table_definition(
            table_name,
            primary_key=primary_key,
            incremental=incremental,
            schema=schema,
            has_header=True,
        )

    def _column_definition(self, name: str, shape: ObjectShape, field_meta: dict) -> ColumnDefinition:
        if name == "id":
            return ColumnDefinition(data_types=BaseType.string(), primary_key=shape.has_id, nullable=not shape.has_id)
        if name == "_row_hash":
            return ColumnDefinition(data_types=BaseType.string(), primary_key=True, nullable=False)
        return ColumnDefinition(data_types=self._base_type(name, field_meta), primary_key=False, nullable=True)

    @staticmethod
    def _base_type(name: str, field_meta: dict) -> BaseType:
        """Map a column to a Keboola BaseType from Medallia metadata (spec §9)."""
        meta = field_meta.get(name) or {}
        if meta.get("dataType") is not None:
            if meta.get("multivalued"):
                return BaseType.string()  # JSON-encoded list
            data_type = str(meta["dataType"]).upper()
            if data_type in {"INT", "INTEGER"}:
                return BaseType.integer()
            if data_type == "FLOAT":
                return BaseType.numeric()
            if data_type == "DATE":
                return BaseType.date()
            if data_type == "DATETIME":
                return BaseType.timestamp()
            return BaseType.string()
        scalar_type = meta.get("scalar_type")
        if scalar_type == "Int":
            return BaseType.integer()
        if scalar_type == "Float":
            return BaseType.numeric()
        if scalar_type == "Boolean":
            return BaseType.boolean()
        return BaseType.string()

    def _write_rows(
        self,
        table_path: str,
        fieldnames: list[str],
        nodes,
        has_id: bool,
        incremental_field: str,
        is_int: bool,
        initial_watermark: int | str | None,
    ) -> int | str | None:
        """Stream nodes into the output CSV, returning the advanced watermark."""
        watermark = initial_watermark
        record_count = 0
        with open(table_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for node in nodes:
                row = flatten_node(node)
                if not has_id:
                    row["_row_hash"] = row_hash(row)
                writer.writerow(row)
                if incremental_field:
                    watermark = advance_watermark(watermark, row.get(incremental_field), is_int)
                record_count += 1
        logging.info("Wrote %s record(s) to %s.", record_count, os.path.basename(table_path))
        return watermark

    def _write_raw_table(self, table_name: str, nodes) -> None:
        """Write raw-mode nodes: generic flatten, id/row-hash PK, full load, no state."""
        first_table = self.create_out_table_definition(table_name)
        fieldnames: list[str] | None = None
        has_id = False
        record_count = 0
        with open(first_table.full_path, "w", encoding="utf-8", newline="") as out_file:
            writer: csv.DictWriter | None = None
            for node in nodes:
                row = flatten_node(node)
                if fieldnames is None:
                    has_id = "id" in row
                    ordered = (["id"] if has_id else []) + [k for k in row if k != "id"]
                    fieldnames = ordered if has_id else [*ordered, "_row_hash"]
                    writer = csv.DictWriter(out_file, fieldnames=fieldnames, extrasaction="ignore")
                    writer.writeheader()
                if not has_id:
                    row["_row_hash"] = row_hash(row)
                assert writer is not None
                writer.writerow(row)
                record_count += 1
        if fieldnames is None:
            logging.warning("Raw query returned no rows; wrote an empty table for %s.", table_name)
            fieldnames = []
        primary_key = ["id"] if has_id else (["_row_hash"] if fieldnames else [])
        schema = {
            name: ColumnDefinition(
                data_types=BaseType.string(),
                primary_key=(name in primary_key),
                nullable=(name not in primary_key),
            )
            for name in fieldnames
        }
        table = self.create_out_table_definition(
            table_name, primary_key=primary_key, incremental=False, schema=schema, has_header=True
        )
        self.write_manifest(table)
        logging.info("Wrote %s record(s) to %s (raw mode, full load).", record_count, table_name)

    # -- raw-mode contract validation --------------------------------------------------

    @staticmethod
    def _validate_raw_static(query: str) -> None:
        missing = [token for token in ("$first", "$after", "pageInfo") if token not in query]
        if missing:
            raise UserException(
                "Raw query must declare $first/$after and select pageInfo{hasNextPage endCursor}. "
                f"Missing: {', '.join(missing)}."
            )

    @staticmethod
    def _raw_connection(data: dict[str, Any]) -> dict[str, Any]:
        """Return the single Relay connection under ``data`` (spec §6.5), else a precise error."""
        connections = {
            key: value
            for key, value in data.items()
            if isinstance(value, dict) and isinstance(value.get("nodes"), list)
        }
        if not connections:
            raise UserException("Raw query returned no Relay connection (a field selecting nodes + pageInfo).")
        if len(connections) > 1:
            found = ", ".join(sorted(connections))
            raise UserException(f"Raw query must return exactly one connection (found {len(connections)}: {found}).")
        connection = next(iter(connections.values()))
        if not isinstance(connection.get("pageInfo"), dict):
            raise UserException("Raw query connection must select pageInfo { hasNextPage endCursor }.")
        return connection

    # -- sync actions ------------------------------------------------------------------

    @sync_action("testConnection")
    def test_connection(self) -> ValidationResult:
        """Mint a token and run a free metadata pre-flight to validate connectivity."""
        client = self._build_client(self._get_config())
        try:
            client.run_metadata_query("query { __typename }", compute_cost_only=True)
        except MedalliaClientError as exc:
            raise UserException(f"Connection to Medallia Query API failed: {exc}") from None
        return ValidationResult("Connection to Medallia Query API succeeded.")

    @sync_action("listObjects")
    def list_objects(self) -> list[SelectElement]:
        """Introspect the schema and return the extractable connections (static fallback)."""
        client = self._build_client(self._get_config())
        try:
            objects = list_extractable_objects(client.run_introspection())
        except (MedalliaClientError, UserException) as exc:
            logging.warning("Introspection unavailable (%s); using the static object allowlist.", exc)
            objects = []
        if not objects:
            objects = list(STATIC_EXTRACTABLE_OBJECTS)
        return [SelectElement(value=name, label=self._humanize(name)) for name in objects]

    @sync_action("listFields")
    def list_fields(self) -> list[SelectElement]:
        """Object-aware field list (names shown, ids preserved) for the current object."""
        return self._field_elements(date_only=False)

    @sync_action("listDateFields")
    def list_date_fields(self) -> list[SelectElement]:
        """Object-aware incremental-field candidates (DATE/DATETIME + sortable INT)."""
        return self._field_elements(date_only=True)

    @sync_action("validateQuery")
    def validate_query(self) -> ValidationResult:
        """Raw-mode pre-flight: static contract check + compute_cost_only + single-connection."""
        row = self._get_row()
        query = row.raw_query or ""
        try:
            self._validate_raw_static(query)
        except UserException as exc:
            return ValidationResult(str(exc), MessageType.ERROR)
        client = self._build_client(self._get_config())
        try:
            client.run_metadata_query(query, compute_cost_only=True, variables={"first": row.page_size, "after": None})
        except (MedalliaClientError, UserException) as exc:
            return ValidationResult(f"Query validation failed: {exc}", MessageType.ERROR)
        return ValidationResult("Raw query compiled successfully and passed the cost pre-flight.")

    def _field_elements(self, date_only: bool) -> list[SelectElement]:
        row = self._get_row()
        if not row.data_object:
            return []
        client = self._build_client(self._get_config())
        shape = self._resolve_object(client, row.data_object)
        if shape.shape == SHAPE_FIELDDATA:
            return self._catalog_field_elements(client, date_only)
        return self._scalar_field_elements(shape, date_only)

    def _catalog_field_elements(self, client: MedalliaClient, date_only: bool) -> list[SelectElement]:
        try:
            data = client.run_metadata_query(_FIELDS_CATALOG_QUERY)
        except MedalliaClientError as exc:
            raise UserException(f"Could not load Medallia fields: {exc}") from None
        nodes = ((data.get("fields") or {}).get("nodes")) or []
        elements: list[SelectElement] = []
        for node in nodes:
            field_id = node.get("id")
            if not field_id:
                continue
            if date_only and not self._is_date_candidate(node):
                continue
            elements.append(SelectElement(value=field_id, label=node.get("name") or field_id))
        return elements

    @staticmethod
    def _scalar_field_elements(shape: ObjectShape, date_only: bool) -> list[SelectElement]:
        elements: list[SelectElement] = []
        for name, scalar_type in shape.scalar_fields.items():
            if date_only and scalar_type != "Int":
                continue
            elements.append(SelectElement(value=name, label=name))
        return elements

    @staticmethod
    def _is_date_candidate(node: dict[str, Any]) -> bool:
        data_type = str(node.get("dataType") or "").upper()
        if data_type in {"DATE", "DATETIME"}:
            return True
        return data_type in {"INT", "INTEGER"} and bool(node.get("sortable"))

    @staticmethod
    def _humanize(name: str) -> str:
        """Turn a camelCase object name into a Title Case label (e.g. socialURLs → Social URLs)."""
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
        spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
        return " ".join(word[:1].upper() + word[1:] for word in spaced.split())


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
