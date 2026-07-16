"""Hand-authored, deterministic functional coverage — NO live API, NO fabricated cassettes.

Each test drives the REAL ``Component.run()`` orchestration end-to-end against a temporary
``KBC_DATADIR``, with the HTTP layer replaced by an in-process stub ``requests.Session`` (a
Python object, not a committed cassette — nothing here masquerades as a real recording). The
stub returns synthetic GraphQL payloads shaped exactly like the live API's three node shapes
(spec §4), so the component's introspection, shape resolution, generic query building, cursor
pagination, flatten, PK strategy, typed manifest, incremental watermark and raw mode are all
exercised deterministically. Output CSV / manifest / ``state.json`` are asserted directly.

Live VCR cassettes (real recordings → sanitized) live under ``tests/functional`` and are
replayed by ``tests/test_functional.py``; this module is the broad no-live logic net.
"""

import json
from pathlib import Path

import pytest
from freezegun import freeze_time
from keboola.component.base import ComponentBase
from keboola.component.exceptions import UserException

import client.medallia_client as mc

RUN_AT = "2026-09-15T00:00:00"

# --------------------------------------------------------------------------------------------
# Introspection fixture builder (mirrors the nesting resolve_object_shape / _named_type expect)
# --------------------------------------------------------------------------------------------


def _ref(name: str, kind: str = "OBJECT") -> dict:
    """A TypeRef wrapped in NON_NULL(LIST(...)) so the code's ofType unwrap has work to do."""
    return {"kind": "NON_NULL", "name": None, "ofType": {"kind": kind, "name": name, "ofType": None}}


def _connection_type(conn_name: str, node_name: str) -> dict:
    return {
        "name": conn_name,
        "kind": "OBJECT",
        "fields": [
            {"name": "nodes", "type": {"kind": "LIST", "name": None, "ofType": _ref(node_name)}},
            {"name": "pageInfo", "type": _ref("PageInfo")},
            {"name": "totalCount", "type": _ref("Int", "SCALAR")},
        ],
    }


def _node_type(node_name: str, fields: list[dict]) -> dict:
    return {"name": node_name, "kind": "OBJECT", "fields": fields}


def _scalar(name: str, scalar_type: str = "String") -> dict:
    return {"name": name, "type": _ref(scalar_type, "SCALAR")}


# Medallia's connections declare first: Int! / after: ID (verified live). The fixture carries
# these arg types so resolve_object_shape threads them into the query's variable declaration —
# a nullable $first: Int in a non-null slot is a hard VariableTypeMismatch at the gateway.
def _paging_args(extra: list[dict] | None = None) -> list[dict]:
    return [
        {"name": "first", "type": _ref("Int", "SCALAR")},  # -> Int! via the NON_NULL wrapper
        {"name": "after", "type": {"kind": "SCALAR", "name": "ID", "ofType": None}},
        *(extra or []),
    ]


INTROSPECTION = {
    "__schema": {
        "queryType": {
            "fields": [
                {
                    "name": "feedback",
                    "args": _paging_args([{"name": "filter"}, {"name": "orderBy"}]),
                    "type": _ref("FeedbackConnection"),
                },
                {"name": "customers", "args": _paging_args(), "type": _ref("CustomerConnection")},
                {"name": "programs", "args": _paging_args(), "type": _ref("ProgramConnection")},
                {"name": "socialURLs", "args": _paging_args(), "type": _ref("SocialUrlConnection")},
                # metadata catalog (must be excluded from the extractable set)
                {"name": "fields", "args": [{"name": "first"}], "type": _ref("FieldConnection")},
                # non-connection root field (must be excluded)
                {"name": "me", "args": [], "type": _ref("Viewer")},
            ]
        },
        "types": [
            _connection_type("FeedbackConnection", "Feedback"),
            _connection_type("CustomerConnection", "Customer"),
            _connection_type("ProgramConnection", "Program"),
            _connection_type("SocialUrlConnection", "SocialUrl"),
            _connection_type("FieldConnection", "Field"),
            # feedback → fieldData shape, has id
            _node_type("Feedback", [_scalar("id", "ID"), {"name": "fieldData", "type": _ref("FieldDatum")}]),
            # customers → data shape, has id
            _node_type("Customer", [_scalar("id", "ID"), {"name": "data", "type": _ref("Datum")}]),
            # programs → bare-scalar shape, has id + typed scalar fields
            _node_type(
                "Program",
                [_scalar("id", "ID"), _scalar("programName", "String"), _scalar("recordCount", "Int")],
            ),
            # socialURLs → bare-scalar shape, NO id → row-hash PK
            _node_type("SocialUrl", [_scalar("url", "String"), _scalar("healthy", "Boolean")]),
            {"name": "Viewer", "kind": "OBJECT", "fields": [_scalar("name", "String")]},
        ],
    }
}

