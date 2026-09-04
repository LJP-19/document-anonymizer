"""Test configuration.

The LLM audit pass is disabled for the suite: it costs 30-60 seconds per
uncertain page, which would make the regression tests unusable. It has its own
targeted tests that opt back in.
"""

import os

os.environ.setdefault("DOCANON_LLM", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
