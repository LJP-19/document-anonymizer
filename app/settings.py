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


# -- typed preferences (Settings window) --------------------------------------
#
# Each has an environment variable that, when set, wins - CI and tests pin
# behaviour that way - and otherwise the saved choice applies.

def _env_flag(name: str):
    value = os.environ.get(name, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def fast_mode() -> bool:
    """Fast mode skips the optional extras - the AI review and the re-scan of the
    planned output, whose findings only ever join the review list. Every
    detection layer that decides what is redacted still runs. DOCANON_FAST."""
    env = _env_flag("DOCANON_FAST")
    return env if env is not None else get("fast_mode") is True


def careful_mode() -> bool:
    """DOCANON_CONSERVATIVE, else the saved choice (default off)."""
    env = _env_flag("DOCANON_CONSERVATIVE")
    return env if env is not None else get("careful_mode") is True


def output_folder(default: str) -> str:
    value = get("output_folder")
    return value if isinstance(value, str) and value.strip() else default
