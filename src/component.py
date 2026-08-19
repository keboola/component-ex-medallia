"""Medallia Experience Cloud — generic Query API extractor.

Thin orchestrator (spec §9): it loads and validates configuration, builds the separated
GraphQL client, and — depending on the row ``mode`` — either extracts one introspection-
resolved object (structured mode) or drives a user-authored Relay query (raw mode). Every
object is paged via ``pageInfo.hasNextPage`` with ``first``/``after`` as GraphQL variables.
The date window comes from the row's Start/End Date on every run — the component keeps no
cursor and no state. There is a SINGLE generic code path — no feedback-specific branch.
"""

import csv
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import dateparser
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
# the production import clean. The response-body sanitizer overwrites EVERY node value across
# all three generic node shapes (spec §4): (a) fieldData(fieldId){values}, (b) data(fieldId)
# {value,values} (customers), and (c) bare scalar node fields (programs/social/unitWarnings) —
# including id-less nodes — plus pageInfo.endCursor and the request `after` cursor, so a
# recorded cassette is clean BY CONSTRUCTION and verifiable by allowlist.
try:
    from keboola.vcr import BaseSanitizer, DefaultSanitizer, UrlPatternSanitizer

    class MedalliaResponseBodySanitizer(BaseSanitizer):
        """Overwrite Query API response node payloads with deterministic synthetic data.

        Free-text verbatim fields can contain arbitrary customer PII, so a denylist is unsafe:
        this REPLACES every value across every node shape (fieldData / data / bare scalar) and
        every node id, so nothing real survives into a cassette. Values keep their ARITY and
        SHAPE (a multi-value field stays multi-value; an epoch-int field stays a long integer; a
        date/datetime field stays date-shaped) so type inference and date bounds behave.

        ``scrub_before_read = True``: keboola.vcr applies this sanitizer to the response BEFORE
        the component reads it during recording (as well as to the cassette), so the component
        computes its output tables / sync-action results / state from the SYNTHETIC values — the
        committed ``expected/`` therefore matches replay exactly and never holds real data. Two
        constraints follow from that contract: it MUST be idempotent (the recorder applies it
        twice — once pre-read, once on the cassette copy — with the same instance), and it MUST
        NOT touch any value the component round-trips to the live API. The paging cursor IS
        round-tripped, so it is scrubbed cassette-only by ``MedalliaCursorSanitizer`` instead.
        """

        scrub_before_read = True

        SYNTHETIC_COMMENT = "Synthetic feedback comment."
        # Idempotency marker stamped on every scrubbed node. keboola.vcr applies a
        # scrub_before_read sanitizer multiple times with the SAME instance (pre-read, cassette
        # copy, and every replay read), so it MUST be idempotent. A ``RESP-`` id alone can't mark
        # id-less node shapes (socialURLs, unitWarnings, …); this marker works for ANY shape and is
        # dropped by ``flatten_node`` (``__``-prefixed), so it never reaches an output column.
        _SCRUBBED_MARKER = "__scrubbed__"
        # A fixed synthetic field catalogue. Field IDs are schema identifiers (not PII) and are
        # deliberately the ones the recorded cases select/order by, so replay types columns and
        # detects an INT date field exactly as the live schema would (an INT finish field →
        # numeric bound + integer column; a DATE/DATETIME field → date/timestamp column; a
        # multivalued field → JSON-encoded string column).
        _SYNTHETIC_FIELD_CATALOG = [
            {"id": "a_surveyid", "name": "Survey ID", "dataType": "STRING", "sortable": True, "multivalued": False},
            {"id": "a_customerid", "name": "Customer ID", "dataType": "STRING", "sortable": True, "multivalued": False},
            {
                "id": "e_creationdate",
                "name": "Creation Date",
                "dataType": "DATE",
                "sortable": True,
                "multivalued": False,
            },
            {
                "id": "e_accepteddate",
                "name": "Accepted Date",
                "dataType": "DATETIME",
                "sortable": True,
                "multivalued": False,
            },
            {
                "id": "a_initial_finish_timestamp",
                "name": "Initial Finish Timestamp",
                "dataType": "INT",
                "sortable": True,
                "multivalued": False,
            },
            {
                "id": "a_recognition_sentiment_types",
                "name": "Recognition Sentiment Types",
                "dataType": "STRING",
                "sortable": False,
                "multivalued": True,
            },
            {"id": "e_nps", "name": "NPS Score", "dataType": "INTEGER", "sortable": True, "multivalued": False},
            {"id": "e_comment", "name": "Comment", "dataType": "STRING", "sortable": False, "multivalued": False},
            {
                "id": "a_survey_channel",
                "name": "Survey Channel",
                "dataType": "STRING",
                "sortable": False,
                "multivalued": False,
            },
        ]
        # An all-digit value at least this long is treated as an epoch timestamp (10-digit
        # seconds since 1970), so the synthetic replacement stays a long integer too.
        _EPOCH_INT_THRESHOLD = 10**8

        def __init__(self) -> None:
            self._node_counter = 0

        # -- response: overwrite every node value (the cursor is scrubbed separately) -----

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
            for key, obj in data.items():
                if not isinstance(obj, dict):
                    continue
                nodes = obj.get("nodes")
                if not isinstance(nodes, list):
                    continue
                if not nodes:
                    continue
                # The field metadata catalogue (the `fields` query) has a fixed replacement so
                # the field pickers stay deterministic; every other connection's nodes are
                # value-overwritten in place regardless of shape (fieldData / data / bare scalar).
                if key == "fields" and self._is_field_catalog(nodes):
                    obj["nodes"] = [dict(field) for field in self._SYNTHETIC_FIELD_CATALOG]
                    # Collapse the (possibly multi-page) real catalogue to a single synthetic
                    # page: with scrub_before_read the component reads this fixed set and stops,
                    # so exactly one catalogue interaction is recorded and the field defs are not
                    # duplicated once per real page.
                    if isinstance(obj.get("pageInfo"), dict):
                        obj["pageInfo"] = {"hasNextPage": False, "endCursor": None}
                else:
                    for node in nodes:
                        # Idempotency (the recorder applies this sanitizer twice — pre-read then
                        # cassette copy): skip a node already synthesised so a re-run never
                        # re-numbers already-synthetic values.
                        if isinstance(node, dict) and not self._is_synthetic_node(node):
                            self._synthesize_node(node)
                if "totalCount" in obj:
                    obj["totalCount"] = len(obj["nodes"])
                changed = True
            return changed

        @staticmethod
        def _is_field_catalog(nodes: list) -> bool:
            head = nodes[0]
            return isinstance(head, dict) and "dataType" in head and "values" not in head

        @classmethod
        def _is_synthetic_node(cls, node: dict) -> bool:
            """True if a node was already synthesised (idempotency guard for the multi-apply).

            The ``__scrubbed__`` marker covers EVERY node shape, including id-less objects
            (socialURLs, unitWarnings, …) that have no id. The legacy ``RESP-####`` id sentinel is
            still honoured so cassettes recorded before the marker replay unchanged (real Medallia
            node ids are numeric, so the ``RESP-`` prefix never collides).
            """
            if node.get(cls._SCRUBBED_MARKER) is True:
                return True
            nid = node.get("id")
            return isinstance(nid, str) and nid.startswith("RESP-")

        def _synthesize_node(self, node: dict) -> None:
            """Overwrite every field of one node, detecting each value's shape, then mark it."""
            self._node_counter += 1
            n = self._node_counter
            for key in list(node.keys()):
                node[key] = self._synthesize_field(key, n, node[key])
            # Stamp the idempotency marker LAST so a re-application skips this node instead of
            # re-numbering it (which is exactly what broke id-less run cassettes: a drifting
            # _node_counter produced different synthetic values in expected/ vs the cassette).
            node[self._SCRUBBED_MARKER] = True

        def _synthesize_field(self, key: str, n: int, value: Any) -> Any:
            """Return a synthetic replacement for one field value, preserving its shape."""
            if key == "id":
                return f"RESP-{n:04d}"
            if isinstance(value, dict):
                if "value" in value or "values" in value:  # shapes (a)/(b): fieldData / data
                    new = dict(value)
                    if isinstance(new.get("values"), list):
                        new["values"] = self._synthetic_values(key, n, new["values"])
                    if new.get("value") is not None:
                        new["value"] = self._synthetic_scalar(key, n, 0, [new["value"]])
                    return new
                # Any other nested object → recurse so no real leaf value survives.
                return {k: self._synthesize_field(k, n, v) for k, v in value.items()}
            if isinstance(value, list):
                return [self._synthesize_field(key, n, item) for item in value]
            if value is None or isinstance(value, bool):
                return value  # a boolean carries no PII; leave it deterministic
            if isinstance(value, (int, float)):  # fmt: skip
                return self._synthetic_number(key, n, value)
            return self._synthetic_scalar(key, n, 0, [value])  # shape (c): bare scalar string

        def _synthetic_number(self, alias: str, n: int, value: int | float) -> int | float:
            """Synthetic numeric replacement, preserving int/float and epoch magnitude."""
            if isinstance(value, float):
                return round(1.5 + n * 0.5, 2)
            if value >= self._EPOCH_INT_THRESHOLD:  # keep an epoch-seconds integer epoch-shaped
                return _SANITIZER_EPOCH_SECONDS + n
            return n

        def _synthetic_values(self, alias: str, n: int, original: list) -> list[str]:
            """Replace a field's values with synthetic ones, preserving EXACT ARITY and SHAPE.

            An empty ``values`` list stays empty (the field had no value for that node → the
            flatten collapses it to None), so synthetic data mirrors the real cardinality.
            """
            return [self._synthetic_scalar(alias, n, i, original) for i in range(len(original))]

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
            if "url" in a:
                return f"https://synthetic.example.com/{alias}/{ordinal:06d}"
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

    class MedalliaCursorSanitizer(BaseSanitizer):
        """Scrub the Relay paging cursor out of the committed cassette (cassette-only).

        Cursors may encode offsets/PII (spec risk #7), so the request ``after`` and the response
        ``pageInfo.endCursor`` are normalised to a synthetic value in the cassette. This stays
        ``scrub_before_read = False`` (the default) on purpose: the component round-trips the
        cursor to the live API while recording, so it must read the REAL cursor — scrubbing it
        before read would break live pagination, and keboola.vcr's round-trip guard rejects it.
        The request body is not part of the VCR match key, so a normalised cursor never affects
        replay ordering.
        """

        SYNTHETIC_CURSOR = "SYNTHETIC-CURSOR"

        def __init__(self) -> None:
            self._cursor_counter = 0

        def before_record_request(self, request):
            """Normalise the ``after`` paging cursor in the request body (not a match key)."""
            body = getattr(request, "body", None)
            if body is None:
                return request
            is_bytes = isinstance(body, bytes)
            text = body.decode("utf-8", "ignore") if is_bytes else body
            if not isinstance(text, str) or '"after"' not in text:
                return request
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):  # fmt: skip
                return request
            variables = payload.get("variables")
            if isinstance(variables, dict) and variables.get("after") not in (None, ""):
                variables["after"] = self.SYNTHETIC_CURSOR
                new_text = json.dumps(payload)
                request.body = new_text.encode("utf-8") if is_bytes else new_text
            return request

        def before_record_response(self, response: dict) -> dict:
            """Normalise ``pageInfo.endCursor`` in every connection of the response body."""
            body = response.get("body")
            if not isinstance(body, dict) or "string" not in body:
                return response
            raw = body["string"]
            is_bytes = isinstance(raw, bytes)
            text = raw.decode("utf-8", "ignore") if is_bytes else raw
            if not isinstance(text, str) or '"endCursor"' not in text:
                return response
            try:
                payload = json.loads(text)
            except (ValueError, TypeError):  # fmt: skip
                return response
            data = payload.get("data")
            if not isinstance(data, dict):
                return response
            changed = False
            for obj in data.values():
                if not isinstance(obj, dict):
                    continue
                page_info = obj.get("pageInfo")
                if isinstance(page_info, dict) and page_info.get("endCursor"):
                    self._cursor_counter += 1
                    page_info["endCursor"] = f"{self.SYNTHETIC_CURSOR}-{self._cursor_counter:04d}"
                    changed = True
            if changed:
                new_text = json.dumps(payload)
                body["string"] = new_text.encode("utf-8") if is_bytes else new_text
            return response

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
        MedalliaCursorSanitizer(),
    ]
