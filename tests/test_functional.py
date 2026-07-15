"""VCR functional tests for the Medallia extractor (replay of recorded cassettes).

Cassettes are NOT committed (they contain live customer data — see tests/README.md).
When no cassettes exist (the committed / CI state) this module skips cleanly. When
cassettes exist under ``tests/functional/*/`` each REPLAYABLE case is replayed with no
live network access.

Feedback extract recordings are deliberately excluded from replay (see
``_is_replayable``): this tenant's feedback volume combined with Medallia's day-
granularity date filter means a gentle, bounded (<=2 page) cassette can never contain the
full keyset pagination the un-capped component walks, so replay would always request an
unrecorded page. Feedback extraction is instead verified deterministically by the
monkeypatched datadir test in ``tests/test_component.py``. The feedback cassette is kept
on disk (gitignored) only as a one-time proof-of-life artifact for human review.
"""

from pathlib import Path

import pytest
from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")

_ALL_CASES = get_test_cases(FUNCTIONAL_DIR) if Path(FUNCTIONAL_DIR).exists() else []

_NON_REPLAYABLE_REASON = (
    "Non-replayable feedback recording: a bounded (<=2 page) live sample cannot contain "
    "the full keyset pagination the un-capped component walks for this tenant's volume / "
    "day-granularity date filter. Feedback is covered by the datadir test; see tests/README.md."
)

_NO_CASSETTES_REASON = (
    "No VCR cassettes present (the committed / CI state) — recorded cassettes are customer "
    "data and are never committed. See tests/README.md."
)


def _is_replayable(case_name: str) -> bool:
    """Feedback extract recordings are proof-of-life samples, not replayable fixtures."""
    return "feedback" not in case_name


def _parametrized_cases() -> list:
    if not _ALL_CASES:
        return [pytest.param("_no_cassettes_", marks=pytest.mark.skip(reason=_NO_CASSETTES_REASON))]
    params = []
    for name in _ALL_CASES:
        if _is_replayable(name):
            params.append(name)
        else:
            params.append(pytest.param(name, marks=pytest.mark.skip(reason=_NON_REPLAYABLE_REASON)))
    return params


@pytest.mark.parametrize("test_name", _parametrized_cases())
def test_functional(test_name):
    """Replay a single VCR functional test case."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
    )
    tester.run()