FIELD_CATALOG = {
    "fields": {
        "nodes": [
            {"id": "a_customerid", "name": "Customer ID", "dataType": "STRING", "sortable": True, "multivalued": False},
            {
                "id": "e_creationdate",
                "name": "Creation Date",
                "dataType": "DATE",
                "sortable": True,
                "multivalued": False,
            },
            {"id": "a_finish_ts", "name": "Finish", "dataType": "INT", "sortable": True, "multivalued": False},
            {"id": "e_nps", "name": "NPS", "dataType": "INTEGER", "sortable": True, "multivalued": False},
            {"id": "a_tags", "name": "Tags", "dataType": "STRING", "sortable": False, "multivalued": True},
        ]
    }
}


# --------------------------------------------------------------------------------------------
# In-process stub transport
# --------------------------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, payload: dict, status: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload


class StubSession:
    """Routes token / introspection / metadata / data POSTs to synthetic payloads."""

    def __init__(self, data_pages: dict[str, list[dict]]):
        # object name -> list of connection dicts (one per page, in order)
        self._data_pages = {k: list(v) for k, v in data_pages.items()}
        self._page_idx: dict[str, int] = {}
        self.data_requests: list[dict] = []

    def post(self, url, params=None, json=None, headers=None, data=None, timeout=None, auth=None):  # noqa: A002
        if url.endswith("/token"):
            return _StubResponse({"access_token": "stub-token", "token_type": "Bearer", "expires_in": 3600})
        body = json or {}
        query = body.get("query", "")
        if params and params.get("compute_cost_only"):
            return _StubResponse({"data": None, "errors": [{"message": "Estimated query cost is: 1."}]})
        if "__schema" in query:
            return _StubResponse({"data": INTROSPECTION})
        if "__typename" in query:
            return _StubResponse({"data": {"__typename": "Query"}})
        if "fields(first: 1000)" in query:
            return _StubResponse({"data": FIELD_CATALOG})
        # data query — find which registered object appears as a connection field
        for obj, pages in self._data_pages.items():
            if f"{obj}(" in query:
                self.data_requests.append(body)
                idx = self._page_idx.get(obj, 0)
                page = pages[min(idx, len(pages) - 1)]
                self._page_idx[obj] = idx + 1
                return _StubResponse({"data": {obj: page}})
        raise AssertionError(f"StubSession: unrouted query: {query[:120]}")


def _page(nodes: list[dict], has_next: bool = False, cursor: str | None = None) -> dict:
    return {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}


# --------------------------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------------------------

ROOT = {
    "instance_host": "example.medallia.com",
    "api_host": "example.apis.medallia.com",
    "company_name": "example",
    "client_id": "DUMMY_CLIENT_ID",
    "#client_secret": "DUMMY_SECRET",
}


def _write_datadir(tmp_path: Path, params: dict, action: str = "run", in_state: dict | None = None) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "out" / "tables").mkdir(parents=True, exist_ok=True)
    (data_dir / "in" / "tables").mkdir(parents=True, exist_ok=True)
    config = {"action": action, "parameters": {**ROOT, **params}}
    (data_dir / "config.json").write_text(json.dumps(config))
    if in_state is not None:
        (data_dir / "in" / "state.json").write_text(json.dumps(in_state))
    return data_dir


def _run(monkeypatch, data_dir: Path, stub: StubSession):
    """Run Component.run() in-process with the stubbed transport; return the Component."""
    monkeypatch.setenv("KBC_DATADIR", str(data_dir))
    monkeypatch.setenv("KBC_DATA_TYPE_SUPPORT", "authoritative")
    monkeypatch.delenv("MEDALLIA_MAX_PAGES", raising=False)
    monkeypatch.setattr(mc.requests, "Session", lambda: stub)
    monkeypatch.setattr(ComponentBase, "_should_vcr_replay", staticmethod(lambda: False))
    import component  # imported here so KBC_DATADIR is already set

    comp = component.Component()
    with freeze_time(RUN_AT):
        comp.run()
    return comp


