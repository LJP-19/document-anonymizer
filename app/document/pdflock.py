"""A single lock around every MuPDF call.

PyMuPDF wraps MuPDF, whose context is not safe to use from several threads at
once. The UI renders the original page, the redacted preview and the export on
different worker threads, which produced intermittent segmentation and bus
faults - roughly one run in three under test, and the same crash would reach
users on a large document.

Serialising the calls costs a little parallelism and removes the whole class of
failure. It is reentrant so a locked function may call another.
"""

from __future__ import annotations

import threading

PDF_LOCK = threading.RLock()
