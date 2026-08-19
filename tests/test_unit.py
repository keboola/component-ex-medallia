"""Unit tests for the generic Medallia Query API redesign (spec §10.1).

Pure in-memory unit coverage — no network, no datadir. Exercises the pieces documented in
``docs/superpowers/specs/2026-07-16-generic-query-api-redesign.md``:

* ``client.medallia_client`` — ``flatten_node`` / ``row_hash`` / ``advance_watermark``,
  ``GenericQueryBuilder``, the introspection classifiers, ``MedalliaClient`` (pagination,
  retries, errors) and ``MedalliaTokenManager``.
* ``configuration`` — ``Configuration`` / ``RowConfiguration`` validation, ``parsed_filters``.
* ``component.Component`` — the pure static helpers (type mapping, humanize, incremental
  planning, raw-mode contract checks) that do not require a datadir.

HTTP interactions are exercised via small in-memory stub sessions/token managers — never real
``requests`` calls.
"""

import csv
import json
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
import requests
from freezegun import freeze_time
from keboola.component.dao import BaseType
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import MessageType

from client.medallia_client import (
    SHAPE_DATA,
    SHAPE_FIELDDATA,
    SHAPE_SCALAR,
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
from component import _METADATA_CATALOGS, Component, MedalliaResponseBodySanitizer
from configuration import MAX_PAGE_SIZE, MIN_PAGE_SIZE, Configuration, RowConfiguration

# ==================================================================================================
# Stubs — in-memory requests.Session / token-manager doubles (no network).
# ==================================================================================================


class _StubResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: dict | None = None,
        headers: dict | None = None,
        raise_json: bool = False,
    ):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.headers = headers or {}
        # When True, ``json()`` raises like ``requests`` does on a non-JSON / empty 2xx body
        # (``requests.exceptions.JSONDecodeError`` is a ``ValueError`` subclass).
        self._raise_json = raise_json

    def json(self) -> dict:
        if self._raise_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_data


class _StubSession(requests.Session):
    """Replays a fixed queue of responses and records every posted request.

    Subclasses ``requests.Session`` only so it type-checks where a real session is expected;
    it does not use any base behaviour (``post`` is fully overridden).
    """

    def __init__(self, responses: list[_StubResponse]):
        super().__init__()
        self._responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url, **kwargs):  # ty: ignore[invalid-method-override]  # narrow test double
        self.requests.append({"url": url, **kwargs})
        if not self._responses:  # pragma: no cover - defensive; a test bug, not prod code.
            raise AssertionError("Stub session ran out of queued responses.")
        return self._responses.pop(0)


