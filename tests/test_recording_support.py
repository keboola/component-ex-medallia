"""Recording-support unit tests — superseded by the generic redesign (regenerated in Phase 5).

These covered the v1 ``MedalliaClient`` keyset ``max_pages`` cap and the ``fieldData``-only
``MedalliaResponseBodySanitizer`` arity/shape preservation. The client is now a Relay cursor
paginator and the sanitizer is EXTENDED for the new node shapes in plan task 5.3, which
re-authors these tests.

Skipped at module level so the suite stays collectable until Phase 5 lands.
"""

import pytest

pytest.skip(
    "redesign-pending: v1 recording-support tests removed; regenerated in Phase 5 (task 5.3).",
    allow_module_level=True,
)
