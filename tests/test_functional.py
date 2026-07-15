"""VCR functional tests for the Medallia extractor (replay of recorded cassettes).

Cassettes are NOT committed (they contain live customer data — see tests/README.md).
Until they are recorded and reviewed, ``get_test_cases`` finds no case directories and
this module skips cleanly. Once cassettes exist under ``tests/functional/*/``, each case
is replayed automatically with no live network access.
"""

from pathlib import Path

import pytest
from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")

_TEST_CASES = get_test_cases(FUNCTIONAL_DIR) if Path(FUNCTIONAL_DIR).exists() else []


@pytest.mark.skipif(
    not _TEST_CASES,
    reason="No VCR cassettes present — live recording is deferred pending Medallia hosts + manual review (tests/README.md).",
)
@pytest.mark.parametrize("test_name", _TEST_CASES or ["_no_cassettes_"])
def test_functional(test_name):
    """Run a single VCR functional test case."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
    )
    tester.run()
