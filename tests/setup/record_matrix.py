"""Re-record the VCR functional test matrix for keboola.ex-medallia.

This is the canonical, reproducible recorder for the cases under ``tests/functional/``. It is
NOT a synthetic-cassette generator: every cassette is a REAL recording against the live
Medallia instance, sanitized AT RECORD TIME by the ``VCR_SANITIZERS`` in ``src/component.py``
(``MedalliaResponseBodySanitizer`` overwrites every response value with deterministic
synthetic data; ``UrlPatternSanitizer`` rewrites the tenant/gateway hosts; ``DefaultSanitizer``
redacts credentials). Because keboola.vcr returns the REAL response to the component during
recording (only the cassette is sanitized), each case is then REPLAYED network-free against
its own sanitized cassette to regenerate synthetic ``expected/`` tables, ``logs.json``,
``sync_action_result.json`` and ``expected_status.json`` — otherwise those would hold real data.

Prerequisites: a gitignored ``secrets.json`` at the repo root with the real
``parameters`` (``instance_host``, ``api_host``, ``company_name``, ``client_id``,
``#client_secret``). NEVER commit it, and NEVER print its values.

Usage (from the repo root, inside ``uv run``):

    uv run python tests/setup/record_matrix.py both  <name>[,<name>...]   # record + regenerate
    uv run python tests/setup/record_matrix.py record <names>            # record only (live calls)
    uv run python tests/setup/record_matrix.py regen  <names>            # regenerate only (no network)
    uv run python tests/setup/record_matrix.py both ALL                  # every data/sync/error case

The HTTP-free failure cases (03-07) are NOT recorded here — they need no live call and no
secrets; scaffold them with:  uv run python -m keboola.datadirtest scaffold \
    --definitions tests/setup/configs.failure.json --output tests/functional --component src/component.py

Live-call etiquette: page_size=5, a hard MEDALLIA_MAX_PAGES=3 cap, narrow windows, one
deliberately-wrong call each for the 401/4xx cases. Be gentle — this hits a live instance.
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
REPLAY_MAX_PAGES = "3"  # must match tests/conftest.py and the cap used while recording

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
SURVEY = "a_surveyid"
FINISH_DT = "e_creationdate"  # DATETIME finish field (sparse -> natural pagination end)
FINISH_EPOCH = "a_initial_finish_timestamp"  # 10-digit epoch finish field (dense)
MULTI = "a_recognition_sentiment_types"  # returns >1 value on some records
# NB: the documented default `k_initialfinishdate_epoch_int` is ABSENT on this instance;
# `a_initial_finish_timestamp` is its real epoch-finish equivalent.

# Seeds/initial-starts sit BEFORE the sanitizer's synthetic base (2026-08-01) so the
# synthetic finish values sort after them and first-page rows survive boundary dedup.
DT_START = "2026-06-25"
EPOCH_START = 1780272000  # 2026-06-01T00:00:00Z


def cfg(action: str, **params) -> dict:
    return {"action": action, "parameters": {**DUMMY_ROOT, **params}}


def _run_cfg(finish_id, finish_type, extra=None, **params):
    base = dict(
        data_object="feedback",
        survey_id_field_id=SURVEY,
        finish_date_field_id=finish_id,
        finish_date_field_type=finish_type,
        fields=["a_customerid", "e_accepteddate"],
        page_size=5,
        load_type="incremental_load",
    )
    if extra:
        base.update(extra)
    base.update(params)
    return cfg("run", **base)


CASES = {
    "01_testConnection": dict(
        description="testConnection sync action — token mint + compute_cost_only pre-flight (no quota).",
        config=cfg("testConnection"),
        secrets="real",
        max_pages=None,
        seed=None,
    ),
    "02_listFields": dict(
        description="listFields sync action — one metadata call; catalogue replaced by a synthetic field set.",
        config=cfg("listFields"),
        secrets="real",
        max_pages=None,
        seed=None,
    ),
    "08_epoch_watermark": dict(
        description="Epoch watermark: finish=a_initial_finish_timestamp (epoch). Asserts INTEGER typing + advance.",
        config=_run_cfg(FINISH_EPOCH, "epoch", initial_start_epoch=EPOCH_START),
        secrets="real",
        max_pages=3,
        seed=None,
    ),
    "09_incremental_resume": dict(
        description="Incremental resume from a seeded in/state.json (see source/set_up.py); watermark advances.",
        config=_run_cfg(FINISH_DT, "datetime"),
        secrets="real",
        max_pages=3,
        seed={"last_finish_date_epoch": DT_START, "last_survey_id": "-1"},
    ),
    "10_full_load": dict(
        description="Full load (load_type=full_load): non-incremental manifest + boundary-deduped output.",
        config=_run_cfg(FINISH_DT, "datetime", load_type="full_load", initial_start_value=DT_START),
        secrets="real",
        max_pages=3,
        seed=None,
    ),
    "11_empty_result": dict(
        description="Empty result: an impossible filter -> 0 rows, no lower bound -> state left unchanged.",
        config=_run_cfg(FINISH_DT, "datetime", filters={"fieldIds": [FINISH_DT], "gt": "2099-01-01"}),
        secrets="real",
        max_pages=3,
        seed=None,
    ),
    "12_multi_value_field": dict(
        description="Multi-value field: includes a_recognition_sentiment_types -> JSON-encoded multi-value column.",
        config=_run_cfg(FINISH_DT, "datetime", fields=[MULTI, "a_customerid"], initial_start_value=DT_START),
        secrets="real",
        max_pages=3,
        seed=None,
    ),
    "13_business_filters": dict(
        description="Business filters applied (filters=...) and injected into the keyset query.",
        config=_run_cfg(
            FINISH_DT,
            "datetime",
            filters={"fieldIds": ["e_accepteddate"], "gt": "2020-01-01"},
            initial_start_value=DT_START,
        ),
        secrets="real",
        max_pages=3,
        seed=None,
    ),
    "14_pagination_multi_page": dict(
        description="Pagination across 3 pages (page_size=5, dense epoch field, MEDALLIA_MAX_PAGES=3 cap).",
        config=_run_cfg(FINISH_EPOCH, "epoch", initial_start_epoch=EPOCH_START),
        secrets="real",
        max_pages=3,
        seed=None,
    ),
    "15_auth_401": dict(
        description="401: one deliberately-wrong #client_secret -> actionable UserException, exit 1.",
        config=cfg("testConnection"),
        secrets="wrong_secret",
        max_pages=None,
        seed=None,
    ),
    "16_bad_request_4xx": dict(
        description="4xx: an unknown tenant in the token URL -> HTTP 400 -> actionable UserException, exit 1.",
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
