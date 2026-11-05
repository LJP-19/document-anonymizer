"""Small persistent user preferences, kept in ~/.document-anonymizer/settings.json.

One flat JSON object. Reading never raises (a missing or damaged file is the
same as no preferences); writing is atomic so a crash cannot leave half a file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _path() -> Path:
    return Path.home() / ".document-anonymizer" / "settings.json"


def load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def put(key: str, value: Any) -> None:
    data = load()
    data[key] = value
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