def _read_csv(data_dir: Path, name: str) -> tuple[list[str], list[dict]]:
    import csv

    path = data_dir / "out" / "tables" / name
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def _read_manifest(data_dir: Path, name: str) -> dict:
    return json.loads((data_dir / "out" / "tables" / f"{name}.manifest").read_text())


def _manifest_pk(manifest: dict) -> list[str]:
    if "primary_key" in manifest:
        return manifest["primary_key"]
    schema = manifest.get("schema") or []
    return [c["name"] for c in schema if c.get("primary_key")]


# --------------------------------------------------------------------------------------------
# Tests — structured, per node shape + PK strategy + mode
# --------------------------------------------------------------------------------------------


def test_feedback_fielddata_incremental_resume_node_id_pk(tmp_path, monkeypatch):
    """Shape (a): fieldData, node-id PK, incremental resume from seeded state → gte + advance."""
    pages = {
        "feedback": [
            _page(
                [
                    {"id": "F1", "a_customerid": {"values": ["C1"]}, "a_finish_ts": {"values": ["1780000100"]}},
                    {"id": "F2", "a_customerid": {"values": ["C2"]}, "a_finish_ts": {"values": ["1780000200"]}},
                ]
            )
        ]
    }
    params = {
        "mode": "structured",
        "data_object": "feedback",
        "fields": ["a_customerid", "a_finish_ts"],
        "load_type": "incremental_load",
        "incremental_field": "a_finish_ts",
        "page_size": 5,
    }
    data_dir = _write_datadir(tmp_path, params, in_state={"last_incremental_value": 1780000000})
    stub = StubSession(pages)
    _run(monkeypatch, data_dir, stub)

    fieldnames, rows = _read_csv(data_dir, "feedback.csv")
    assert fieldnames[0] == "id"
    assert [r["id"] for r in rows] == ["F1", "F2"]
    assert rows[0]["a_customerid"] == "C1"

    manifest = _read_manifest(data_dir, "feedback.csv")
    assert _manifest_pk(manifest) == ["id"]
    assert manifest["incremental"] is True

    # watermark advanced to the max finish value seen
    state = json.loads((data_dir / "out" / "state.json").read_text())
    assert state["last_incremental_value"] == 1780000200

    # gte lower bound from the seeded watermark was injected into the query
    data_query = next(b["query"] for b in stub.data_requests)
    assert "gte" in data_query and "1780000000" in data_query
    # the connection's real first/after types (Int!/ID) are threaded into the variable header,
    # not the GraphQL-nullable defaults — a nullable $first would be rejected by the gateway
    assert data_query.startswith("query ($first: Int!, $after: ID)")


def test_customers_data_shape_full_load_node_id_pk(tmp_path, monkeypatch):
    """Shape (b): data(fieldId){value,values}, full load → incremental=False, node-id PK."""
    pages = {
        "customers": [
            _page(
                [
                    {
                        "id": "K1",
                        "email": {"value": "a@example.com", "values": None},
                        "tags": {"value": None, "values": ["x", "y"]},
                    },
                    {
                        "id": "K2",
                        "email": {"value": "b@example.com", "values": None},
                        "tags": {"value": None, "values": ["z"]},
                    },
                ]
            )
        ]
    }
    params = {
        "mode": "structured",
        "data_object": "customers",
        "fields": ["email", "tags"],
        "load_type": "full_load",
        "page_size": 5,
    }
    data_dir = _write_datadir(tmp_path, params)
    stub = StubSession(pages)
    _run(monkeypatch, data_dir, stub)

    fieldnames, rows = _read_csv(data_dir, "customers.csv")
    assert rows[0]["email"] == "a@example.com"
    assert rows[0]["tags"] == json.dumps(["x", "y"])  # multi-value → JSON
    assert rows[1]["tags"] == "z"  # single value → scalar

    manifest = _read_manifest(data_dir, "customers.csv")
    assert _manifest_pk(manifest) == ["id"]
    assert manifest["incremental"] is False
    # full load writes no watermark (no incremental_field)
    assert not (data_dir / "out" / "state.json").exists()


