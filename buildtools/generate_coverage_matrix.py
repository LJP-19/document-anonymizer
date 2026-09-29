"""Generate the PII type coverage matrix (spec section 8).

Run this whenever the taxonomy, rules, or detector mappings change:
    python buildtools/generate_coverage_matrix.py

It answers one question with evidence, not memory: for every PiiType, which
detector paths actually reach it. A type is a genuine gap if this list is
empty for it - the same check that found ACCOUNT_ID, FAX, MARITAL_STATUS,
MATTER_ID, MEDICARE_ID, PAYROLL_ID, SOCIAL_HANDLE, STATE_TAX_ID and
URL_PERSONAL had no detector at all before this generator existed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from app.detection.types import PiiType
from app.detection.gliner import LABELS as GLINER_LABELS
from app.detection.presidio_adapter import ENTITY_MAP as PRESIDIO_MAP

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "resources" / "rules" / "pii_rules.yaml"
OUT_PATH = ROOT / "resources" / "coverage_matrix.json"

#: Deliberately detector-less: the type-or-skip fallback for anything no
#: detector could type. Not a gap.
INTENTIONALLY_UNSUPPORTED = {"UNCLASSIFIED_GROUP_VALUE"}


def build() -> dict:
    rules = yaml.safe_load(RULES_PATH.read_text())
    regex_rules = {}
    for r in rules["rules"]:
        regex_rules.setdefault(r["type"], []).append(r["name"])

    label_types: dict[str, list[str]] = {}
    for lb in rules["labels"]:
        for expected in lb["expects"]:
            label_types.setdefault(expected.value if hasattr(expected, "value") else expected, []).append(
                lb["pattern"][:40]
            )

    gliner_types = set(GLINER_LABELS.values())
    presidio_types = set(PRESIDIO_MAP.values())

    matrix = {}
    gaps = []
    for pii_type in sorted(PiiType, key=lambda x: x.value):
        name = pii_type.value
        paths = {
            "regex_rules": regex_rules.get(name, []),
            "label_patterns": label_types.get(name, []),
            "gliner": pii_type in gliner_types,
            "presidio": pii_type in presidio_types,
        }
        has_any = bool(paths["regex_rules"] or paths["label_patterns"] or paths["gliner"] or paths["presidio"])
        matrix[name] = {**paths, "has_detector": has_any}
        if not has_any and name not in INTENTIONALLY_UNSUPPORTED:
            gaps.append(name)

    return {"types": matrix, "gaps": gaps, "total_types": len(matrix), "gap_count": len(gaps)}


def main() -> int:
    result = build()
    OUT_PATH.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"wrote {OUT_PATH} - {result['total_types']} types, {result['gap_count']} gap(s)")
    if result["gaps"]:
        print("  GAPS:", ", ".join(result["gaps"]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
