"""Unit tests for the two recording-support mechanisms added for the VCR data matrix:

* ``MedalliaClient`` ``max_pages`` cap (wired from ``MEDALLIA_MAX_PAGES``) — bounds the live
  blast radius while recording and defends against a runaway keyset loop.
* ``MedalliaResponseBodySanitizer`` arity/shape preservation — a recorded cassette is
  synthetic by construction yet keeps each field's arity and value shape, so multi-value and
  epoch-watermark behaviour can be exercised on replay.

Kept separate from ``test_unit.py`` (which stays the fine-grained pre-existing logic suite).
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from client.medallia_client import MedalliaClient, MedalliaQueryBuilder, MedalliaTokenManager  # noqa: E402
from component import VCR_SANITIZERS  # noqa: E402

INSTANCE_HOST = "instance.example.test"
API_HOST = "acme.apis.example.test"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeHTTPSession:
    def __init__(self):
        self.token_calls = 0
        self.query_calls = []
        self._page = 0

    def post(self, url, **kwargs):
        if url.endswith("/token"):
            self.token_calls += 1
            return FakeResponse(200, {"access_token": "tok", "expires_in": 3600})
        # Every query returns a full page (totalCount >= page_size) so pagination would run
        # forever if the max_pages cap did not stop it.
        self.query_calls.append(kwargs)
        self._page += 1
        node = {"surveyId": {"values": [f"S{self._page}"]}, "k_fin": {"values": [str(self._page * 100)]}}
        return FakeResponse(200, {"data": {"feedback": {"totalCount": 999, "nodes": [node]}}})


def _client(session, **kwargs):
    tm = MedalliaTokenManager(INSTANCE_HOST, "acme", "cid", "secret", session=session)
    qb = MedalliaQueryBuilder(
        data_object="feedback", survey_id_field_id="a_sid", finish_date_field_id="k_fin", fields=["c1"]
    )
    return MedalliaClient(API_HOST, tm, qb, session=session, **kwargs)


class TestMaxPagesCap(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(MedalliaClient, "_sleep", staticmethod(lambda s: None))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_cap_stops_after_n_pages(self):
        session = FakeHTTPSession()
        client = _client(session, max_pages=3)
        nodes = list(client.fetch_feedback(None, end_bound=10**12, page_size=1))
        # Exactly 3 query requests despite an effectively infinite dataset.
        self.assertEqual(len(session.query_calls), 3)
        self.assertEqual(len(nodes), 3)

    def test_no_cap_by_default_is_bounded_only_by_data(self):
        # With a dataset that terminates naturally (totalCount < page_size), the absence of a
        # cap does not loop forever.
        session = FakeHTTPSession()
        session.post = mock.Mock(  # single short page → natural termination
            side_effect=lambda url, **kw: (
                FakeResponse(200, {"access_token": "t", "expires_in": 3600})
                if url.endswith("/token")
                else FakeResponse(
                    200,
                    {
                        "data": {
                            "feedback": {
                                "totalCount": 1,
                                "nodes": [{"surveyId": {"values": ["S1"]}, "k_fin": {"values": ["100"]}}],
                            }
                        }
                    },
                )
            )
        )
        client = _client(session)  # no max_pages
        nodes = list(client.fetch_feedback(None, end_bound=10**12, page_size=5))
        self.assertEqual(len(nodes), 1)


class TestSanitizerArityAndShape(unittest.TestCase):
    def _sanitizer(self):
        return next(s for s in VCR_SANITIZERS if s.__class__.__name__ == "MedalliaResponseBodySanitizer")

    def test_epoch_int_field_stays_integer_and_monotonic(self):
        san = self._sanitizer()
        v1 = san._synthetic_values("a_initial_finish_timestamp", 1, ["1699999999"])
        v2 = san._synthetic_values("a_initial_finish_timestamp", 2, ["1700000005"])
        self.assertTrue(v1[0].isdigit() and v2[0].isdigit())
        self.assertLess(int(v1[0]), int(v2[0]))  # strictly increasing across nodes

    def test_datetime_field_stays_datetime_and_monotonic(self):
        san = self._sanitizer()
        v1 = san._synthetic_values("e_creationdate", 1, ["2026-07-09T00:00:01"])
        v2 = san._synthetic_values("e_creationdate", 2, ["2026-07-09T00:00:09"])
        # Lexicographic order (how the datetime watermark compares) tracks node order.
        self.assertLess(v1[0], v2[0])
        self.assertRegex(v1[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_multi_value_field_preserves_arity(self):
        san = self._sanitizer()
        out = san._synthetic_values("a_recognition_sentiment_types", 3, ["POS", "NEG", "MIX"])
        self.assertEqual(len(out), 3)
        self.assertEqual(len(set(out)), 3)  # distinct synthetic entries

    def test_no_real_value_is_copied(self):
        san = self._sanitizer()
        real = ["1699999999"]
        out = san._synthetic_values("a_initial_finish_timestamp", 1, real)
        self.assertNotIn(real[0], out)

    def test_before_record_response_rewrites_feedback_values(self):
        san = self._sanitizer()
        body = (
            '{"data": {"feedback": {"totalCount": 1, "nodes": [{"id": "REALID",'
            ' "surveyId": {"values": ["999888"]},'
            ' "e_creationdate": {"values": ["2026-07-09T12:34:56"]}}]}}}'
        )
        out = san.before_record_response({"body": {"string": body}})
        text = out["body"]["string"]
        self.assertNotIn("REALID", text)
        self.assertNotIn("999888", text)
        self.assertNotIn("2026-07-09T12:34:56", text)
        self.assertIn("RESP-", text)
        self.assertIn("SURVEY-", text)


if __name__ == "__main__":
    unittest.main()
