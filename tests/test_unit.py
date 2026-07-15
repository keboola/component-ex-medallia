"""Unit tests for the Medallia Query API client and configuration models.

All tests run against an in-memory stub HTTP session (``FakeHTTPSession``) — no network,
no credentials. They exercise the token manager, query builder, keyset paginator, response
helpers and the Pydantic configuration layer in isolation.
"""

import unittest
from unittest import mock

from keboola.component.exceptions import UserException

from client.medallia_client import (
    MedalliaClient,
    MedalliaClientError,
    MedalliaQueryBuilder,
    MedalliaTokenManager,
    Watermark,
    flatten_node,
    watermark_from_node,
)
from configuration import MAX_PAGE_SIZE, Configuration, LoadType, RowConfiguration

INSTANCE_HOST = "instance.example.test"
API_HOST = "acme.apis.example.test"
COMPANY = "acme"
CLIENT_ID = "client-id"
CLIENT_SECRET = "client-secret"


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeHTTPSession:
    """One stub session shared by the token manager (/token) and client (/query).

    Routes by URL suffix. ``/token`` always mints a fresh token; ``/query`` pops the next
    queued response (a ``FakeResponse`` or an ``Exception`` to raise).
    """

    def __init__(self):
        self.token_calls = 0
        self.query_responses = []
        self.query_calls = []

    def post(self, url, **kwargs):
        if url.endswith("/token"):
            self.token_calls += 1
            return FakeResponse(200, {"access_token": f"tok-{self.token_calls}", "expires_in": 3600})
        self.query_calls.append(kwargs)
        item = self.query_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _node(survey_id, finish_epoch, comment="ok"):
    return {
        "surveyId": {"values": [survey_id]},
        "k_fin": {"values": [finish_epoch]},
        "c1": {"values": [comment]},
    }


def _feedback_page(nodes, total_count):
    return FakeResponse(200, {"data": {"feedback": {"totalCount": total_count, "nodes": nodes}}})


def _token_manager(session, **kwargs):
    # session is intentionally unannotated so the duck-typed FakeHTTPSession is accepted.
    return MedalliaTokenManager(INSTANCE_HOST, COMPANY, CLIENT_ID, CLIENT_SECRET, session=session, **kwargs)


def _make_client(session, **client_kwargs):
    token_manager = MedalliaTokenManager(INSTANCE_HOST, COMPANY, CLIENT_ID, CLIENT_SECRET, session=session)
    query_builder = MedalliaQueryBuilder(
        data_object="feedback",
        survey_id_field_id="a_sid",
        finish_date_field_id="k_fin",
        fields=["c1"],
    )
    return MedalliaClient(API_HOST, token_manager, query_builder, session=session, **client_kwargs)


# --------------------------------------------------------------------------------------
# Token manager
# --------------------------------------------------------------------------------------
class TestTokenManager(unittest.TestCase):
    def test_mint_and_cache(self):
        session = FakeHTTPSession()
        tm = _token_manager(session)
        self.assertEqual(tm.get_token(), "tok-1")
        # Second call is served from cache — no additional mint.
        self.assertEqual(tm.get_token(), "tok-1")
        self.assertEqual(session.token_calls, 1)

    def test_pre_expiry_re_mint(self):
        session = FakeHTTPSession()
        # Huge skew forces "near expiry" on every call → re-mint each time.
        tm = _token_manager(session, expiry_skew_seconds=10**9)
        self.assertEqual(tm.get_token(), "tok-1")
        self.assertEqual(tm.get_token(), "tok-2")
        self.assertEqual(session.token_calls, 2)

    def test_invalidate_forces_re_mint(self):
        session = FakeHTTPSession()
        tm = _token_manager(session)
        self.assertEqual(tm.get_token(), "tok-1")
        tm.invalidate()
        self.assertEqual(tm.get_token(), "tok-2")
        self.assertEqual(session.token_calls, 2)

    def test_401_raises_user_exception(self):
        session = mock.Mock()
        session.post.return_value = FakeResponse(401)
        tm = MedalliaTokenManager(INSTANCE_HOST, COMPANY, CLIENT_ID, CLIENT_SECRET, session=session)
        with self.assertRaises(UserException):
            tm.get_token()

    def test_non_200_raises_client_error(self):
        session = mock.Mock()
        session.post.return_value = FakeResponse(500)
        tm = MedalliaTokenManager(INSTANCE_HOST, COMPANY, CLIENT_ID, CLIENT_SECRET, session=session)
        with self.assertRaises(MedalliaClientError):
            tm.get_token()

    def test_missing_access_token_raises_client_error(self):
        session = mock.Mock()
        session.post.return_value = FakeResponse(200, {"expires_in": 3600})
        tm = MedalliaTokenManager(INSTANCE_HOST, COMPANY, CLIENT_ID, CLIENT_SECRET, session=session)
        with self.assertRaises(MedalliaClientError):
            tm.get_token()