class _StubTokenManager(MedalliaTokenManager):
    """Fixed-token stand-in for ``MedalliaTokenManager`` (bypasses the real OAuth __init__)."""

    def __init__(self, token: str = "test-token"):
        self.token = token
        self.invalidate_calls = 0

    def get_token(self) -> str:
        return self.token

    def invalidate(self) -> None:
        self.invalidate_calls += 1


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Every retry/backoff test stubs the sleep hook so the suite stays fast and deterministic."""
    monkeypatch.setattr(MedalliaClient, "_sleep", staticmethod(lambda seconds: None))


# ==================================================================================================
# 1. flatten_node
# ==================================================================================================


class TestFlattenNode:
    """The three node shapes (fieldData / data / bare scalar) plus nested-object fallback."""

    def test_fielddata_shape_single_value_becomes_scalar(self):
        node = {"id": "1", "e_nps": {"values": [9]}}
        assert flatten_node(node) == {"id": "1", "e_nps": 9}

    def test_fielddata_shape_empty_values_becomes_none(self):
        node = {"id": "1", "e_nps": {"values": []}}
        assert flatten_node(node) == {"id": "1", "e_nps": None}

    def test_fielddata_shape_multiple_values_becomes_json_list(self):
        node = {"id": "1", "tags": {"values": ["a", "b", "c"]}}
        flat = flatten_node(node)
        assert flat["tags"] == json.dumps(["a", "b", "c"])

    def test_data_shape_uses_value_when_present(self):
        node = {"id": "1", "email": {"value": "a@example.com", "values": ["ignored"]}}
        assert flatten_node(node)["email"] == "a@example.com"

    def test_data_shape_falls_back_to_values_when_value_is_none(self):
        node = {"id": "1", "email": {"value": None, "values": ["a@example.com"]}}
        assert flatten_node(node)["email"] == "a@example.com"

    def test_data_shape_falls_back_to_values_multi_becomes_json(self):
        node = {"id": "1", "email": {"value": None, "values": ["a@example.com", "b@example.com"]}}
        flat = flatten_node(node)
        assert flat["email"] == json.dumps(["a@example.com", "b@example.com"])

    def test_bare_scalar_copied_through_unchanged(self):
        node = {"id": "1", "programId": "P-100", "count": 5, "active": True}
        assert flatten_node(node) == {"id": "1", "programId": "P-100", "count": 5, "active": True}

    def test_nested_dict_without_value_or_values_is_json_encoded(self):
        node = {"id": "1", "nested": {"foo": "bar", "baz": 1}}
        flat = flatten_node(node)
        assert flat["nested"] == json.dumps({"foo": "bar", "baz": 1})

    def test_bare_list_is_json_encoded(self):
        node = {"id": "1", "raw_list": [1, 2, 3]}
        flat = flatten_node(node)
        assert flat["raw_list"] == json.dumps([1, 2, 3])


# ==================================================================================================
# 2. row_hash
# ==================================================================================================


class TestRowHash:
    def test_deterministic_for_identical_rows(self):
        row = {"id": "1", "e_nps": 9, "e_comment": "great"}
        assert row_hash(row) == row_hash(dict(row))

    def test_independent_of_key_order(self):
        row_a = {"id": "1", "e_nps": 9, "e_comment": "great"}
        row_b = {"e_comment": "great", "id": "1", "e_nps": 9}
        assert row_hash(row_a) == row_hash(row_b)

    def test_two_separate_constructions_of_the_same_row_match(self):
        first = row_hash({"a": 1, "b": 2})
        second = row_hash({"b": 2, "a": 1})
        assert first == second

    def test_excludes_its_own_row_hash_key(self):
        row = {"id": "1", "e_nps": 9}
        with_hash = dict(row, _row_hash="whatever-was-there-before")
        assert row_hash(row) == row_hash(with_hash)

    def test_none_and_empty_string_hash_equal(self):
        assert row_hash({"id": "1", "value": None}) == row_hash({"id": "1", "value": ""})

    def test_different_rows_hash_differently(self):
        assert row_hash({"id": "1"}) != row_hash({"id": "2"})


# ==================================================================================================
# 3. GenericQueryBuilder
# ==================================================================================================


class TestGenericQueryBuilderShapes:
    def test_fielddata_shape_emits_alias_fielddata_fragment_with_id_first(self):
        builder = GenericQueryBuilder(
            object_name="feedback",
            node_shape=SHAPE_FIELDDATA,
            selected_fields=["e_nps"],
            scalar_fields=[],
            incremental_field=None,
            filter_tree=None,
            supports_filter=True,
            supports_order=True,
            has_id=True,
        )
        assert builder.selection_columns() == ["id", "e_nps"]
        expected = (
            "query ($first: Int, $after: String) { feedback(first: $first, after: $after) { "
            'nodes { id e_nps: fieldData(fieldId: "e_nps") { values } } '
            "pageInfo { hasNextPage endCursor } } }"
        )
        assert builder.build_query() == expected

    def test_data_shape_emits_alias_data_fragment_plus_scalar_fields_plus_id(self):
        builder = GenericQueryBuilder(
            object_name="customers",
            node_shape=SHAPE_DATA,
            selected_fields=["email"],
            scalar_fields=["customerId"],
            incremental_field=None,
            filter_tree=None,
            supports_filter=False,
            supports_order=False,
            has_id=True,
        )
        assert builder.selection_columns() == ["id", "customerId", "email"]
        expected = (
            "query ($first: Int, $after: String) { customers(first: $first, after: $after) { "
            'nodes { id customerId email: data(fieldId: "email") { value values } } '
            "pageInfo { hasNextPage endCursor } } }"
        )
        assert builder.build_query() == expected

    def test_scalar_shape_uses_selected_fields_when_provided(self):
        builder = GenericQueryBuilder(
            object_name="programs",
            node_shape=SHAPE_SCALAR,
            selected_fields=["name"],
            scalar_fields=["programId", "name"],
            incremental_field=None,
            filter_tree=None,
            supports_filter=False,
            supports_order=False,
            has_id=True,
        )
        assert builder.selection_columns() == ["id", "name"]
        expected = (
            "query ($first: Int, $after: String) { programs(first: $first, after: $after) { "
            "nodes { id name } pageInfo { hasNextPage endCursor } } }"
        )
        assert builder.build_query() == expected

    def test_scalar_shape_falls_back_to_scalar_fields_and_omits_id_when_no_id(self):
        builder = GenericQueryBuilder(
            object_name="socialURLs",
            node_shape=SHAPE_SCALAR,
            selected_fields=[],
            scalar_fields=["url", "domain"],
            incremental_field=None,
            filter_tree=None,
            supports_filter=False,
            supports_order=False,
            has_id=False,
        )
        assert builder.selection_columns() == ["url", "domain"]
        expected = (
            "query ($first: Int, $after: String) { socialURLs(first: $first, after: $after) { "
            "nodes { url domain } pageInfo { hasNextPage endCursor } } }"
        )
        assert builder.build_query() == expected

    def test_empty_selection_raises_user_exception(self):
        builder = GenericQueryBuilder(
            object_name="socialURLs",
            node_shape=SHAPE_SCALAR,
            selected_fields=[],
            scalar_fields=[],
            incremental_field=None,
            filter_tree=None,
            supports_filter=False,
            supports_order=False,
            has_id=False,
        )
        with pytest.raises(UserException):
            builder.build_query()


class TestGenericQueryBuilderIncrementalAndFilters:
    def _builder(self, filter_tree=None):
        return GenericQueryBuilder(
            object_name="feedback",
            node_shape=SHAPE_FIELDDATA,
            selected_fields=["e_nps"],
            scalar_fields=[],
            incremental_field="e_creationdate",
            filter_tree=filter_tree,
            supports_filter=True,
            supports_order=True,
            has_id=True,
        )

    def test_always_declares_first_after_variables_and_header(self):
        query = self._builder().build_query()
        assert query.startswith("query ($first: Int, $after: String) { ")
        assert "first: $first, after: $after" in query

    def test_lower_bound_only_adds_gte_clause_not_gt(self):
        query = self._builder().build_query(lower_bound="2024-01-01")
        assert 'filter: {fieldIds: ["e_creationdate"], gte: "2024-01-01"}' in query
        assert ", gt:" not in query

    def test_upper_bound_only_adds_lt_clause(self):
        query = self._builder().build_query(upper_bound="2024-12-31")
        assert 'filter: {fieldIds: ["e_creationdate"], lt: "2024-12-31"}' in query

    def test_both_bounds_and_join_upper_before_lower(self):
        query = self._builder().build_query(lower_bound="2024-01-01", upper_bound="2024-12-31")
        expected_filter = (
            "filter: {and: ["
            '{fieldIds: ["e_creationdate"], lt: "2024-12-31"}, '
            '{fieldIds: ["e_creationdate"], gte: "2024-01-01"}'
            "]}"
        )
        assert expected_filter in query

    def test_user_filter_tree_and_incremental_bounds_are_and_joined(self):
        user_filter = {"fieldIds": ["region"], "eq": "EU"}
        query = self._builder(filter_tree=user_filter).build_query(lower_bound="2024-01-01", upper_bound="2024-12-31")
        expected_filter = (
            "filter: {and: ["
            '{fieldIds: ["region"], eq: "EU"}, '
            '{fieldIds: ["e_creationdate"], lt: "2024-12-31"}, '
            '{fieldIds: ["e_creationdate"], gte: "2024-01-01"}'
            "]}"
        )
        assert expected_filter in query

    def test_supports_order_with_incremental_field_adds_order_by_asc(self):
        query = self._builder().build_query()
        assert 'orderBy: [{fieldId: "e_creationdate", direction: ASC}]' in query

    def test_no_order_by_when_supports_order_is_false(self):
        builder = GenericQueryBuilder(
            object_name="feedback",
            node_shape=SHAPE_FIELDDATA,
            selected_fields=["e_nps"],
            scalar_fields=[],
            incremental_field="e_creationdate",
            filter_tree=None,
            supports_filter=True,
            supports_order=False,
            has_id=True,
        )
        assert "orderBy" not in builder.build_query()


class TestGenericQueryBuilderInjectionGuard:
    @pytest.mark.parametrize("bad_name", ["a b", "1x", "a;drop"])
    def test_bad_object_name_raises(self, bad_name):
        with pytest.raises(UserException):
            GenericQueryBuilder(
                object_name=bad_name,
                node_shape=SHAPE_SCALAR,
                selected_fields=[],
                scalar_fields=["x"],
                incremental_field=None,
                filter_tree=None,
                supports_filter=False,
                supports_order=False,
                has_id=False,
            )

    @pytest.mark.parametrize("bad_name", ["a b", "1x", "a;drop"])
    def test_bad_selected_field_raises(self, bad_name):
        with pytest.raises(UserException):
            GenericQueryBuilder(
                object_name="feedback",
                node_shape=SHAPE_FIELDDATA,
                selected_fields=[bad_name],
                scalar_fields=[],
                incremental_field=None,
                filter_tree=None,
                supports_filter=False,
                supports_order=False,
                has_id=True,
            )

    @pytest.mark.parametrize("bad_name", ["a b", "1x", "a;drop"])
    def test_bad_incremental_field_raises(self, bad_name):
        with pytest.raises(UserException):
            GenericQueryBuilder(
                object_name="feedback",
                node_shape=SHAPE_FIELDDATA,
                selected_fields=["e_nps"],
                scalar_fields=[],
                incremental_field=bad_name,
                filter_tree=None,
                supports_filter=True,
                supports_order=True,
                has_id=True,
            )


# ==================================================================================================
# Shared introspection fixture (spec §10.1: feedback / customers / programs / socialURLs +
# a metadata catalog `fields` and a non-connection root field `me`).
# ==================================================================================================


def _named(kind: str, name: str | None) -> dict:
    return {"kind": kind, "name": name, "ofType": None}


def _list_of(named_type: dict) -> dict:
    return {"kind": "LIST", "name": None, "ofType": named_type}


def _connection_type(name: str, node_type_name: str) -> dict:
    return {
        "name": name,
        "kind": "OBJECT",
        "fields": [
            {"name": "nodes", "type": _list_of(_named("OBJECT", node_type_name))},
            {"name": "pageInfo", "type": _named("OBJECT", "PageInfo")},
        ],
    }


def build_introspection_fixture() -> dict:
    """Hand-built introspection covering every shape + the exclusion cases."""
    query_fields = [
        {
            "name": "feedback",
            "args": [{"name": "first"}, {"name": "after"}, {"name": "filter"}, {"name": "orderBy"}],
            "type": _named("OBJECT", "FeedbackConnection"),
        },
        {
            "name": "customers",
            "args": [{"name": "first"}, {"name": "after"}],
            "type": _named("OBJECT", "CustomersConnection"),
        },
        {
            "name": "programs",
            "args": [{"name": "first"}, {"name": "after"}],
            "type": _named("OBJECT", "ProgramsConnection"),
        },
        {
            "name": "socialURLs",
            "args": [{"name": "first"}, {"name": "after"}],
            "type": _named("OBJECT", "SocialURLsConnection"),
        },
        {
            # Metadata catalog — must be excluded from the extractable-object set.
            "name": "fields",
            "args": [{"name": "first"}, {"name": "after"}],
            "type": _named("OBJECT", "FieldsConnection"),
        },
        {
            # Non-connection root field (a singleton) — must be excluded.
            "name": "me",
            "args": [],
            "type": _named("OBJECT", "Viewer"),
        },
    ]
    types = [
        _connection_type("FeedbackConnection", "Feedback"),
        {
            "name": "Feedback",
            "kind": "OBJECT",
            "fields": [
                {"name": "id", "type": _named("SCALAR", "ID")},
                {"name": "fieldData", "type": _named("OBJECT", "FieldData")},
            ],
        },
        _connection_type("CustomersConnection", "Customer"),
        {
            "name": "Customer",
            "kind": "OBJECT",
            "fields": [
                {"name": "id", "type": _named("SCALAR", "ID")},
                {"name": "data", "type": _named("OBJECT", "FieldValue")},
            ],
        },
        _connection_type("ProgramsConnection", "Program"),
        {
            "name": "Program",
            "kind": "OBJECT",
            "fields": [
                {"name": "id", "type": _named("SCALAR", "ID")},
                {"name": "programId", "type": _named("SCALAR", "String")},
                {"name": "name", "type": _named("SCALAR", "String")},
            ],
        },
        _connection_type("SocialURLsConnection", "SocialURL"),
        {
            "name": "SocialURL",
            "kind": "OBJECT",
            "fields": [
                # NO id field — has_id must resolve False.
                {"name": "url", "type": _named("SCALAR", "String")},
                {"name": "domain", "type": _named("SCALAR", "String")},
            ],
        },
        _connection_type("FieldsConnection", "FieldMeta"),
        {
            "name": "FieldMeta",
            "kind": "OBJECT",
            "fields": [{"name": "id", "type": _named("SCALAR", "ID")}],
        },
        {
            "name": "Viewer",
            "kind": "OBJECT",
            "fields": [{"name": "id", "type": _named("SCALAR", "ID")}],
        },
    ]
    return {"__schema": {"queryType": {"fields": query_fields}, "types": types}}


# ==================================================================================================
# 4. list_extractable_objects
# ==================================================================================================


class TestListExtractableObjects:
    def test_returns_only_connections_excluding_catalog_and_non_connections_sorted(self):
        objects = list_extractable_objects(build_introspection_fixture())
        assert objects == sorted(["feedback", "customers", "programs", "socialURLs"])
        assert "fields" not in objects  # metadata catalog denylist
        assert "me" not in objects  # non-connection root field

    def test_empty_introspection_returns_empty_list(self):
        assert list_extractable_objects({}) == []

    def test_garbage_introspection_returns_empty_list(self):
        assert list_extractable_objects({"__schema": {"queryType": {}, "types": []}}) == []
        assert list_extractable_objects({"totally": "unrelated"}) == []


# ==================================================================================================
# 5. resolve_object_shape
# ==================================================================================================


class TestResolveObjectShape:
    def test_fielddata_node_type_resolves_shape_fielddata(self):
        shape = resolve_object_shape("feedback", build_introspection_fixture())
        assert shape is not None
        assert shape.shape == SHAPE_FIELDDATA
        assert shape.has_id is True
        assert shape.supports_filter is True
        assert shape.supports_order is True

    def test_data_node_type_resolves_shape_data(self):
        shape = resolve_object_shape("customers", build_introspection_fixture())
        assert shape is not None
        assert shape.shape == SHAPE_DATA
        assert shape.has_id is True
        assert shape.supports_filter is False
        assert shape.supports_order is False

    def test_bare_scalar_node_type_resolves_shape_scalar_with_scalar_fields(self):
        shape = resolve_object_shape("programs", build_introspection_fixture())
        assert shape is not None
        assert shape.shape == SHAPE_SCALAR
        assert shape.has_id is True
        assert shape.scalar_fields == {"programId": "String", "name": "String"}

    def test_scalar_object_without_id_field_has_id_false(self):
        shape = resolve_object_shape("socialURLs", build_introspection_fixture())
        assert shape is not None
        assert shape.shape == SHAPE_SCALAR
        assert shape.has_id is False
        assert shape.scalar_fields == {"url": "String", "domain": "String"}

    def test_absent_object_returns_none(self):
        assert resolve_object_shape("doesNotExist", build_introspection_fixture()) is None

    def test_non_connection_root_field_returns_none(self):
        assert resolve_object_shape("me", build_introspection_fixture()) is None


# ==================================================================================================
# 6. RowConfiguration
# ==================================================================================================


class TestRowConfiguration:
    def _merged_params(self, **overrides) -> dict:
        base = {
            "instance_host": "acme.medallia.com",
            "company_name": "acme",
            "api_host": "acme.apis.medallia.com",
            "client_id": "client-123",
            "#client_secret": "super-secret",
            "mode": "structured",
            "data_object": "feedback",
            "fields": ["e_nps", "e_comment"],
            "load_type": "incremental_load",
            "incremental_field": "e_creationdate",
            "initial_start": "2024-01-01",
            "filters": "",
            "page_size": 100,
        }
        base.update(overrides)
        return base

    def test_structured_sample_validates_from_merged_params(self):
        row = RowConfiguration(**self._merged_params())
        assert row.data_object == "feedback"
        assert row.fields == ["e_nps", "e_comment"]
        assert row.incremental is True

    def test_raw_sample_validates_from_merged_params(self):
        params = self._merged_params(
            mode="raw",
            data_object="",
            fields=[],
            raw_query="query ($first: Int, $after: String) { feedback(first: $first, after: $after)"
            " { nodes { id } pageInfo { hasNextPage endCursor } } }",
            output_table="feedback_raw",
        )
        row = RowConfiguration(**params)
        assert row.mode.value == "raw"
        assert row.output_table == "feedback_raw"

    def test_extra_root_only_keys_are_ignored(self):
        # instance_host/company_name/etc. belong to Configuration, not RowConfiguration; merged
        # dict must still validate the row without complaint (extra="ignore").
        row = RowConfiguration(**self._merged_params())
        assert not hasattr(row, "instance_host")

    def test_incremental_computed_field_true_for_incremental_load(self):
        row = RowConfiguration(**self._merged_params(load_type="incremental_load"))
        assert row.incremental is True

    def test_incremental_computed_field_false_for_full_load(self):
        row = RowConfiguration(**self._merged_params(load_type="full_load"))
        assert row.incremental is False

    def test_page_size_clamps_to_max_when_over(self):
        row = RowConfiguration(**self._merged_params(page_size=5000))
        assert row.page_size == MAX_PAGE_SIZE == 500

    def test_page_size_clamps_to_min_when_under(self):
        row = RowConfiguration(**self._merged_params(page_size=1))
        assert row.page_size == MIN_PAGE_SIZE == 25

    def test_page_size_kept_when_within_bounds(self):
        row = RowConfiguration(**self._merged_params(page_size=250))
        assert row.page_size == 250

    def test_page_size_default_when_not_supplied(self):
        params = self._merged_params()
        del params["page_size"]
        row = RowConfiguration(**params)
        assert row.page_size == 100

    def test_empty_half_filled_form_validates_without_error(self):
        # No empty-rejecting validators: an async-fed sync-action form with nothing filled in
        # yet must still construct successfully.
        row = RowConfiguration()
        assert row.data_object == ""
        assert row.fields == []
        assert row.incremental_field == ""

    @pytest.mark.parametrize("bad_name", ["a b", "1x", "a;drop"])
    def test_invalid_data_object_raises_user_exception(self, bad_name):
        with pytest.raises(UserException):
            RowConfiguration(data_object=bad_name)

    @pytest.mark.parametrize("bad_name", ["a b", "1x", "a;drop"])
    def test_invalid_incremental_field_raises_user_exception(self, bad_name):
        with pytest.raises(UserException):
            RowConfiguration(incremental_field=bad_name)

    @pytest.mark.parametrize("bad_name", ["a b", "1x", "a;drop"])
    def test_invalid_fields_entry_raises_user_exception(self, bad_name):
        with pytest.raises(UserException):
            RowConfiguration(fields=["ok_field", bad_name])

    def test_output_table_defaults_empty_and_is_optional(self):
        # Optional per-row field, valid in BOTH modes, empty by default (must not break rows).
        assert RowConfiguration().output_table == ""

    @pytest.mark.parametrize("name", ["feedback_variant_a", "my-table", "table1", "customers"])
    def test_valid_output_table_accepted(self, name):
        assert RowConfiguration(output_table=name).output_table == name

    @pytest.mark.parametrize("bad_name", ["a/b", "..\\evil", "with space", "tab\tname", "semi;colon"])
    def test_invalid_output_table_raises_user_exception(self, bad_name):
        with pytest.raises(UserException):
            RowConfiguration(output_table=bad_name)


class TestParsedFilters:
    def test_valid_json_object_returns_dict(self):
        row = RowConfiguration(filters='{"fieldIds": ["e_nps"], "gt": "5"}')
        assert row.parsed_filters() == {"fieldIds": ["e_nps"], "gt": "5"}

    def test_empty_string_returns_none(self):
        row = RowConfiguration(filters="")
        assert row.parsed_filters() is None

    def test_blank_string_returns_none(self):
        row = RowConfiguration(filters="   ")
        assert row.parsed_filters() is None

    def test_malformed_json_raises_user_exception(self):
        row = RowConfiguration(filters="{not valid json")
        with pytest.raises(UserException):
            row.parsed_filters()

    def test_non_object_json_raises_user_exception(self):
        row = RowConfiguration(filters="[1, 2]")
        with pytest.raises(UserException):
            row.parsed_filters()

    def test_invalid_object_key_raises_user_exception(self):
        row = RowConfiguration(filters='{"a b": 1}')
        with pytest.raises(UserException):
            row.parsed_filters()

    def test_legit_operators_pass(self):
        filter_tree = {"and": [{"or": [{"fieldIds": ["e_nps"], "gte": "1", "lt": "10"}]}]}
        row = RowConfiguration(filters=json.dumps(filter_tree))
        assert row.parsed_filters() == filter_tree


# ==================================================================================================
# 7. Configuration
# ==================================================================================================


class TestConfiguration:
    def _valid_params(self, **overrides) -> dict:
        base = {
            "instance_host": "acme.medallia.com",
            "company_name": "acme",
            "api_host": "acme.apis.medallia.com",
            "client_id": "client-123",
            "#client_secret": "super-secret",
        }
        base.update(overrides)
        return base

    def test_valid_root_params_validate(self):
        config = Configuration(**self._valid_params())
        assert config.instance_host == "acme.medallia.com"
        assert config.client_secret == "super-secret"

    def test_missing_required_field_raises_user_exception(self):
        params = self._valid_params()
        del params["instance_host"]
        with pytest.raises(UserException):
            Configuration(**params)

    def test_secret_alias_maps_to_client_secret(self):
        config = Configuration(**self._valid_params(**{"#client_secret": "sekrit-value"}))
        assert config.client_secret == "sekrit-value"


# ==================================================================================================
# 8. Secret-leak guard (unit level)
# ==================================================================================================


class TestSecretLeakGuard:
    SENTINEL = "SENTINEL_SECRET_DO_NOT_LEAK_UNIT"

    def test_configuration_error_message_excludes_secret_and_pydantic_internals(self):
        params = {
            "company_name": "acme",
            "api_host": "acme.apis.medallia.com",
            "client_id": "client-123",
            "#client_secret": self.SENTINEL,
            # instance_host deliberately missing -> validation error.
        }
        with pytest.raises(UserException) as exc_info:
            Configuration(**params)
        message = str(exc_info.value)
        assert self.SENTINEL not in message
        assert "input_value" not in message
        assert "ValidationError" not in message
        assert "instance_host" in message

    def test_row_configuration_error_message_excludes_secret_and_pydantic_internals(self):
        params = {
            "#client_secret": self.SENTINEL,
            "data_object": "a;drop table",
        }
        with pytest.raises(UserException) as exc_info:
            RowConfiguration(**params)
        message = str(exc_info.value)
        assert self.SENTINEL not in message
        assert "input_value" not in message
        assert "ValidationError" not in message


# ==================================================================================================
# 9. incremental capability + watermark helpers
# ==================================================================================================


class TestIncrementalIsInt:
    def test_int_datatype_is_true(self):
        shape = ObjectShape("feedback", SHAPE_FIELDDATA, True, True, True)
        assert Component._incremental_is_int(shape, "e_nps", {"e_nps": {"dataType": "INT"}}) is True

    def test_integer_datatype_is_true(self):
        shape = ObjectShape("feedback", SHAPE_FIELDDATA, True, True, True)
        assert Component._incremental_is_int(shape, "e_nps", {"e_nps": {"dataType": "INTEGER"}}) is True

    def test_date_datatype_is_false(self):
        shape = ObjectShape("feedback", SHAPE_FIELDDATA, True, True, True)
        assert Component._incremental_is_int(shape, "e_date", {"e_date": {"dataType": "DATE"}}) is False

    def test_datetime_datatype_is_false(self):
        shape = ObjectShape("feedback", SHAPE_FIELDDATA, True, True, True)
        assert Component._incremental_is_int(shape, "e_ts", {"e_ts": {"dataType": "DATETIME"}}) is False

    def test_falls_back_to_scalar_fields_int(self):
        shape = ObjectShape("programs", SHAPE_SCALAR, True, False, False, scalar_fields={"seq": "Int"})
        assert Component._incremental_is_int(shape, "seq", {}) is True

    def test_falls_back_to_scalar_fields_non_int(self):
        shape = ObjectShape("programs", SHAPE_SCALAR, True, False, False, scalar_fields={"name": "String"})
        assert Component._incremental_is_int(shape, "name", {}) is False

    def test_no_field_id_is_false(self):
        shape = ObjectShape("feedback", SHAPE_FIELDDATA, True, True, True)
        assert Component._incremental_is_int(shape, "", {}) is False


class TestAdvanceWatermark:
    def test_numeric_max_when_is_int(self):
        assert advance_watermark(5, 10, is_int=True) == 10
        assert advance_watermark(10, 5, is_int=True) == 10

    def test_ignores_non_numeric_candidate_when_is_int(self):
        assert advance_watermark(10, "not-a-number", is_int=True) == 10

    def test_ignores_none_candidate_when_is_int(self):
        assert advance_watermark(10, None, is_int=True) == 10

    def test_lexicographic_max_for_strings(self):
        assert advance_watermark("2024-01-01", "2024-06-01", is_int=False) == "2024-06-01"
        assert advance_watermark("2024-06-01", "2024-01-01", is_int=False) == "2024-06-01"

    def test_current_unchanged_on_empty_candidate(self):
        assert advance_watermark("2024-01-01", "", is_int=False) == "2024-01-01"

    def test_current_unchanged_on_none_candidate(self):
        assert advance_watermark("2024-01-01", None, is_int=False) == "2024-01-01"

    def test_seeds_from_none_current_int(self):
        assert advance_watermark(None, 7, is_int=True) == 7

    def test_seeds_from_none_current_string(self):
        assert advance_watermark(None, "2024-01-01", is_int=False) == "2024-01-01"

    def test_mixed_str_current_int_candidate_compares_lexicographically(self):
        # A str ``current`` against an int ``candidate`` (even with is_int=True) falls to the
        # lexicographic branch, so "5" is (wrongly) kept over 10. This is why an INT seed must be
        # coerced to int BEFORE it reaches advance_watermark (see Component._coerce_seed).
        assert advance_watermark("5", 10, is_int=True) == "5"
        # Once the seed is an int, comparison is numeric and the max wins as expected.
        assert advance_watermark(5, 10, is_int=True) == 10


class TestCoerceSeed:
    def test_int_field_coerces_numeric_string_to_int(self):
        assert Component._coerce_seed("5", is_int=True) == 5
        assert Component._coerce_seed("1700000000", is_int=True) == 1700000000

    def test_int_field_leaves_non_numeric_seed_unchanged(self):
        assert Component._coerce_seed("not-a-number", is_int=True) == "not-a-number"

    def test_non_int_field_leaves_seed_as_string(self):
        assert Component._coerce_seed("2024-01-01", is_int=False) == "2024-01-01"

    def test_int_seed_passthrough(self):
        assert Component._coerce_seed(42, is_int=True) == 42


class TestResolveInitialStart:
    def test_epoch_seconds_passthrough_int_field(self):
        assert Component._resolve_initial_start("1780272000", is_int=True) == 1780272000

    def test_epoch_seconds_passthrough_string_field(self):
        assert Component._resolve_initial_start("1780272000", is_int=False) == "1780272000"

    def test_iso_date_datetime_field_returns_date_only(self):
        assert Component._resolve_initial_start("2026-01-01", is_int=False) == "2026-01-01"

    def test_iso_date_epoch_field_returns_epoch_seconds(self):
        assert Component._resolve_initial_start("2026-01-01", is_int=True) == 1767225600

    def test_relative_datetime_field(self):
        with freeze_time("2026-07-16T12:00:00+00:00"):
            assert Component._resolve_initial_start("yesterday", is_int=False) == "2026-07-15"
            assert Component._resolve_initial_start("5 days ago", is_int=False) == "2026-07-11"

    def test_relative_epoch_field(self):
        with freeze_time("2026-07-16T12:00:00+00:00"):
            # 2026-07-11 00:00:00Z
            assert Component._resolve_initial_start("5 days ago", is_int=True) == 1783728000

    def test_unparseable_raises_user_exception(self):
        with pytest.raises(UserException, match="Could not parse 'Initial Start'"):
            Component._resolve_initial_start("not a real date", is_int=False)


class TestDateWindowDecoupling:
    """Start Date / Date Field apply to BOTH load types; load type only sets the write mode."""

    def _row(self, **kw) -> RowConfiguration:
        base = dict(data_object="feedback", mode="structured", incremental_field="e_creationdate")
        base.update(kw)
        return RowConfiguration(**base)

    def test_window_supported_requires_field_and_filter_support(self):
        shape_ok = ObjectShape("feedback", SHAPE_FIELDDATA, True, True, True)
        assert Component._date_window_supported(self._row(), shape_ok) is True
        assert Component._date_window_supported(self._row(incremental_field=""), shape_ok) is False
        shape_no_filter = ObjectShape("x", SHAPE_SCALAR, True, False, False)
        assert Component._date_window_supported(self._row(), shape_no_filter) is False

    def test_full_load_uses_start_date_and_ignores_stored_watermark(self, monkeypatch):
        comp = object.__new__(Component)
        monkeypatch.setattr(comp, "get_state_file", lambda: {"last_incremental_value": "2099-01-01"})
        row = self._row(load_type="full_load", initial_start="2026-01-01")
        assert row.incremental is False
        assert comp._compute_lower_bound(row, is_int=False) == "2026-01-01"  # Start Date, not the watermark

    def test_incremental_resume_prefers_stored_watermark_over_start_date(self, monkeypatch):
        comp = object.__new__(Component)
        monkeypatch.setattr(comp, "get_state_file", lambda: {"last_incremental_value": "2026-05-01"})
        row = self._row(load_type="incremental_load", initial_start="2026-01-01")
        assert comp._compute_lower_bound(row, is_int=False) == "2026-05-01"

    def test_incremental_first_run_seeds_from_start_date(self, monkeypatch):
        comp = object.__new__(Component)
        monkeypatch.setattr(comp, "get_state_file", lambda: {})
        row = self._row(load_type="incremental_load", initial_start="2026-01-01")
        assert comp._compute_lower_bound(row, is_int=False) == "2026-01-01"

    def test_no_start_date_no_watermark_is_unbounded(self, monkeypatch):
        comp = object.__new__(Component)
        monkeypatch.setattr(comp, "get_state_file", lambda: {})
        assert comp._compute_lower_bound(self._row(load_type="full_load"), is_int=False) is None


class TestLookback:
    """Look Back re-reads a trailing slice so late-arriving records are not lost forever."""

    def _row(self, **kw) -> RowConfiguration:
        base = dict(data_object="feedback", mode="structured", incremental_field="e_creationdate")
        base.update(kw)
        return RowConfiguration(**base)

    def _comp(self, monkeypatch, stored):
        comp = object.__new__(Component)
        state = {"last_incremental_value": stored} if stored is not None else {}
        monkeypatch.setattr(comp, "get_state_file", lambda: state)
        return comp

    def test_lookback_shifts_date_watermark(self, monkeypatch):
        comp = self._comp(monkeypatch, "2026-05-10")
        row = self._row(load_type="incremental_load", lookback="2 days")
        assert comp._compute_lower_bound(row, is_int=False) == "2026-05-08"

    def test_lookback_shifts_epoch_watermark_by_seconds(self, monkeypatch):
        comp = self._comp(monkeypatch, 1783728000)
        row = self._row(load_type="incremental_load", lookback="36 hours")
        assert comp._compute_lower_bound(row, is_int=True) == 1783728000 - 36 * 3600

    def test_epoch_watermark_never_goes_negative(self, monkeypatch):
        comp = self._comp(monkeypatch, 100)
        row = self._row(load_type="incremental_load", lookback="5 days")
        assert comp._compute_lower_bound(row, is_int=True) == 0

    def test_empty_lookback_leaves_the_bound_untouched(self, monkeypatch):
        comp = self._comp(monkeypatch, "2026-05-10")
        assert comp._compute_lower_bound(self._row(load_type="incremental_load"), is_int=False) == "2026-05-10"

    def test_lookback_is_ignored_on_the_first_run(self, monkeypatch):
        comp = self._comp(monkeypatch, None)
        row = self._row(load_type="incremental_load", initial_start="2026-01-01", lookback="30 days")
        assert comp._compute_lower_bound(row, is_int=False) == "2026-01-01"

    def test_lookback_is_ignored_for_a_full_load(self, monkeypatch):
        comp = self._comp(monkeypatch, "2099-01-01")
        row = self._row(load_type="full_load", initial_start="2026-01-01", lookback="30 days")
        assert comp._compute_lower_bound(row, is_int=False) == "2026-01-01"

    def test_unparseable_lookback_raises_user_exception(self, monkeypatch):
        comp = self._comp(monkeypatch, "2026-05-10")
        row = self._row(load_type="incremental_load", lookback="not a duration")
        with pytest.raises(UserException, match="Could not parse 'Look Back'"):
            comp._compute_lower_bound(row, is_int=False)

    def test_unparseable_lookback_is_caught_on_the_first_run_too(self, monkeypatch):
        # Validated up front, so a typo fails immediately instead of lying dormant until the
        # first run that actually resumes from a saved position.
        comp = self._comp(monkeypatch, None)
        row = self._row(load_type="incremental_load", initial_start="2026-01-01", lookback="nonsense")
        with pytest.raises(UserException, match="Could not parse 'Look Back'"):
            comp._compute_lower_bound(row, is_int=False)

    def test_non_numeric_saved_position_on_an_int_field_fails_visibly(self, monkeypatch):
        # Raising bare here would exit 2 as an "application" error whose stderr Keboola hides.
        comp = self._comp(monkeypatch, "2026-05-10")
        row = self._row(load_type="incremental_load", lookback="2 days")
        with pytest.raises(UserException, match="not a number"):
            comp._compute_lower_bound(row, is_int=True)

    def test_lookback_duration_is_independent_of_wall_clock(self):
        with freeze_time("2026-07-16T12:00:00+00:00"):
            first = Component._parse_lookback("3 days")
        with freeze_time("2027-02-01T23:59:00+00:00"):
            second = Component._parse_lookback("3 days")
        assert first == second == timedelta(days=3)


class TestLateArrivingRecordRegression:
    """End-to-end reproduction of the reported data loss.

    Drives the real extraction path — filter construction, a fake Medallia that honours that
    filter, the CSV writer and the state write — rather than asserting on a bound in isolation.

    Scenario: a survey is created 2026-07-08 but only completes (and so only enters the
    ``feedback`` view) after the watermark has advanced to 2026-07-10.
    """

    LATE = {"id": "late-1", "e_creationdate": "2026-07-08", "a_score": "9"}
    FRESH = {"id": "fresh-1", "e_creationdate": "2026-07-11", "a_score": "7"}

    def _extract(self, tmp_path, monkeypatch, lookback: str):
        """Run one incremental resume against a fake Medallia; return (rows, filter, watermark)."""
        from client.medallia_client import GenericQueryBuilder

        row = RowConfiguration(
            data_object="feedback",
            mode="structured",
            incremental_field="e_creationdate",
            load_type="incremental_load",
            lookback=lookback,
        )
        comp = object.__new__(Component)
        monkeypatch.setattr(comp, "get_state_file", lambda: {"last_incremental_value": "2026-07-10"})

        lower = comp._compute_lower_bound(row, is_int=False)
        builder = GenericQueryBuilder(
            object_name="feedback",
            node_shape=SHAPE_DATA,
            selected_fields=["e_creationdate", "a_score"],
            scalar_fields=["id", "e_creationdate", "a_score"],
            incremental_field="e_creationdate",
            filter_tree=None,
            supports_filter=True,
            supports_order=True,
            has_id=True,
        )
        query = builder.build_query(lower, "2026-07-12")

        # Fake Medallia: return only records the emitted gte bound actually admits.
        match = re.search(r'gte:\s*"([^"]+)"', query)
        assert match is not None, f"no gte clause was emitted into the query: {query}"
        gte = match.group(1)
        served = [n for n in (self.LATE, self.FRESH) if n["e_creationdate"] >= gte]

        csv_path = tmp_path / f"feedback_{lookback or 'none'}.csv"
        watermark = comp._write_rows(
            str(csv_path), ["id", "e_creationdate", "a_score"], served, True, "e_creationdate", False, lower
        )
        with open(csv_path, encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        return rows, gte, comp._clamp_watermark(comp._normalize_watermark(watermark, False), False)

    def test_without_lookback_the_late_record_is_lost(self, tmp_path, monkeypatch):
        rows, gte, watermark = self._extract(tmp_path, monkeypatch, lookback="")
        assert gte == "2026-07-10"
        ids = [r["id"] for r in rows]
        assert ids == ["fresh-1"], "the late-arriving record is silently absent from the output"
        assert watermark == "2026-07-11"  # and the cursor moves past it, forever

    def test_with_lookback_the_late_record_is_recovered(self, tmp_path, monkeypatch):
        rows, gte, watermark = self._extract(tmp_path, monkeypatch, lookback="7 days")
        assert gte == "2026-07-03"
        ids = sorted(r["id"] for r in rows)
        assert ids == ["fresh-1", "late-1"], "the late-arriving record is recovered"
        assert watermark == "2026-07-11"  # the cursor still advances normally

    def test_lookback_run_returning_nothing_does_not_rewind_the_cursor(self, tmp_path, monkeypatch):
        """The drift guard: an empty Look Back run must not persist the shifted-back bound."""
        row = RowConfiguration(
            data_object="feedback",
            mode="structured",
            incremental_field="e_creationdate",
            load_type="incremental_load",
            lookback="7 days",
        )
        comp = object.__new__(Component)
        monkeypatch.setattr(comp, "get_state_file", lambda: {"last_incremental_value": "2026-07-10"})
        lower = comp._compute_lower_bound(row, is_int=False)
        assert lower == "2026-07-03"

        csv_path = tmp_path / "empty.csv"
        watermark = comp._write_rows(str(csv_path), ["id", "e_creationdate"], [], True, "e_creationdate", False, lower)
        assert watermark == "2026-07-03"  # _write_rows hands back the seed unchanged
        assert comp._clamp_watermark(comp._normalize_watermark(watermark, False), False) == "2026-07-10"


class TestReprocessRange:
    """The backfill switch makes Start/End Date authoritative without disturbing the cursor."""

    def _row(self, **kw) -> RowConfiguration:
        base = dict(data_object="feedback", mode="structured", incremental_field="e_creationdate")
        base.update(kw)
        return RowConfiguration(**base)

    def _comp(self, monkeypatch, stored):
        comp = object.__new__(Component)
        monkeypatch.setattr(comp, "get_state_file", lambda: {"last_incremental_value": stored})
        return comp

    def test_default_is_off_and_resumes_from_the_watermark(self, monkeypatch):
        comp = self._comp(monkeypatch, "2026-08-19")
        row = self._row(load_type="incremental_load", initial_start="2026-07-27")
        assert row.reprocess_range is False
        assert comp._compute_lower_bound(row, is_int=False) == "2026-08-19"

    def test_on_uses_the_start_date_instead_of_the_watermark(self, monkeypatch):
        comp = self._comp(monkeypatch, "2026-08-19")
        row = self._row(load_type="incremental_load", initial_start="2026-07-27", reprocess_range=True)
        assert comp._compute_lower_bound(row, is_int=False) == "2026-07-27"

    def test_on_without_a_start_date_is_refused_rather_than_pulling_all_history(self, monkeypatch):
        # Falling through to an unbounded lower bound here would silently re-pull the object's
        # entire history on a tenant that already has a perfectly good saved position.
        comp = self._comp(monkeypatch, "2026-08-19")
        row = self._row(load_type="incremental_load", reprocess_range=True)
        with pytest.raises(UserException, match="Reload the dates above"):
            comp._compute_lower_bound(row, is_int=False)


class TestWatermarkClamp:
    """The persisted cursor only ever moves forward."""

    def _comp(self, monkeypatch, stored):
        comp = object.__new__(Component)
        state = {"last_incremental_value": stored} if stored is not None else {}
        monkeypatch.setattr(comp, "get_state_file", lambda: state)
        return comp

    def test_a_backfill_run_does_not_rewind_the_cursor(self, monkeypatch):
        comp = self._comp(monkeypatch, "2026-08-19")
        assert comp._clamp_watermark("2026-08-02", is_int=False) == "2026-08-19"

    def test_a_normal_run_still_advances(self, monkeypatch):
        comp = self._comp(monkeypatch, "2026-08-19")
        assert comp._clamp_watermark("2026-08-20", is_int=False) == "2026-08-20"

    def test_an_empty_lookback_run_cannot_drift_backwards(self, monkeypatch):
        # _write_rows seeds the watermark from the (shifted-back) lower bound, so an empty run
        # would otherwise persist the shifted value and reach further back on every run.
        comp = self._comp(monkeypatch, "2026-08-19")
        assert comp._clamp_watermark("2026-08-12", is_int=False) == "2026-08-19"

    def test_epoch_clamp_compares_numerically(self, monkeypatch):
        comp = self._comp(monkeypatch, 1785551000)
        assert comp._clamp_watermark(1785000000, is_int=True) == 1785551000
        comp = self._comp(monkeypatch, 1785551000)
        assert comp._clamp_watermark(1785600000, is_int=True) == 1785600000

    def test_no_stored_value_passes_through(self, monkeypatch):
        comp = self._comp(monkeypatch, None)
        assert comp._clamp_watermark("2026-08-02", is_int=False) == "2026-08-02"

    def test_none_watermark_stays_none(self, monkeypatch):
        comp = self._comp(monkeypatch, "2026-08-19")
        assert comp._clamp_watermark(None, is_int=False) is None

    def test_a_legacy_timestamp_in_state_still_heals_to_day_granularity(self, monkeypatch):
        # State written before watermarks were day-floored holds a full timestamp. Clamping to it
        # verbatim would reinstate that format permanently, defeating the self-heal.
        comp = self._comp(monkeypatch, "2026-08-19 23:30:11")
        assert comp._clamp_watermark("2026-08-19", is_int=False) == "2026-08-19"


class TestNewRowOptionsAreBackwardsCompatible:
    def test_a_config_without_the_new_keys_parses_with_todays_behaviour(self):
        row = RowConfiguration(
            data_object="feedback",
            mode="structured",
            incremental_field="e_creationdate",
            load_type="incremental_load",
            initial_start="yesterday",
        )
        assert row.lookback == ""
        assert row.reprocess_range is False


class TestUpperBound:
    def test_int_upper_bound_is_epoch_seconds(self):
        with freeze_time("2026-07-16T12:00:00+00:00"):
            bound = Component._upper_bound(is_int=True)
        assert isinstance(bound, int)
        assert bound == 1784203200

    def test_string_upper_bound_is_date_only(self):
        with freeze_time("2026-07-16T12:34:56+00:00"):
            bound = Component._upper_bound(is_int=False)
        assert bound == "2026-07-16"

    def test_explicit_end_date_is_inclusive_next_day(self):
        # lt bound, so an inclusive End Date resolves to the day AFTER (records through end-of-day).
        assert Component._upper_bound(is_int=False, end_date="2026-01-10") == "2026-01-11"
        assert Component._upper_bound(is_int=True, end_date="2026-01-10") == 1768089600

    def test_relative_end_date(self):
        with freeze_time("2026-07-16T12:00:00+00:00"):
            assert Component._upper_bound(is_int=False, end_date="yesterday") == "2026-07-16"  # 07-15 +1

    def test_bad_end_date_raises_user_exception(self):
        with pytest.raises(UserException, match="Could not parse 'End Date'"):
            Component._upper_bound(is_int=False, end_date="not a date")


class TestRawPlaceholders:
    def _row(self, query: str, start: str = "", end: str = "") -> RowConfiguration:
        return RowConfiguration(mode="raw", raw_query=query, output_table="t", initial_start=start, end_date=end)

    def test_substitutes_start_and_end(self):
        comp = object.__new__(Component)
        q = 'q { feedback(filter:{and:[{gte:"{{start_date}}"},{lt:"{{end_date}}"}]}) }'
        out = comp._apply_raw_placeholders(self._row(q, start="2026-01-01", end="2026-02-01"))
        assert '"2026-01-01"' in out and '"2026-02-01"' in out and "{{" not in out

    def test_relative_start_placeholder(self):
        comp = object.__new__(Component)
        with freeze_time("2026-07-16T12:00:00+00:00"):
            out = comp._apply_raw_placeholders(self._row('gte:"{{start_date}}"', start="5 days ago"))
        assert out == 'gte:"2026-07-11"'

    def test_placeholder_without_value_raises(self):
        comp = object.__new__(Component)
        with pytest.raises(UserException, match=r"\{\{start_date\}\}"):
            comp._apply_raw_placeholders(self._row('gte:"{{start_date}}"', start=""))

    def test_no_placeholder_leaves_query_unchanged(self):
        comp = object.__new__(Component)
        q = "q { feedback(first:$first) { nodes { id } pageInfo { hasNextPage endCursor } } }"
        assert comp._apply_raw_placeholders(self._row(q, start="2026-01-01")) == q


# ==================================================================================================
# 10. hasNextPage paginator
# ==================================================================================================


def _page_response(nodes: list[dict], has_next: bool, end_cursor: str | None) -> _StubResponse:
    return _StubResponse(
        json_data={
            "data": {"feedback": {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor}}}
        }
    )


class TestPaginator:
    def test_two_pages_yields_all_nodes_and_carries_after_cursor(self):
        session = _StubSession(
            [
                _page_response([{"id": "1"}], has_next=True, end_cursor="c1"),
                _page_response([{"id": "2"}], has_next=False, end_cursor=None),
            ]
        )
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        nodes = list(client.fetch_object("feedback", "query {}", page_size=50))
        assert nodes == [{"id": "1"}, {"id": "2"}]
        assert len(session.requests) == 2
        assert session.requests[0]["json"]["variables"] == {"first": 50, "after": None}
        assert session.requests[1]["json"]["variables"] == {"first": 50, "after": "c1"}

    def test_total_count_is_never_required_or_read(self):
        # No totalCount anywhere in the payload; pagination must still complete successfully.
        session = _StubSession([_page_response([{"id": "1"}], has_next=False, end_cursor=None)])
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        nodes = list(client.fetch_object("feedback", "query {}", page_size=50))
        assert nodes == [{"id": "1"}]

    def test_has_next_page_true_with_empty_end_cursor_stops_safely(self):
        session = _StubSession([_page_response([{"id": "1"}], has_next=True, end_cursor="")])
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        nodes = list(client.fetch_object("feedback", "query {}", page_size=50))
        assert nodes == [{"id": "1"}]
        assert len(session.requests) == 1

    def test_nonnull_int_cursor_seeds_zero_on_first_page(self):
        # A NonNull-Int `after` (socialURLs / unitWarnings …) cannot be null on page 1 — the gateway
        # 500s. It must be seeded with 0 instead. Regression for the exhaustive-smoke failure.
        session = _StubSession([_page_response([{"id": "1"}], has_next=False, end_cursor=None)])
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        nodes = list(client.fetch_object("feedback", "query {}", page_size=25, after_type="Int!"))
        assert nodes == [{"id": "1"}]
        assert session.requests[0]["json"]["variables"] == {"first": 25, "after": 0}

    def test_nonnull_int_cursor_coerces_numeric_end_cursor_to_int(self):
        session = _StubSession(
            [
                _page_response([{"id": "1"}], has_next=True, end_cursor="25"),
                _page_response([{"id": "2"}], has_next=False, end_cursor=None),
            ]
        )
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        nodes = list(client.fetch_object("feedback", "query {}", page_size=25, after_type="Int!"))
        assert nodes == [{"id": "1"}, {"id": "2"}]
        assert session.requests[0]["json"]["variables"] == {"first": 25, "after": 0}
        assert session.requests[1]["json"]["variables"] == {"first": 25, "after": 25}  # int, not "25"

    def test_nullable_cursor_still_starts_null(self):
        session = _StubSession([_page_response([{"id": "1"}], has_next=False, end_cursor=None)])
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        list(client.fetch_object("feedback", "query {}", page_size=25, after_type="ID"))
        assert session.requests[0]["json"]["variables"] == {"first": 25, "after": None}


class TestRunWrapsDataFetchError:
    def test_client_error_during_fetch_becomes_user_exception(self, monkeypatch):
        # A data-fetch MedalliaClientError must surface as a clean UserException (exit 1) instead of
        # an uncaught exit-2 "application" crash whose stderr Keboola hides from the user.
        comp = object.__new__(Component)
        row = RowConfiguration(data_object="socialURLs", mode="structured")
        monkeypatch.setattr(comp, "_get_config", lambda: None)
        monkeypatch.setattr(comp, "_get_row", lambda: row)
        monkeypatch.setattr(comp, "_build_client", lambda config: object())

        def boom(client, r):
            raise MedalliaClientError("Medallia returned HTTP 500 after 5 retries.")

        monkeypatch.setattr(comp, "_run_structured", boom)
        with pytest.raises(UserException, match=r"socialURLs.*HTTP 500"):
            comp.run()


class TestResponseBodyScrubberIdempotency:
    """The scrub_before_read value scrubber is applied multiple times with the SAME instance
    (pre-read + cassette copy + every replay read), so it MUST be idempotent for every node
    shape. Regression: a drifting `_node_counter` on id-less nodes (which lack the `RESP-` id
    sentinel) made their run cassettes non-deterministic."""

    MARKER = MedalliaResponseBodySanitizer._SCRUBBED_MARKER

    @staticmethod
    def _resp(obj):
        return {"body": {"string": json.dumps({"data": obj})}}

    def _apply(self, san, response, connection, times):
        out = []
        for _ in range(times):
            body = json.loads(san.before_record_response(response)["body"]["string"])
            out.append(body["data"][connection]["nodes"][0])
        return out

    def test_id_less_node_is_idempotent_across_applies(self):
        san = MedalliaResponseBodySanitizer()
        resp = self._resp(
            {
                "socialURLs": {
                    "nodes": [{"textIdentifier": "T", "name": "RealName", "address": "RealAddr"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        )
        out = self._apply(san, resp, "socialURLs", 3)
        assert out[0] == out[1] == out[2]  # stable synthetic values across re-applies
        assert "RealName" not in json.dumps(out) and "RealAddr" not in json.dumps(out)  # scrubbed
        assert out[0][self.MARKER] is True

    def test_id_bearing_node_stays_idempotent(self):
        san = MedalliaResponseBodySanitizer()
        resp = self._resp(
            {
                "feedback": {
                    "nodes": [{"id": "12345", "name": "RealName"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        )
        out = self._apply(san, resp, "feedback", 3)
        assert out[0] == out[1] == out[2]
        assert out[0]["id"] == "RESP-0001"

    def test_scrub_marker_never_becomes_an_output_column(self):
        san = MedalliaResponseBodySanitizer()
        resp = self._resp(
            {
                "socialURLs": {
                    "nodes": [{"textIdentifier": "T", "name": "RealName", "address": "RealAddr"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        )
        node = self._apply(san, resp, "socialURLs", 1)[0]
        assert self.MARKER not in flatten_node(node)


# ==================================================================================================
# 11. MedalliaClient error / retry paths
# ==================================================================================================


class TestMedalliaClientAuthRetry:
    def test_single_401_re_mints_token_and_retries(self):
        session = _StubSession(
            [
                _StubResponse(status_code=401),
                _StubResponse(status_code=200, json_data={"data": {"ok": 1}}),
            ]
        )
        token_manager = _StubTokenManager()
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=token_manager, session=session)
        data = client.run_metadata_query("query { __typename }")
        assert data == {"ok": 1}
        assert token_manager.invalidate_calls == 1

    def test_second_consecutive_401_raises_user_exception(self):
        session = _StubSession([_StubResponse(status_code=401), _StubResponse(status_code=401)])
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        with pytest.raises(UserException):
            client.run_metadata_query("query { __typename }")


class TestMedalliaClientHttpErrors:
    def test_non_retryable_4xx_raises_user_exception(self):
        session = _StubSession([_StubResponse(status_code=400)])
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        with pytest.raises(UserException):
            client.run_metadata_query("query { __typename }")

    def test_retryable_5xx_retried_then_client_error_after_max_retries(self):
        session = _StubSession([_StubResponse(status_code=500) for _ in range(3)])
        client = MedalliaClient(
            api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session, max_retries=2
        )
        with pytest.raises(MedalliaClientError):
            client.run_metadata_query("query { __typename }")
        assert len(session.requests) == 3

    def test_retryable_429_eventually_succeeds(self):
        session = _StubSession(
            [
                _StubResponse(status_code=429),
                _StubResponse(status_code=200, json_data={"data": {"ok": 1}}),
            ]
        )
        client = MedalliaClient(
            api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session, max_retries=2
        )
        assert client.run_metadata_query("query { __typename }") == {"ok": 1}


class TestMedalliaClientGraphqlErrors:
    def test_fatal_graphql_error_raises_user_exception(self):
        session = _StubSession(
            [_StubResponse(status_code=200, json_data={"data": {}, "errors": [{"message": "Something exploded"}]})]
        )
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        with pytest.raises(UserException):
            client.run_metadata_query("query { __typename }")

    def test_invalid_field_id_error_is_tolerated_and_data_returned(self):
        session = _StubSession(
            [
                _StubResponse(
                    status_code=200,
                    json_data={"data": {"feedback": {"nodes": []}}, "errors": [{"message": "Invalid field id: xyz"}]},
                )
            ]
        )
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        data = client.run_metadata_query("query { __typename }")
        assert data == {"feedback": {"nodes": []}}

    def test_compute_cost_only_estimated_cost_message_is_tolerated(self):
        session = _StubSession(
            [
                _StubResponse(
                    status_code=200,
                    json_data={"data": {}, "errors": [{"message": "Estimated query cost is: 1."}]},
                )
            ]
        )
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        data = client.run_metadata_query("query { __typename }", compute_cost_only=True)
        assert data == {}

    def test_estimated_cost_message_without_compute_cost_only_is_fatal(self):
        # Guards against accidentally tolerating the cost message on a real (non-preflight) run.
        session = _StubSession(
            [
                _StubResponse(
                    status_code=200, json_data={"data": {}, "errors": [{"message": "Estimated query cost is: 1."}]}
                )
            ]
        )
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        with pytest.raises(UserException):
            client.run_metadata_query("query { __typename }", compute_cost_only=False)


class TestMedalliaTokenManager:
    def test_mint_success_sets_token(self):
        session = _StubSession(
            [_StubResponse(status_code=200, json_data={"access_token": "tok-1", "expires_in": 3600})]
        )
        manager = MedalliaTokenManager("acme.medallia.com", "acme", "cid", "secret", session=session)
        assert manager.get_token() == "tok-1"

    def test_token_reused_until_near_expiry(self):
        session = _StubSession(
            [_StubResponse(status_code=200, json_data={"access_token": "tok-1", "expires_in": 3600})]
        )
        manager = MedalliaTokenManager("acme.medallia.com", "acme", "cid", "secret", session=session)
        assert manager.get_token() == "tok-1"
        assert manager.get_token() == "tok-1"
        assert len(session.requests) == 1

    def test_token_re_minted_within_expiry_skew(self):
        session = _StubSession(
            [
                _StubResponse(status_code=200, json_data={"access_token": "tok-1", "expires_in": 100}),
                _StubResponse(status_code=200, json_data={"access_token": "tok-2", "expires_in": 3600}),
            ]
        )
        # expiry_skew_seconds (300) > expires_in (100) on the first mint, so the token is
        # already "near expiry" the moment it is minted -> the next get_token() re-mints.
        manager = MedalliaTokenManager(
            "acme.medallia.com", "acme", "cid", "secret", session=session, expiry_skew_seconds=300
        )
        assert manager.get_token() == "tok-1"
        assert manager.get_token() == "tok-2"
        assert len(session.requests) == 2

    def test_401_at_token_endpoint_raises_user_exception(self):
        session = _StubSession([_StubResponse(status_code=401)])
        manager = MedalliaTokenManager("acme.medallia.com", "acme", "cid", "secret", session=session)
        with pytest.raises(UserException):
            manager.get_token()

    def test_4xx_at_token_endpoint_raises_user_exception(self):
        session = _StubSession([_StubResponse(status_code=403)])
        manager = MedalliaTokenManager("acme.medallia.com", "acme", "cid", "secret", session=session)
        with pytest.raises(UserException):
            manager.get_token()

    def test_missing_access_token_raises_client_error(self):
        session = _StubSession([_StubResponse(status_code=200, json_data={"expires_in": 3600})])
        manager = MedalliaTokenManager("acme.medallia.com", "acme", "cid", "secret", session=session)
        with pytest.raises(MedalliaClientError):
            manager.get_token()

    def test_non_numeric_expires_in_falls_back_and_does_not_crash(self):
        # A non-numeric expires_in must not raise an uncaught ValueError (that would be exit 2).
        session = _StubSession(
            [_StubResponse(status_code=200, json_data={"access_token": "tok-1", "expires_in": "not-a-number"})]
        )
        manager = MedalliaTokenManager("acme.medallia.com", "acme", "cid", "secret", session=session)
        assert manager.get_token() == "tok-1"


# ==================================================================================================
# 12. raw-mode validation
# ==================================================================================================


class TestRawModeValidation:
    def test_validate_raw_static_passes_when_all_tokens_present(self):
        query = (
            "query ($first: Int, $after: String) { feedback(first: $first, after: $after) "
            "{ nodes { id } pageInfo { hasNextPage endCursor } } }"
        )
        Component._validate_raw_static(query)  # must not raise

    def test_validate_raw_static_lists_missing_tokens(self):
        with pytest.raises(UserException) as exc_info:
            Component._validate_raw_static("query { feedback { nodes { id } } }")
        message = str(exc_info.value)
        assert "$first" in message
        assert "$after" in message
        assert "pageInfo" in message

    def test_raw_connection_returns_the_single_connection(self):
        data = {"feedback": {"nodes": [{"id": "1"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        assert Component._raw_connection(data) == data["feedback"]

    def test_raw_connection_zero_connections_raises(self):
        with pytest.raises(UserException, match="no Relay connection"):
            Component._raw_connection({"someScalar": 1})

    def test_raw_connection_multiple_connections_raises_with_count(self):
        data = {
            "feedback": {"nodes": [], "pageInfo": {}},
            "customers": {"nodes": [], "pageInfo": {}},
        }
        with pytest.raises(UserException, match="exactly one connection \\(found 2"):
            Component._raw_connection(data)

    def test_raw_connection_missing_page_info_raises(self):
        data = {"feedback": {"nodes": [{"id": "1"}]}}
        with pytest.raises(UserException, match="must select pageInfo"):
            Component._raw_connection(data)


# ==================================================================================================
# 12b. output-table name resolution (per-row override, both modes)
# ==================================================================================================


class TestOutputTableName:
    def test_explicit_override_wins_over_default(self):
        # Structured mode: an override lets the same object land in a distinct table.
        assert Component._output_table_name("feedback_variant_a", "feedback") == "feedback_variant_a.csv"

    def test_falls_back_to_default_when_empty(self):
        assert Component._output_table_name("", "feedback") == "feedback.csv"

    def test_falls_back_to_default_when_whitespace_only(self):
        assert Component._output_table_name("   ", "customers") == "customers.csv"

    def test_override_is_stripped(self):
        assert Component._output_table_name("  raw_out  ", "") == "raw_out.csv"

    def test_raw_style_uses_explicit_with_empty_default(self):
        # Raw mode passes default="" — presence is enforced separately in _run_raw.
        assert Component._output_table_name("raw_out", "") == "raw_out.csv"


# ==================================================================================================
# 13. dataType -> BaseType mapping
# ==================================================================================================


class TestBaseTypeMapping:
    # Medallia's fieldData/data APIs return display *labels* (strings) for every field, and
    # numeric-declared fields (dataType INT/FLOAT) can carry enumerated text — e.g. e_status is
    # dataType=INT but returns "COMPLETED", a_customerid is INT but can be non-numeric. Typing
    # those INTEGER/NUMERIC breaks the warehouse load, so catalogue numerics are typed STRING;
    # only the temporal types (parseable machine formats) stay typed.
    @pytest.mark.parametrize("data_type", ["INT", "INTEGER"])
    def test_int_datatype_maps_to_string(self, data_type):
        result = Component._base_type("e_nps", {"e_nps": {"dataType": data_type}})
        assert result == BaseType.string()

    def test_float_datatype_maps_to_string(self):
        result = Component._base_type("score", {"score": {"dataType": "FLOAT"}})
        assert result == BaseType.string()

    def test_int_declared_field_with_text_value_is_string(self):
        # Regression for the platform load failure: e_status (dataType=INT) holds "COMPLETED".
        result = Component._base_type("e_status", {"e_status": {"dataType": "INT"}})
        assert result == BaseType.string()

    def test_date_maps_to_date(self):
        result = Component._base_type("e_date", {"e_date": {"dataType": "DATE"}})
        assert result == BaseType.date()

    def test_datetime_maps_to_timestamp(self):
        result = Component._base_type("e_ts", {"e_ts": {"dataType": "DATETIME"}})
        assert result == BaseType.timestamp()

    @pytest.mark.parametrize("data_type", ["STRING", "EMAIL", "ENUM"])
    def test_other_datatypes_map_to_string(self, data_type):
        result = Component._base_type("x", {"x": {"dataType": data_type}})
        assert result == BaseType.string()

    def test_multivalued_int_still_maps_to_string(self):
        result = Component._base_type("tags", {"tags": {"dataType": "INT", "multivalued": True}})
        assert result == BaseType.string()

    def test_scalar_type_int_fallback(self):
        result = Component._base_type("seq", {"seq": {"scalar_type": "Int"}})
        assert result == BaseType.integer()

    def test_scalar_type_float_fallback(self):
        result = Component._base_type("ratio", {"ratio": {"scalar_type": "Float"}})
        assert result == BaseType.numeric()

    def test_scalar_type_boolean_fallback(self):
        result = Component._base_type("active", {"active": {"scalar_type": "Boolean"}})
        assert result == BaseType.boolean()

    def test_scalar_type_other_falls_back_to_string(self):
        result = Component._base_type("name", {"name": {"scalar_type": "String"}})
        assert result == BaseType.string()

    def test_no_metadata_defaults_to_string(self):
        result = Component._base_type("unknown", {})
        assert result == BaseType.string()


# ==================================================================================================
# 15. 2xx non-JSON body guard (IMP-2) — never an uncaught JSONDecodeError -> exit 2
# ==================================================================================================


class TestNonJsonResponseGuard:
    """A 2xx with a non-JSON / empty body is user-actionable (wrong host) -> UserException (exit 1)."""

    def test_token_endpoint_non_json_2xx_raises_user_exception(self):
        session = _StubSession([_StubResponse(status_code=200, raise_json=True)])
        manager = MedalliaTokenManager("acme.medallia.com", "acme", "cid", "secret", session=session)
        with pytest.raises(UserException, match="non-JSON"):
            manager.get_token()

    def test_query_api_non_json_2xx_raises_user_exception(self):
        session = _StubSession([_StubResponse(status_code=200, raise_json=True)])
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        with pytest.raises(UserException, match="non-JSON"):
            client.run_metadata_query("query { __typename }")

    def test_query_api_non_json_2xx_message_names_api_host(self):
        session = _StubSession([_StubResponse(status_code=200, raise_json=True)])
        client = MedalliaClient(api_host="x.apis.medallia.com", token_manager=_StubTokenManager(), session=session)
        with pytest.raises(UserException) as exc_info:
            client.run_metadata_query("query { __typename }")
        assert "API host" in str(exc_info.value)


# ==================================================================================================
# 16. Object-aware field-metadata routing (IMP-1, spec §6.3) — customerSchema for `customers`
# ==================================================================================================


class _StubMetadataClient:
    """Client double exposing only ``run_metadata_query`` (for sync-action / metadata tests)."""

    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self._response = response if response is not None else {}
        self._error = error
        self.queries: list[dict] = []

    def run_metadata_query(self, query: str, **kwargs) -> dict:
        self.queries.append({"query": query, **kwargs})
        if self._error is not None:
            raise self._error
        return self._response


# customerSchema (ContactSchema) metadata — ContactAttribute nodes under the NESTED ``attributes``
# Relay connection: key/name/type/containsPii/isKey/isIndexed (NOT the Field type's
# id/name/dataType/sortable/multivalued). The component normalises key→id, type→dataType,
# isIndexed→sortable. Covers a DATE, a DATETIME, an indexed INT (incremental candidate), a
# NON-indexed INT (must NOT surface as a date field) and plain string/email attributes.
_CUSTOMER_SCHEMA_RESPONSE = {
    "customerSchema": {
        "attributes": {
            "nodes": [
                {
                    "key": "c_email",
                    "name": "Email",
                    "type": "EMAIL",
                    "containsPii": True,
                    "isKey": False,
                    "isIndexed": False,
                },
                {
                    "key": "c_created",
                    "name": "Created Date",
                    "type": "DATE",
                    "containsPii": False,
                    "isKey": False,
                    "isIndexed": True,
                },
                {
                    "key": "c_lastseen",
                    "name": "Last Seen",
                    "type": "DATETIME",
                    "containsPii": False,
                    "isKey": False,
                    "isIndexed": True,
                },
                {
                    "key": "c_loyalty",
                    "name": "Loyalty Points",
                    "type": "INT",
                    "containsPii": False,
                    "isKey": False,
                    "isIndexed": True,
                },
                {
                    "key": "c_nps",
                    "name": "NPS",
                    "type": "INT",
                    "containsPii": False,
                    "isKey": False,
                    "isIndexed": False,
                },
                {
                    "key": "c_name",
                    "name": "Full Name",
                    "type": "STRING",
                    "containsPii": True,
                    "isKey": True,
                    "isIndexed": False,
                },
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
}


class TestMetadataNodeRouting:
    def test_customers_routes_to_customer_schema(self):
        shape = ObjectShape("customers", SHAPE_DATA, True, False, False)
        assert Component._metadata_node(shape) == "customerSchema"

    def test_fielddata_object_routes_to_fields_catalog(self):
        shape = ObjectShape("feedback", SHAPE_FIELDDATA, True, True, True)
        assert Component._metadata_node(shape) == "fields"

    def test_scalar_object_has_no_catalogue(self):
        shape = ObjectShape("programs", SHAPE_SCALAR, True, False, False, scalar_fields={"name": "String"})
        assert Component._metadata_node(shape) is None


class TestFieldMetadataRouting:
    def test_customers_metadata_from_customer_schema_and_types_from_datatype(self):
        client = _StubMetadataClient(_CUSTOMER_SCHEMA_RESPONSE)
        shape = ObjectShape("customers", SHAPE_DATA, True, False, False, scalar_fields={"c_id": "String"})
        comp = object.__new__(Component)
        meta = comp._field_metadata(client, shape)
        # routed to customerSchema.attributes (NOT the global `fields` catalogue, and NOT the
        # bogus customerSchema.fields the first cut queried — ContactSchema has no `fields`)
        assert "customerSchema" in client.queries[0]["query"]
        assert "attributes" in client.queries[0]["query"]
        assert meta["c_created"]["dataType"] == "DATE"
        # introspected bare node scalar is preserved (merged, not dropped)
        assert meta["c_id"] == {"scalar_type": "String"}
        # _base_type now types customers columns from dataType instead of defaulting to string
        assert Component._base_type("c_created", meta) == BaseType.date()
        assert Component._base_type("c_lastseen", meta) == BaseType.timestamp()
        # c_loyalty has dataType=INT, but Medallia catalogue numerics are typed STRING (they can
        # carry enumerated labels); only temporal dataTypes stay typed.
        assert Component._base_type("c_loyalty", meta) == BaseType.string()

    def test_metadata_fetch_failure_degrades_to_scalar_seed(self):
        client = _StubMetadataClient(error=MedalliaClientError("boom"))
        shape = ObjectShape("customers", SHAPE_DATA, True, False, False, scalar_fields={"c_id": "String"})
        comp = object.__new__(Component)
        meta = comp._field_metadata(client, shape)
        assert meta == {"c_id": {"scalar_type": "String"}}

    def test_metadata_catalogue_query_error_degrades_not_aborts(self):
        # A GraphQL/validation error from the catalogue query itself (UserException) must degrade to
        # the scalar seed, not abort the extraction — metadata only refines typing/pickers, and the
        # object was already validated upstream. Regression guard for the customerSchema fix.
        client = _StubMetadataClient(error=UserException("Field 'fields' in type 'ContactSchema' is undefined"))
        shape = ObjectShape("customers", SHAPE_DATA, True, False, False, scalar_fields={"c_id": "String"})
        comp = object.__new__(Component)
        meta = comp._field_metadata(client, shape)
        assert meta == {"c_id": {"scalar_type": "String"}}


def _bare_component(monkeypatch, row: RowConfiguration, client: _StubMetadataClient, shape: ObjectShape) -> Component:
    """A Component instance bypassing ComponentBase.__init__, with the datadir-touching
    collaborators (config/client/shape) stubbed so a sync action runs purely in memory."""
    comp = object.__new__(Component)
    monkeypatch.setattr(comp, "_get_row", lambda: row)
    monkeypatch.setattr(comp, "_get_config", lambda: None)
    monkeypatch.setattr(comp, "_build_client", lambda config: client)
    monkeypatch.setattr(comp, "_resolve_object", lambda c, name: shape)
    return comp


class TestListFieldsCustomers:
    def test_list_fields_returns_all_customer_schema_fields_by_name(self, monkeypatch):
        client = _StubMetadataClient(_CUSTOMER_SCHEMA_RESPONSE)
        row = RowConfiguration(data_object="customers", mode="structured")
        shape = ObjectShape("customers", SHAPE_DATA, True, False, False)
        comp = _bare_component(monkeypatch, row, client, shape)
        elements = Component.list_fields.__wrapped__(comp)
        assert {e.value for e in elements} == {"c_email", "c_created", "c_lastseen", "c_loyalty", "c_nps", "c_name"}
        assert {e.value: e.label for e in elements}["c_created"] == "Created Date"


class _PagingMetadataClient:
    """run_metadata_query double that returns a queued page per call (for pagination tests)."""

    def __init__(self, pages: list[dict]):
        self._pages = pages
        self.calls: list[dict] = []

    def run_metadata_query(self, query: str, **kwargs) -> dict:
        self.calls.append({"query": query, **kwargs})
        return self._pages[min(len(self.calls) - 1, len(self._pages) - 1)]


def _fld(fid: str, used: list | None = None, data_type: str = "STRING") -> dict:
    d = {"id": fid, "name": fid.upper(), "dataType": data_type, "sortable": False, "multivalued": False}
    if used is not None:
        d["usedOnPrograms"] = used
    return d


class TestFieldCatalogPagination:
    """#1 — the field catalogue is paged in FULL via pageInfo (no fixed first:N cap)."""

    def test_fetch_follows_pageinfo_across_pages(self):
        catalog = _METADATA_CATALOGS["fields"]
        client = _PagingMetadataClient(
            [
                {"fields": {"nodes": [_fld("a_1"), _fld("e_2")], "pageInfo": {"hasNextPage": True, "endCursor": "C1"}}},
                {"fields": {"nodes": [_fld("k_3"), _fld("u_4")], "pageInfo": {"hasNextPage": False}}},
            ]
        )
        defs = catalog.fetch(client)  # ty: ignore[invalid-argument-type]
        assert [d["id"] for d in defs] == ["a_1", "e_2", "k_3", "u_4"]  # later k_/u_ fields no longer dropped
        assert len(client.calls) == 2
        assert client.calls[1]["variables"]["after"] == "C1"  # cursor threaded to the next page

    def test_single_page_when_no_next(self):
        catalog = _METADATA_CATALOGS["fields"]
        client = _PagingMetadataClient([{"fields": {"nodes": [_fld("a_1")], "pageInfo": {"hasNextPage": False}}}])
        assert [d["id"] for d in catalog.fetch(client)] == ["a_1"]  # ty: ignore[invalid-argument-type]
        assert len(client.calls) == 1

    def test_empty_end_cursor_stops_instead_of_refetching(self):
        # hasNextPage true but endCursor empty must NOT re-fetch the first page up to the ceiling.
        catalog = _METADATA_CATALOGS["fields"]
        client = _PagingMetadataClient(
            [{"fields": {"nodes": [_fld("a_1")], "pageInfo": {"hasNextPage": True, "endCursor": ""}}}]
        )
        assert [d["id"] for d in catalog.fetch(client)] == ["a_1"]  # ty: ignore[invalid-argument-type]
        assert len(client.calls) == 1  # stopped, did not loop