except ImportError:  # pragma: no cover - production image has no dev dependencies.
    VCR_SANITIZERS = []

# validateQuery row-preview limits (kept small — one gentle live page shown in the config UI).
_PREVIEW_ROWS = 5
_PREVIEW_COLUMNS = 8
_PREVIEW_CELL_WIDTH = 40


# Field-catalogue paging (spec §6.3). Medallia field catalogues can exceed a single page;
# fetch the WHOLE catalogue via ``pageInfo`` cursoring rather than a fixed ``first: N`` cap
# (a bug that silently dropped later fields, e.g. k_/q_/u_* on large instances).
_METADATA_PAGE_SIZE = 500
_MAX_METADATA_PAGES = 400  # ceiling guard (≤200k fields); real catalogues are far smaller
# The Field-type selection includes ``usedOnPrograms`` so the picker can hide fields not used on
# any program (they would only ever produce empty columns). Only the ``fields`` catalogue (the
# Field type) exposes it; the other catalogues use the base selection. ``usedOnPrograms`` is a
# ``[Program!]!`` (not a scalar), so it needs a sub-selection.
_FIELD_SELECTION = "id name dataType sortable multivalued usedOnPrograms { id }"
_BASE_SELECTION = "id name dataType sortable multivalued"


# ContactSchema (the ``customers`` object's schema, shape b) types its field definitions as
# ContactAttribute — a DIFFERENT shape than the Field type: ``key``/``name``/``type``/``isIndexed``
# rather than ``id``/``name``/``dataType``/``sortable``. They are reached via a NESTED Relay
# connection (``customerSchema.attributes``, a ``ContactAttributeConnection``) — NOT the
# ``customerSchema.fields`` list the first cut assumed (``ContactSchema`` has no ``fields``, which
# failed live with a FieldUndefined validation error). ``_normalize_contact_attribute`` maps a node
# onto the common definition shape the rest of the extractor keys on (``type`` shares the
# DATE/DATETIME/… vocabulary of ``dataType``; ``isIndexed`` marks the ordered/sortable attributes).
_CONTACT_ATTRIBUTE_SELECTION = "key name type containsPii isKey isIndexed"