# --------------------------------------------------------------------------------------
# Query builder
# --------------------------------------------------------------------------------------
class TestQueryBuilder(unittest.TestCase):
    def _builder(self, fields=None, business_filters=None):
        return MedalliaQueryBuilder(
            data_object="feedback",
            survey_id_field_id="a_sid",
            finish_date_field_id="k_fin",
            fields=fields if fields is not None else ["c1", "c2"],
            business_filters=business_filters,
        )

    def test_first_run_filter_has_no_keyset_lower_bound(self):
        query = self._builder().build_query(None, end_epoch=2000, page_size=50)
        self.assertIn("first: 50", query)
        # Upper bound present; no keyset OR / gt lower-bound branch on first run.
        self.assertIn('lt: "2000"', query)
        self.assertNotIn("or:", query)
        self.assertNotIn("gt:", query)

    def test_seeded_watermark_builds_keyset_lower_bound(self):
        wm = Watermark(finish_date_epoch=1500, survey_id="S9")
        query = self._builder().build_query(wm, end_epoch=2000, page_size=50)
        self.assertIn("or:", query)
        self.assertIn('gt: "1500"', query)
        self.assertIn('gte: "1500"', query)
        self.assertIn('gt: "S9"', query)

    def test_field_selection_and_reserved_dedupe(self):
        # Include the watermark/survey field ids in ``fields`` — they must not be duplicated.
        query = self._builder(fields=["a_sid", "k_fin", "c1"]).build_query(None, 2000, 10)
        # Reserved ids appear exactly once in the fieldData selection (orderBy also names
        # them, hence matching the ``fieldData(...)`` form rather than bare ``fieldId:``).
        self.assertEqual(query.count('fieldData(fieldId: "a_sid")'), 1)
        self.assertEqual(query.count('fieldData(fieldId: "k_fin")'), 1)
        self.assertIn("surveyId: fieldData", query)
        self.assertIn("c1: fieldData", query)

    def test_order_by_ascending_composite(self):
        query = self._builder().build_query(None, 2000, 10)
        self.assertIn('fieldId: "k_fin", direction: ASC', query)
        self.assertIn('fieldId: "a_sid", direction: ASC', query)

    def test_business_filters_included(self):
        biz = {"fieldIds": ["a_channel"], "eq": "web"}
        query = self._builder(business_filters=biz).build_query(None, 2000, 10)
        self.assertIn('fieldIds: ["a_channel"]', query)
        self.assertIn('eq: "web"', query)


