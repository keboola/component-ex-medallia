"""Medallia Experience Cloud — Query API (GraphQL) client.

Separated from ``component.py`` per the design spec (§6). Contains four collaborating
pieces:

* ``MedalliaTokenManager`` — OAuth2 client-credentials token minting with pre-expiry and
  401-driven re-mint.
* ``MedalliaQueryBuilder`` — assembles the ``feedback`` GraphQL query: ``fieldData``
  selection, the composite keyset watermark filter tree, ``orderBy`` (oldest→newest) and
  the ``first`` page-size argument.
* ``MedalliaClient`` — POSTs GraphQL to the API gateway with bearer auth, cost-aware
  throttling (``X-RateLimit-*``), exponential backoff on 429/5xx (honouring
  ``Retry-After``), a single 401 re-mint+retry, and a keyset paginator.
* ``flatten_node`` — maps one GraphQL node to one output row.

Neither the client secret nor the access token is ever logged.
"""

import json
import logging
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

import requests
from keboola.component.exceptions import UserException

# Token is re-minted when it expires within this safety window (Medallia's own
# recommended "refresh if expiring in the next 5 minutes" pattern).
TOKEN_EXPIRY_SKEW_SECONDS = 300

# Backoff / retry defaults for transient (429 / 5xx) failures.
DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_MAX_SECONDS = 60.0

# When a rate-limit "remaining" header drops to this floor or below, pause briefly to let
# the window reset rather than racing into a 429.
RATE_LIMIT_REMAINING_FLOOR = 1
RATE_LIMIT_PAUSE_SECONDS = 1.0

_HTTP_UNAUTHORIZED = 401
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Substring of the non-fatal per-field GraphQL error Medallia returns when a selected field
# is not valid for the queried entity. The valid data still comes back, so these are logged
# and skipped rather than raised (mirrors the reference extractor).
INVALID_FIELD_ID_MARKER = "Invalid field id:"


class MedalliaClientError(Exception):
    """Unexpected/transport failure that is NOT user-actionable (maps to exit code 2)."""


# Finish-date watermark field value formats (mirror ``configuration.FinishDateFieldType``;
# kept as bare strings here so the client stays free of a config-module import).
FINISH_FIELD_TYPE_EPOCH = "epoch"
FINISH_FIELD_TYPE_DATETIME = "datetime"


@dataclass(frozen=True)
class Watermark:
    """Composite keyset cursor: (finish-date value, survey id).

    ``finish_date_value`` is stored in the watermark field's native format — an ``int``
    for epoch-seconds fields, a ``str`` for ISO date/datetime fields. It is never coerced
    to ``int``, so pointing the watermark at a DATETIME field does not crash or stall.
    """

    finish_date_value: int | str
    survey_id: str


def _to_graphql(value: object) -> str:
    """Serialise a Python value into GraphQL argument-literal syntax.

    Object keys are emitted unquoted (GraphQL names); strings are JSON-escaped and quoted.
    All Medallia filter-tree leaf scalars are strings or booleans, so no unquoted enum
    handling is needed here (``orderBy`` directions are emitted separately).
    """
    if isinstance(value, dict):
        parts = [f"{key}: {_to_graphql(val)}" for key, val in value.items()]
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_to_graphql(item) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (int, float)):
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
        if response.status_code >= 400:
            raise MedalliaClientError(f"Medallia token endpoint returned HTTP {response.status_code}.")

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise MedalliaClientError("Medallia token response did not contain an access_token.")
        self._access_token = token
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))


