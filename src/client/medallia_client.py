"""Medallia Experience Cloud — generic Query API (GraphQL) client.

Separated from ``component.py`` per the design spec (§9). Contains the collaborating pieces
of a GENERIC, introspection-driven extractor (no feedback-specific code):

* ``MedalliaTokenManager`` — OAuth2 client-credentials token minting with pre-expiry and
  401-driven re-mint (unchanged from v1).
* ``run_introspection`` + the pure classifiers ``list_extractable_objects`` /
  ``resolve_object_shape`` — turn a ``__schema`` introspection into the extractable-object
  set and each object's node shape / id / filter support (spec §6.2, §6.3).
* ``GenericQueryBuilder`` — emits one Relay-connection query for any object, with ``first`` /
  ``after`` as GraphQL VARIABLES and ``pageInfo`` pagination (spec §6.4).
* ``MedalliaClient`` — POSTs GraphQL to the API gateway with bearer auth, cost-aware
  throttling (``X-RateLimit-*``), exponential backoff on 429/5xx, a single 401 re-mint, and a
  Relay cursor paginator driven by ``pageInfo.hasNextPage`` (never ``totalCount``).
* pure helpers ``flatten_node`` (shape-detecting), ``row_hash`` (id-less PK) and
  ``advance_watermark`` (single-scalar incremental cursor).

Neither the client secret nor the access token is ever logged.
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import requests
from keboola.component.exceptions import UserException

# Token is re-minted when it expires within this safety window (Medallia's own recommended
# "refresh if expiring in the next 5 minutes" pattern).
TOKEN_EXPIRY_SKEW_SECONDS = 300

# Backoff / retry defaults for transient (429 / 5xx) failures.
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_MAX_SECONDS = 60.0

# When a rate-limit "remaining" header drops to this floor or below, pause briefly to let the
# window reset rather than racing into a 429.
RATE_LIMIT_REMAINING_FLOOR = 1
RATE_LIMIT_PAUSE_SECONDS = 1.0

_HTTP_UNAUTHORIZED = 401
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Substring of the non-fatal per-field GraphQL error Medallia returns when a selected field is
# not valid for the queried entity. The valid data still comes back, so these are logged and
# skipped rather than raised (mirrors the reference extractor).
INVALID_FIELD_ID_MARKER = "Invalid field id:"

# When compute_cost_only is requested, Medallia returns the cost estimate THROUGH the GraphQL
# ``errors[]`` channel (e.g. "Estimated query cost is: 1. To actually get results please remove
# the 'compute cost only' parameter."). That is the expected success signal of the free
# pre-flight, not a failure — it must not abort a connection/query check.
COST_ONLY_MARKER = "Estimated query cost"

# Node shapes (spec §4). ``fielddata`` = ``fieldData(fieldId){values}``; ``data`` = a scalar
# ``value`` plus ``data(fieldId){value,values}``; ``scalar`` = bare scalar node fields.
SHAPE_FIELDDATA = "fielddata"
SHAPE_DATA = "data"
SHAPE_SCALAR = "scalar"

# Query-root fields that are schema catalogs, not extraction targets (spec §4/§6.2). They power
# the field pickers internally but never appear in the object dropdown.
METADATA_CATALOG_DENYLIST = frozenset({"fields", "eventSchemas", "programRecordSchemas"})

# Node fields handled specially (never emitted as ordinary scalar columns).
_RESERVED_NODE_FIELDS = frozenset({"id", "fieldData", "data"})

# Static fallback used when instance introspection is disabled (spec §4/§6.2). The fixed
# extractable set doubles as the object-dropdown allowlist.
STATIC_EXTRACTABLE_OBJECTS = (
    "feedback",
    "invitations",
    "customers",
    "programs",
    "missingSocialURLs",
    "socialURLs",
    "socialUrlsHealth",
    "unitWarnings",
)


class MedalliaClientError(Exception):
    """Unexpected/transport failure that is NOT user-actionable (maps to exit code 2)."""


@dataclass(frozen=True)
class ObjectShape:
    """Runtime classification of one extractable connection (spec §6.3)."""

    name: str
    shape: str
    has_id: bool
    supports_filter: bool
    supports_order: bool
    node_type: str | None = None
    # Bare scalar node fields → their GraphQL scalar type name (Int/Float/Boolean/String/ID).
    scalar_fields: dict[str, str] = field(default_factory=dict)
    # GraphQL types of the connection's ``first`` / ``after`` arguments, exactly as the schema
    # declares them (e.g. ``Int!`` and ``ID`` on this instance — NOT the GraphQL defaults). They
    # are emitted verbatim in the paginated query's variable declaration; declaring a nullable
    # ``$first: Int`` where the field expects ``Int!`` is a hard VariableTypeMismatch at the
    # gateway, so the real types must flow through from introspection.
    first_type: str = "Int"
    after_type: str = "String"


# Conservative offline shapes for the known extractable objects (spec §4 table), used only when
# live introspection is unavailable. ``supports_filter``/``supports_order`` default False for the
# operational/scalar objects so they degrade to a safe full load; feedback/invitations keep their
# incremental capability. ``scalar_fields`` is left empty offline (the node type is not known).
# ``first``/``after`` are ``Int!``/``ID`` across Medallia's Relay connections (verified live on
# this instance); the static fallback declares them so an introspection-disabled run still emits
# a gateway-valid variable declaration.
_STATIC_FIRST_TYPE = "Int!"
_STATIC_AFTER_TYPE = "ID"


def _static_shape(name: str, shape: str, has_id: bool, supports_filter: bool, supports_order: bool) -> ObjectShape:
    return ObjectShape(
        name,
        shape,
        has_id,
        supports_filter,
        supports_order,
        first_type=_STATIC_FIRST_TYPE,
        after_type=_STATIC_AFTER_TYPE,
    )


STATIC_OBJECT_SHAPES: dict[str, ObjectShape] = {
    "feedback": _static_shape("feedback", SHAPE_FIELDDATA, True, True, True),
    "invitations": _static_shape("invitations", SHAPE_FIELDDATA, True, True, True),
    "customers": _static_shape("customers", SHAPE_DATA, True, False, False),
    "programs": _static_shape("programs", SHAPE_SCALAR, True, False, False),
    "missingSocialURLs": _static_shape("missingSocialURLs", SHAPE_SCALAR, True, False, False),
    "socialURLs": _static_shape("socialURLs", SHAPE_SCALAR, False, False, False),
    "socialUrlsHealth": _static_shape("socialUrlsHealth", SHAPE_SCALAR, False, False, False),
    "unitWarnings": _static_shape("unitWarnings", SHAPE_SCALAR, False, False, False),
}


def _to_graphql(value: object) -> str:
    """Serialise a Python value into GraphQL argument-literal syntax.

    Object keys are emitted unquoted (GraphQL names — validated upstream); strings are
    JSON-escaped and quoted, so a scalar value can never break out of the query.
    """
    if isinstance(value, dict):
        parts = [f"{key}: {_to_graphql(val)}" for key, val in value.items()]
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, (list, tuple)):  # fmt: skip
        return "[" + ", ".join(_to_graphql(item) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):  # fmt: skip
        return json.dumps(value)
    if value is None:
        return "null"
    raise TypeError(f"Unsupported GraphQL literal type: {type(value)!r}")


class MedalliaTokenManager:
    """Mints and caches an OAuth2 client-credentials bearer token."""

    def __init__(
        self,
        instance_host: str,
        company_name: str,
        client_id: str,
        client_secret: str,
        session: requests.Session | None = None,
        expiry_skew_seconds: int = TOKEN_EXPIRY_SKEW_SECONDS,
        request_timeout: float = 30.0,
    ):
        self._token_url = f"https://{instance_host}/oauth/{company_name}/token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session or requests.Session()
        self._expiry_skew = expiry_skew_seconds
        self._request_timeout = request_timeout
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    @property
    def token_url(self) -> str:
        return self._token_url

    def get_token(self) -> str:
        """Return a valid token, minting a fresh one when missing or near expiry."""
        if self._access_token is None or time.time() >= (self._expires_at - self._expiry_skew):
            self._mint()
        if self._access_token is None:  # pragma: no cover - _mint sets the token or raises.
            raise MedalliaClientError("Medallia token could not be minted.")
        return self._access_token

    def invalidate(self) -> None:
        """Drop the cached token so the next ``get_token`` re-mints (used on a 401)."""
        self._access_token = None
        self._expires_at = 0.0

    def _mint(self) -> None:
        logging.info("Minting Medallia OAuth token.")
        try:
            response = self._session.post(
                self._token_url,
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                timeout=self._request_timeout,
            )
        except requests.RequestException as exc:
            raise MedalliaClientError(f"Failed to reach the Medallia token endpoint: {exc}") from exc

        if response.status_code == _HTTP_UNAUTHORIZED:
            raise UserException(
                "Medallia rejected the OAuth credentials (401). Check client_id / #client_secret, "
                "instance host and company name."
            )
        if 400 <= response.status_code < 500:
            # Any other 4xx from the token endpoint is a user-actionable configuration problem
            # (wrong host/tenant, or a client without permission), not an unexpected transport
            # fault — surface it as exit 1, not exit 2.
            raise UserException(
                f"Medallia token endpoint returned HTTP {response.status_code}. "
                "Check the instance host and company name (the token URL is "
                "https://<instance_host>/oauth/<company_name>/token); a 403 means the OAuth "
                "client may lack access to the token endpoint."
            )
        if response.status_code >= 400:
            raise MedalliaClientError(f"Medallia token endpoint returned HTTP {response.status_code}.")

        try:
            payload = response.json()
        except ValueError:
            # A 2xx with a non-JSON/empty body is almost always a wrong host/tenant (the request
            # hit something other than the OAuth token endpoint), i.e. user-actionable → exit 1,
            # never an uncaught JSONDecodeError → exit 2.
            raise UserException(
                f"Medallia token endpoint returned a non-JSON response (HTTP {response.status_code}). "
                "Check instance_host and company_name (the token URL is "
                "https://<instance_host>/oauth/<company_name>/token)."
            ) from None
        token = payload.get("access_token")
        if not token:
            raise MedalliaClientError("Medallia token response did not contain an access_token.")
        self._access_token = token
        # A non-numeric expires_in must not raise (an uncaught ValueError would surface as the
        # opaque exit code 2); fall back to the 1h default and let the token be used.
        try:
            expires_in = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):  # fmt: skip
            expires_in = 3600.0
        self._expires_at = time.time() + expires_in


# --------------------------------------------------------------------------------------------
# Introspection query + pure classifiers
# --------------------------------------------------------------------------------------------

# One-shot introspection: the Query-root fields (with their args, to detect filter/orderBy
# support) plus every type's field list (to detect a connection's ``nodes`` field and the node
# type's own fields). ``TypeRef`` unwraps NON_NULL/LIST wrappers to the named type.
INTROSPECTION_QUERY = """
query MedalliaIntrospect {
  __schema {
    queryType {
      fields {
        name
        args { name type { ...TypeRef } }
        type { ...TypeRef }
      }
    }
    types {
      name
      kind
      fields {
        name
        args { name }
        type { ...TypeRef }
      }
    }
  }
}
fragment TypeRef on __Type {
  kind
  name
  ofType { kind name ofType { kind name ofType { kind name } } }
}
"""


def _named_type(type_ref: dict[str, Any] | None) -> dict[str, Any]:
    """Descend a GraphQL TypeRef through NON_NULL/LIST wrappers to the named base type."""
    current = type_ref
    while current and current.get("ofType") is not None:
        current = current["ofType"]
    return current or {}


def _named_type_name(type_ref: dict[str, Any] | None) -> str | None:
    return _named_type(type_ref).get("name")


def _type_ref_str(type_ref: dict[str, Any] | None) -> str | None:
    """Render a GraphQL TypeRef as its schema string (e.g. ``Int!``, ``ID``, ``[String!]``).

    Used to declare the paginated query's ``$first`` / ``$after`` variables with the connection's
    ACTUAL argument types, so the gateway does not reject a nullable variable in a non-null slot.
    """
    if not type_ref:
        return None
    kind = type_ref.get("kind")
    if kind == "NON_NULL":
        inner = _type_ref_str(type_ref.get("ofType"))
        return f"{inner}!" if inner else None
    if kind == "LIST":
        inner = _type_ref_str(type_ref.get("ofType"))
        return f"[{inner}]" if inner else None
    return type_ref.get("name")


def _index_types(introspection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    schema = (introspection or {}).get("__schema") or {}
    return {t["name"]: t for t in (schema.get("types") or []) if t.get("name")}


def _query_root_fields(introspection: dict[str, Any]) -> list[dict[str, Any]]:
    schema = (introspection or {}).get("__schema") or {}
    return ((schema.get("queryType") or {}).get("fields")) or []


def list_extractable_objects(introspection: dict[str, Any]) -> list[str]:
    """Classify Query-root fields into the extractable-connection set (spec §6.2).

    A field is extractable iff its return OBJECT type exposes a ``nodes`` field (a Relay
    connection) AND it is not one of the metadata-catalog nodes. Non-connections
    (aggregate/utility/singleton) are dropped. Returns object names sorted for stable UX.
    """
    types = _index_types(introspection)
    extractable: list[str] = []
    for root_field in _query_root_fields(introspection):
        name = root_field.get("name")
        if not name or name in METADATA_CATALOG_DENYLIST:
            continue
        return_type = types.get(_named_type_name(root_field.get("type")))
        if not return_type:
            continue
        field_names = {f.get("name") for f in (return_type.get("fields") or [])}
        if "nodes" in field_names:
            extractable.append(name)
    return sorted(extractable)


def resolve_object_shape(object_name: str, introspection: dict[str, Any]) -> ObjectShape | None:
    """Resolve one object's node shape, id presence and filter/order support (spec §6.3).

    Returns ``None`` when the object is absent from the introspected schema (e.g. a manually
    typed object on an introspection-disabled instance); the caller then falls back to the
    static shape map.
    """
    types = _index_types(introspection)
    root_field = next((f for f in _query_root_fields(introspection) if f.get("name") == object_name), None)
    if root_field is None:
        return None
    arg_list = root_field.get("args") or []
    args = {a.get("name") for a in arg_list}
    arg_types = {a.get("name"): _type_ref_str(a.get("type")) for a in arg_list if a.get("name")}

    return_type = types.get(_named_type_name(root_field.get("type"))) or {}
    nodes_field = next((f for f in (return_type.get("fields") or []) if f.get("name") == "nodes"), None)
    if nodes_field is None:
        return None
    node_type_name = _named_type_name(nodes_field.get("type"))
    node_type = types.get(node_type_name) if node_type_name else None
    node_fields = (node_type or {}).get("fields") or []
    node_field_names = {f.get("name") for f in node_fields}

    if "fieldData" in node_field_names:
        shape = SHAPE_FIELDDATA
    elif "data" in node_field_names:
        shape = SHAPE_DATA
    else:
        shape = SHAPE_SCALAR

    scalar_fields: dict[str, str] = {}
    for node_field in node_fields:
        field_name = node_field.get("name")
        if not field_name or field_name in _RESERVED_NODE_FIELDS:
            continue
        # Skip accessor fields that REQUIRE arguments (e.g. the Contact node's value(fieldId),
        # aggregate(definition) …). Their return type is a scalar, but selecting them bare is a
        # MissingFieldArgument error at the gateway — they are not extractable leaf columns.
        if node_field.get("args"):
            continue
        base = _named_type(node_field.get("type"))
        if base.get("kind") in {"SCALAR", "ENUM"}:
            scalar_fields[field_name] = base.get("name") or "String"

    return ObjectShape(
        name=object_name,
        shape=shape,
        has_id="id" in node_field_names,
        supports_filter="filter" in args,
        supports_order="orderBy" in args,
        node_type=node_type_name,
        scalar_fields=scalar_fields,
        first_type=arg_types.get("first") or "Int",
        after_type=arg_types.get("after") or "String",
    )


# --------------------------------------------------------------------------------------------
# Generic query builder
# --------------------------------------------------------------------------------------------

_GRAPHQL_NAME_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def _assert_graphql_name(value: str, what: str) -> str:
    """Defence-in-depth: reject a non-GraphQL-name identifier before it is interpolated.

    Config validation already enforces this, but the builder is the last gate before an
    identifier is written into the query string, so it re-checks (never trust the caller).
    """
    if not value or value[0].isdigit() or not set(value) <= _GRAPHQL_NAME_ALLOWED:
        raise UserException(f"{what} '{value}' is not a valid Medallia identifier.")
    return value


# A GraphQL type reference — a name plus optional non-null (``!``) / list (``[]``) wrappers.
_GRAPHQL_TYPE_ALLOWED = _GRAPHQL_NAME_ALLOWED | set("![]")


def _assert_graphql_type(value: str, what: str) -> str:
    """Reject anything that is not a plain GraphQL type reference before it hits the header.

    ``first``/``after`` types come from introspection, but the builder is the last gate before
    they are written into the variable declaration, so it re-validates (never trust the caller).
    """
    if not value or value[0].isdigit() or not set(value) <= _GRAPHQL_TYPE_ALLOWED:
        raise UserException(f"{what} type '{value}' is not a valid GraphQL type reference.")
    return value


class GenericQueryBuilder:
    """Builds one Relay-connection GraphQL query for any object shape (spec §6.4).

    ``first`` / ``after`` are declared as GraphQL variables, so the same builder drives every
    object and the client's cursor paginator supplies the values — no string surgery.
    """

    def __init__(
        self,
        object_name: str,
        node_shape: str,
        selected_fields: list[str],
        scalar_fields: list[str],
        incremental_field: str | None,
        filter_tree: dict | None,
        supports_filter: bool,
        supports_order: bool,
        has_id: bool = True,
        first_type: str = "Int",
        after_type: str = "String",
    ):
        self._object_name = _assert_graphql_name(object_name, "data_object")
        self._node_shape = node_shape
        self._selected_fields = [_assert_graphql_name(f, "field") for f in selected_fields]
        self._scalar_fields = [_assert_graphql_name(f, "field") for f in scalar_fields]
        self._incremental_field = (
            _assert_graphql_name(incremental_field, "incremental_field") if incremental_field else None
        )
        self._filter_tree = filter_tree
        self._supports_filter = supports_filter
        self._supports_order = supports_order
        self._has_id = has_id
        # GraphQL type of the connection's first/after args (verbatim from introspection). The
        # names themselves are validated so a crafted type string cannot break out of the header.
        self._first_type = _assert_graphql_type(first_type, "first")
        self._after_type = _assert_graphql_type(after_type, "after")

    def _selection_entries(self) -> list[tuple[str, str]]:
        """``(output_column, selection_fragment)`` pairs, ``id`` first, de-duplicated by column.

        Bare node scalars (``scalar_fields``) are always selected bare; the user-picked
        ``selected_fields`` go through the shape's accessor (``fieldData``/``data``), except in
        the pure scalar shape where everything is a bare field name.

        The ``incremental_field`` is ALWAYS added to the selection (even when the user did not
        list it in ``fields``): the watermark advances off the field's value in each output row,
        so it must be fetched — otherwise the cursor never moves and every run re-reads the
        whole window. It de-dupes against an explicit selection, so it is added at most once.
        """
        selected = list(self._selected_fields)
        if self._node_shape == SHAPE_SCALAR:
            names = list(selected or self._scalar_fields)
            if self._incremental_field and self._incremental_field not in names:
                names.append(self._incremental_field)
            entries: list[tuple[str, str]] = [("id", "id")] if self._has_id else []
            entries.extend((name, name) for name in names)
        else:
            if (
                self._incremental_field
                and self._incremental_field not in selected
                and (self._incremental_field not in self._scalar_fields)
            ):
                selected.append(self._incremental_field)
            entries = [("id", "id")] if self._has_id else []
            entries.extend((name, name) for name in self._scalar_fields)
            for name in selected:
                if self._node_shape == SHAPE_FIELDDATA:
                    fragment = f"{name}: fieldData(fieldId: {json.dumps(name)}) {{ values }}"
                else:  # SHAPE_DATA
                    fragment = f"{name}: data(fieldId: {json.dumps(name)}) {{ value values }}"
                entries.append((name, fragment))

        seen: set[str] = set()
        deduped: list[tuple[str, str]] = []
        for column, fragment in entries:
            if column and column not in seen:
                seen.add(column)
                deduped.append((column, fragment))
        if not deduped:
            raise UserException(
                f"No fields to select for '{self._object_name}'. Choose at least one field, "
                "or the object exposes no scalar fields to extract."
            )
        return deduped

    def selection_columns(self) -> list[str]:
        """Output column names in query order (``id`` first when present)."""
        return [column for column, _ in self._selection_entries()]

    def _build_selection(self) -> str:
        return " ".join(fragment for _, fragment in self._selection_entries())

    def _build_filter(self, lower_bound: int | str | None, upper_bound: int | str | None) -> dict | None:
        clauses: list[dict] = []
        if self._filter_tree:
            clauses.append(self._filter_tree)
        if self._incremental_field and self._supports_filter:
            if upper_bound is not None:
                clauses.append({"fieldIds": [self._incremental_field], "lt": str(upper_bound)})
            # ``gte`` (not ``gt``): pagination is cursor-driven, so re-reading the boundary
            # record is harmless (deduped by the PK upsert) and guarantees no same-value record
            # is ever missed.
            if lower_bound is not None:
                clauses.append({"fieldIds": [self._incremental_field], "gte": str(lower_bound)})
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"and": clauses}

    def build_query(self, lower_bound: int | str | None = None, upper_bound: int | str | None = None) -> str:
        """Return the GraphQL query string for the connection (variable-paginated)."""
        args = ["first: $first", "after: $after"]
        filter_tree = self._build_filter(lower_bound, upper_bound)
        if filter_tree is not None:
            args.append(f"filter: {_to_graphql(filter_tree)}")
        if self._supports_order and self._incremental_field:
            args.append(f"orderBy: [{{fieldId: {json.dumps(self._incremental_field)}, direction: ASC}}]")
        return (
            f"query ($first: {self._first_type}, $after: {self._after_type}) {{ "
            f"{self._object_name}({', '.join(args)}) {{ "
            f"nodes {{ {self._build_selection()} }} "
            f"pageInfo {{ hasNextPage endCursor }} }} }}"
        )


# --------------------------------------------------------------------------------------------
# HTTP client + Relay cursor paginator
# --------------------------------------------------------------------------------------------

# Cursor value types that carry a number: a NON-NULL one (e.g. ``Int!``) is seeded with 0 on
# page 1 (it cannot be null), and a numeric ``endCursor`` string is coerced back to int for the
# following page's variable.
_NUMERIC_GRAPHQL_TYPES = frozenset({"Int", "Long", "Float", "BigInt", "BigDecimal"})


class MedalliaClient:
    """POSTs GraphQL to the Medallia API gateway with auth, throttling and cursor pagination."""

    def __init__(
        self,
        api_host: str,
        token_manager: MedalliaTokenManager,
        session: requests.Session | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max: float = DEFAULT_BACKOFF_MAX_SECONDS,
        request_timeout: float = 95.0,
    ):
        self._query_url = f"https://{api_host}/data/v0/query"
        self._token_manager = token_manager
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._request_timeout = request_timeout

    @property
    def query_url(self) -> str:
        return self._query_url

    def paginate(
        self,
        query: str,
        page_size: int,
        extract_connection: Callable[[dict[str, Any]], dict[str, Any]],
        after_type: str = "String",
    ) -> Iterator[dict[str, Any]]:
        """Yield nodes across Relay cursor pages driven by ``pageInfo.hasNextPage``.

        ``first`` / ``after`` travel as GraphQL variables. ``extract_connection`` selects the
        single connection object from the response ``data`` (by object name in structured mode,
        or by single-connection detection in raw mode). ``totalCount`` is never consulted.

        ``after_type`` is the connection's declared cursor type. A NULLABLE cursor (``ID`` /
        ``String`` / ``Int``) starts as ``null`` — the Relay first-page convention. A NON-NULL
        cursor cannot take ``null`` on page 1: some Medallia connections (``socialURLs``,
        ``unitWarnings``, ``missingSocialURLs``, ``socialUrlsHealth``) type ``after`` as ``Int!``,
        and passing null 500s the gateway ("coerced Null value for NonNull type 'Int!'"). Such a
        cursor is seeded with the type's zero value (``0`` for Int), and a numeric ``endCursor``
        string is coerced back to ``int`` so the NonNull-Int variable accepts it on later pages.
        """
        non_null = after_type.endswith("!")
        numeric_cursor = (after_type[:-1] if non_null else after_type) in _NUMERIC_GRAPHQL_TYPES
        after: int | str | None = (0 if numeric_cursor else "") if non_null else None
        pages = 0
        while True:
            data = self._post_graphql(query, variables={"first": page_size, "after": after})
            connection = extract_connection(data)
            nodes = connection.get("nodes") or []
            pages += 1
            logging.info("Fetched page %s: %s node(s).", pages, len(nodes))
            yield from nodes

            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                logging.warning("hasNextPage is true but endCursor is empty; stopping to avoid a loop.")
                break
            if numeric_cursor and isinstance(after, str) and after.lstrip("-").isdigit():
                after = int(after)  # a NonNull-Int cursor variable rejects a numeric STRING endCursor

    def fetch_object(
        self, object_name: str, query: str, page_size: int, after_type: str = "String"
    ) -> Iterator[dict[str, Any]]:
        """Paginate a structured-mode connection selected by its Query-root field name."""
        return self.paginate(query, page_size, lambda data: data.get(object_name) or {}, after_type=after_type)

    def run_metadata_query(
        self, query: str, compute_cost_only: bool = False, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute an arbitrary top-level GraphQL query (sync actions / cost pre-flight).

        With ``compute_cost_only`` the query is validated and priced without executing or
        consuming quota. Returns the ``data`` object.
        """
        return self._post_graphql(query, compute_cost_only=compute_cost_only, variables=variables)

    def run_introspection(self) -> dict[str, Any]:
        """Return the ``__schema`` introspection ``data`` (powers object/field discovery)."""
        return self._post_graphql(INTROSPECTION_QUERY)

    def _post_graphql(
        self, query: str, compute_cost_only: bool = False, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = {"compute_cost_only": "true"} if compute_cost_only else None
        payload = self._request_with_retries(query, params, variables)
        self._raise_for_graphql_errors(payload, compute_cost_only=compute_cost_only)
        return payload.get("data") or {}

    def _request_with_retries(
        self, query: str, params: dict[str, str] | None, variables: dict[str, Any] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query}
        if variables is not None:
            body["variables"] = variables
        attempt = 0
        reminted = False
        while True:
            token = self._token_manager.get_token()
            try:
                response = self._session.post(
                    self._query_url,
                    params=params,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self._request_timeout,
                )
            except requests.RequestException as exc:
                attempt += 1
                if attempt > self._max_retries:
                    raise MedalliaClientError(f"Medallia request failed after retries: {exc}") from exc
                self._sleep(self._backoff_seconds(attempt))
                continue

            if response.status_code == _HTTP_UNAUTHORIZED:
                if reminted:
                    raise UserException(
                        "Medallia returned 401 after re-minting the token. The OAuth client may lack Query API access."
                    )
                logging.info("Received 401; re-minting token and retrying once.")
                self._token_manager.invalidate()
                reminted = True
                continue

            if response.status_code in _RETRYABLE_STATUS:
                attempt += 1
                if attempt > self._max_retries:
                    raise MedalliaClientError(
                        f"Medallia returned HTTP {response.status_code} after {self._max_retries} retries."
                    )
                self._sleep(self._retry_delay(response, attempt))
                continue

            if 400 <= response.status_code < 500:
                # A non-retryable 4xx (e.g. 400/404 wrong api_host or query path, 403 no
                # Query-API access) is user-actionable → exit 1, not an exit-2 crash.
                raise UserException(
                    f"Medallia Query API returned HTTP {response.status_code}. "
                    "Check the API host (requests go to https://<api_host>/data/v0/query); "
                    "a 403 means the OAuth client may lack Query API access."
                )
            if response.status_code >= 400:
                raise MedalliaClientError(f"Medallia returned unexpected HTTP {response.status_code}.")

            self._apply_rate_limit(response.headers)
            try:
                return response.json()
            except ValueError:
                # A 2xx with a non-JSON/empty body means the request did not reach the Query API
                # (wrong api_host, or a proxy/HTML error page); user-actionable → exit 1, never an
                # uncaught JSONDecodeError → exit 2.
                raise UserException(
                    f"Medallia Query API returned a non-JSON response (HTTP {response.status_code}). "
                    "Check the API host (requests go to https://<api_host>/data/v0/query)."
                ) from None

    @staticmethod
    def _raise_for_graphql_errors(payload: dict[str, Any], compute_cost_only: bool = False) -> None:
        """Raise on real GraphQL errors; tolerate the two known non-fatal ones.

        * ``Invalid field id: <x>`` — a selected field is not valid for the entity, yet the
          valid data is still returned. Logged as a warning.
        * ``Estimated query cost is: <n>`` — the compute_cost_only pre-flight's success signal
          (only tolerated when ``compute_cost_only`` was requested).
        """
        errors = payload.get("errors")
        if not errors:
            return
        fatal: list[str] = []
        for err in errors:
            message = str(err.get("message", err) if isinstance(err, dict) else err)
            if INVALID_FIELD_ID_MARKER in message:
                logging.warning("Medallia reported a non-fatal field error (data still returned): %s", message)
            elif compute_cost_only and COST_ONLY_MARKER in message:
                logging.info("compute_cost_only pre-flight succeeded: %s", message)
            else:
                fatal.append(message)
        if fatal:
            raise UserException(f"Medallia GraphQL query returned errors: {'; '.join(fatal)}")

    def _retry_delay(self, response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self._backoff_seconds(attempt)

    def _backoff_seconds(self, attempt: int) -> float:
        return min(self._backoff_base * (2 ** (attempt - 1)), self._backoff_max)

    def _apply_rate_limit(self, headers: Mapping[str, str]) -> None:
        """Slow down when a live ``X-RateLimit-Remaining-*`` header approaches zero."""
        for header in ("X-RateLimit-Remaining-second", "X-RateLimit-Remaining-day"):
            raw = headers.get(header)
            if raw is None:
                continue
            try:
                remaining = int(raw)
            except ValueError:
                continue
            if remaining <= RATE_LIMIT_REMAINING_FLOOR:
                logging.info("Rate-limit header %s=%s low; pausing.", header, remaining)
                self._sleep(RATE_LIMIT_PAUSE_SECONDS)
                return

    @staticmethod
    def _sleep(seconds: float) -> None:
        time.sleep(seconds)


# --------------------------------------------------------------------------------------------
# Pure helpers — flatten, row hash, watermark
# --------------------------------------------------------------------------------------------


def _from_values(values: list | None) -> Any:
    """Collapse a ``values`` list: 0 → None, 1 → scalar, >1 → JSON-encoded list."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return json.dumps(values)


def _flatten_value(value: Any) -> Any:
    """Flatten one node field value by shape detection (spec §4)."""
    if isinstance(value, dict):
        if "value" in value:  # shape (b): data(fieldId){value values}
            scalar = value.get("value")
            return scalar if scalar is not None else _from_values(value.get("values"))
        if "values" in value:  # shape (a): fieldData(fieldId){values}
            return _from_values(value.get("values"))
        return json.dumps(value)  # nested object → JSON
    if isinstance(value, list):
        return json.dumps(value)
    return value  # shape (c): bare scalar


def flatten_node(node: dict[str, Any]) -> dict[str, Any]:
    """Map one GraphQL node to one flat output row, detecting each value's shape."""
    return {key: _flatten_value(value) for key, value in node.items()}


def row_hash(row: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 hex of a flattened row (id-less PK, spec §7).

    Keys are sorted and values stringified so an identical source row always yields an
    identical hash (cross-run stable ⇒ upsert dedupes). ``_row_hash`` itself is excluded.
    """
    material = {key: ("" if value is None else str(value)) for key, value in row.items() if key != "_row_hash"}
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def advance_watermark(current: int | str | None, candidate_raw: Any, is_int: bool) -> int | str | None:
    """Return the max of ``current`` and a row's incremental-field value (spec §8).

    ``is_int`` selects numeric vs. lexicographic (ISO string) comparison. An unusable candidate
    (missing / non-numeric for an int field) leaves the watermark unchanged.
    """
    if candidate_raw is None or candidate_raw == "":
        return current
    if is_int:
        try:
            candidate: int | str = int(candidate_raw)
        except (TypeError, ValueError):  # fmt: skip
            return current
    else:
        candidate = str(candidate_raw)
    if current is None:
        return candidate
    if isinstance(candidate, int) and isinstance(current, int):
        return candidate if candidate > current else current
    # ISO strings compare lexicographically; a mixed pair (a field format changed between runs)
    # falls back to a string compare so the guard never raises.
    return candidate if str(candidate) > str(current) else current
