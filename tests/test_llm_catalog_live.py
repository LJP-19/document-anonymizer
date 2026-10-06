"""Check every catalog entry against the live Hugging Face API.

This is the control for a real, shipped bug: the catalog's sizes were typed
from rounded web-page figures, one filename did not exist (the 7B model is
two shards), and the first real download was rejected as "corrupted" even
though it was complete. Nothing offline can prove the numbers match the
repository, so this does - at the commit each entry is pinned to.

Skips (never fails) when the API cannot be reached, so an offline machine or
a Hugging Face outage cannot break a build; a MISMATCH always fails.
"""

from __future__ import annotations

import pytest

requests = pytest.importorskip("requests")

from app.llm.catalog import CATALOG  # noqa: E402


def _listing(choice):
    url = f"https://huggingface.co/api/models/{choice.repo}/tree/{choice.revision}"
    try:
        response = requests.get(url, timeout=20)
    except requests.RequestException as exc:
        pytest.skip(f"Hugging Face API not reachable: {exc}")
    if response.status_code != 200:
        pytest.skip(f"Hugging Face API answered HTTP {response.status_code}")
    return {entry["path"]: entry.get("size") for entry in response.json()}


@pytest.mark.parametrize("choice", CATALOG, ids=lambda c: c.id)
def test_catalog_entry_matches_the_real_repository(choice):
    listing = _listing(choice)
    for model_file in choice.files:
        assert model_file.filename in listing, (
            f"{choice.id}: {model_file.filename} does not exist in {choice.repo} "
            f"at revision {choice.revision}. Files there: {sorted(listing)}"
        )
        assert listing[model_file.filename] == model_file.size_bytes, (
            f"{choice.id}: {model_file.filename} is {listing[model_file.filename]} bytes "
            f"in the repository, catalog says {model_file.size_bytes}"
        )
