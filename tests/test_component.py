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
_FEEDBACK_NODES = [
    {
        "surveyId": {"values": ["S100"]},
        "k_fin": {"values": ["1704153600"]},
        "e_nps": {"values": ["9"]},
        "e_comment": {"values": ["Great"]},
    },
    {
        "surveyId": {"values": ["S101"]},
        "k_fin": {"values": ["1704240000"]},
        "e_nps": {"values": ["7"]},
        "e_comment": {"values": ["Okay"]},
    },
    {
        "surveyId": {"values": ["S102"]},
        "k_fin": {"values": ["1704326400"]},
        "e_nps": {"values": ["3"]},
        "e_comment": {"values": ["Poor"]},
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
        self.assertEqual(set(rows[0].keys()), {"surveyId", "k_fin", "e_nps", "e_comment"})
        self.assertEqual([r["surveyId"] for r in rows], ["S100", "S101", "S102"])
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


class TestNoConfig(unittest.TestCase):
    @freeze_time("2010-10-10")
    @mock.patch.dict(os.environ, {"KBC_DATADIR": "./non-existing-dir"})
    def test_run_no_cfg_fails(self):
        with self.assertRaises(ValueError):
            Component().run()


if __name__ == "__main__":
    unittest.main()
