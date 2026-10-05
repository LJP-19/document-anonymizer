"""Which catalog entry the user already chose, if any.

A plain text file next to the downloaded models, not a database - the
only thing stored is a catalog id, and it only needs to answer one
question: has the mandatory first-launch picker already been completed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .catalog import LlmChoice, by_id
from .downloader import is_downloaded, model_dir


def _marker_path() -> Path:
    return model_dir() / "selected.txt"


def get_selected() -> Optional[LlmChoice]:
    """The previously chosen catalog entry, but ONLY if its file is
    actually still present and the right size - a marker recorded for a
    model that was since deleted or never finished downloading must not
    be trusted, or the app would silently skip the picker and then fail
    to load anything when the auditor actually runs."""
    marker = _marker_path()
    if not marker.exists():
        return None
    choice_id = marker.read_text(encoding="utf-8").strip()
    choice = by_id(choice_id)
    if choice is None or not is_downloaded(choice):
        return None
    return choice


def set_selected(choice: LlmChoice) -> None:
    _marker_path().write_text(choice.id, encoding="utf-8")
