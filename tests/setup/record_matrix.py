"""Re-record the VCR functional test matrix for the generic keboola.ex-medallia extractor.

This is the canonical, reproducible recorder for the LIVE-recorded cases under
``tests/functional/`` (the ``10``+ range). It is NOT a synthetic-cassette generator: every
cassette is a REAL recording against the live Medallia instance, sanitized AT RECORD TIME by
the ``VCR_SANITIZERS`` in ``src/component.py`` (``MedalliaResponseBodySanitizer`` overwrites
EVERY node value across all three node shapes — fieldData / data / bare scalar — plus every
node id, the ``pageInfo.endCursor`` and the request ``after`` cursor; ``UrlPatternSanitizer``
rewrites the tenant/gateway hosts; ``DefaultSanitizer`` redacts credentials). Because
keboola.vcr returns the REAL response to the component during recording (only the cassette is
sanitized), each case is then REPLAYED network-free against its own sanitized cassette to
regenerate synthetic ``expected/`` tables, ``logs.json``, ``sync_action_result.json`` and
``expected_status.json`` — otherwise those would hold real data.

Prerequisites: a gitignored ``secrets.json`` at the repo root with the real ``parameters``
(``instance_host``, ``api_host``, ``company_name``, ``client_id``, ``#client_secret``). NEVER
commit it, and NEVER print its values.

Usage (from the repo root, inside ``uv run``):

    uv run python tests/setup/record_matrix.py both  <name>[,<name>...]   # record + regenerate
    uv run python tests/setup/record_matrix.py record <names>            # record only (live calls)
    uv run python tests/setup/record_matrix.py regen  <names>            # regenerate only (no network)
    uv run python tests/setup/record_matrix.py both ALL                  # every live case

The HTTP-free failure cases (03-08) are NOT recorded here — they need no live call and no
secrets; scaffold them with:  uv run python -m keboola.datadirtest scaffold \
    --definitions tests/setup/configs.failure.json --output tests/functional --component src/component.py

Live-call etiquette (spec §10.3): page_size=25, a hard MEDALLIA_MAX_PAGES=2 cap, narrow
windows, one deliberately-wrong call each for the 401/4xx cases. Be gentle — this hits a LIVE
PRODUCTION instance. Objects that return nothing/errors on this instance are skipped with a
noted reason rather than forced.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import shutil
import sys
from pathlib import Path
from runpy import run_path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from keboola.datadirtest.vcr.tester import _load_vcr_sanitizers_from_script  # noqa: E402
from keboola.vcr.recorder import VCRRecorder  # noqa: E402
from keboola.vcr.scaffolder import TestScaffolder  # noqa: E402
from keboola.vcr.validator import save_output_snapshot  # noqa: E402

COMPONENT = REPO / "src" / "component.py"
FUNC = REPO / "tests" / "functional"
FREEZE = "2026-07-16T12:00:00"
REPLAY_MAX_PAGES = "2"  # must match tests/conftest.py and the cap used while recording

_secrets_path = REPO / "secrets.json"
REAL = json.loads(_secrets_path.read_text()) if _secrets_path.exists() else {"parameters": {}}


def secrets_variant(kind: str) -> dict:
    """Return the real secrets, or a deliberately-broken variant for the failure cases."""
    v = copy.deepcopy(REAL)
    p = v["parameters"]
    if kind == "wrong_secret":  # -> 401 at the token endpoint
        p["#client_secret"] = "deliberately-wrong-not-a-real-secret"
    elif kind == "wrong_tenant":  # -> non-401 4xx (unknown company in the token URL)
        p["company_name"] = "nonexistent-tenant-does-not-exist-zzz"
    return v


# Dummy root values committed to each config.json (overridden in-memory by the secrets
# variant at record time; rewritten to placeholders by the URL sanitizer so replay matches).
DUMMY_ROOT = {
    "instance_host": "example.medallia.com",
    "api_host": "example.apis.medallia.com",
    "company_name": "example",
    "client_id": "DUMMY_CLIENT_ID",
    "#client_secret": "DUMMY_CLIENT_SECRET",
}

# Real field IDs for this instance (schema identifiers, not PII), discovered via listFields.
CUSTOMER_ID = "a_customerid"
ACCEPTED = "e_accepteddate"
FINISH_EPOCH = "a_initial_finish_timestamp"  # 10-digit epoch finish field (dense, sortable INT)
MULTI = "a_recognition_sentiment_types"  # returns >1 value on some records
# Seed/initial-start sits BEFORE the sanitizer's synthetic base (2026-08-01) so the synthetic
# finish values sort after it and first-page rows survive boundary dedup.
EPOCH_START = 1780272000  # 2026-06-01T00:00:00Z


def cfg(action: str, **params) -> dict:
    # ``filters`` is a JSON STRING in the redesigned row model (spec §5.2), so serialise any
    # dict a case supplies before it lands in config.json.
    if isinstance(params.get("filters"), (dict, list)):
        params["filters"] = json.dumps(params["filters"])
    return {"action": action, "parameters": {**DUMMY_ROOT, **params}}


def _structured(data_object, **params) -> dict:
    base = dict(mode="structured", data_object=data_object, page_size=25, load_type="incremental_load")
    base.update(params)
    return cfg("run", **base)


# Live-recorded matrix (numbered 10+ so it never collides with the HTTP-free failure cases
# 03-08 that share tests/functional/). Each object is attempted gently; skip-with-reason if the
# live instance returns nothing or errors for it.
CASES = {
    "10_testConnection": dict(
        description="testConnection — token mint + compute_cost_only pre-flight (no quota).",
        config=cfg("testConnection"),
        secrets="real",
        max_pages=None,
        seed=None,
    ),
    "11_listObjects": dict(
        description="listObjects — one __schema introspection; classified into the extractable set.",
        config=cfg("listObjects"),
        secrets="real",
        max_pages=None,
        seed=None,
    ),
    "12_listFields_feedback": dict(
        description="listFields for feedback — the field catalogue (replaced by a synthetic set).",
        config=cfg("listFields", mode="structured", data_object="feedback"),
        secrets="real",
        max_pages=None,
        seed=None,
    ),
    "13_feedback": dict(
        description="feedback (shape a, fieldData, node-id PK) incremental on the epoch finish field.",
        config=_structured(
            "feedback",
            fields=[CUSTOMER_ID, ACCEPTED],
            incremental_field=FINISH_EPOCH,
            initial_start=str(EPOCH_START),
        ),
        secrets="real",
        max_pages=2,
        seed=None,
    ),
    "14_invitations": dict(
        description="invitations (shape a, fieldData, node-id PK) incremental on the epoch finish field.",
        config=_structured(
            "invitations",
            fields=[CUSTOMER_ID, ACCEPTED],
            incremental_field=FINISH_EPOCH,
            initial_start=str(EPOCH_START),
        ),
        secrets="real",
        max_pages=2,
        seed=None,
    ),
    # NOTE (skip-with-reason, spec §10.3): two required VCR shapes are NOT recordable on this
    # live instance, so they are covered network-free by the hand-authored datadir fixtures in
    # tests/test_datadir.py instead (per the "skip rather than force" rule):
    #   * customers (data shape) — the instance returns "Unsupported Operation - Elasticsearch is
    #     not enabled"; the data(fieldId){value,values} logic is covered by
    #     test_datadir.test_customers_data_shape_full_load_node_id_pk.
    #   * an id-less scalar (socialURLs / socialUrlsHealth / unitWarnings) — these operational
    #     connections declare a NON-NULL offset cursor (`after: Int!`), which the variable-based
    #     first-page request (after=null) cannot satisfy; the id-less _row_hash logic is covered
    #     by test_datadir.test_scalar_idless_object_row_hash_pk / test_raw_mode_idless_row_hash.
    "17_raw": dict(
        description="raw mode — user Relay query paginated via $first/$after; full load, no state.",
        config=cfg(
            "run",
            mode="raw",
            output_table="raw_feedback",
            page_size=25,
            # The variable types MUST match the connection's real arg types on this instance
            # (feedback: first Int!, after ID); the static contract check only requires the
            # $first/$after/pageInfo tokens to be present, which they are.
            raw_query=(
                "query ($first: Int!, $after: ID) { "
                "feedback(first: $first, after: $after) { "
                "nodes { id " + CUSTOMER_ID + ': fieldData(fieldId: "' + CUSTOMER_ID + '") { values } } '
                "pageInfo { hasNextPage endCursor } } }"
            ),
        ),
        secrets="real",
        max_pages=2,
        seed=None,
    ),
    "20_validateQuery_preview": dict(
        description="validateQuery — cost pre-flight then a small live row preview (Keboola standard).",
        config=cfg(
            "validateQuery",
            mode="raw",
            output_table="raw_feedback",
            page_size=25,
            raw_query=(
                "query ($first: Int!, $after: ID) { "
                "feedback(first: $first, after: $after) { "
                "nodes { id " + CUSTOMER_ID + ': fieldData(fieldId: "' + CUSTOMER_ID + '") { values } } '
                "pageInfo { hasNextPage endCursor } } }"
            ),
        ),
        secrets="real",
        max_pages=2,
        seed=None,
    ),
    "18_auth_401": dict(
        description="401: one deliberately-wrong #client_secret -> actionable UserException, exit 1.",
        config=cfg("testConnection"),
        secrets="wrong_secret",
        max_pages=None,
        seed=None,
    ),
    "19_bad_request_4xx": dict(
        description="4xx: an unknown tenant in the token URL -> HTTP 4xx -> actionable UserException, exit 1.",
        config=cfg("testConnection"),
        secrets="wrong_tenant",
        max_pages=None,
        seed=None,
    ),
}


def record(name: str) -> None:
    spec = CASES[name]
    if spec["max_pages"] is not None:
        os.environ["MEDALLIA_MAX_PAGES"] = str(spec["max_pages"])
    else:
        os.environ.pop("MEDALLIA_MAX_PAGES", None)
    TestScaffolder()._scaffold_single_test(
        definition={"name": name, "config": spec["config"]},
        output_dir=FUNC,
        component_script=COMPONENT,
        record=True,
        freeze_time_at=FREEZE,
        secrets_override=secrets_variant(str(spec["secrets"])),
        input_state=spec["seed"],  # ty: ignore[invalid-argument-type]
        regenerate=True,
    )
    os.environ.pop("MEDALLIA_MAX_PAGES", None)
    cassette = FUNC / name / "source" / "data" / "cassettes" / "requests.json"
    n = len(json.loads(cassette.read_text()).get("interactions", [])) if cassette.exists() else 0
    print(f"RECORDED {name}: interactions={n}")


def _clear_dir(d: Path) -> None:
    if d.exists():
        for item in d.iterdir():
            if item.is_file():
                item.unlink()


def regen(name: str) -> None:
    """Replay the sanitized cassette network-free; overwrite expected/ + logs with synthetic output."""
    sdd = FUNC / name / "source" / "data"
    expected_out = FUNC / name / "expected" / "data" / "out"
    for sub in ("tables", "files"):
        _clear_dir(sdd / "out" / sub)
        (sdd / "out" / sub).mkdir(parents=True, exist_ok=True)
        (expected_out / sub).mkdir(parents=True, exist_ok=True)
    (sdd / "out" / "state.json").unlink(missing_ok=True)

    sanitizers = _load_vcr_sanitizers_from_script(str(COMPONENT))
    rec = VCRRecorder.from_test_dir(sdd, freeze_time_at="auto", sanitizers=sanitizers, secrets_override={})
    captured: dict = {}

    def save_instead_of_assert(run_result, stdout_capture):
        captured["exit"] = run_result.exit_code if run_result and run_result.exit_code is not None else 0
        rec._save_log_artefacts(run_result, is_recording=True)  # synthetic logs.json + expected_status.json
        if stdout_capture is not None:
            raw = stdout_capture.getvalue().strip()
            if raw:
                rec.sync_action_result_path.write_text(raw)
            else:
                rec.sync_action_result_path.unlink(missing_ok=True)

    rec._assert_replay_result = save_instead_of_assert  # ty: ignore[invalid-assignment]

    def runner():
        os.environ["KBC_DATADIR"] = str(sdd)
        os.environ["MEDALLIA_MAX_PAGES"] = REPLAY_MAX_PAGES
        if str(COMPONENT.parent) not in sys.path:
            sys.path.insert(0, str(COMPONENT.parent))
        # Disable the component SDK's own auto-VCR-replay layer (avoids a conflicting
        # second VCR layer), exactly as the datadirtest tester does during replay.
        from keboola.component.base import ComponentBase

        ComponentBase._should_vcr_replay = staticmethod(lambda: False)  # ty: ignore[invalid-assignment]
        run_path(str(COMPONENT), run_name="__main__")

    # A seeded case must be re-seeded before this network-free replay (its in/state.json is
    # otherwise the last recorded one); source/set_up.py handles seeding under pytest.
    if CASES[name]["seed"] is not None:
        (sdd / "in").mkdir(parents=True, exist_ok=True)
        (sdd / "in" / "state.json").write_text(json.dumps(CASES[name]["seed"]))

    rec.replay(runner)
    os.environ.pop("MEDALLIA_MAX_PAGES", None)

    for sub in ("tables", "files"):
        src, dst = sdd / "out" / sub, expected_out / sub
        _clear_dir(dst)
        if src.exists():
            for item in src.iterdir():
                if item.is_file():
                    if item.suffix == ".manifest":
                        item.write_text(json.dumps(json.loads(item.read_text()), indent=2) + "\n")
                    shutil.copy2(item, dst / item.name)
    with contextlib.suppress(Exception):
        save_output_snapshot(sdd, output_subdir="out")
    print(f"REGEN {name}: exit={captured.get('exit')}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    arg = sys.argv[2] if len(sys.argv) > 2 else "ALL"
    names = list(CASES) if arg == "ALL" else arg.split(",")
    for nm in names:
        if mode in ("record", "both"):
            record(nm)
        if mode in ("regen", "both"):
            regen(nm)
