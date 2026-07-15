"""Typed Pydantic configuration models for the Medallia Query API extractor.

The component always receives a single platform-merged ``config.json``. Root-level
connection/auth parameters and row-level object parameters therefore arrive in the
same ``parameters`` dict, so both models parse from that merged dict and ignore the
keys that belong to the other model (``extra="ignore"``).
"""

import logging
import re
from enum import StrEnum

from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field, field_validator

# GraphQL name pattern — Medallia field IDs are used both as ``fieldData`` arguments and
# as query aliases, so they must be valid GraphQL names. Validating here prevents any
# possibility of query injection through a crafted field ID.
_GRAPHQL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Medallia API hard ceiling for the ``first`` page-size argument (reference repo:
# MAX_RECORDS_PER_REQUEST = 1000). The configured value is clamped to this.
MAX_PAGE_SIZE = 1000

# Component-level default page size. Matches the reference extractor's default (100): a
# modest page is gentler on the live instance / 24h quota than the API ceiling while still
# keeping the request count low. Still clamped to ``MAX_PAGE_SIZE``.
DEFAULT_PAGE_SIZE = 100

DEFAULT_FINISH_DATE_FIELD_ID = "k_initialfinishdate_epoch_int"


class DataObject(StrEnum):
    """Supported Medallia data-source root node. v1 ships ``feedback`` only."""

    feedback = "feedback"


class FinishDateFieldType(StrEnum):
    """Value format of the finish-date watermark field.

    ``epoch``    — integer epoch-seconds (e.g. ``k_initialfinishdate_epoch_int``); the
                   default, so existing configs keep their behaviour.
    ``datetime`` — an ISO 8601 date / datetime string (e.g. ``e_creationdate``,
                   ``e_responsedate`` with values like ``2026-05-01``). ISO 8601 sorts
                   correctly lexicographically, so the keyset watermark still advances.
    """

    epoch = "epoch"
    datetime = "datetime"


class LoadType(StrEnum):
    full_load = "full_load"
    incremental_load = "incremental_load"


def _validate_graphql_name(value: str, field_name: str) -> str:
    if not _GRAPHQL_NAME.match(value):
        raise UserException(
            f"{field_name} '{value}' is not a valid Medallia field ID (must match {_GRAPHQL_NAME.pattern})."
        )
    return value


class Configuration(BaseModel):
    """Root (config-level) connection and authentication parameters."""

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
            # ``#client_secret`` and tenant host. Chaining it would surface that value in
            # the traceback that ``logging.exception`` prints to the customer-visible job
            # log. Suppress the chain and raise a value-free message instead.
            raise UserException(_format_validation_error(e)) from None


class RowConfiguration(BaseModel):
    """Row-level parameters — one config row per Medallia data object / output table."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    data_object: DataObject = DataObject.feedback
    fields: list[str] = Field(..., min_length=1, description="Field IDs to extract via fieldData(fieldId).")
    finish_date_field_id: str = DEFAULT_FINISH_DATE_FIELD_ID
    finish_date_field_type: FinishDateFieldType = Field(
        default=FinishDateFieldType.epoch,
        description="Value format of finish_date_field_id: 'epoch' (integer seconds) or 'datetime' (ISO string).",
    )
    survey_id_field_id: str = Field(..., description="Field ID used as the survey identifier / primary key.")
    filters: dict | None = Field(default=None, description="Optional Medallia business filter tree.")
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1)
    initial_start_epoch: int | None = Field(
        default=None, description="First-run lower bound (finish-date epoch seconds) for an epoch watermark field."
    )
    initial_start_value: str | None = Field(
        default=None,
        description="First-run lower bound as an ISO date/datetime string for a 'datetime' watermark field.",
    )
    load_type: LoadType = LoadType.incremental_load

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            # See Configuration.__init__: ``from None`` keeps the decrypted secret /
            # config in the chained ValidationError's ``input_value`` out of the log.
            raise UserException(_format_validation_error(e)) from None

    @field_validator("fields")
    @classmethod
    def _validate_field_ids(cls, value: list[str]) -> list[str]:
        for field_id in value:
            _validate_graphql_name(field_id, "fields entry")
        return value

    @field_validator("finish_date_field_id", "survey_id_field_id")
    @classmethod
    def _validate_watermark_field(cls, value: str) -> str:
        return _validate_graphql_name(value, "watermark field ID")

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
        return self.load_type == LoadType.incremental_load

    @property
    def output_columns(self) -> list[str]:
        """Output column set: node id + surveyId + watermark finish field + configured fields (deduped).

        ``id`` is the node's direct scalar identifier (selected alongside the survey field) and
        gives every record a stable identity independent of the configured survey field.
        """
        columns = ["id", "surveyId", self.finish_date_field_id]
        reserved = {self.survey_id_field_id, self.finish_date_field_id, "id", "surveyId"}
        for field_id in self.fields:
            if field_id not in reserved and field_id not in columns:
                columns.append(field_id)
        return columns

    @property
    def initial_start(self) -> int | str | None:
        """First-run lower-bound value matching the configured watermark field format."""
        if self.finish_date_field_type == FinishDateFieldType.datetime:
            return self.initial_start_value
        return self.initial_start_epoch


def _format_validation_error(error: ValidationError) -> str:
    """Build a user-actionable message from a ValidationError WITHOUT echoing any value.

    Only the field location, the human-readable message and the error type are used.
    ``include_input=False`` / ``include_url=False`` guarantee the per-error dicts never
    carry ``input`` (the offending value — which for this config is the decrypted
    ``#client_secret`` and tenant details), so a value can never leak into the message
    even if this function is edited later.
    """
    messages = []
    for err in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in err["loc"]) or "configuration"
        messages.append(f"field '{location}' — {err['msg']} ({err['type']})")
    return f"Invalid configuration: {'; '.join(messages)}"
