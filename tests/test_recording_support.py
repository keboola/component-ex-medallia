"""Unit coverage for the VCR recording-support mechanisms.

The one committed mechanism that shapes every cassette is ``MedalliaResponseBodySanitizer``
(``src/component.py``): it runs AT RECORD TIME and overwrites every response value so a
cassette is synthetic BY CONSTRUCTION. These tests prove that guarantee across all three
generic node shapes (spec §4) plus the ``pageInfo.endCursor`` and request ``after`` cursor —
i.e. that NO real value can survive into a committed cassette. The client's page cap and
cursor paginator are exercised in ``test_unit.py``.
"""

import json

import pytest

from component import MedalliaResponseBodySanitizer

# Real-looking sentinels planted in the "live" payloads below. After sanitization NONE of these
# may appear anywhere in the cassette body — that is the allowlist guarantee.
REAL_TOKENS = [
    "john.doe@real-customer.example",  # a real email
    "Absolutely terrible service, ticket #55021",  # free-text verbatim (PII risk)
    "cust-8837120",  # a real customer id
    "zetabrand-fake",  # a fabricated tenant-like marker
    "zeta_user_fake_9931",  # a fabricated user-id-like marker
    "ZXhhbXBsZS1jdXJzb3Itb2Zmc2V0LTEyMzQ1",  # a real base64-ish cursor
    "1719300000",  # a real epoch value
]


def _resp(payload: dict) -> dict:
    """Wrap a GraphQL payload in the vcrpy response-dict shape the sanitizer expects."""
    return {"status": {"code": 200}, "body": {"string": json.dumps(payload)}}


def _sanitize(payload: dict) -> dict:
    out = MedalliaResponseBodySanitizer().before_record_response(_resp(payload))
    return json.loads(out["body"]["string"])


def _all_strings(obj) -> str:
    """Flatten the whole structure to one string for a coarse leak scan."""
    return json.dumps(obj)


def _assert_no_real_values(sanitized: dict) -> None:
    blob = _all_strings(sanitized)
    for token in REAL_TOKENS:
        assert token not in blob, f"real value leaked into cassette: {token!r}"


# -- shape (a): fieldData(fieldId){values} ---------------------------------------------------