def test_scalar_idless_object_row_hash_pk(tmp_path, monkeypatch):
    """Shape (c) id-less: socialURLs → deterministic _row_hash PK column."""
    pages = {
        "socialURLs": [
            _page(
                [
                    {"url": "https://s.example/1", "healthy": True},
                    {"url": "https://s.example/2", "healthy": False},
                ]
            )
        ]
    }
    params = {
        "mode": "structured",
        "data_object": "socialURLs",
        "fields": [],  # empty ⇒ all scalar fields for shape (c)
        "load_type": "full_load",
        "page_size": 5,
    }
    data_dir = _write_datadir(tmp_path, params)
    stub = StubSession(pages)
    _run(monkeypatch, data_dir, stub)

    fieldnames, rows = _read_csv(data_dir, "socialURLs.csv")
    assert "_row_hash" in fieldnames
    assert "id" not in fieldnames
    assert len({r["_row_hash"] for r in rows}) == 2  # distinct, deterministic
    assert all(len(r["_row_hash"]) == 64 for r in rows)  # sha256 hex

    manifest = _read_manifest(data_dir, "socialURLs.csv")
    assert _manifest_pk(manifest) == ["_row_hash"]
    assert manifest["incremental"] is False


def test_empty_result_writes_header_no_state(tmp_path, monkeypatch):
    """Empty connection → header-only CSV, no rows; watermark left unchanged (nothing seen)."""
    pages = {"feedback": [_page([])]}
    params = {
        "mode": "structured",
        "data_object": "feedback",
        "fields": ["a_customerid"],
        "load_type": "incremental_load",
        "incremental_field": "a_finish_ts",
        "page_size": 5,
    }
    data_dir = _write_datadir(tmp_path, params)  # no seed state
    stub = StubSession(pages)
    _run(monkeypatch, data_dir, stub)

    fieldnames, rows = _read_csv(data_dir, "feedback.csv")
    assert rows == []
    assert "id" in fieldnames
    # no lower bound and nothing seen → no watermark persisted
    assert not (data_dir / "out" / "state.json").exists()


def test_multi_page_pagination_via_hasnextpage(tmp_path, monkeypatch):
    """Cursor pagination walks pages via pageInfo.hasNextPage/endCursor (no totalCount)."""
    pages = {
        "programs": [
            _page([{"id": "P1", "programName": "Alpha", "recordCount": 10}], has_next=True, cursor="c1"),
            _page([{"id": "P2", "programName": "Beta", "recordCount": 20}], has_next=False),
        ]
    }
    params = {"mode": "structured", "data_object": "programs", "fields": [], "load_type": "full_load", "page_size": 1}
    data_dir = _write_datadir(tmp_path, params)
    stub = StubSession(pages)
    _run(monkeypatch, data_dir, stub)

    _, rows = _read_csv(data_dir, "programs.csv")
    assert [r["id"] for r in rows] == ["P1", "P2"]
    # second request carried the endCursor from page 1
    assert stub.data_requests[1]["variables"]["after"] == "c1"

    manifest = _read_manifest(data_dir, "programs.csv")
    # typed manifest: recordCount is a GraphQL Int scalar → INTEGER base type
    schema = {c["name"]: c for c in manifest["schema"]} if "schema" in manifest else {}
    if schema:
        assert schema["recordCount"]["data_type"]["base"]["type"] == "INTEGER"
        assert schema["programName"]["data_type"]["base"]["type"] == "STRING"


# --------------------------------------------------------------------------------------------
# Tests — raw mode
# --------------------------------------------------------------------------------------------

RAW_QUERY = (
    "query ($first: Int, $after: String) { "
    "feedback(first: $first, after: $after) { nodes { id score } pageInfo { hasNextPage endCursor } } }"
)


def test_raw_mode_full_load_no_state(tmp_path, monkeypatch):
    """Raw mode: user query, single connection, full load, id PK, NO state file written."""
    pages = {"feedback": [_page([{"id": "R1", "score": 7}, {"id": "R2", "score": 9}])]}
    params = {
        "mode": "raw",
        "raw_query": RAW_QUERY,
        "output_table": "raw_out",
        "page_size": 5,
    }
    data_dir = _write_datadir(tmp_path, params)
    stub = StubSession(pages)
    _run(monkeypatch, data_dir, stub)

    fieldnames, rows = _read_csv(data_dir, "raw_out.csv")
    assert [r["id"] for r in rows] == ["R1", "R2"]
    manifest = _read_manifest(data_dir, "raw_out.csv")
    assert _manifest_pk(manifest) == ["id"]
    assert manifest["incremental"] is False
    assert not (data_dir / "out" / "state.json").exists()


