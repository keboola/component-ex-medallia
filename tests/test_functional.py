"""VCR functional replay for the generic Medallia extractor (network-free).

Auto-discovers and replays every case under ``tests/functional/*``. Two kinds of case live
here, both replayed with NO live network:

* **HTTP-free failure cases** (``03``–``08``) — config-validation / raw-contract failures that
  return exit 1 before any HTTP call (empty cassette). Scaffolded from
  ``tests/setup/configs.failure.json``; no secrets, no network.
* **Live-recorded data / sync cases** — recorded once against the live instance and sanitized
  AT RECORD TIME by ``MedalliaResponseBodySanitizer`` (``src/component.py``) so every committed
  value is synthetic by construction. Recorded with ``tests/setup/record_matrix.py``.

Deterministic logic (per node shape / PK / mode, incremental resume, raw mode, failures) is
covered independently and network-free in ``tests/test_datadir.py``; this module is the
replay-regression net over the real recorded wire shapes. When no cassettes are present it
skips cleanly so the suite stays collectable.
"""

from pathlib import Path

import pytest
from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")

_ALL_CASES = get_test_cases(FUNCTIONAL_DIR) if Path(FUNCTIONAL_DIR).exists() else []

_NO_CASSETTES_REASON = "No VCR functional cases present — nothing to replay. See tests/README.md."


def _parametrized_cases() -> list:
    if not _ALL_CASES:
        return [pytest.param("_no_cases_", marks=pytest.mark.skip(reason=_NO_CASSETTES_REASON))]
    return list(_ALL_CASES)


@pytest.mark.parametrize("test_name", _parametrized_cases())
def test_functional(test_name):
    """Replay a single VCR functional case (data replay or HTTP-free failure)."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
    )
    tester.run()