def test_fielddata_shape_values_overwritten_arity_and_shape_preserved():
    payload = {
        "data": {
            "feedback": {
                "nodes": [
                    {
                        "id": "cust-8837120",
                        "e_comment": {"values": ["Absolutely terrible service, ticket #55021"]},
                        "e_creationdate": {"values": ["2024-06-25"]},
                        "a_initial_finish_timestamp": {"values": ["1719300000"]},
                        "e_nps": {"values": ["9"]},
                        "a_recognition_sentiment_types": {"values": ["Positive", "Negative"]},
                        "empty_field": {"values": []},
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": "ZXhhbXBsZS1jdXJzb3Itb2Zmc2V0LTEyMzQ1"},
                "totalCount": 1,
            }
        }
    }
    out = _sanitize(payload)
    node = out["data"]["feedback"]["nodes"][0]
    _assert_no_real_values(out)

    assert node["id"].startswith("RESP-")
    # multi-value arity preserved
    assert len(node["a_recognition_sentiment_types"]["values"]) == 2
    # single-value arity preserved
    assert len(node["e_comment"]["values"]) == 1
    # empty stays empty
    assert node["empty_field"]["values"] == []
    # date shape preserved
    assert node["e_creationdate"]["values"][0][:4].isdigit() and "-" in node["e_creationdate"]["values"][0]
    # epoch (10-digit) stays a long all-digit integer string
    epoch = node["a_initial_finish_timestamp"]["values"][0]
    assert epoch.isdigit() and len(epoch) >= 10


def test_fielddata_endcursor_normalised():
    payload = {
        "data": {
            "feedback": {
                "nodes": [{"id": "cust-8837120", "x": {"values": ["v"]}}],
                "pageInfo": {"hasNextPage": True, "endCursor": "ZXhhbXBsZS1jdXJzb3Itb2Zmc2V0LTEyMzQ1"},
            }
        }
    }
    out = _sanitize(payload)
    assert out["data"]["feedback"]["pageInfo"]["endCursor"].startswith("SYNTHETIC-CURSOR")


# -- shape (b): data(fieldId){value,values} (customers) --------------------------------------


def test_data_shape_value_and_values_overwritten():
    payload = {
        "data": {
            "customers": {
                "nodes": [
                    {
                        "id": "cust-8837120",
                        "email": {"value": "john.doe@real-customer.example", "values": None},
                        "tags": {"value": None, "values": ["zeta_user_fake_9931", "loyalty"]},
                        "score": {"value": 42, "values": None},
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }
    out = _sanitize(payload)
    node = out["data"]["customers"]["nodes"][0]
    _assert_no_real_values(out)
    assert node["id"].startswith("RESP-")
    assert node["email"]["value"] and node["email"]["value"] != "john.doe@real-customer.example"
    assert node["tags"]["values"] is not None and len(node["tags"]["values"]) == 2
    # a scalar `value` survives as a synthetic scalar (arity/shape kept)
    assert node["score"]["value"] is not None


# -- shape (c): bare scalar nodes, including id-less --------------------------------------------


def test_scalar_shape_bare_fields_overwritten():
    payload = {
        "data": {
            "programs": {
                "nodes": [
                    {
                        "id": "cust-8837120",
                        "programName": "zetabrand-fake",
                        "recordCount": 8837120,
                        "ratio": 0.87,
                        "active": True,
                        "programUrl": "https://zetabrand-fake.example/p/1",
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }
    out = _sanitize(payload)
    node = out["data"]["programs"]["nodes"][0]
    _assert_no_real_values(out)
    assert node["id"].startswith("RESP-")
    assert node["programName"] != "zetabrand-fake"
    assert isinstance(node["recordCount"], int) and node["recordCount"] != 8837120
    assert isinstance(node["ratio"], float)
    assert node["active"] is True  # booleans carry no PII, kept deterministic
    assert "zetabrand-fake" not in node["programUrl"]


def test_idless_node_fully_synthetic():
    """An id-less scalar node must have EVERY field overwritten (no id to anchor on)."""
    payload = {
        "data": {
            "unitWarnings": {
                "nodes": [
                    {"warningType": "zeta_user_fake_9931", "message": "Absolutely terrible service, ticket #55021"},
                    {"warningType": "zetabrand-fake", "unit": "cust-8837120"},
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }
    out = _sanitize(payload)
    _assert_no_real_values(out)
    nodes = out["data"]["unitWarnings"]["nodes"]
    assert len(nodes) == 2
    for node in nodes:
        assert "id" not in node  # no id was added by the sanitizer
        for value in node.values():
            assert isinstance(value, str) and value  # every leaf replaced


# -- field metadata catalogue (the `fields` query) ------------------------------------------


def test_field_catalog_replaced_with_synthetic_set():
    payload = {
        "data": {
            "fields": {
                "nodes": [
                    {"id": "a_secret_real_field", "name": "zetabrand-fake internal", "dataType": "STRING"},
                ]
            }
        }
    }
    out = _sanitize(payload)
    _assert_no_real_values(out)
    ids = {n["id"] for n in out["data"]["fields"]["nodes"]}
    assert "a_secret_real_field" not in ids
    assert "e_creationdate" in ids  # deterministic synthetic catalogue


# -- request cursor scrubbing ----------------------------------------------------------------


class _FakeRequest:
    def __init__(self, body):
        self.body = body


def test_before_record_request_scrubs_after_cursor():
    body = json.dumps(
        {
            "query": "query($first:Int,$after:String){ feedback(first:$first,after:$after){ nodes{id} } }",
            "variables": {"first": 5, "after": "ZXhhbXBsZS1jdXJzb3Itb2Zmc2V0LTEyMzQ1"},
        }
    )
    req = MedalliaResponseBodySanitizer().before_record_request(_FakeRequest(body))
    parsed = json.loads(req.body)
    assert parsed["variables"]["after"] == "SYNTHETIC-CURSOR"
    assert "ZXhhbXBsZS1jdXJzb3Itb2Zmc2V0LTEyMzQ1" not in req.body


def test_before_record_request_leaves_first_page_untouched():
    body = json.dumps({"query": "q", "variables": {"first": 5, "after": None}})
    req = MedalliaResponseBodySanitizer().before_record_request(_FakeRequest(body))
    assert json.loads(req.body)["variables"]["after"] is None


def test_before_record_request_ignores_non_json_body():
    req = MedalliaResponseBodySanitizer().before_record_request(_FakeRequest("grant_type=client_credentials"))
    assert req.body == "grant_type=client_credentials"


# -- non-Query payloads pass through unchanged -----------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"__typename": "Query"}},  # test-connection style
        {"data": None, "errors": [{"message": "Estimated query cost is: 1."}]},  # cost pre-flight
    ],
)
def test_non_node_payloads_untouched(payload):
    out = MedalliaResponseBodySanitizer().before_record_response(_resp(payload))
    assert json.loads(out["body"]["string"]) == payload


def test_bytes_body_roundtrips():
    payload = {"data": {"programs": {"nodes": [{"id": "cust-8837120", "programName": "zetabrand-fake"}]}}}
    resp = {"body": {"string": json.dumps(payload).encode("utf-8")}}
    out = MedalliaResponseBodySanitizer().before_record_response(resp)
    text = out["body"]["string"]
    assert isinstance(text, bytes)
    assert b"zetabrand-fake" not in text
