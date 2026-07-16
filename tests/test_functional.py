"""VCR functional tests for the Medallia extractor (replay of recorded cassettes).

The cassettes committed under ``tests/functional/*/`` contain ONLY deterministic
synthetic data — every real customer value was overwritten during sanitization (see
``MedalliaResponseBodySanitizer`` in ``src/component.py`` and ``tests/README.md``). Each
case replays with no live network access. The matrix spans sync actions (01-02),
HTTP-free config-validation failures (03-07), the live-recorded data behaviours
(08-14: epoch/datetime watermarks, incremental resume, full load, empty result,
multi-value field, business filters, multi-page pagination) and the auth/4xx failure
cases (15-16). See ``tests/README.md`` for the full case list.

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


def test_incremental_resume_advances_watermark_from_seed():
    """The seeded-resume case must resume from its stored watermark and advance past it.

    The generic replay above enforces this via log comparison (the recorded logs.json is
    reproduced exactly on replay). This makes the intent explicit and unmissable: the
    committed cassette's logs must show the resume from the seed AND a persisted watermark
    that sorts strictly after it.
    """
    import json

    logs_path = Path(FUNCTIONAL_DIR) / "09_incremental_resume" / "source" / "data" / "cassettes" / "logs.json"
    entries = json.loads(logs_path.read_text()).get("logs", [])
    messages = [e.get("message", "") if isinstance(e, dict) else str(e) for e in entries]
    blob = "\n".join(messages)

    seed = "2026-06-25"
    assert f"Resuming from stored watermark (finish={seed})." in blob, blob
    persisted = [m for m in messages if m.startswith("Persisted watermark (finish=")]
    assert persisted, "no persisted-watermark log line found"
    advanced = persisted[-1].split("finish=", 1)[1].rstrip(").")
    assert advanced > seed, f"watermark {advanced!r} did not advance past seed {seed!r}"