# --------------------------------------------------------------------------------------
# Client (paginator, auth retry, backoff, rate-limit, errors)
# --------------------------------------------------------------------------------------
class TestClient(unittest.TestCase):
    def setUp(self):
        self._sleeps = []
        self._sleep_patch = mock.patch.object(
            MedalliaClient, "_sleep", staticmethod(lambda seconds: self._sleeps.append(seconds))
        )
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)

    def test_keyset_paginator_stitches_pages(self):
        session = FakeHTTPSession()
        session.query_responses = [
            _feedback_page([_node("1", "100"), _node("2", "200")], total_count=5),
            _feedback_page([_node("3", "300")], total_count=1),
        ]
        client = _make_client(session)
        nodes = list(client.fetch_feedback(None, end_epoch=9999, page_size=2))
        self.assertEqual(len(nodes), 3)
        self.assertEqual(len(session.query_calls), 2)
        # Second page's query advanced past the last node of page one (finish=200).
        second_query = session.query_calls[1]["json"]["query"]
        self.assertIn('"200"', second_query)

    def test_empty_page_terminates(self):
        session = FakeHTTPSession()
        session.query_responses = [
            _feedback_page([_node("1", "100")], total_count=5),
            _feedback_page([], total_count=5),
        ]
        client = _make_client(session)
        nodes = list(client.fetch_feedback(None, 9999, page_size=1))
        self.assertEqual(len(nodes), 1)
        self.assertEqual(len(session.query_calls), 2)

    def test_mid_run_401_re_mints_and_retries_once(self):
        session = FakeHTTPSession()
        session.query_responses = [
            FakeResponse(401),
            _feedback_page([_node("1", "100")], total_count=1),
        ]
        client = _make_client(session)
        nodes = list(client.fetch_feedback(None, 9999, page_size=5))
        self.assertEqual(len(nodes), 1)
        # Token minted twice: once initially, once after the 401 invalidation.
        self.assertEqual(session.token_calls, 2)

    def test_second_401_raises_user_exception(self):
        session = FakeHTTPSession()
        session.query_responses = [FakeResponse(401), FakeResponse(401)]
        client = _make_client(session)
        with self.assertRaises(UserException):
            list(client.fetch_feedback(None, 9999, page_size=5))

    def test_retryable_status_then_success(self):
        for status in (429, 500, 503):
            with self.subTest(status=status):
                session = FakeHTTPSession()
                session.query_responses = [
                    FakeResponse(status),
                    _feedback_page([_node("1", "100")], total_count=1),
                ]
                client = _make_client(session)
                nodes = list(client.fetch_feedback(None, 9999, page_size=5))
                self.assertEqual(len(nodes), 1)
                self.assertTrue(self._sleeps)  # backoff slept at least once

    def test_retry_exhaustion_raises_client_error(self):
        session = FakeHTTPSession()
        session.query_responses = [FakeResponse(500)] * 10
        client = _make_client(session, max_retries=2)
        with self.assertRaises(MedalliaClientError):
            list(client.fetch_feedback(None, 9999, page_size=5))

    def test_retry_after_header_honoured(self):
        session = FakeHTTPSession()
        session.query_responses = [
            FakeResponse(429, headers={"Retry-After": "7"}),
            _feedback_page([_node("1", "100")], total_count=1),
        ]
        client = _make_client(session)
        list(client.fetch_feedback(None, 9999, page_size=5))
        self.assertIn(7.0, self._sleeps)

    def test_graphql_errors_raise_user_exception(self):
        session = FakeHTTPSession()
        session.query_responses = [FakeResponse(200, {"errors": [{"message": "boom"}]})]
        client = _make_client(session)
        with self.assertRaises(UserException):
            list(client.fetch_feedback(None, 9999, page_size=5))

    def test_low_rate_limit_triggers_pause(self):
        session = FakeHTTPSession()
        session.query_responses = [
            _feedback_page([_node("1", "100")], total_count=1),
        ]
        session.query_responses[0].headers = {"X-RateLimit-Remaining-second": "0"}
        client = _make_client(session)
        list(client.fetch_feedback(None, 9999, page_size=5))
        self.assertTrue(self._sleeps)

    def test_compute_cost_only_passed_as_param(self):
        session = FakeHTTPSession()
        session.query_responses = [FakeResponse(200, {"data": {"fields": {"totalCount": 3}}})]
        client = _make_client(session)
        result = client.run_metadata_query("query { fields { totalCount } }", compute_cost_only=True)
        self.assertEqual(result, {"fields": {"totalCount": 3}})
        self.assertEqual(session.query_calls[0]["params"], {"compute_cost_only": "true"})


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
class TestHelpers(unittest.TestCase):
    def test_flatten_scalar_empty_and_multi(self):
        node = {
            "surveyId": {"values": ["S1"]},
            "empty": {"values": []},
            "multi": {"values": ["a", "b"]},
            "missing": None,
        }
        row = flatten_node(node, ["surveyId", "empty", "multi", "missing"])
        self.assertEqual(row["surveyId"], "S1")
        self.assertIsNone(row["empty"])
        self.assertEqual(row["multi"], '["a", "b"]')
        self.assertIsNone(row["missing"])

    def test_watermark_from_node_good(self):
        node = {"surveyId": {"values": ["S1"]}, "k_fin": {"values": ["1700"]}}
        wm = watermark_from_node(node, "k_fin")
        self.assertEqual(wm, Watermark(finish_date_epoch=1700, survey_id="S1"))

    def test_watermark_from_node_bad_value_falls_back(self):
        fallback = Watermark(finish_date_epoch=1, survey_id="prev")
        node = {"surveyId": {"values": ["S1"]}, "k_fin": {"values": ["not-an-int"]}}
        self.assertEqual(watermark_from_node(node, "k_fin", fallback), fallback)

    def test_watermark_from_node_missing_values_falls_back(self):
        fallback = Watermark(finish_date_epoch=1, survey_id="prev")
        node = {"surveyId": {"values": []}, "k_fin": {"values": []}}
        self.assertEqual(watermark_from_node(node, "k_fin", fallback), fallback)


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
def _valid_params(**overrides):
    params = {
        "instance_host": INSTANCE_HOST,
        "company_name": COMPANY,
        "api_host": API_HOST,
        "client_id": CLIENT_ID,
        "#client_secret": CLIENT_SECRET,
        "data_object": "feedback",
        "fields": ["e_nps", "e_comment"],
        "finish_date_field_id": "k_fin",
        "survey_id_field_id": "a_sid",
        "page_size": 100,
        "load_type": "incremental_load",
    }
    params.update(overrides)
    return params


