"""Offline document anonymizer.

Every model is loaded from a bundled file. These environment variables are set
before any library that could look for a remote update is imported, so the
code path that would reach the network cannot execute (spec section 4).
"""

import os

for _variable in (
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "HF_DATASETS_OFFLINE",
    "HF_HUB_DISABLE_TELEMETRY",
    "DISABLE_TELEMETRY",
    "DO_NOT_TRACK",
):
    os.environ.setdefault(_variable, "1")
