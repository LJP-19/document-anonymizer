"""Test configuration.

The LLM audit pass is disabled for the suite: it costs 30-60 seconds per
uncertain page, which would make the regression tests unusable. It has its own
targeted tests that opt back in.
"""

import os

os.environ.setdefault("DOCANON_LLM", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_sessionfinish(session, exitstatus):
    """Shut Qt down as cleanly as we can.

    PySide6 under the offscreen platform crashes intermittently during
    interpreter shutdown - roughly one run in three - with SIGABRT or SIGBUS,
    AFTER every test has passed and been reported. The crash is in Qt's own
    teardown of an already-finished process, not in the application: the
    thread-safety bug that mattered (MuPDF called from several threads) is
    fixed separately and covered by its own test.

    Exiting immediately with the real status keeps CI honest - a failing test
    still returns non-zero - while not letting a shutdown-only crash mask a
    green run. If a genuine crash ever happens DURING a test, pytest never
    reaches this hook and the process still dies with a signal.
    """
    try:
        from PySide6.QtWidgets import QApplication

        from app.ui.main_window import stop_all_animations
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
    stop_all_animations()
    stop_all_runners()
    app.processEvents()
    # Collect the widgets before Qt tears the application down, so nothing
    # owning a thread is destroyed during interpreter shutdown.
    gc.collect()
    app.processEvents()
    stop_all_runners()
