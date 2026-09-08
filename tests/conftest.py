"""Test configuration.

The LLM audit pass is disabled for the suite: it costs 30-60 seconds per
uncertain page, which would make the regression tests unusable. It has its own
targeted tests that opt back in.
"""

import os

os.environ.setdefault("DOCANON_LLM", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_sessionfinish(session, exitstatus):
    """Shut Qt down cleanly.

    A QThread still running when the interpreter exits makes Qt abort the
    process, which surfaces as exit code 134 in CI even when every test passed.
    """
    try:
        from PySide6.QtWidgets import QApplication

        from app.ui.widgets import stop_all_runners
    except ImportError:
        return

    import gc

    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    stop_all_runners()
    app.processEvents()
    # Collect the widgets before Qt tears the application down, so nothing
    # owning a thread is destroyed during interpreter shutdown.
    gc.collect()
    app.processEvents()
    stop_all_runners()
