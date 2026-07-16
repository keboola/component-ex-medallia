"""Pytest configuration for the Medallia test suite.

The VCR data-matrix cassettes were recorded against the live instance with a hard page cap
(``MEDALLIA_MAX_PAGES=2``) to bound the blast radius. Some cassettes therefore stop at 2
recorded pages on a dataset that has more. Replay must apply the SAME cap so the component
stops at the end of the recorded pages instead of requesting an un-recorded 3rd page (which
vcrpy would reject). Cases whose data is exhausted in fewer pages terminate naturally and
are unaffected by the cap; non-paginating cases (sync actions, failure cases) ignore it.
This MUST stay in sync with ``REPLAY_MAX_PAGES`` in ``tests/setup/record_matrix.py``.
"""

import os

os.environ.setdefault("MEDALLIA_MAX_PAGES", "2")
