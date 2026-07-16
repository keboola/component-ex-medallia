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
from component import Component
from configuration import MAX_PAGE_SIZE, Configuration, FinishDateFieldType, LoadType, RowConfiguration

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

    def test_other_4xx_raises_user_exception(self):
        # A non-401 4xx (wrong host/tenant, or a client lacking token-endpoint access) is a
        # user-actionable configuration error → UserException (exit 1), never an exit-2 crash.
        for status in (400, 403, 404):
            with self.subTest(status=status):
                session = mock.Mock()
                session.post.return_value = FakeResponse(status)
                tm = MedalliaTokenManager(INSTANCE_HOST, COMPANY, CLIENT_ID, CLIENT_SECRET, session=session)
                with self.assertRaises(UserException):
                    tm.get_token()

    def test_5xx_raises_client_error(self):
        # 5xx from the token endpoint stays a MedalliaClientError (exit 2, genuinely unexpected).
        for status in (500, 503):
            with self.subTest(status=status):
                session = mock.Mock()
                session.post.return_value = FakeResponse(status)
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
        query = self._builder().build_query(None, end_bound=2000, page_size=50)
        self.assertIn("first: 50", query)
        # Upper bound present; no keyset OR / gt lower-bound branch on first run.
        self.assertIn('lt: "2000"', query)
        self.assertNotIn("or:", query)
        self.assertNotIn("gt:", query)

    def test_seeded_watermark_builds_keyset_lower_bound(self):
        wm = Watermark(finish_date_value=1500, survey_id="S9")
        query = self._builder().build_query(wm, end_bound=2000, page_size=50)
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

    def test_reserved_alias_field_ids_not_duplicated(self):
        # A configured field literally named ``id`` or ``surveyId`` collides with the reserved
        # response keys (the bare ``id`` scalar and the ``surveyId`` survey alias). It must NOT
        # emit a second selection for that key — a non-mergeable duplicate is a GraphQL error.
        query = self._builder(fields=["id", "surveyId", "c1"]).build_query(None, 2000, 10)
        self.assertNotIn('fieldData(fieldId: "id")', query)
        self.assertNotIn('fieldData(fieldId: "surveyId")', query)
        self.assertEqual(query.count("surveyId: fieldData"), 1)
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

    def test_node_id_selected_directly(self):
        # The bare ``id`` scalar is selected (not wrapped in fieldData) on every node.
        query = self._builder().build_query(None, 2000, 10)
        self.assertIn("nodes { id ", query)

    def test_datetime_bounds_emitted_as_iso_strings(self):
        builder = MedalliaQueryBuilder(
            data_object="feedback",
            survey_id_field_id="a_sid",
            finish_date_field_id="e_creationdate",
            fields=["c1"],
            finish_date_field_type="datetime",
        )
        wm = Watermark(finish_date_value="2026-05-01", survey_id="S9")
        query = builder.build_query(wm, end_bound="2026-07-15T00:00:00Z", page_size=50)
        # Upper and lower bounds are the ISO strings verbatim — never coerced to epoch.
        self.assertIn('lt: "2026-07-15T00:00:00Z"', query)
        self.assertIn('gt: "2026-05-01"', query)
        self.assertIn('gte: "2026-05-01"', query)


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
        nodes = list(client.fetch_feedback(None, end_bound=9999, page_size=2))
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

    def test_invalid_field_id_error_is_tolerated(self):
        # Medallia returns a non-fatal "Invalid field id:" error but still returns data —
        # the run must continue and yield the valid nodes rather than aborting.
        session = FakeHTTPSession()
        session.query_responses = [
            FakeResponse(
                200,
                {
                    "data": {"feedback": {"totalCount": 1, "nodes": [_node("1", "100")]}},
                    "errors": [{"message": "Invalid field id: e_bogus"}],
                },
            )
        ]
        client = _make_client(session)
        nodes = list(client.fetch_feedback(None, 9999, page_size=5))
        self.assertEqual(len(nodes), 1)

    def test_invalid_field_id_mixed_with_real_error_still_raises(self):
        # If a real error accompanies the tolerated one, the run must still fail.
        session = FakeHTTPSession()
        session.query_responses = [
            FakeResponse(
                200,
                {"errors": [{"message": "Invalid field id: e_bogus"}, {"message": "cost limit exceeded"}]},
            )
        ]
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

    def test_query_4xx_raises_user_exception(self):
        # A non-retryable 4xx (wrong api_host/path, or no Query API access) is user-actionable.
        for status in (400, 403, 404):
            with self.subTest(status=status):
                session = FakeHTTPSession()
                session.query_responses = [FakeResponse(status)]
                client = _make_client(session)
                with self.assertRaises(UserException):
                    list(client.fetch_feedback(None, 9999, page_size=5))

    def test_query_non_retryable_5xx_raises_client_error(self):
        # 501 is a 5xx but NOT in the retryable set → genuinely unexpected (exit 2).
        session = FakeHTTPSession()
        session.query_responses = [FakeResponse(501)]
        client = _make_client(session)
        with self.assertRaises(MedalliaClientError):
            list(client.fetch_feedback(None, 9999, page_size=5))

    def test_full_load_no_duplicate_boundary_rows(self):
        # Page 2 repeats the last node of page 1 — a boundary duplicate the exclusive keyset
        # filter can still return. It must be dropped so full_load emits each survey once
        # (under incremental the PK upsert would mask it, but full_load has no such safety net).
        session = FakeHTTPSession()
        session.query_responses = [
            _feedback_page([_node("S1", "100"), _node("S2", "200")], total_count=5),
            _feedback_page([_node("S2", "200"), _node("S3", "300")], total_count=1),
        ]
        client = _make_client(session)
        nodes = list(client.fetch_feedback(None, end_bound=9999, page_size=2))
        survey_ids = [node["surveyId"]["values"][0] for node in nodes]
        self.assertEqual(survey_ids, ["S1", "S2", "S3"])

    def test_page_of_only_boundary_duplicates_terminates(self):
        # A page whose every node is at/behind the exclusive lower bound yields no forward
        # progress; the paginator must stop rather than re-issue the identical query forever.
        session = FakeHTTPSession()
        session.query_responses = [
            _feedback_page([_node("S1", "100")], total_count=5),
            _feedback_page([_node("S1", "100")], total_count=5),
        ]
        client = _make_client(session)
        nodes = list(client.fetch_feedback(None, end_bound=9999, page_size=1))
        self.assertEqual([node["surveyId"]["values"][0] for node in nodes], ["S1"])
        self.assertEqual(len(session.query_calls), 2)

    def test_non_advancing_cursor_terminates(self):
        # A full page (totalCount >= page_size) whose node yields no extractable watermark
        # (empty surveyId) leaves the cursor unmoved. The fail-safe must stop rather than
        # re-issue the identical query forever — only one page is queued, so a re-issue would
        # raise IndexError.
        session = FakeHTTPSession()
        no_watermark = {"surveyId": {"values": []}, "k_fin": {"values": ["100"]}, "c1": {"values": ["x"]}}
        session.query_responses = [_feedback_page([no_watermark], total_count=5)]
        client = _make_client(session)
        nodes = list(client.fetch_feedback(None, end_bound=9999, page_size=1))
        self.assertEqual(len(nodes), 1)
        self.assertEqual(len(session.query_calls), 1)

    def test_cursor_folds_across_page_when_last_node_lacks_watermark(self):
        # The last node of a full page has no surveyId, but an earlier node does. The cursor
        # must advance from the folded page max (not just nodes[-1]), so the next page's query
        # carries the good node's watermark rather than stalling.
        session = FakeHTTPSession()
        no_watermark = {"surveyId": {"values": []}, "k_fin": {"values": ["150"]}, "c1": {"values": ["x"]}}
        session.query_responses = [
            _feedback_page([_node("S1", "100"), no_watermark], total_count=5),
            _feedback_page([], total_count=5),
        ]
        client = _make_client(session)
        nodes = list(client.fetch_feedback(None, end_bound=9999, page_size=2))
        self.assertEqual(len(nodes), 2)
        self.assertEqual(len(session.query_calls), 2)
        second_query = session.query_calls[1]["json"]["query"]
        self.assertIn('"100"', second_query)
        self.assertIn('"S1"', second_query)


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
        self.assertEqual(wm, Watermark(finish_date_value=1700, survey_id="S1"))

    def test_watermark_from_node_bad_value_falls_back(self):
        fallback = Watermark(finish_date_value=1, survey_id="prev")
        node = {"surveyId": {"values": ["S1"]}, "k_fin": {"values": ["not-an-int"]}}
        self.assertEqual(watermark_from_node(node, "k_fin", fallback), fallback)

    def test_watermark_from_node_missing_values_falls_back(self):
        fallback = Watermark(finish_date_value=1, survey_id="prev")
        node = {"surveyId": {"values": []}, "k_fin": {"values": []}}
        self.assertEqual(watermark_from_node(node, "k_fin", fallback), fallback)

    def test_watermark_from_node_datetime_kept_as_string(self):
        # A DATETIME/ISO watermark field must NOT be forced through int() — the value is
        # stored verbatim as a string so the watermark can advance.
        node = {"surveyId": {"values": ["S1"]}, "e_creationdate": {"values": ["2026-05-01"]}}
        wm = watermark_from_node(node, "e_creationdate", field_type="datetime")
        self.assertEqual(wm, Watermark(finish_date_value="2026-05-01", survey_id="S1"))

    def test_watermark_advances_monotonically(self):
        # ISO strings compare lexicographically; a newer record advances, an older one does not.
        current = Watermark(finish_date_value="2026-05-01", survey_id="S5")
        newer = {"surveyId": {"values": ["S6"]}, "d": {"values": ["2026-05-02"]}}
        older = {"surveyId": {"values": ["S1"]}, "d": {"values": ["2026-04-01"]}}
        self.assertEqual(
            watermark_from_node(newer, "d", current, field_type="datetime"),
            Watermark(finish_date_value="2026-05-02", survey_id="S6"),
        )
        self.assertEqual(watermark_from_node(older, "d", current, field_type="datetime"), current)

    def test_watermark_epoch_advances_numerically(self):
        # Epoch values must compare numerically, not lexicographically ("9" < "1000" as ints).
        current = Watermark(finish_date_value=9, survey_id="S1")
        node = {"surveyId": {"values": ["S2"]}, "k_fin": {"values": ["1000"]}}
        self.assertEqual(
            watermark_from_node(node, "k_fin", current),
            Watermark(finish_date_value=1000, survey_id="S2"),
        )

    def test_flatten_node_direct_scalar_id(self):
        # The node ``id`` is a bare scalar (not a fieldData ``{values}`` wrapper).
        node = {"id": "F1", "surveyId": {"values": ["S1"]}, "missing_id": None}
        row = flatten_node(node, ["id", "surveyId", "missing_id"])
        self.assertEqual(row["id"], "F1")
        self.assertEqual(row["surveyId"], "S1")
        self.assertIsNone(row["missing_id"])


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

    def test_validation_error_message_names_field_without_leaking_secret(self):
        # Security regression: the UserException message built from a ValidationError must
        # name the offending field but never echo any config value — above all the decrypted
        # #client_secret. The chained ValidationError (whose input_value holds the secret)
        # must also be suppressed (raised ``from None``).
        sentinel = "SENTINEL_SECRET_DO_NOT_LEAK"
        params = _valid_params(**{"#client_secret": sentinel})
        del params["instance_host"]
        with self.assertRaises(UserException) as ctx:
            Configuration(**params)
        message = str(ctx.exception)
        self.assertIn("instance_host", message)
        self.assertNotIn(sentinel, message)
        # No config values at all leak into the message.
        self.assertNotIn(INSTANCE_HOST, message)
        self.assertNotIn(COMPANY, message)
        # The chained ValidationError (carrying input_value) is not attached.
        self.assertIsNone(ctx.exception.__cause__)

    def test_page_size_clamped(self):
        row = RowConfiguration(**_valid_params(page_size=5000))
        self.assertEqual(row.page_size, MAX_PAGE_SIZE)

    def test_field_id_injection_guard(self):
        with self.assertRaises(UserException):
            RowConfiguration(**_valid_params(fields=["ok", "bad id!"]))

    def test_watermark_field_injection_guard(self):
        with self.assertRaises(UserException):
            RowConfiguration(**_valid_params(survey_id_field_id="a b"))

    def test_valid_filter_tree_accepted(self):
        # A legitimate nested Medallia filter (operators + fieldIds) parses cleanly — its keys
        # are all valid GraphQL names.
        filters = {"and": [{"fieldIds": ["a_channel"], "eq": "web"}, {"or": [{"isNull": ["e_nps"]}]}]}
        row = RowConfiguration(**_valid_params(filters=filters))
        self.assertEqual(row.filters, filters)

    def test_filter_key_injection_guard(self):
        # An unquoted filter object key is emitted verbatim into the GraphQL query; a crafted
        # key must be rejected at config validation to close the injection vector.
        with self.assertRaises(UserException):
            RowConfiguration(**_valid_params(filters={"and) { evil": [{"fieldIds": ["x"]}]}))

    def test_filter_key_injection_guard_nested(self):
        # The check is recursive: an invalid key nested inside a list/dict is caught too.
        with self.assertRaises(UserException):
            RowConfiguration(**_valid_params(filters={"and": [{"bad key": "v"}]}))

    def test_output_columns_dedupe(self):
        row = RowConfiguration(**_valid_params(fields=["a_sid", "k_fin", "e_nps"]))
        # Node ``id`` leads, then surveyId, watermark field, and the non-reserved user fields.
        self.assertEqual(row.output_columns, ["id", "surveyId", "k_fin", "e_nps"])

    def test_full_load_not_incremental(self):
        row = RowConfiguration(**_valid_params(load_type="full_load"))
        self.assertFalse(row.incremental)

    def test_finish_date_field_type_defaults_to_epoch(self):
        row = RowConfiguration(**_valid_params())
        self.assertEqual(row.finish_date_field_type, FinishDateFieldType.epoch)
        # Epoch first-run seed comes from initial_start_epoch.
        row = RowConfiguration(**_valid_params(initial_start_epoch=1700))
        self.assertEqual(row.initial_start, 1700)

    def test_datetime_field_type_uses_iso_seed(self):
        row = RowConfiguration(
            **_valid_params(
                finish_date_field_id="e_creationdate",
                finish_date_field_type="datetime",
                initial_start_value="2026-05-01",
            )
        )
        self.assertEqual(row.finish_date_field_type, FinishDateFieldType.datetime)
        self.assertEqual(row.initial_start, "2026-05-01")