class TestFieldPickerUsageScope:
    """The Fields picker offers EVERY field by default; ``only_program_fields`` opts into the
    usedOnPrograms scope.

    Regression: usedOnPrograms reflects survey-program usage, so record-attribute fields (a_*,
    and most k_/r_/u_) are always empty for it yet carry data on every record. The DEFAULT picker
    (toggle off) must offer them; turning the toggle ON may hide them by design.
    """

    def _feedback_component(self, monkeypatch, client, only_program_fields=False):
        row = RowConfiguration(data_object="feedback", mode="structured", only_program_fields=only_program_fields)
        shape = ObjectShape("feedback", SHAPE_FIELDDATA, True, True, True)
        return _bare_component(monkeypatch, row, client, shape)

    def _mixed_client(self):
        # a_customerid + k_unused report usedOnPrograms=[]; only e_used is on a program.
        return _PagingMetadataClient(
            [
                {
                    "fields": {
                        "nodes": [
                            _fld("a_customerid", used=[]),
                            _fld("k_unused", used=[]),
                            _fld("e_used", used=["p2"]),
                        ],
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            ]
        )

    def test_default_offers_all_fields_including_empty_used_on_programs(self, monkeypatch):
        # Customer-reported bug: a_* attributes report usedOnPrograms=[] but are populated; the
        # default picker (toggle OFF) must still offer them.
        comp = self._feedback_component(monkeypatch, self._mixed_client(), only_program_fields=False)
        elements = Component.list_fields.__wrapped__(comp)
        assert {e.value for e in elements} == {"a_customerid", "k_unused", "e_used"}

    def test_toggle_on_scopes_to_program_fields(self, monkeypatch):
        # Opt-in trim: only fields with a non-empty usedOnPrograms survive.
        comp = self._feedback_component(monkeypatch, self._mixed_client(), only_program_fields=True)
        elements = Component.list_fields.__wrapped__(comp)
        assert {e.value for e in elements} == {"e_used"}

    def test_toggle_on_falls_back_to_all_when_usage_not_reported(self, monkeypatch):
        # Toggle ON but no field reports usedOnPrograms at all → keep them all (never empty picker).
        client = _PagingMetadataClient(
            [{"fields": {"nodes": [_fld("a_1"), _fld("e_2")], "pageInfo": {"hasNextPage": False}}}]
        )
        comp = self._feedback_component(monkeypatch, client, only_program_fields=True)
        elements = Component.list_fields.__wrapped__(comp)
        assert {e.value for e in elements} == {"a_1", "e_2"}

    def test_typing_metadata_keeps_unused_fields(self, monkeypatch):
        # The usage filter is picker-only: _field_metadata (column typing) must keep every field,
        # so a selected-but-"unused" field still types correctly.
        client = _PagingMetadataClient(
            [{"fields": {"nodes": [_fld("k_unused", used=[], data_type="DATE")], "pageInfo": {"hasNextPage": False}}}]
        )
        shape = ObjectShape("feedback", SHAPE_FIELDDATA, True, True, True)
        comp = object.__new__(Component)
        meta = comp._field_metadata(client, shape)
        assert "k_unused" in meta and meta["k_unused"]["dataType"] == "DATE"


# ==================================================================================================
# 17. listDateFields (IMP-3) — DATE/DATETIME + sortable INT (epoch auto-detect), names shown
# ==================================================================================================


class TestListDateFields:
    def _component(self, monkeypatch) -> tuple[Component, _StubMetadataClient]:
        client = _StubMetadataClient(_CUSTOMER_SCHEMA_RESPONSE)
        row = RowConfiguration(data_object="customers", mode="structured")
        shape = ObjectShape("customers", SHAPE_DATA, True, False, False)
        return _bare_component(monkeypatch, row, client, shape), client

    def test_returns_only_date_datetime_and_sortable_int_fields(self, monkeypatch):
        comp, _ = self._component(monkeypatch)
        elements = Component.list_date_fields.__wrapped__(comp)
        # c_created (DATE), c_lastseen (DATETIME), c_loyalty (sortable INT epoch candidate);
        # NOT c_nps (INT but not sortable), NOT c_email/c_name (non-date).
        assert {e.value for e in elements} == {"c_created", "c_lastseen", "c_loyalty"}

    def test_non_sortable_int_is_excluded(self, monkeypatch):
        comp, _ = self._component(monkeypatch)
        values = {e.value for e in Component.list_date_fields.__wrapped__(comp)}
        assert "c_nps" not in values

    def test_labels_are_field_names(self, monkeypatch):
        comp, _ = self._component(monkeypatch)
        labels = {e.value: e.label for e in Component.list_date_fields.__wrapped__(comp)}
        assert labels["c_lastseen"] == "Last Seen"

    def test_routed_to_customer_schema_not_global_fields_catalogue(self, monkeypatch):
        comp, client = self._component(monkeypatch)
        Component.list_date_fields.__wrapped__(comp)
        assert "customerSchema" in client.queries[0]["query"]

    def test_scalar_int_field_surfaces_for_scalar_shape(self, monkeypatch):
        # Shape (c) has no catalogue: a sortable-int equivalent is surfaced from the introspected
        # scalar type (`Int`), covering the epoch auto-detect on the scalar path too.
        client = _StubMetadataClient({})
        row = RowConfiguration(data_object="programs", mode="structured")
        shape = ObjectShape(
            "programs", SHAPE_SCALAR, True, False, False, scalar_fields={"seq": "Int", "name": "String"}
        )
        comp = _bare_component(monkeypatch, row, client, shape)
        values = {e.value for e in Component.list_date_fields.__wrapped__(comp)}
        assert values == {"seq"}


# ==================================================================================================
# 18. validateQuery (IMP-3) — accept valid single-connection; reject with clean message (exit 1)
# ==================================================================================================


class TestValidateQuery:
    VALID_QUERY = (
        "query ($first: Int, $after: String) { feedback(first: $first, after: $after) "
        "{ nodes { id } pageInfo { hasNextPage endCursor } } }"
    )

    def _component(self, monkeypatch, raw_query: str, client: _StubMetadataClient) -> Component:
        row = RowConfiguration(mode="raw", raw_query=raw_query, output_table="t", page_size=50)
        comp = object.__new__(Component)
        monkeypatch.setattr(comp, "_get_row", lambda: row)
        monkeypatch.setattr(comp, "_get_config", lambda: None)
        monkeypatch.setattr(comp, "_build_client", lambda config: client)
        return comp

    _PREVIEW_RESPONSE = {
        "feedback": {
            "nodes": [{"id": "R1", "score": "9"}, {"id": "R2", "score": "3"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }

    def test_accepts_valid_query_and_returns_row_preview(self, monkeypatch):
        client = _StubMetadataClient(self._PREVIEW_RESPONSE)  # cost pre-flight ok + rows to preview
        comp = self._component(monkeypatch, self.VALID_QUERY, client)
        result = Component.validate_query.__wrapped__(comp)
        assert result.type != MessageType.ERROR
        assert "Preview" in result.message and "R1" in result.message and "R2" in result.message
        # first call = cost-only pre-flight with the paging variables (does not consume quota)
        assert client.queries[0]["compute_cost_only"] is True
        assert client.queries[0]["variables"] == {"first": 50, "after": None}
        # second call = the row preview, NOT cost-only, capped at _PREVIEW_ROWS
        assert client.queries[1].get("compute_cost_only") in (False, None)
        assert client.queries[1]["variables"] == {"first": 5, "after": None}

    def test_valid_query_without_preview_rows_is_not_an_error(self, monkeypatch):
        # cost pre-flight ok, but the response carries no Relay connection to preview -> WARNING, not ERROR
        client = _StubMetadataClient({})
        comp = self._component(monkeypatch, self.VALID_QUERY, client)
        result = Component.validate_query.__wrapped__(comp)
        assert result.type == MessageType.WARNING
        assert "preview could not be fetched" in result.message

    def test_rejects_missing_pageinfo_before_any_call(self, monkeypatch):
        client = _StubMetadataClient({})
        query = "query ($first: Int, $after: String) { feedback(first: $first, after: $after) { nodes { id } } }"
        comp = self._component(monkeypatch, query, client)
        result = Component.validate_query.__wrapped__(comp)
        assert result.type == MessageType.ERROR
        assert "pageInfo" in result.message
        assert client.queries == []  # static contract check fails before the gateway is touched

    def test_rejects_missing_variables(self, monkeypatch):
        client = _StubMetadataClient({})
        query = "query { feedback { nodes { id } pageInfo { hasNextPage endCursor } } }"
        comp = self._component(monkeypatch, query, client)
        result = Component.validate_query.__wrapped__(comp)
        assert result.type == MessageType.ERROR
        assert "$first" in result.message
        assert "$after" in result.message

    def test_rejects_cost_or_compile_failure_as_error_not_exit_2(self, monkeypatch):
        # A cost/compile failure surfaces as a UserException from the client; validateQuery
        # converts it to a clean ERROR result rather than letting it escalate.
        client = _StubMetadataClient(error=UserException("Estimated query cost exceeds the 3M ceiling"))
        comp = self._component(monkeypatch, self.VALID_QUERY, client)
        result = Component.validate_query.__wrapped__(comp)
        assert result.type == MessageType.ERROR
        assert "validation failed" in result.message

    def test_zero_and_multi_connection_rejected_at_runtime_with_user_exception(self):
        # validateQuery is a cost-only pre-flight and cannot count connections; the zero/multi
        # rejection is enforced at run time by `_raw_connection`, as a UserException (exit 1, not
        # an uncaught error -> exit 2).
        with pytest.raises(UserException, match="no Relay connection"):
            Component._raw_connection({"onlyScalar": 1})
        two = {"feedback": {"nodes": [], "pageInfo": {}}, "customers": {"nodes": [], "pageInfo": {}}}
        with pytest.raises(UserException, match="exactly one connection"):
            Component._raw_connection(two)


# ---------------------------------------------------------------------------------------------
# Watermark normalisation (persisted value must match the day-granular bound format the query
# builder emits — a DATE/DATETIME field's value carries a time part that Medallia's date filter
# never produces itself).
# ---------------------------------------------------------------------------------------------
class TestNormalizeWatermark:
    def test_int_watermark_passes_through(self):
        assert Component._normalize_watermark(1785551000, is_int=True) == 1785551000

    def test_none_passes_through(self):
        assert Component._normalize_watermark(None, is_int=False) is None

    def test_datetime_with_space_floored_to_day(self):
        # The exact live case from the smoke config: a persisted DATETIME watermark.
        assert Component._normalize_watermark("2026-07-26 23:30:11", is_int=False) == "2026-07-26"

    def test_iso_t_and_trailing_zone_floored_to_day(self):
        assert Component._normalize_watermark("2026-07-26T23:30:11Z", is_int=False) == "2026-07-26"

    def test_date_only_string_is_unchanged(self):
        assert Component._normalize_watermark("2026-07-26", is_int=False) == "2026-07-26"

    def test_non_iso_but_parseable_is_floored(self):
        assert Component._normalize_watermark("July 26, 2026", is_int=False) == "2026-07-26"

    def test_unparseable_string_left_as_is(self):
        # Never raise on an unexpected format — leave the value untouched rather than lose state.
        assert Component._normalize_watermark("not-a-date", is_int=False) == "not-a-date"


# ---------------------------------------------------------------------------------------------
# Raw-mode empty result: a query returning zero rows is a legitimate empty result, but raw mode
# cannot infer a schema, so it writes NOTHING and succeeds (rather than a malformed zero-column
# table or a hard failure).
# ---------------------------------------------------------------------------------------------
class TestWriteRawTableEmpty:
    @staticmethod
    def _component(tmp_path, monkeypatch):
        """A bare Component whose table factory writes into tmp_path (ComponentBase.__init__ and
        its read-only ``tables_out_path`` bypassed)."""
        comp = object.__new__(Component)
        manifests: list = []

        def _make_table(name, **kwargs):
            return SimpleNamespace(full_path=str(tmp_path / name), **kwargs)

        monkeypatch.setattr(comp, "create_out_table_definition", _make_table)
        monkeypatch.setattr(comp, "write_manifest", lambda table: manifests.append(table))
        return comp, manifests

    def test_zero_rows_writes_no_file_and_no_manifest_and_succeeds(self, tmp_path, monkeypatch):
        comp, manifests = self._component(tmp_path, monkeypatch)
        comp._write_raw_table("empty_out.csv", iter([]))
        assert manifests == []  # no manifest written
        assert not (tmp_path / "empty_out.csv").exists()  # the empty CSV was removed
        assert list(tmp_path.iterdir()) == []  # Storage left completely untouched

    def test_rows_still_write_table_and_manifest(self, tmp_path, monkeypatch):
        comp, manifests = self._component(tmp_path, monkeypatch)
        comp._write_raw_table("out.csv", iter([{"id": "1", "score": {"value": "9"}}]))
        assert len(manifests) == 1  # positive control: a non-empty result still writes
        assert (tmp_path / "out.csv").exists()
