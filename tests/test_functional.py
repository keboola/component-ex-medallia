"""VCR functional tests for the Medallia extractor (replay of recorded cassettes).

The cassettes committed under ``tests/functional/*/`` contain ONLY deterministic
synthetic data — every real customer value was overwritten during sanitization (see
``MedalliaResponseBodySanitizer`` in ``src/component.py`` and ``tests/README.md``). Each
case replays with no live network access:

* ``01_testConnection`` — token + compute-cost-only pre-flight (sync action).
* ``02_listFields`` — token + field-catalogue query (sync action).
* ``03_feedback_incremental`` — token + a self-terminating two-page keyset feedback
  extract (the final page is short, so pagination stops within the recorded pages).

When no cassettes are present the module skips cleanly.
"""

from pathlib import Path

import pytest
from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")

_ALL_CASES = get_test_cases(FUNCTIONAL_DIR) if Path(FUNCTIONAL_DIR).exists() else []

_NO_CASSETTES_REASON = "No VCR cassettes present — nothing to replay. See tests/README.md."


def _parametrized_cases() -> list:
    if not _ALL_CASES:
        return [pytest.param("_no_cassettes_", marks=pytest.mark.skip(reason=_NO_CASSETTES_REASON))]
    return list(_ALL_CASES)


@pytest.mark.parametrize("test_name", _parametrized_cases())
def test_functional(test_name):
    """Replay a single VCR functional test case."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
    )
    tester.run()
