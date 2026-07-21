"""Typed Pydantic configuration models for the generic Medallia Query API extractor.

The component always receives a single platform-merged ``config.json``. Root-level
connection/auth parameters and row-level object parameters therefore arrive in the same
``parameters`` dict, so both models parse from that merged dict and ignore the keys that
belong to the other model (``extra="ignore"``).

Redesign (spec §5.2): the row is generic over every paginated Medallia connection. It has
two modes — ``structured`` (pick an object, its fields, incremental settings and filters)
and ``raw`` (a user-authored GraphQL query). Every field fed by an async sync action has a
safe empty default and NO empty-rejecting validator, because the sync-action machinery
instantiates the config on a half-filled form; presence is validated inside ``run()`` / the
action method, not on the model.
"""

import json
import logging
import re
from enum import StrEnum

from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field, field_validator

# GraphQL name pattern — Medallia field IDs and object names are interpolated into the query
# string (as ``fieldData``/``data`` arguments, aliases and the connection field name), so they
# must be valid GraphQL names. Validating here prevents any query injection through a crafted
# field ID / object name.
_GRAPHQL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Output table name pattern. The value becomes ``<output_table>.csv`` under ``data/out/tables``,
# so it must be a bare filesystem-safe slug — letters, digits, underscores, hyphens — with NO
# path separator or other special character, so a crafted name cannot escape the output dir.
_SAFE_TABLE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")

# Medallia API hard ceiling for the ``first`` page-size argument (reference repo:
# MAX_RECORDS_PER_REQUEST = 1000). The configured value is clamped to this.
MAX_PAGE_SIZE = 1000

# Component-level default page size. Matches the reference extractor's default (100): a
# modest page is gentler on the live instance / 24h quota than the API ceiling while still
# keeping the request count low. Still clamped to ``MAX_PAGE_SIZE``.
DEFAULT_PAGE_SIZE = 100


class Mode(StrEnum):
    """Row extraction mode (spec §5.2)."""

    structured = "structured"
    raw = "raw"


class LoadType(StrEnum):
    full_load = "full_load"
    incremental_load = "incremental_load"


def _validate_graphql_name(value: str, field_name: str) -> str:
    if not _GRAPHQL_NAME.match(value):
        raise UserException(
            f"{field_name} '{value}' is not a valid Medallia identifier (must match {_GRAPHQL_NAME.pattern})."
        )
    return value


def _validate_filter_keys(node: object) -> None:
    """Assert every object key in a user ``filters`` tree is a valid GraphQL name.

    The query builder emits object keys UNQUOTED as GraphQL names, so an invalid key in the
    user-provided ``filters`` object could corrupt or break out of the generated query (a
    query-injection vector). Every legitimate Medallia filter operator — ``and``/``or``/
    ``not``/``fieldIds``/``in``/``gt``/``gte``/``lt``/``lte``/``isNull``/``eq`` — is a valid
    GraphQL name, so this rejects only malformed/crafted keys. Scalar VALUES need no check:
    the builder JSON-escapes and quotes them, so they cannot break out of the query.
    """
    if isinstance(node, dict):
        for key, val in node.items():
            if not _GRAPHQL_NAME.match(str(key)):
                raise UserException(
                    f"filters contains an invalid key '{key}'; filter object keys must be GraphQL "
                    f"names matching {_GRAPHQL_NAME.pattern} (e.g. and, or, fieldIds, gt, lt)."
                )
            _validate_filter_keys(val)
    elif isinstance(node, list):
        for item in node:
            _validate_filter_keys(item)