def _normalize_contact_attribute(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("key"),
        "name": node.get("name"),
        "dataType": node.get("type"),
        "sortable": bool(node.get("isIndexed")),
    }


@dataclass(frozen=True)
class _MetadataCatalog:
    """A field-definition metadata source (spec §6.3).

    ``node`` is the root query field. Definitions live in a Relay connection paged in full via
    ``pageInfo``: for ``fields`` / ``eventSchemas`` / ``programRecordSchemas`` ``node`` IS the
    connection; for ``customerSchema`` the connection is a nested field (``wrapper`` =
    ``attributes``). ``normalizer``, when set, maps each raw node onto the common
    ``{id, name, dataType, sortable, multivalued}`` shape (ContactAttribute uses different field
    names). ``fetch`` returns every definition, paging as needed.
    """

    node: str
    selection: str = _BASE_SELECTION
    wrapper: str | None = None
    normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def _query(self) -> str:
        # Medallia's Relay connections type the cursor as ``ID`` (not ``String``) — verified live.
        conn = f"(first: $first, after: $after) {{ nodes {{ {self.selection} }} pageInfo {{ hasNextPage endCursor }} }}"
        inner = f"{self.node} {{ {self.wrapper}{conn} }}" if self.wrapper else f"{self.node}{conn}"
        return f"query ($first: Int!, $after: ID) {{ {inner} }}"

    def fetch(self, client: MedalliaClient) -> list[dict[str, Any]]:
        query = self._query()
        out: list[dict[str, Any]] = []
        after: str | None = None
        for _ in range(_MAX_METADATA_PAGES):
            data = client.run_metadata_query(query, variables={"first": _METADATA_PAGE_SIZE, "after": after})
            conn = data.get(self.node) if isinstance(data, dict) else None
            if self.wrapper:
                conn = conn.get(self.wrapper) if isinstance(conn, dict) else None
            conn = conn if isinstance(conn, dict) else {}
            for item in conn.get("nodes") or []:
                if isinstance(item, dict):
                    out.append(self.normalizer(item) if self.normalizer else item)
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
            if not after:
                # Mirror MedalliaClient.paginate: a truthy hasNextPage with an empty endCursor
                # would re-fetch the same first page up to _MAX_METADATA_PAGES times. Stop instead.
                logging.warning("%s: hasNextPage is true but endCursor is empty; stopping to avoid a loop.", self.node)
                break
        return out