class MedalliaQueryBuilder:
    """Builds the ``feedback`` keyset GraphQL query for a given watermark window."""

    def __init__(
        self,
        data_object: str,
        survey_id_field_id: str,
        finish_date_field_id: str,
        fields: list[str],
        business_filters: dict | None = None,
        finish_date_field_type: str = FINISH_FIELD_TYPE_EPOCH,
    ):
        self._data_object = data_object
        self._survey_field = survey_id_field_id
        self._finish_field = finish_date_field_id
        self._fields = fields
        self._business_filters = business_filters
        self._finish_field_type = finish_date_field_type

    @property
    def data_object(self) -> str:
        return self._data_object

    @property
    def finish_date_field_id(self) -> str:
        return self._finish_field

    @property
    def finish_date_field_type(self) -> str:
        return self._finish_field_type

    def build_query(self, lower_bound: Watermark | None, end_bound: int | str, page_size: int) -> str:
        """Return the GraphQL query string for one keyset page.

        ``end_bound`` is the run's upper watermark bound in the field's native format
        (epoch seconds for epoch fields, an ISO date/datetime string for datetime fields).
        """
        selection = self._build_selection()
        filter_arg = _to_graphql(self._build_filter(lower_bound, end_bound))
        order_by = (
            f"[{{fieldId: {json.dumps(self._finish_field)}, direction: ASC}}, "
            f"{{fieldId: {json.dumps(self._survey_field)}, direction: ASC}}]"
        )
        return (
            f"query {{ {self._data_object}("
            f"first: {int(page_size)}, "
            f"filter: {filter_arg}, "
            f"orderBy: {order_by}"
            f") {{ totalCount nodes {{ {selection} }} }} }}"
        )

    def _build_selection(self) -> str:
        # ``id`` is the node's direct scalar identifier (a bare field, NOT fieldData) — the
        # reference extractor selects it on feedback/invitations nodes for a stable record id.
        parts = [
            "id",
            f"surveyId: fieldData(fieldId: {json.dumps(self._survey_field)}) {{ values }}",
            f"{self._finish_field}: fieldData(fieldId: {json.dumps(self._finish_field)}) {{ values }}",
        ]
        reserved = {self._survey_field, self._finish_field}
        for field_id in self._fields:
            if field_id in reserved:
                continue
            parts.append(f"{field_id}: fieldData(fieldId: {json.dumps(field_id)}) {{ values }}")
        return " ".join(parts)

    def _build_filter(self, lower_bound: Watermark | None, end_bound: int | str) -> dict:
        clauses: list[dict] = []
        if self._business_filters:
            clauses.append(self._business_filters)
        # Upper bound: this run's "now", emitted in the field's own format (str() covers
        # both an int epoch and an ISO date/datetime string).
        clauses.append({"fieldIds": [self._finish_field], "lt": str(end_bound)})
        # Composite exclusive lower bound: (finishDate, surveyId) > watermark. Bound values
        # are emitted in the same native format the field expects (epoch string or ISO string).
        if lower_bound is not None:
            clauses.append(
                {
                    "or": [
                        {"fieldIds": [self._finish_field], "gt": str(lower_bound.finish_date_value)},
                        {
                            "and": [
                                {"fieldIds": [self._finish_field], "gte": str(lower_bound.finish_date_value)},
                                {"fieldIds": [self._survey_field], "gt": str(lower_bound.survey_id)},
                            ]
                        },
                    ]
                }
            )
        return {"and": clauses}