class Configuration(BaseModel):
    """Root (config-level) connection and authentication parameters (unchanged from v1)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    instance_host: str = Field(..., description="Reporting instance host for the OAuth token endpoint.")
    company_name: str = Field(..., description="The <companyName> OAuth path segment (tenant).")
    api_host: str = Field(..., description="The .apis.medallia.com gateway host for the Query API.")
    client_id: str = Field(..., description="OAuth client id (non-secret).")
    client_secret: str = Field(..., alias="#client_secret", description="OAuth client secret (encrypted).")

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            # ``from None`` (never ``from e``): a Pydantic ValidationError carries
            # ``input_value`` — the full merged config, including the decrypted
            # ``#client_secret`` and tenant host. Chaining it would surface that value in the
            # traceback that ``logging.exception`` prints to the customer-visible job log.
            # Suppress the chain and raise a value-free message instead.
            raise UserException(_format_validation_error(e)) from None


class RowConfiguration(BaseModel):
    """Row-level parameters — one config row per Medallia object / raw query / output table."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    mode: Mode = Mode.structured
    # Structured mode.
    data_object: str = Field(default="", description="Medallia connection to extract (structured mode).")
    fields: list[str] = Field(default_factory=list, description="Field IDs to extract; empty ⇒ all scalar fields.")
    load_type: LoadType = LoadType.incremental_load
    incremental_field: str = Field(default="", description="Date/int field ID driving the incremental watermark.")
    initial_start: str = Field(
        default="",
        description="First-run lower bound: ISO date, epoch seconds, or a relative expression (e.g. '5 days ago').",
    )
    filters: str = Field(default="", description="Optional Medallia filter tree as a JSON string.")
    # Raw mode.
    raw_query: str = Field(default="", description="Raw GraphQL query (raw mode).")
    # Both modes.
    output_table: str = Field(
        default="",
        description="Optional output table name; defaults to the data object name (structured mode). "
        "Required in raw mode, where there is no object name to derive it from.",
    )
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1)

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            # See Configuration.__init__: ``from None`` keeps the decrypted secret / config in
            # the chained ValidationError's ``input_value`` out of the log.
            raise UserException(_format_validation_error(e)) from None

    @field_validator("data_object")
    @classmethod
    def _validate_object_name(cls, value: str) -> str:
        # Empty is allowed (async-fed / half-filled form); validate only a supplied value.
        return _validate_graphql_name(value, "data_object") if value else value

    @field_validator("incremental_field")
    @classmethod
    def _validate_incremental_field(cls, value: str) -> str:
        return _validate_graphql_name(value, "incremental_field") if value else value

    @field_validator("output_table")
    @classmethod
    def _validate_output_table(cls, value: str) -> str:
        # Optional in both modes; empty is allowed (a half-filled async form, and structured mode
        # derives the name from the object). Raw-mode presence is checked in run(), not here.
        name = value.strip()
        if name and not _SAFE_TABLE_NAME.match(name):
            raise UserException(
                f"output_table '{value}' is not a valid table name; use letters, digits, underscores "
                "or hyphens only (no path separators)."
            )
        return value

    @field_validator("fields")
    @classmethod
    def _validate_field_ids(cls, value: list[str]) -> list[str]:
        for field_id in value:
            _validate_graphql_name(field_id, "fields entry")
        return value

    @field_validator("page_size")
    @classmethod
    def _clamp_page_size(cls, value: int) -> int:
        if value > MAX_PAGE_SIZE:
            logging.warning("page_size %s exceeds the Medallia maximum; clamping to %s.", value, MAX_PAGE_SIZE)
            return MAX_PAGE_SIZE
        return value

    @computed_field
    @property
    def incremental(self) -> bool:
        """Whether the row REQUESTS incremental load (runtime capability is checked separately)."""
        return self.load_type == LoadType.incremental_load

    def parsed_filters(self) -> dict | None:
        """Parse and validate the ``filters`` JSON string; empty ⇒ no filter.

        Raised as a ``UserException`` (exit 1) for malformed JSON or a non-object root or an
        injecting object key — all user-fixable. Called from ``run()``, not model init, so a
        half-filled sync-action form with partial JSON does not crash the action.
        """
        if not self.filters.strip():
            return None
        try:
            parsed = json.loads(self.filters)
        except (ValueError, TypeError) as exc:  # fmt: skip
            raise UserException(f"filters is not valid JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise UserException('filters must be a JSON object (a Medallia filter tree), e.g. {"fieldIds": [...]}.')
        _validate_filter_keys(parsed)
        return parsed


def _format_validation_error(error: ValidationError) -> str:
    """Build a user-actionable message from a ValidationError WITHOUT echoing any value.

    Only the field location, the human-readable message and the error type are used.
    ``include_input=False`` / ``include_url=False`` guarantee the per-error dicts never carry
    ``input`` (the offending value — which for this config is the decrypted ``#client_secret``
    and tenant details), so a value can never leak into the message even if this function is
    edited later.
    """
    messages = []
    for err in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in err["loc"]) or "configuration"
        messages.append(f"field '{location}' — {err['msg']} ({err['type']})")
    return f"Invalid configuration: {'; '.join(messages)}"
