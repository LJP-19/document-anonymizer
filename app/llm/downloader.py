"""Download a chosen model to the user's own config directory.

Not hf_hub_download(): verified its real signature directly and it has
no progress-callback parameter, only an internal tqdm console bar -
no hook a Qt progress bar can use. A direct, streaming HTTPS download
against Hugging Face's standard resolve URL gives full control over
progress reporting and resumability instead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from .catalog import LlmChoice

log = logging.getLogger(__name__)

CHUNK_SIZE = 1 << 20  # 1 MiB


class DownloadError(RuntimeError):
    pass


def model_dir() -> Path:
    """User-writable, outside the installed app entirely - the app
    bundle is read-only/signed on both platforms, and nothing is bundled
    here anymore regardless (see this package's own __init__.py)."""
    base = Path.home() / ".document-anonymizer" / "models" / "llm"
    base.mkdir(parents=True, exist_ok=True)
    return base


def model_path(choice: LlmChoice) -> Path:
    return model_dir() / choice.filename


def is_downloaded(choice: LlmChoice) -> bool:
    """A size match, not a hash check - no verified checksum is stored in
    the catalog for every entry, and size alone already catches the most
    likely real failure (an interrupted download), which is what this
    needs to detect before trusting a file enough to load it into
    llama.cpp."""
    path = model_path(choice)
    return path.exists() and path.stat().st_size == choice.size_bytes


def download(
    choice: LlmChoice,
    on_progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> Path:
    """Download choice's file, resuming a partial download if one exists.

    on_progress(bytes_so_far, total_bytes) is called after every chunk -
    the caller (the picker dialog) uses this to drive a real progress bar.
    cancel() is checked between chunks; returning True stops the download
    cleanly, leaving the partial file in place so a later attempt can
    resume rather than starting over.
    """
    import requests

    dest = model_path(choice)
    if is_downloaded(choice):
        return dest

    url = f"https://huggingface.co/{choice.repo}/resolve/main/{choice.filename}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    resume_from = tmp.stat().st_size if tmp.exists() else 0
    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}

    try:
        with requests.get(url, headers=headers, stream=True, timeout=30) as response:
            if resume_from and response.status_code == 416:
                # The server doesn't like our resume point (e.g. a stale
                # partial from a different file version) - start over.
                tmp.unlink(missing_ok=True)
                return download(choice, on_progress, cancel)
            if response.status_code not in (200, 206):
                raise DownloadError(
                    f"download failed: HTTP {response.status_code} for {choice.filename}"
                )
            mode = "ab" if resume_from and response.status_code == 206 else "wb"
            if mode == "wb":
                resume_from = 0
            written = resume_from
            with open(tmp, mode) as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if cancel is not None and cancel():
                        if on_progress is not None:
                            on_progress(written, choice.size_bytes)
                        return tmp
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
                    if on_progress is not None:
                        on_progress(written, choice.size_bytes)
    except requests.RequestException as exc:
        raise DownloadError(f"download failed for {choice.filename}: {exc}") from exc

    if tmp.stat().st_size != choice.size_bytes:
        raise DownloadError(
            f"{choice.filename} downloaded as {tmp.stat().st_size} bytes, "
            f"expected {choice.size_bytes} - incomplete or corrupted, not installed"
        )
    tmp.replace(dest)
    return dest
