"""Datadir (functional) tests for the Medallia extractor — no network, no credentials.

The success case runs the full component against a hand-authored ``KBC_DATADIR`` fixture
with the HTTP transport monkeypatched (``requests.Session.post``) so the real token
manager, query builder, keyset paginator, flattening, and state handling all execute
end-to-end against fabricated GraphQL responses.

The failure case runs the component as a subprocess against a config missing a required
field and asserts a non-zero (exit code 1) UserException exit.
"""

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freezegun import freeze_time

from component import Component

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
FIXTURES = Path(__file__).resolve().parent / "data"

# Fabricated feedback nodes (oldest → newest); aliases match the query builder selection.
# ``id`` is the node's direct scalar identifier (selected alongside the survey field).
_FEEDBACK_NODES = [
    {
        "id": "F100",
        "surveyId": {"values": ["S100"]},
        "k_fin": {"values": ["1704153600"]},
        "e_nps": {"values": ["9"]},
        "e_comment": {"values": ["Great"]},
    },
    {
        "id": "F101",
        "surveyId": {"values": ["S101"]},
        "k_fin": {"values": ["1704240000"]},
        "e_nps": {"values": ["7"]},
        "e_comment": {"values": ["Okay"]},
    },
    {
        "id": "F102",
        "surveyId": {"values": ["S102"]},
        "k_fin": {"values": ["1704326400"]},
        "e_nps": {"values": ["3"]},
        "e_comment": {"values": ["Poor"]},
    },
]

# Fabricated nodes for a DATETIME/ISO watermark field (values like "2026-05-01"); a
# non-fatal "Invalid field id:" error accompanies the valid data (must be tolerated).
_DATETIME_NODES = [
    {
        "id": "F200",
        "surveyId": {"values": ["S200"]},
        "e_creationdate": {"values": ["2026-05-01T08:00:00Z"]},
        "e_comment": {"values": ["Early"]},
    },
    {
        "id": "F201",
        "surveyId": {"values": ["S201"]},
        "e_creationdate": {"values": ["2026-05-02T09:30:00Z"]},
        "e_comment": {"values": ["Later"]},
    },
]


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.headers = {}

    def json(self):
        return self._json


def _fake_post(self, url, **kwargs):
    """Route token/query POSTs to canned responses — a single feedback page of 3 nodes."""
    if url.endswith("/token"):
        return _FakeResponse(200, {"access_token": "fake-token", "expires_in": 3600})
    return _FakeResponse(200, {"data": {"feedback": {"totalCount": len(_FEEDBACK_NODES), "nodes": _FEEDBACK_NODES}}})


def _fake_post_datetime(self, url, **kwargs):
    """Datetime-watermark page carrying a tolerated ``Invalid field id:`` error alongside data."""
    if url.endswith("/token"):
        return _FakeResponse(200, {"access_token": "fake-token", "expires_in": 3600})
    return _FakeResponse(
        200,
        {
            "data": {"feedback": {"totalCount": len(_DATETIME_NODES), "nodes": _DATETIME_NODES}},
            "errors": [{"message": "Invalid field id: e_bogus"}],
        },
    )


class TestDatadirSuccess(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="medallia-datadir-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        shutil.copytree(FIXTURES / "test_incremental_success", self._tmp, dirs_exist_ok=True)
        (Path(self._tmp) / "out" / "tables").mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {"KBC_DATADIR": self._tmp})
        self._env.start()
        self.addCleanup(self._env.stop)

    @freeze_time("2024-02-01")
    @mock.patch("requests.Session.post", new=_fake_post)
    def test_run_writes_table_manifest_and_state(self):
        Component().run()

        out_tables = Path(self._tmp) / "out" / "tables"
        csv_path = out_tables / "feedback.csv"
        manifest_path = out_tables / "feedback.csv.manifest"
        state_path = Path(self._tmp) / "out" / "state.json"

        self.assertTrue(csv_path.exists())
        self.assertTrue(manifest_path.exists())
        self.assertTrue(state_path.exists())

        with open(csv_path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 3)
        self.assertEqual(set(rows[0].keys()), {"id", "surveyId", "k_fin", "e_nps", "e_comment"})
        self.assertEqual([r["surveyId"] for r in rows], ["S100", "S101", "S102"])
        # The node ``id`` scalar is captured per record.
        self.assertEqual([r["id"] for r in rows], ["F100", "F101", "F102"])
        self.assertEqual(rows[2]["e_comment"], "Poor")

        with open(manifest_path) as fh:
            manifest = json.load(fh)
        self.assertTrue(manifest["incremental"])
        # New typed-manifest format: primary key is flagged per-column in ``schema``.
        pk_columns = [col["name"] for col in manifest["schema"] if col.get("primary_key")]
        self.assertEqual(pk_columns, ["surveyId"])
        # The finish-date watermark column is typed as INTEGER (epoch seconds).
        finish_col = next(col for col in manifest["schema"] if col["name"] == "k_fin")
        self.assertEqual(finish_col["data_type"]["base"]["type"], "INTEGER")

        with open(state_path) as fh:
            state = json.load(fh)
        # Watermark advanced to the last (newest) record.
        self.assertEqual(state["last_finish_date_epoch"], 1704326400)
        self.assertEqual(state["last_survey_id"], "S102")