# Field-metadata catalogues keyed by metadata-node name (spec §6.3). ``fields`` is the global
# shape-(a) catalogue (feedback / invitations) and, with ``eventSchemas`` / ``programRecordSchemas``,
# a top-level Relay connection (``{nodes{…}}``). ``customerSchema`` (ContactSchema) types the
# ``customers`` object (shape b): its definitions are ContactAttribute nodes under a NESTED
# connection ``attributes`` and are normalised onto the common definition shape.
_METADATA_CATALOGS: dict[str, _MetadataCatalog] = {
    "fields": _MetadataCatalog("fields", selection=_FIELD_SELECTION),
    "customerSchema": _MetadataCatalog(
        "customerSchema",
        selection=_CONTACT_ATTRIBUTE_SELECTION,
        wrapper="attributes",
        normalizer=_normalize_contact_attribute,
    ),
    "eventSchemas": _MetadataCatalog("eventSchemas"),
    "programRecordSchemas": _MetadataCatalog("programRecordSchemas"),
}

# Objects whose field metadata comes from a dedicated catalogue instead of the shape-based
# default (spec §6.3). ``customers`` (shape b) is typed by ``customerSchema``. Event and
# program-record connections register here by their instance-specific object name mapping to
# ``eventSchemas`` / ``programRecordSchemas``; the reference instance exposes none as extractable
# objects, so only ``customers`` is wired today.
_OBJECT_METADATA_NODE: dict[str, str] = {"customers": "customerSchema"}


