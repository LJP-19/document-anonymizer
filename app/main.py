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


def _attach_console_for_self_test() -> None:
    """Make print() actually visible when this is a --windowed PyInstaller
    build run from an existing terminal.

    A GUI-subsystem Windows executable (which --windowed produces) has NO
    console attached at all - print() output goes nowhere, even when
    launched from cmd.exe/PowerShell, and the terminal returns to the
    prompt immediately with no visible error either way. This is exactly
    why a real user running `DocumentAnonymizer.exe --self-test` got
    nothing back: the self-test logic ran correctly, but its output had
    no console to write to. My own testing never caught this because it
    always ran `python -m app.main --self-test` directly - ordinary
    console Python, nothing like what --windowed actually does.

    AttachConsole(-1) (ATTACH_PARENT_PROCESS) is the standard, documented
    fix: it connects this process to whichever console launched it, if
    any. It is a deliberate no-op when there is no parent console (e.g.
    double-clicked from Explorer with no --self-test flag at all) - normal
    windowed operation is completely unaffected.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w")  # noqa: SIM115 - lives for the process
            sys.stderr = open("CONOUT$", "w")  # noqa: SIM115
    except Exception:  # noqa: BLE001 - console output is a nicety, never fatal
        pass


def _self_test() -> int:
    """Headless diagnostic mode for the FROZEN executable specifically.

    Every existing CI smoke test runs `python -m app.cli` - the CI runner's
    own, normal, unfrozen Python interpreter, with each package's compiled
    binaries properly isolated in their own site-packages directory. That
    can never catch a PyInstaller-freezing-specific bug: PyInstaller merges
    every package's binary files into one flat directory when it builds the
    .exe/.app, and if two packages (torch and numpy, most plausibly) each
    vendor their own copy of an overlapping shared library, the FROZEN
    bundle can collide in a way a normal venv never would - exactly the
    `SystemError: <class 'ImportError'> returned a result with an exception
    set` reported from a real packaged build, which every unfrozen smoke
    test in CI passed cleanly right up until the moment the actual .exe
    tried the same thing.

    This runs the exact operation that crashed - loading en_core_web_trf
    and analysing a real page - inside the FROZEN process itself, with no
    GUI. Exit 0 means the frozen bundle's import chain actually works, not
    just that the CI runner's own Python could do it. Invoke directly:

        DocumentAnonymizer.exe --self-test        (Windows)
        DocumentAnonymizer.app/Contents/MacOS/DocumentAnonymizer --self-test  (macOS)

    The result is ALWAYS also written to a plain file next to app.log,
    regardless of whether console output worked - console attachment on
    Windows is a best-effort nicety, not something to depend on.
    """
    _attach_console_for_self_test()
    result_path = _log_path().parent / "self-test-result.txt"
    lines: list[str] = []

    def _out(line: str) -> None:
        lines.append(line)
        try:
            print(line)
        except Exception:  # noqa: BLE001 - a closed/unavailable stdout must not crash this
            pass

    _out("Self-test: loading the detection pipeline inside this frozen build...")
    try:
        import spacy

        spacy.require_cpu()
        nlp = spacy.load("en_core_web_trf", exclude=["lemmatizer", "tagger", "attribute_ruler"])
        doc = nlp("John Smith submitted the report to ABC Company.")
        found = {ent.label_ for ent in doc.ents}
        if "PERSON" not in found:
            _out(f"FAIL: model loaded but found no PERSON entity (got: {found})")
            result_path.write_text("\n".join(lines), encoding="utf-8")
            return 1
        _out("  spaCy + en_core_web_trf: OK")
    except Exception as exc:  # noqa: BLE001 - report it, do not hide it
        _out(f"FAIL: {type(exc).__name__}: {exc}")
        _out(traceback.format_exc())
        result_path.write_text("\n".join(lines), encoding="utf-8")
        return 1

    try:
        from .detection.gliner import GlinerDetector

        GlinerDetector().load()
        _out("  GLiNER: OK")
    except Exception as exc:  # noqa: BLE001
        _out(f"FAIL: {type(exc).__name__}: {exc}")
        _out(traceback.format_exc())
        result_path.write_text("\n".join(lines), encoding="utf-8")
        return 1

    _out("Self-test PASSED - the frozen build's detection pipeline works.")
    result_path.write_text("\n".join(lines), encoding="utf-8")
    _out(f"(This result was also written to: {result_path})")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    _install_crash_handler()
    from .ui.main_window import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
