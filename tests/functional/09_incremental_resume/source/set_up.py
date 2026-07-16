"""Seed the incremental state before replay.

The datadirtest framework overwrites in/state.json in setUp (to the empty last-state), which
would erase a committed seed. This set_up hook runs AFTER that reset and re-writes the seed,
so the component resumes from a stored watermark and its advance can be asserted. The same
seed was used when the cassette was recorded, so the replayed keyset query matches.
"""

import json
import os

SEED = {"last_finish_date_epoch": "2026-06-25", "last_survey_id": "-1"}


def run(context):
    state_path = os.path.join(context.source_data_dir, "in", "state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(SEED, fh)