def test_raw_mode_idless_row_hash(tmp_path, monkeypatch):
    """Raw mode with id-less nodes → _row_hash PK."""
    pages = {"socialURLs": [_page([{"url": "https://s/1"}, {"url": "https://s/2"}])]}
    raw = (
        "query ($first: Int, $after: String) { "
        "socialURLs(first: $first, after: $after) { nodes { url } pageInfo { hasNextPage endCursor } } }"
    )
    params = {"mode": "raw", "raw_query": raw, "output_table": "raw_social", "page_size": 5}
    data_dir = _write_datadir(tmp_path, params)
    _run(monkeypatch, data_dir, StubSession(pages))
    fieldnames, rows = _read_csv(data_dir, "raw_social.csv")
    assert "_row_hash" in fieldnames
    assert _manifest_pk(_read_manifest(data_dir, "raw_social.csv")) == ["_row_hash"]


# --------------------------------------------------------------------------------------------
# Tests — HTTP-free / contract failures (exit-1 UserException, no network)
# --------------------------------------------------------------------------------------------


def test_raw_mode_missing_pageinfo_static_check(tmp_path, monkeypatch):
    params = {"mode": "raw", "raw_query": "query { feedback { nodes { id } } }", "output_table": "x", "page_size": 5}
    data_dir = _write_datadir(tmp_path, params)
    with pytest.raises(UserException, match="pageInfo"):
        _run(monkeypatch, data_dir, StubSession({}))


def test_raw_mode_multi_connection_rejected(tmp_path, monkeypatch):
    raw = (
        "query ($first: Int, $after: String) { "
        "feedback(first: $first, after: $after) { nodes { id } pageInfo { hasNextPage endCursor } } "
        "customers { nodes { id } pageInfo { hasNextPage endCursor } } }"
    )
    # Both objects present → runtime response has two connections → exit 1.
    pages = {
        "feedback": [_page([{"id": "A"}])],
    }

    class TwoConnSession(StubSession):
        def post(self, url, params=None, json=None, headers=None, data=None, timeout=None, auth=None):  # noqa: A002
            body = json or {}
            if (
                (params and params.get("compute_cost_only"))
                or url.endswith("/token")
                or "__schema" in body.get("query", "")
            ):
                return super().post(
                    url, params=params, json=json, headers=headers, data=data, timeout=timeout, auth=auth
                )
            return _StubResponse({"data": {"feedback": _page([{"id": "A"}]), "customers": _page([{"id": "B"}])}})

    params = {"mode": "raw", "raw_query": raw, "output_table": "x", "page_size": 5}
    data_dir = _write_datadir(tmp_path, params)
    with pytest.raises(UserException, match="exactly one connection"):
        _run(monkeypatch, data_dir, TwoConnSession(pages))


def test_missing_credentials_exit1(tmp_path, monkeypatch):
    """A missing required root field raises UserException (exit 1) before any HTTP."""
    data_dir = tmp_path / "data"
    (data_dir / "out" / "tables").mkdir(parents=True, exist_ok=True)
    params: dict[str, object] = {k: v for k, v in ROOT.items() if k != "#client_secret"}
    params.update({"mode": "structured", "data_object": "feedback", "fields": ["a_customerid"]})
    (data_dir / "config.json").write_text(json.dumps({"action": "run", "parameters": params}))
    with pytest.raises(UserException):
        _run(monkeypatch, data_dir, StubSession({}))


def test_bad_filters_json_exit1(tmp_path, monkeypatch):
    params = {
        "mode": "structured",
        "data_object": "feedback",
        "fields": ["a_customerid"],
        "load_type": "full_load",
        "filters": "{not valid json",
        "page_size": 5,
    }
    data_dir = _write_datadir(tmp_path, params)
    with pytest.raises(UserException, match="filters"):
        _run(monkeypatch, data_dir, StubSession({"feedback": [_page([])]}))