class TestDatadirDatetimeWatermark(unittest.TestCase):
    """A DATETIME/ISO watermark field must extract, type, and advance without int() coercion."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="medallia-datetime-")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        shutil.copytree(FIXTURES / "test_datetime_watermark", self._tmp, dirs_exist_ok=True)
        (Path(self._tmp) / "out" / "tables").mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {"KBC_DATADIR": self._tmp})
        self._env.start()
        self.addCleanup(self._env.stop)

    @freeze_time("2026-07-15")
    @mock.patch("requests.Session.post", new=_fake_post_datetime)
    def test_datetime_watermark_advances_and_types_as_string(self):
        Component().run()

        out_tables = Path(self._tmp) / "out" / "tables"
        csv_path = out_tables / "feedback.csv"
        manifest_path = out_tables / "feedback.csv.manifest"
        state_path = Path(self._tmp) / "out" / "state.json"

        with open(csv_path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["surveyId"] for r in rows], ["S200", "S201"])
        self.assertEqual(rows[0]["e_creationdate"], "2026-05-01T08:00:00Z")

        with open(manifest_path) as fh:
            manifest = json.load(fh)
        # The datetime watermark column stays STRING — never forced to INTEGER.
        finish_col = next(col for col in manifest["schema"] if col["name"] == "e_creationdate")
        self.assertEqual(finish_col["data_type"]["base"]["type"], "STRING")

        with open(state_path) as fh:
            state = json.load(fh)
        # Watermark advanced to the newest ISO value (stored verbatim as a string).
        self.assertEqual(state["last_finish_date_epoch"], "2026-05-02T09:30:00Z")
        self.assertEqual(state["last_survey_id"], "S201")


class TestDatadirFailure(unittest.TestCase):
    def test_invalid_config_exits_nonzero(self):
        env = dict(os.environ)
        env["KBC_DATADIR"] = str(FIXTURES / "test_invalid_config")
        result = subprocess.run(
            [sys.executable, str(SRC_DIR / "component.py")],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 1)


class TestConfigErrorSecretLeakGuard(unittest.TestCase):
    """Security regression: a config-validation failure must NOT leak the decrypted secret.

    The fixture carries a recognizable ``#client_secret`` sentinel and is missing a required
    field, so config parsing raises. The component is run as a subprocess (the real platform
    execution path, where ``logging.exception`` prints the active exception to stderr / the
    job log). We assert the sentinel appears NOWHERE in stdout/stderr — proving the Pydantic
    ValidationError's ``input_value`` (which holds the decrypted secret) is not chained into
    the traceback — while the offending field is still named so the error stays actionable.
    """

    SENTINEL = "SENTINEL_SECRET_DO_NOT_LEAK"

    def test_config_error_does_not_leak_secret(self):
        env = dict(os.environ)
        env["KBC_DATADIR"] = str(FIXTURES / "test_secret_leak_guard")
        result = subprocess.run(
            [sys.executable, str(SRC_DIR / "component.py")],
            capture_output=True,
            text=True,
            env=env,
        )
        combined = result.stdout + result.stderr
        # User error → exit 1 (message shown to the user).
        self.assertEqual(result.returncode, 1)
        # The decrypted secret sentinel must not appear anywhere in the output.
        self.assertNotIn(self.SENTINEL, combined)
        # Deterministic leak markers: a rendered Pydantic ValidationError ALWAYS prints
        # ``input_value=`` (the offending value — Pydantic truncates it, so the sentinel
        # check alone is not sufficient). Its absence proves the ValidationError was not
        # chained into the traceback / message.
        self.assertNotIn("input_value", combined)
        self.assertNotIn("ValidationError", combined)
        # The error must still name the offending field so it is user-actionable.
        self.assertIn("instance_host", combined)


class TestNoConfig(unittest.TestCase):
    @freeze_time("2010-10-10")
    @mock.patch.dict(os.environ, {"KBC_DATADIR": "./non-existing-dir"})
    def test_run_no_cfg_fails(self):
        with self.assertRaises(ValueError):
            Component().run()


if __name__ == "__main__":
    unittest.main()
