"""GUI entry point."""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path


def _log_path() -> Path:
    base = Path.home() / ".document-anonymizer"
    base.mkdir(parents=True, exist_ok=True)
    return base / "app.log"


def _install_crash_handler() -> None:
    """Report unhandled errors instead of vanishing.

    The app was observed exiting silently during processing. An exception that
    escapes a Qt slot terminates the process with nothing on screen and nothing
    written down, which is unusable to diagnose.
    """
    logging.basicConfig(
        filename=str(_log_path()),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    def handle(exc_type, exc_value, exc_tb):
        logging.critical("unhandled error", exc_info=(exc_type, exc_value, exc_tb))
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.stderr.write(text)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                box = QMessageBox()
                box.setWindowTitle("Something went wrong")
                box.setText(
                    "The last action failed, but your documents are untouched.\n\n"
                    f"Details were written to {_log_path()}"
                )
                box.setDetailedText(text)
                box.exec()
        except Exception:  # noqa: BLE001 - never let the handler itself crash
            pass

    sys.excepthook = handle
    try:
        import threading

        threading.excepthook = lambda args: handle(
            args.exc_type, args.exc_value, args.exc_traceback
        )
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _install_crash_handler()
    from .ui.main_window import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