class MedalliaClient:
    """POSTs GraphQL to the Medallia API gateway with auth, throttling and pagination."""

    def __init__(
        self,
        api_host: str,
        token_manager: MedalliaTokenManager,
        query_builder: MedalliaQueryBuilder,
        session: requests.Session | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max: float = DEFAULT_BACKOFF_MAX_SECONDS,
        request_timeout: float = 95.0,
    ):
        self._query_url = f"https://{api_host}/data/v0/query"
        self._token_manager = token_manager
        self._query_builder = query_builder
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._request_timeout = request_timeout

    @property
    def query_url(self) -> str:
        return self._query_url

    def fetch_feedback(self, lower_bound: Watermark | None, end_bound: int | str, page_size: int) -> Iterator[dict]:
        """Yield feedback nodes across keyset pages, advancing the in-memory watermark."""
        current = lower_bound
        page_index = 0
        while True:
            query = self._query_builder.build_query(current, end_bound, page_size)
            data_object = self._post_graphql(query)
            page_index += 1
            nodes = data_object.get("nodes") or []
            total_count = int(data_object.get("totalCount", len(nodes)))
            logging.info("Fetched page %s: %s node(s), totalCount=%s.", page_index, len(nodes), total_count)

            if not nodes:
                break
            yield from nodes
            current = self._advance_watermark(nodes[-1], current)

            if total_count < page_size:
                break

    def run_metadata_query(self, query: str, compute_cost_only: bool = False) -> dict:
        """Execute an arbitrary top-level GraphQL query (used by sync actions).

        With ``compute_cost_only`` the query is validated and priced without executing or
        consuming quota — used for a cheap connection pre-flight.
        """
        return self._post_graphql(query, compute_cost_only=compute_cost_only, unwrap_object=False)

    def _advance_watermark(self, node: dict, fallback: Watermark | None) -> Watermark | None:
        return watermark_from_node(
            node,
            self._query_builder.finish_date_field_id,
            fallback,
            field_type=self._query_builder.finish_date_field_type,
        )

    def _post_graphql(self, query: str, compute_cost_only: bool = False, unwrap_object: bool = True) -> dict:
        params = {"compute_cost_only": "true"} if compute_cost_only else None
        payload = self._request_with_retries(query, params)
        self._raise_for_graphql_errors(payload)

        data = payload.get("data") or {}
        if not unwrap_object:
            return data
        return data.get(self._query_builder.data_object) or {}

    def _request_with_retries(self, query: str, params: dict | None) -> dict:
        attempt = 0
        reminted = False
        while True:
            token = self._token_manager.get_token()
            try:
                response = self._session.post(
                    self._query_url,
                    params=params,
                    json={"query": query},
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

            if response.status_code >= 400:
                raise MedalliaClientError(f"Medallia returned unexpected HTTP {response.status_code}.")

            self._apply_rate_limit(response.headers)
            return response.json()

    @staticmethod
    def _raise_for_graphql_errors(payload: dict) -> None:
        """Raise on real GraphQL errors; tolerate non-fatal ``Invalid field id:`` ones.

        Medallia returns a per-field ``Invalid field id: <x>`` error when a selected or
        auto-discovered field is not valid for the entity, yet still returns the valid
        data. Such errors are logged as warnings; only OTHER errors abort the run.
        """
        errors = payload.get("errors")
        if not errors:
            return
        fatal: list[str] = []
        for err in errors:
            message = str(err.get("message", err) if isinstance(err, dict) else err)
            if INVALID_FIELD_ID_MARKER in message:
                logging.warning("Medallia reported a non-fatal field error (data still returned): %s", message)
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


def _coerce_finish_value(raw: object, field_type: str) -> int | str | None:
    """Coerce a raw finish-date value into the field's native watermark type.

    ``datetime`` fields keep the string as-is (ISO 8601 sorts correctly). ``epoch`` fields
    parse to ``int``; a non-numeric epoch value is treated as unusable (``None``) so the
    caller keeps the previous watermark rather than crashing.
    """
    if field_type == FINISH_FIELD_TYPE_DATETIME:
        return str(raw)
    try:
        return int(str(raw))
    except (TypeError, ValueError):  # fmt: skip
        return None


def _is_after(candidate: Watermark, current: Watermark) -> bool:
    """True if ``candidate`` sorts strictly after ``current`` on ``(finish, surveyId)``.

    Epoch values compare numerically, ISO date/datetime strings lexicographically. Mismatched
    value types (e.g. the field type changed between runs) fall back to a string comparison so
    the guard never raises.
    """
    cand_finish = candidate.finish_date_value
    curr_finish = current.finish_date_value
    if isinstance(cand_finish, int) and isinstance(curr_finish, int):
        if cand_finish != curr_finish:
            return cand_finish > curr_finish
    else:
        cand_str, curr_str = str(cand_finish), str(curr_finish)
        if cand_str != curr_str:
            return cand_str > curr_str
    # Finish values tie → break on surveyId.
    return candidate.survey_id > current.survey_id


def watermark_from_node(
    node: dict,
    finish_date_field_id: str,
    fallback: Watermark | None = None,
    field_type: str = FINISH_FIELD_TYPE_EPOCH,
) -> Watermark | None:
    """Extract the composite ``(finishDate, surveyId)`` watermark from a node.

    The finish-date value is stored in its native format (int epoch or ISO string) and the
    watermark advances monotonically: the candidate replaces the fallback only when it sorts
    strictly after it, so out-of-order or duplicate boundary records never regress the cursor.
    """
    survey_values = (node.get("surveyId") or {}).get("values") or []
    finish_values = (node.get(finish_date_field_id) or {}).get("values") or []
    if not survey_values or not finish_values:
        return fallback
    value = _coerce_finish_value(finish_values[0], field_type)
    if value is None:
        return fallback
    candidate = Watermark(finish_date_value=value, survey_id=str(survey_values[0]))
    if fallback is None:
        return candidate
    return candidate if _is_after(candidate, fallback) else fallback


def flatten_node(node: dict, columns: list[str]) -> dict:
    """Map one GraphQL node to one output row.

    ``fieldData`` columns arrive as ``{"values": [...]}`` — single-element → scalar, empty →
    None, multi-element → JSON-encoded string. A direct scalar column (e.g. the node ``id``)
    arrives as a bare value and is copied through as-is (missing → None).
    """
    row: dict[str, object] = {}
    for column in columns:
        raw = node.get(column)
        if isinstance(raw, dict):
            values = raw.get("values")
            if not values:
                row[column] = None
            elif len(values) == 1:
                row[column] = values[0]
            else:
                row[column] = json.dumps(values)
        else:
            row[column] = raw
    return row
