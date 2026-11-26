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

    # The AI engine (llama.cpp) ships inside the app: the AI review downloads only
    # a model file, never an engine. Importing llama_cpp loads its native library
    # (libllama), so a missing or broken engine fails HERE, in the shipped build -
    # before it reaches anyone - rather than silently turning the AI review off.
    try:
        import llama_cpp

        llama_cpp.llama_backend_init()
        _out(f"  AI engine (llama.cpp {getattr(llama_cpp, '__version__', '?')}): OK")
    except Exception as exc:  # noqa: BLE001
        _out(f"FAIL: AI engine: {type(exc).__name__}: {exc}")
        _out(traceback.format_exc())
        result_path.write_text("\n".join(lines), encoding="utf-8")
        return 1

    # End to end, inside the shipped app: a fictional two-page return whose values
    # repeat on a page with no captions. Every occurrence must be replaced, with one
    # replacement per value, and the saved file must pass the export check. CI runs
    # this in the app copied out of the DMG, so the Mac build is proven on a whole
    # document, not just on loading its models (0.44.2).
    try:
        problems = _self_test_document()
        if problems:
            for problem in problems:
                _out(f"FAIL: end-to-end document: {problem}")
            result_path.write_text("\n".join(lines), encoding="utf-8")
            return 1
        _out("  End-to-end document (apply to all, one replacement per value, export check): OK")
    except Exception as exc:  # noqa: BLE001
        _out(f"FAIL: end-to-end document: {type(exc).__name__}: {exc}")
        _out(traceback.format_exc())
        result_path.write_text("\n".join(lines), encoding="utf-8")
        return 1

    _out("Self-test PASSED - the frozen build's detection pipeline works.")
    result_path.write_text("\n".join(lines), encoding="utf-8")
    _out(f"(This result was also written to: {result_path})")
    return 0


def _self_test_document() -> list[str]:
    """Anonymize a fictional return and report what is wrong (empty = all good)."""
    import os
    import tempfile

    import pymupdf

    from .document.pdflock import PDF_LOCK
    from .session import AnonymizationSession

    os.environ["DOCANON_LLM"] = "0"          # no model ships; the AI review is optional
    name, ssn, city = "Torvin Quell", "512-48-7391", "Lakeside City"
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        src, out = os.path.join(tmp, "self_test.pdf"), os.path.join(tmp, "self_test_out.pdf")
        with PDF_LOCK:
            pdf = pymupdf.open()
            p1 = pdf.new_page(width=612, height=792)
            for x, y, text, font in [
                (72, 80, "Your first name and middle initial", "hebo"), (72, 92, "Torvin", "cour"),
                (260, 80, "Last name", "hebo"), (260, 92, "Quell", "cour"),
                (420, 80, "Your social security number", "hebo"), (420, 92, ssn, "cour"),
                (72, 130, "City, town, or post office", "hebo"), (72, 142, city, "cour"),
            ]:
                p1.insert_text((x, y), text, fontname=font, fontsize=7 if font == "hebo" else 10)
            p2 = pdf.new_page(width=612, height=792)
            p2.insert_text((72, 60), f"{name}  {ssn}", fontname="cour", fontsize=10)
            p2.insert_text((72, 100), f"Statement 1 - {city}", fontname="cour", fontsize=10)
            p2.insert_text((72, 140), "Total wages   85,000", fontname="cour", fontsize=10)
            pdf.save(src)
            pdf.close()
        session = AnonymizationSession(source_path=src)
        session.analyse(use_llm=False)
        session.decisions.approve_auto_eligible(session.candidates)
        plan = session.plan()
        _apply, report = session.process(out)
        if not report.passed:
            problems.append("export check failed: " + ", ".join(c.name for c in report.checks if not c.passed))
        with PDF_LOCK:
            with pymupdf.open(out) as d:
                pages = [d[0].get_text(), d[1].get_text()]
        for value in ("Torvin", "Quell", ssn, city):
            for n, text in enumerate(pages, 1):
                if value in text:
                    problems.append(f"a value is still readable on page {n}")
        if "85,000" not in pages[1]:
            problems.append("an amount was changed")
        replacements = {t.replacement for t in plan.targets if t.original == ssn}
        if len(replacements) != 1:
            problems.append(f"the SSN got {len(replacements)} different replacements")
        if session.mapping_file and os.path.exists(session.mapping_file):
            os.remove(session.mapping_file)   # fictional; leave nothing behind
    return problems


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    _install_crash_handler()
    from .ui.main_window import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
