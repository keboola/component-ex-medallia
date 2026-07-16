"""Security regression: a config-validation failure must NOT leak the decrypted secret.

Migrated from the retired ``tests/test_component.py`` monkeypatch harness. This is the one
assertion that could not be expressed as a VCR functional case: the datadirtest replay
framework compares captured output against a recorded snapshot, it cannot assert the
*absence* of a string. So the guard lives here as an explicit ``assertNotIn`` against the
real platform execution path (``component.py`` run as a subprocess, where
``logging.exception`` prints the active exception to stderr / the job log).

The companion functional case ``tests/functional/06_config_error_secret_leak_guard`` covers
the same scenario's *exit code* in the functional matrix; this test owns the security
assertion itself.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

# A recognizable decrypted-secret sentinel. If config validation ever chains the Pydantic
# ValidationError (whose ``input_value`` holds the merged config, including this value) into
# the traceback, the sentinel would surface in the job log.
SENTINEL = "SENTINEL_SECRET_DO_NOT_LEAK"


class TestConfigErrorSecretLeakGuard(unittest.TestCase):
    def _run_component(self, config: dict) -> subprocess.CompletedProcess:
        """Run ``component.py`` as a subprocess against a self-contained KBC_DATADIR."""
        with tempfile.TemporaryDirectory(prefix="medallia-leak-guard-") as tmp:
            data_dir = Path(tmp)
            (data_dir / "out" / "tables").mkdir(parents=True, exist_ok=True)
            with open(data_dir / "config.json", "w") as fh:
                json.dump(config, fh)
            env = dict(os.environ)
            env["KBC_DATADIR"] = str(data_dir)
            return subprocess.run(
                [sys.executable, str(SRC_DIR / "component.py")],
                capture_output=True,
                text=True,
                env=env,
            )

    def test_config_error_does_not_leak_secret(self):
        # A recognizable secret + a MISSING required field (instance_host) so config parsing
        # raises inside Configuration.__init__.
        config = {
            "parameters": {
                "#client_secret": SENTINEL,
                "company_name": "acme",
                "api_host": "acme.apis.example.test",
                "client_id": "dummy-client-id-not-real",
                "data_object": "feedback",
                "fields": ["e_nps"],
                "survey_id_field_id": "a_sid",
                "load_type": "incremental_load",
            }
        }
        result = self._run_component(config)
        combined = result.stdout + result.stderr

        # User error → exit 1 (a clean, user-actionable failure — never an exit-2 crash).
        self.assertEqual(result.returncode, 1, msg=combined)
        # The decrypted secret sentinel must not appear anywhere in the output.
        self.assertNotIn(SENTINEL, combined)
        # Deterministic leak markers: a rendered Pydantic ValidationError ALWAYS prints
        # ``input_value=`` (Pydantic truncates the offending value, so the sentinel check
        # alone is not sufficient). Its absence — and the absence of the ``ValidationError``
        # class name — proves the error was raised ``from None`` and never chained into the
        # traceback / message.
        self.assertNotIn("input_value", combined)
        self.assertNotIn("ValidationError", combined)
        # The error must still name the offending field so it stays user-actionable.
        self.assertIn("instance_host", combined)


if __name__ == "__main__":
    unittest.main()
