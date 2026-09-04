"""Command line entry point.

Exists so the engine can be exercised, tested and CI-smoke-tested without a
display. The GUI drives the same AnonymizationSession.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .decisions.manager import DecisionState
from .session import AnonymizationSession
from .verification.verifier import format_report
from .version import __version__


def _print_findings(session: AnonymizationSession) -> None:
    detection = session.detection
    groups = session.decisions.occurrence_groups(detection.candidates)
    print(f"\nDocument: {session.source_path}")
    print(f"Pages: {session.document.page_count}   Status: {session.status}")
    print(f"Detections: {len(detection.candidates)} in {len(groups)} distinct value(s)\n")

    print("NEEDS REVIEW")
    any_review = False
    for g in groups:
        if g.needs_review or any(
            session.decisions.state(c) is DecisionState.UNREVIEWED for c in g.candidates
        ):
            any_review = True
            reason = next((c.review_reason for c in g.candidates if c.review_reason), "")
            print(f"  {g.pii_type.value:26} {g.display[:40]!r}  x{g.count}")
            if reason:
                print(f"      -> {reason}")
    if not any_review:
        print("  (none)")

    print("\nREVIEWED / AUTO-ACCEPTED")
    shown = False
    for g in groups:
        if not g.needs_review and all(
            session.decisions.state(c) is not DecisionState.UNREVIEWED for c in g.candidates
        ):
            shown = True
            print(f"  {g.pii_type.value:26} {g.display[:40]!r}  x{g.count}")
    if not shown:
        print("  (none)")

    print("\nLOGICAL FIELD GROUPS")
    for grp in session.detection.groups:
        flag = "COMPLETE" if grp.complete else "PARTIAL"
        print(f"  [{flag}] p{grp.page_no + 1} {grp.describe()}")

    if detection.warnings:
        print("\nWARNINGS")
        for w in detection.warnings:
            print(f"  ! {w}")


def cmd_analyze(args) -> int:
    session = AnonymizationSession(source_path=args.input)
    session.analyse(use_ner=not args.no_ner)
    _print_findings(session)
    return 0


def cmd_process(args) -> int:
    session = AnonymizationSession(source_path=args.input)
    session.analyse(use_ner=not args.no_ner)
    _print_findings(session)

    if args.accept_all:
        session.decisions.set_state(session.candidates, DecisionState.ACCEPTED)
    elif session.needs_review():
        print(
            f"\n{len(session.needs_review())} item(s) still need review. "
            "Re-run with --accept-all or use the GUI to decide."
        )
        return 2

    out = args.output or str(Path(args.input).with_suffix("")) + ".anonymized.pdf"
    apply_report, report = session.process(out)
    print(
        f"\nProcessed: {apply_report.redacted} redaction(s), "
        f"{apply_report.inserted} replacement(s), "
        f"{len(apply_report.shrunk)} shrunk, {len(apply_report.overflowed)} overflowed"
    )
    print("\n" + format_report(report))
    print(f"\nStatus: {session.status}")
    return 0 if report.passed else 1


def cmd_verify(args) -> int:
    from .transform.plan import TransformationPlan

    plan = TransformationPlan(source_path=args.input)
    report = __import__("app.verification.verifier", fromlist=["verify"]).verify(plan, args.output)
    print(format_report(report))
    return 0 if report.passed else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="docanon", description="Offline document anonymizer")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_an = sub.add_parser("analyze", help="detect PII and print findings")
    p_an.add_argument("input")
    p_an.add_argument("--no-ner", action="store_true")
    p_an.set_defaults(func=cmd_analyze)

    p_pr = sub.add_parser("process", help="redact, export and verify")
    p_pr.add_argument("input")
    p_pr.add_argument("-o", "--output")
    p_pr.add_argument("--accept-all", action="store_true", help="accept every detection unreviewed")
    p_pr.add_argument("--no-ner", action="store_true")
    p_pr.set_defaults(func=cmd_process)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