class Component(ComponentBase):
    """Generic extractor for the Medallia Query API."""

    def __init__(self):
        super().__init__()

    # -- orchestration -----------------------------------------------------------------

    def run(self) -> None:
        """Orchestrate a single config-row extract (structured or raw mode)."""
        config = self._get_config()
        row = self._get_row()
        client = self._build_client(config)
        try:
            if row.mode == Mode.raw:
                self._run_raw(client, row)
            else:
                self._run_structured(client, row)
        except MedalliaClientError as exc:
            # A data-fetch failure (e.g. a Medallia 5xx after retries) is otherwise uncaught and
            # surfaces as an exit-2 "application" error whose stderr Keboola HIDES from the user
            # (they only see "Internal Server Error occurred"). Re-raise as a UserException so the
            # job fails cleanly (exit 1) with an actionable, visible message.
            target = row.data_object or row.output_table or "the raw query"
            raise UserException(f"Medallia extraction of '{target}' failed: {exc}") from exc

    def _run_structured(self, client: MedalliaClient, row: RowConfiguration) -> None:
        if not row.data_object:
            raise UserException("No data object selected. Choose a Medallia object (structured mode).")
        shape = self._resolve_object(client, row.data_object)
        field_meta = self._field_metadata(client, shape)
        is_int = self._incremental_is_int(shape, row.incremental_field, field_meta)
        # The Start-Date window applies to BOTH load types (bounds what's fetched); the load type
        # only decides the write mode (upsert vs overwrite).
        windowed = self._date_window_supported(row, shape)
        lower_bound = self._compute_lower_bound(row, is_int) if windowed else None
        upper_bound = self._upper_bound(is_int, row.end_date) if windowed else None

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
            first_type=shape.first_type,
            after_type=shape.after_type,
        )
        query = builder.build_query(lower_bound, upper_bound)
        columns = builder.selection_columns()
        fieldnames = [*columns, "_row_hash"] if not shape.has_id else list(columns)

        table_name = self._output_table_name(row.output_table, row.data_object)
        # Write mode follows the load type: incremental → upsert per PK; full → overwrite.
        table = self._build_table_definition(table_name, fieldnames, shape, field_meta, row.incremental)
        nodes = client.fetch_object(row.data_object, query, row.page_size, after_type=shape.after_type)
        self._write_rows(table.full_path, fieldnames, nodes, shape.has_id)
        self.write_manifest(table)

    def _run_raw(self, client: MedalliaClient, row: RowConfiguration) -> None:
        if not row.raw_query.strip():
            raise UserException("Raw mode requires a GraphQL query in 'raw_query'.")
        if not row.output_table.strip():
            raise UserException("Raw mode requires an 'output_table' name.")
        # Substitute {{start_date}} / {{end_date}} (relative-aware) into the user's query first.
        query = self._apply_raw_placeholders(row)
        self._validate_raw_static(query)
        # Cost pre-flight at run start (compile + price without consuming quota). $first/$after
        # travel as variables so the priced query is identical to the one that will run.
        client.run_metadata_query(query, compute_cost_only=True, variables={"first": row.page_size, "after": None})

        table_name = self._output_table_name(row.output_table, "")
        nodes = client.paginate(query, row.page_size, self._raw_connection)
        self._write_raw_table(table_name, nodes)

    def _apply_raw_placeholders(self, row: RowConfiguration) -> str:
        """Replace ``{{start_date}}`` / ``{{end_date}}`` in a raw query with the resolved dates.

        Each is resolved the same way as the structured Start Date (absolute or relative, e.g.
        ``5 days ago``) to a plain ``YYYY-MM-DD`` string — the user controls the surrounding
        quoting and operator in their query. A placeholder used with an empty date is an error.
        """
        query = row.raw_query
        for token, raw_value in (("{{start_date}}", row.initial_start), ("{{end_date}}", row.end_date)):
            if token not in query:
                continue
            if not raw_value.strip():
                raise UserException(f"Raw query uses {token} but the corresponding date field is empty.")
            resolved = self._resolve_initial_start(raw_value, is_int=False)
            query = query.replace(token, str(resolved))
        return query

    # -- configuration -----------------------------------------------------------------

    def _get_config(self) -> Configuration:
        return Configuration(**self.configuration.parameters)

    def _get_row(self) -> RowConfiguration:
        return RowConfiguration(**self.configuration.parameters)

    @staticmethod
    def _build_client(config: Configuration) -> MedalliaClient:
        token_manager = MedalliaTokenManager(
            instance_host=config.instance_host,
            company_name=config.company_name,
            client_id=config.client_id,
            client_secret=config.client_secret,
        )
        return MedalliaClient(api_host=config.api_host, token_manager=token_manager)

    # -- object / shape resolution -----------------------------------------------------

    @staticmethod
    def _resolve_object(client: MedalliaClient, object_name: str) -> ObjectShape:
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

    @staticmethod
    def _date_window_supported(row: RowConfiguration, shape: ObjectShape) -> bool:
        """Whether a Start-Date window can be applied to this row (either load type).

        Requires a Date Field AND an object that supports ``filter`` + ``orderBy``. Without a
        Date Field the object is loaded unbounded (full history) regardless of load type.
        """
        if not row.incremental_field:
            return False
        if not (shape.supports_filter and shape.supports_order):
            logging.warning(
                "%s: a Date Field is set but the object has no filter/orderBy support; loading unbounded.", shape.name
            )
            return False
        return True

    @staticmethod
    def _incremental_is_int(shape: ObjectShape, field_id: str, field_meta: dict) -> bool:
        """Auto-detect the date-bound format from metadata: INT → numeric, else ISO string."""
        if not field_id:
            return False
        meta = field_meta.get(field_id) or {}
        data_type = meta.get("dataType")
        if data_type:
            return str(data_type).upper() in {"INT", "INTEGER"}
        return shape.scalar_fields.get(field_id) == "Int"

    def _compute_lower_bound(self, row: RowConfiguration, is_int: bool) -> int | str | None:
        """Lower bound = the Start Date, on every run. Empty Start Date → no lower bound.

        The Start Date is what the configuration says to load from, so that is what the component
        loads from — every run, whatever the load type. Set it to ``5 days ago`` and every run
        starts five days ago; change it and the next run obeys immediately.

        This is deliberately the whole rule. The component used to remember the newest value it
        had loaded and resume from that instead, which meant an edit to the Start Date was
        silently discarded on every run but the first. It also lost records permanently whenever
        one surfaced in Medallia after its own date had already been passed — a routine event,
        since ``feedback`` only admits a record once it is completed, long after its creation
        date. A rolling Start Date has neither problem: the window re-reads itself, so a late
        record is picked up next run, and rows are matched on the primary key so nothing
        duplicates. The Start Date is therefore also the safety margin — set it as far back as
        records can realistically arrive late.
        """
        if row.initial_start.strip():
            # Log the RESOLVED bound, not the raw text: a relative Start Date ("5 days ago") and an
            # ISO date on an epoch field both differ from what actually reaches the query, and the
            # log is what someone reads when reconciling a run against Medallia.
            resolved = self._resolve_initial_start(row.initial_start, is_int)
            logging.info("Loading from the Start Date (%s).", resolved)
            return resolved
        logging.warning(
            "No Start Date is set, so every run loads this object's entire history. Set a Start "
            "Date (for example '5 days ago') to bound it."
        )
        return None

    @staticmethod
    def _resolve_initial_start(value: str, is_int: bool) -> int | str:
        """Resolve ``initial_start`` to the date field's bound format.

        Accepts an absolute value — epoch seconds (e.g. ``1780272000``) or an ISO date
        (e.g. ``2026-01-01``) — OR a relative expression (e.g. ``yesterday``, ``5 days ago``,
        ``last monday``). Returns epoch seconds (``int``) for an epoch field, or a date-only
        ``YYYY-MM-DD`` string for a datetime field (Medallia's date filter is day-granular).
        """
        text = value.strip()
        if text.isdigit():  # epoch-seconds passthrough (avoid dateparser mis-reading a bare int)
            return int(text) if is_int else text
        parsed = dateparser.parse(text, settings={"PREFER_DATES_FROM": "past", "RETURN_AS_TIMEZONE_AWARE": False})
        if parsed is None:
            raise UserException(
                f"Could not parse 'Initial Start' value {text!r}. Use an ISO date "
                "(e.g. 2026-01-01), epoch seconds, or a relative expression like "
                "'yesterday' or '5 days ago'."
            )
        # Floor to the start of the resolved day: a first-run lower bound is day-granular
        # (Medallia's date filter is), and it keeps relative values deterministic.
        if is_int:
            return int(datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC).timestamp())
        return parsed.strftime("%Y-%m-%d")

    @staticmethod
    def _upper_bound(is_int: bool, end_date: str = "") -> int | str:
        """Upper bound in the field's format (INT seconds, else ``YYYY-MM-DD`` — Medallia rejects a
        ``…Z`` timestamp, a v1 live-verified fact).

        With an explicit End Date the window closes there, INCLUSIVE of that day: the filter uses
        ``lt``, so the bound is the day AFTER the resolved End Date. Empty End Date → now (today's
        partial day arrives on the next run). End Date accepts absolute or relative expressions.
        """
        if end_date.strip():
            text = end_date.strip()
            if text.isdigit():
                day = datetime.fromtimestamp(int(text), tz=UTC)
            else:
                parsed = dateparser.parse(
                    text, settings={"PREFER_DATES_FROM": "past", "RETURN_AS_TIMEZONE_AWARE": False}
                )
                if parsed is None:
                    raise UserException(
                        f"Could not parse 'End Date' value {text!r}. Use an ISO date (e.g. 2026-01-01), "
                        "epoch seconds, or a relative expression like 'yesterday' or '5 days ago'."
                    )
                day = datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)
            upper = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
            return int(upper.timestamp()) if is_int else upper.strftime("%Y-%m-%d")
        now = datetime.now(tz=UTC)
        return int(now.timestamp()) if is_int else now.strftime("%Y-%m-%d")

    # -- field metadata ----------------------------------------------------------------

    @staticmethod
    def _metadata_node(shape: ObjectShape) -> str | None:
        """Return the metadata-catalogue node for an object, or None (spec §6.3).

        ``customers`` → ``customerSchema`` (and any object registered in
        ``_OBJECT_METADATA_NODE``); shape-(a) ``fieldData`` objects → the global ``fields``
        catalogue; shape-(c) scalar objects → None, i.e. types come from introspected node
        scalars, not a catalogue.
        """
        node = _OBJECT_METADATA_NODE.get(shape.name)
        if node is not None:
            return node
        if shape.shape == SHAPE_FIELDDATA:
            return "fields"
        return None

    def _field_metadata(self, client: MedalliaClient, shape: ObjectShape) -> dict[str, dict[str, Any]]:
        """Per-field metadata for typing + date-bound detection, routed by object/shape (spec §6.3).

        Introspected bare node scalars seed the map (GraphQL scalar type). When the object has a
        dedicated metadata catalogue — the global ``fields`` catalogue for shape (a),
        ``customerSchema`` for ``customers`` (shape b), ``eventSchemas`` / ``programRecordSchemas``
        for event / program-record connections — its ``dataType`` definitions are merged in and
        win over the bare scalar type, so ``listFields``/``listDateFields`` surface fields by name
        and ``_base_type`` types them from ``dataType``. Metadata is non-essential (it only refines
        column typing and the field pickers), so a catalogue-fetch failure degrades to the scalar
        seed (columns default to STRING) with a warning rather than aborting the extraction — both a
        TRANSIENT failure (``MedalliaClientError``) and a ``UserException`` from the catalogue query
        itself (e.g. a tenant whose schema exposes the catalogue differently). The object's
        existence is already validated in ``_resolve_object`` upstream, so a genuinely invalid
        object has failed before this point — this degrade only covers the catalogue, not the object.
        """
        metadata: dict[str, dict[str, Any]] = {
            name: {"scalar_type": scalar_type} for name, scalar_type in shape.scalar_fields.items()
        }
        node = self._metadata_node(shape)
        if node is None:
            return metadata
        catalog = _METADATA_CATALOGS[node]
        try:
            definitions = catalog.fetch(client)
        except (MedalliaClientError, UserException) as exc:
            logging.warning("Could not load field metadata for '%s' (%s); columns default to STRING.", shape.name, exc)
            return metadata
        for definition in definitions:  # typing uses the FULL catalogue (not usage-filtered)
            field_id = definition.get("id")
            if field_id:
                metadata[field_id] = definition
        return metadata

    # -- output ------------------------------------------------------------------------

    @staticmethod
    def _output_table_name(explicit: str, default: str) -> str:
        """Resolve the output CSV filename: per-row ``output_table`` override, else the default.

        The override lets several rows extract the SAME object into distinct tables; without it
        every structured row on one object would collide on ``<object>.csv`` with
        ``tableAlreadyExists``. Structured mode falls back to ``<data_object>.csv``; raw mode has
        no object to derive from, so its ``output_table`` presence is enforced in ``_run_raw``.
        """
        base = explicit.strip() or default
        return f"{base}.csv"

    def _build_table_definition(
        self,
        table_name: str,
        fieldnames: list[str],
        shape: ObjectShape,
        field_meta: dict,
        incremental: bool,
    ) -> TableDefinition:
        # Dedup caveat: id-less objects key on a whole-row hash, so an UPDATED re-fetched record
        # hashes differently and is inserted as a new row rather than upserted. Acceptable here —
        # id-less objects are append-mostly and the incremental window bounds re-reads — but it is
        # why an object WITH an id is always preferred.
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
            # Medallia's fieldData/data APIs return display *labels* (strings) for every field,
            # and numeric-declared fields (dataType INT/FLOAT) routinely carry enumerated text —
            # e.g. e_status has dataType=INT but its value is "COMPLETED", and a_customerid is INT
            # yet can hold non-numeric ids. Typing such a column INTEGER/NUMERIC makes the
            # warehouse reject the text at load time. Only the temporal types come back in a
            # parseable machine format, so only those are typed; every other catalogue field is
            # STRING. (Bare GraphQL node scalars, typed below via scalar_type, ARE reliably typed
            # by the GraphQL contract, so they keep their INT/FLOAT/BOOLEAN mapping.)
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

    def _write_rows(self, table_path: str, fieldnames: list[str], nodes, has_id: bool) -> None:
        """Stream nodes into the output CSV."""
        record_count = 0
        with open(table_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for node in nodes:
                row = flatten_node(node)
                if not has_id:
                    row["_row_hash"] = row_hash(row)
                writer.writerow(row)
                record_count += 1
        logging.info("Wrote %s record(s) to %s.", record_count, os.path.basename(table_path))

    def _write_raw_table(self, table_name: str, nodes) -> None:
        """Write raw-mode nodes: generic flatten, id/row-hash PK, full load, no state.

        A raw query returning zero rows is a legitimate empty result, but raw mode cannot infer a
        column set without at least one row. Rather than emit a zero-column, no-primary-key table
        (a malformed manifest Storage would reject or import as broken), the run removes the empty
        CSV, writes no manifest, and finishes successfully — the same "success, nothing written"
        outcome structured mode gives for an empty window, instead of a hard failure.
        """
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
            # No rows → no inferable schema. Drop the empty file the writer opened and write no
            # manifest, so Storage is left untouched and the run still succeeds.
            try:
                os.remove(first_table.full_path)
            except OSError:
                pass
            logging.warning(
                "Raw query returned no rows; nothing was written for '%s' (raw mode cannot infer a "
                "table schema without at least one row).",
                table_name,
            )
            return
        primary_key = ["id"] if has_id else ["_row_hash"]
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
        # Label = the raw GraphQL object id (no humanize): predictable, matches the docs, and
        # avoids acronym mangling (socialURLs → "Social UR Ls"). The value is the id regardless.
        return [SelectElement(value=name, label=name) for name in objects]

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
        # Fetch a tiny sample (one small page) so the user can preview real rows before running.
        preview_n = min(row.page_size, _PREVIEW_ROWS)
        try:
            data = client.run_metadata_query(query, variables={"first": preview_n, "after": None})
            nodes = self._raw_connection(data).get("nodes", [])[:preview_n]
            rows = [flatten_node(node) for node in nodes]
        except (MedalliaClientError, UserException) as exc:
            return ValidationResult(
                f"Raw query is valid (cost pre-flight passed), but the row preview could not be fetched: {exc}",
                MessageType.WARNING,
            )
        if not rows:
            return ValidationResult("Raw query is valid; it returned no rows for the current filter/window.")
        return ValidationResult(
            f"Raw query is valid. Preview of the first {len(rows)} row(s):\n\n{self._format_preview(rows)}"
        )

    @staticmethod
    def _format_preview(rows: list[dict[str, Any]]) -> str:
        """Render up to _PREVIEW_ROWS flattened rows as a compact Markdown table for the UI."""
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        columns = columns[:_PREVIEW_COLUMNS]

        def cell(value: object) -> str:
            text = "" if value is None else str(value)
            text = text.replace("|", "\\|").replace("\n", " ")
            return text if len(text) <= _PREVIEW_CELL_WIDTH else text[:_PREVIEW_CELL_WIDTH] + "…"

        header = "| " + " | ".join(columns) + " |"
        divider = "| " + " | ".join("---" for _ in columns) + " |"
        body = "\n".join("| " + " | ".join(cell(r.get(c)) for c in columns) + " |" for r in rows)
        return "\n".join([header, divider, body])

    def _field_elements(self, date_only: bool) -> list[SelectElement]:
        row = self._get_row()
        if not row.data_object:
            return []
        client = self._build_client(self._get_config())
        shape = self._resolve_object(client, row.data_object)
        node = self._metadata_node(shape)
        if node is None:
            return self._scalar_field_elements(shape, date_only)
        return self._catalog_field_elements(client, _METADATA_CATALOGS[node], date_only, row.only_program_fields)

    def _catalog_field_elements(
        self, client: MedalliaClient, catalog: _MetadataCatalog, date_only: bool, only_program_fields: bool
    ) -> list[SelectElement]:
        try:
            definitions = catalog.fetch(client)
        except MedalliaClientError as exc:
            raise UserException(f"Could not load Medallia fields: {exc}") from None
        # By default offer EVERY field. The ``only_program_fields`` toggle re-applies the
        # ``usedOnPrograms`` scope for users who want a shorter list. ``usedOnPrograms`` reflects
        # *survey-program* usage, NOT whether a field carries data — many fields with an empty
        # ``usedOnPrograms`` are still populated on every record. Filtering by it is therefore
        # lossy: on the reference instance it hid ~2600 of ~3370 fields (all 790 whose id begins
        # ``a_``, which a config had always extracted), so the scope is opt-in and OFF by default.
        # NOTE: field-id prefixes (``a_``/``e_``/``k_``/…) are instance/tenant naming conventions,
        # not a reliable field-class signal — do NOT branch on them. The ``used or definitions``
        # fallback keeps the picker from ever going empty when usage isn't reported.
        if only_program_fields:
            used = [d for d in definitions if d.get("usedOnPrograms")]
            definitions = used or definitions
        elements: list[SelectElement] = []
        for node in definitions:
            field_id = node.get("id")
            if not field_id:
                continue
            if date_only and not self._is_date_candidate(node):
                continue
            elements.append(SelectElement(value=field_id, label=self._field_label(field_id, node.get("name"))))
        return elements

    @staticmethod
    def _field_label(field_id: str, name: str | None) -> str:
        """Picker label pairing the human name with the API id — ``Initial Finish Date (e_...)``.

        Medallia's own docs, filters and error messages all speak in field ids, so showing the
        name alone forces the reader to translate between the two while picking. Showing both
        also makes the list searchable either way. Degrades to the bare id when the instance
        reports no name, or reports a name identical to the id.
        """
        display = (name or "").strip()
        return f"{display} ({field_id})" if display and display != field_id else field_id

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


"""
        Main entrypoint
"""
if __name__ == "__main__":
    try:
        comp = Component()
        # this triggers the run method by default and is controlled by the configuration.action parameter
        comp.execute_action()
    except UserException as exc:
        # exit 1 is a user-actionable error: log the message only. A full traceback adds noise for
        # the user and embeds absolute local file paths into the job log; the unexpected-error
        # branch below keeps the traceback, where it aids debugging.
        logging.error(str(exc))
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