# --------------------------------------------------------------------------------------
# Sync actions (testConnection / listFields / _field_label)
# --------------------------------------------------------------------------------------
class _StubMetadataClient:
    """Duck-typed stand-in for the metadata client used by the sync actions."""

    def __init__(self, result=None, error=None):
        self._result = result if result is not None else {}
        self._error = error
        self.calls = []

    def run_metadata_query(self, query, compute_cost_only=False):
        self.calls.append((query, compute_cost_only))
        if self._error is not None:
            raise self._error
        return self._result


def _sync_component(client):
    """A Component wired to a stub metadata client, bypassing ComponentBase init.

    The sync-action methods are invoked via ``__wrapped__`` (set by ``functools.wraps`` on
    the @sync_action decorator) so the raw method runs directly — the decorator's stdout /
    exit machinery, which needs a real ``configuration`` (a read-only property), is skipped.
    """
    comp = Component.__new__(Component)
    comp._get_config = lambda: mock.Mock()
    comp._build_metadata_client = lambda config: client
    return comp


def _run_test_connection(comp):
    return Component.test_connection.__wrapped__(comp)


def _run_list_fields(comp):
    return Component.list_fields.__wrapped__(comp)


class TestSyncActions(unittest.TestCase):
    def test_test_connection_success(self):
        client = _StubMetadataClient(result={"fields": {"totalCount": 1}})
        result = _run_test_connection(_sync_component(client))
        self.assertIn("succeeded", result.message.lower())
        # The pre-flight uses compute_cost_only so it validates auth without consuming quota.
        self.assertEqual(client.calls[0][1], True)

    def test_test_connection_client_error_becomes_user_exception(self):
        client = _StubMetadataClient(error=MedalliaClientError("gateway 500"))
        with self.assertRaises(UserException):
            _run_test_connection(_sync_component(client))

    def test_list_fields_returns_humanized_select_elements(self):
        client = _StubMetadataClient(
            result={
                "fields": {
                    "nodes": [
                        {"id": "e_nps", "name": "NPS", "dataType": "NUMBER"},
                        {"id": "e_comment", "name": "Comment"},
                        {"name": "no id — skipped"},
                    ]
                }
            }
        )
        elements = _run_list_fields(_sync_component(client))
        self.assertEqual([e.value for e in elements], ["e_nps", "e_comment"])
        self.assertEqual(elements[0].label, "NPS (NUMBER)")
        self.assertEqual(elements[1].label, "Comment")

    def test_list_fields_empty_nodes(self):
        client = _StubMetadataClient(result={"fields": {"nodes": []}})
        self.assertEqual(_run_list_fields(_sync_component(client)), [])

    def test_list_fields_client_error_becomes_user_exception(self):
        client = _StubMetadataClient(error=MedalliaClientError("gateway 503"))
        with self.assertRaises(UserException):
            _run_list_fields(_sync_component(client))

    def test_field_label(self):
        self.assertEqual(Component._field_label({"id": "e_nps", "name": "NPS", "dataType": "NUMBER"}), "NPS (NUMBER)")
        self.assertEqual(Component._field_label({"id": "e_x", "name": "Only Name"}), "Only Name")
        # No name → falls back to the field id; no dataType → no parenthetical suffix.
        self.assertEqual(Component._field_label({"id": "e_y"}), "e_y")


if __name__ == "__main__":
    unittest.main()
