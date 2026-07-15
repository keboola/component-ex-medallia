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

# Component-level default page size. Note this differs from the Medallia API default of
# 30 (applied only when ``first`` is omitted): the component always sends ``first``, and
# a large page minimises the request count against the 24h quota.
DEFAULT_PAGE_SIZE = 1000

DEFAULT_FINISH_DATE_FIELD_ID = "k_initialfinishdate_epoch_int"


class DataObject(StrEnum):
    """Supported Medallia data-source root node. v1 ships ``feedback`` only."""

    feedback = "feedback"


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
            raise UserException(_format_validation_error(e)) from e


class RowConfiguration(BaseModel):
    """Row-level parameters — one config row per Medallia data object / output table."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    data_object: DataObject = DataObject.feedback
    fields: list[str] = Field(..., min_length=1, description="Field IDs to extract via fieldData(fieldId).")
    finish_date_field_id: str = DEFAULT_FINISH_DATE_FIELD_ID
    survey_id_field_id: str = Field(..., description="Field ID used as the survey identifier / primary key.")
    filters: dict | None = Field(default=None, description="Optional Medallia business filter tree.")
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1)
    initial_start_epoch: int | None = Field(
        default=None, description="First-run lower bound (finish-date epoch seconds) when state is empty."
    )
    load_type: LoadType = LoadType.incremental_load

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            raise UserException(_format_validation_error(e)) from e

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
        """Output column set: surveyId + watermark finish field + configured fields (deduped)."""
        columns = ["surveyId", self.finish_date_field_id]
        reserved = {self.survey_id_field_id, self.finish_date_field_id}
        for field_id in self.fields:
            if field_id not in reserved and field_id not in columns:
                columns.append(field_id)
        return columns


def _format_validation_error(error: ValidationError) -> str:
    messages = []
    for err in error.errors():
        location = ".".join(str(part) for part in err["loc"]) or "configuration"
        messages.append(f"{location}: {err['msg']}")
    return f"Configuration validation error: {'; '.join(messages)}"