class TestConfiguration(unittest.TestCase):
    def test_valid_parse(self):
        config = Configuration(**_valid_params())
        self.assertEqual(config.instance_host, INSTANCE_HOST)
        self.assertEqual(config.client_secret, CLIENT_SECRET)
        row = RowConfiguration(**_valid_params())
        self.assertTrue(row.incremental)
        self.assertEqual(row.load_type, LoadType.incremental_load)

    def test_client_secret_alias(self):
        config = Configuration(**_valid_params())
        # Alias-form key populated the field.
        self.assertEqual(config.client_secret, CLIENT_SECRET)

    def test_missing_required_field_raises_user_exception(self):
        params = _valid_params()
        del params["instance_host"]
        with self.assertRaises(UserException):
            Configuration(**params)

    def test_page_size_clamped(self):
        row = RowConfiguration(**_valid_params(page_size=5000))
        self.assertEqual(row.page_size, MAX_PAGE_SIZE)

    def test_field_id_injection_guard(self):
        with self.assertRaises(UserException):
            RowConfiguration(**_valid_params(fields=["ok", "bad id!"]))

    def test_watermark_field_injection_guard(self):
        with self.assertRaises(UserException):
            RowConfiguration(**_valid_params(survey_id_field_id="a b"))

    def test_output_columns_dedupe(self):
        row = RowConfiguration(**_valid_params(fields=["a_sid", "k_fin", "e_nps"]))
        self.assertEqual(row.output_columns, ["surveyId", "k_fin", "e_nps"])

    def test_full_load_not_incremental(self):
        row = RowConfiguration(**_valid_params(load_type="full_load"))
        self.assertFalse(row.incremental)


if __name__ == "__main__":
    unittest.main()
