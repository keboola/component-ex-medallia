"""Pytest configuration for the Medallia test suite.

The VCR data-matrix cassettes were recorded against the live instance with a hard page cap
(``MEDALLIA_MAX_PAGES=3``) to bound the blast radius. Some cassettes therefore stop at 3
recorded pages on a dataset that has more. Replay must apply the SAME cap so the component
stops at the end of the recorded pages instead of requesting an un-recorded 4th page (which
vcrpy would reject). Cases whose data is exhausted in fewer pages terminate naturally and
are unaffected by the cap; non-paginating cases (sync actions, failure cases) ignore it.
"""

import os

os.environ.setdefault("MEDALLIA_MAX_PAGES", "3")
