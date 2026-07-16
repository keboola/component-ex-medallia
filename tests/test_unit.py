"""Unit tests — superseded by the generic Query API redesign (regenerated in Phase 5).

The v1 suite here exercised the feedback-specific ``MedalliaQueryBuilder``, the composite
keyset ``Watermark`` / ``watermark_from_node``, the ``surveyId`` PK and the epoch/datetime
``finish_date`` config — all removed by the clean generic redesign (spec §11). The generic
replacements (``GenericQueryBuilder``, shape-detecting ``flatten_node``, ``row_hash``,
``advance_watermark``, object classification, the redesigned ``RowConfiguration``) get their
own unit tests in plan task 5.1.

Skipped at module level so the suite stays collectable and green until Phase 5 lands. See git
history (pre-redesign) for the v1 tests.
"""

import pytest

pytest.skip(
    "redesign-pending: v1 feedback unit tests removed; generic unit tests land in Phase 5 (task 5.1).",
    allow_module_level=True,
)
