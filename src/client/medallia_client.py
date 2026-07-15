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
from collections.abc import Iterator
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


class MedalliaClientError(Exception):
    """Unexpected/transport failure that is NOT user-actionable (maps to exit code 2)."""


@dataclass(frozen=True)
class Watermark:
    """Composite keyset cursor: (finish-date epoch seconds, survey id)."""

    finish_date_epoch: int
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
        return self._access_token  # type: ignore[return-value]

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
    ):
        self._data_object = data_object
        self._survey_field = survey_id_field_id
        self._finish_field = finish_date_field_id
        self._fields = fields
        self._business_filters = business_filters

    @property
    def data_object(self) -> str:
        return self._data_object

    @property
    def finish_date_field_id(self) -> str:
        return self._finish_field

    def build_query(self, lower_bound: Watermark | None, end_epoch: int, page_size: int) -> str:
        """Return the GraphQL query string for one keyset page."""
        selection = self._build_selection()
        filter_arg = _to_graphql(self._build_filter(lower_bound, end_epoch))
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
        parts = [
            f"surveyId: fieldData(fieldId: {json.dumps(self._survey_field)}) {{ values }}",
            f"{self._finish_field}: fieldData(fieldId: {json.dumps(self._finish_field)}) {{ values }}",
        ]
        reserved = {self._survey_field, self._finish_field}
        for field_id in self._fields:
            if field_id in reserved:
                continue
            parts.append(f"{field_id}: fieldData(fieldId: {json.dumps(field_id)}) {{ values }}")
        return " ".join(parts)

    def _build_filter(self, lower_bound: Watermark | None, end_epoch: int) -> dict:
        clauses: list[dict] = []
        if self._business_filters:
            clauses.append(self._business_filters)
        # Upper bound: this run's "now".
        clauses.append({"fieldIds": [self._finish_field], "lt": str(end_epoch)})
        # Composite exclusive lower bound: (finishDate, surveyId) > watermark.
        if lower_bound is not None:
            clauses.append(
                {
                    "or": [
                        {"fieldIds": [self._finish_field], "gt": str(lower_bound.finish_date_epoch)},
                        {
                            "and": [
                                {"fieldIds": [self._finish_field], "gte": str(lower_bound.finish_date_epoch)},
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

    def fetch_feedback(self, lower_bound: Watermark | None, end_epoch: int, page_size: int) -> Iterator[dict]:
        """Yield feedback nodes across keyset pages, advancing the in-memory watermark."""
        current = lower_bound
        page_index = 0
        while True:
            query = self._query_builder.build_query(current, end_epoch, page_size)
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

    def estimate_cost(self, lower_bound: Watermark | None, end_epoch: int, page_size: int) -> dict:
        """Run the feedback query in ``compute_cost_only`` mode (free, no quota consumed)."""
        query = self._query_builder.build_query(lower_bound, end_epoch, page_size)
        return self._post_graphql(query, compute_cost_only=True)

    def run_metadata_query(self, query: str) -> dict:
        """Execute an arbitrary top-level GraphQL query (used by sync actions)."""
        return self._post_graphql(query, unwrap_object=False)

    def _advance_watermark(self, node: dict, fallback: Watermark | None) -> Watermark | None:
        return watermark_from_node(node, self._query_builder.finish_date_field_id, fallback)

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
        errors = payload.get("errors")
        if errors:
            messages = "; ".join(str(err.get("message", err)) for err in errors)
            raise UserException(f"Medallia GraphQL query returned errors: {messages}")

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

    def _apply_rate_limit(self, headers: object) -> None:
        """Slow down when a live ``X-RateLimit-Remaining-*`` header approaches zero."""
        for header in ("X-RateLimit-Remaining-second", "X-RateLimit-Remaining-day"):
            raw = headers.get(header) if hasattr(headers, "get") else None
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


def watermark_from_node(node: dict, finish_date_field_id: str, fallback: Watermark | None = None) -> Watermark | None:
    """Extract the composite ``(finishDate epoch, surveyId)`` watermark from a node."""
    survey_values = (node.get("surveyId") or {}).get("values") or []
    finish_values = (node.get(finish_date_field_id) or {}).get("values") or []
    if not survey_values or not finish_values:
        return fallback
    try:
        return Watermark(finish_date_epoch=int(finish_values[0]), survey_id=str(survey_values[0]))
    except TypeError, ValueError:
        return fallback


def flatten_node(node: dict, columns: list[str]) -> dict:
    """Map one GraphQL node to one output row.

    Single-element ``values`` → scalar; empty → None; multi-element → JSON-encoded string.
    """
    row: dict[str, object] = {}
    for column in columns:
        values = (node.get(column) or {}).get("values")
        if not values:
            row[column] = None
        elif len(values) == 1:
            row[column] = values[0]
        else:
            row[column] = json.dumps(values)
    return row
