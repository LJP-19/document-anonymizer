"""Download a chosen model to the user's own config directory.

Not hf_hub_download(): its real signature has no progress-callback
parameter, only an internal tqdm console bar - no hook a Qt progress bar can
use. A direct, streaming HTTPS download against Hugging Face's resolve URL
gives full control over progress and resumability instead.

URLs use the entry's pinned `revision`, never `main`, so the file behind a
name cannot change after its size was checked.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from .catalog import LlmChoice, ModelFile

log = logging.getLogger(__name__)

CHUNK_SIZE = 1 << 20  # 1 MiB


class DownloadError(RuntimeError):
    pass


def model_dir() -> Path:
    """User-writable, outside the installed app entirely - the app bundle is
    read-only/signed on both platforms, and nothing is bundled here anyway
    (see this package's own __init__.py)."""
    base = Path.home() / ".document-anonymizer" / "models" / "llm"
    base.mkdir(parents=True, exist_ok=True)
    return base


def file_path(model_file: ModelFile) -> Path:
    return model_dir() / model_file.filename


def model_path(choice: LlmChoice) -> Path:
    """The file llama.cpp is pointed at - for a split model the first shard,
    from which llama.cpp finds the rest in the same folder."""
    return model_dir() / choice.filename


def _file_complete(model_file: ModelFile) -> bool:
    path = file_path(model_file)
    return path.exists() and path.stat().st_size == model_file.size_bytes


def is_downloaded(choice: LlmChoice) -> bool:
    """Every file present at exactly its expected size. A size match, not a
    hash check, so it catches an interrupted download but not corruption
    inside a right-sized file - LlmAuditor.load() handles that case."""
    return all(_file_complete(f) for f in choice.files)


def _url(choice: LlmChoice, model_file: ModelFile) -> str:
    return f"https://huggingface.co/{choice.repo}/resolve/{choice.revision}/{model_file.filename}"


def download(
    choice: LlmChoice,
    on_progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> Path:
    """Download every file of `choice`, resuming partial downloads.

    on_progress(bytes_so_far, total_bytes) counts across ALL files, so a
    split model shows one continuous progress bar. cancel() is checked
    between chunks; returning True stops cleanly and leaves the partial file
    in place so a later attempt resumes instead of starting over.
    """
    total = choice.size_bytes
    done_before = 0
    for model_file in choice.files:
        if _file_complete(model_file):
            done_before += model_file.size_bytes
            if on_progress is not None:
                on_progress(done_before, total)
            continue
        if not _download_file(choice, model_file, done_before, total, on_progress, cancel):
            return file_path(model_file)  # cancelled; the partial is kept
        done_before += model_file.size_bytes
    return model_path(choice)


def _download_file(
    choice: LlmChoice,
    model_file: ModelFile,
    done_before: int,
    total: int,
    on_progress: Optional[Callable[[int, int], None]],
    cancel: Optional[Callable[[], bool]],
    _retried: bool = False,
) -> bool:
    """True when the file is complete, False when cancelled."""
    import requests

    dest = file_path(model_file)
    tmp = dest.with_suffix(dest.suffix + ".part")
    expected = model_file.size_bytes

    resume_from = tmp.stat().st_size if tmp.exists() else 0
    if resume_from > expected:
        tmp.unlink(missing_ok=True)  # cannot be the start of the right file
        resume_from = 0
    if resume_from == expected:
        # Every byte already arrived (an earlier attempt was rejected, or was
        # interrupted just before the final rename) - nothing to fetch.
        tmp.replace(dest)
        if on_progress is not None:
            on_progress(done_before + expected, total)
        return True

    headers = {"Range": f"bytes={resume_from}-"} if resume_from else {}
    try:
        with requests.get(_url(choice, model_file), headers=headers, stream=True, timeout=30) as response:
            if resume_from and response.status_code == 416:
                # The server rejects our resume point (e.g. a stale partial
                # from a different file version) - start the file over once.
                if _retried:
                    raise DownloadError(f"{model_file.filename}: the server rejected a fresh download")
                tmp.unlink(missing_ok=True)
                return _download_file(
                    choice, model_file, done_before, total, on_progress, cancel, _retried=True
                )
            if response.status_code not in (200, 206):
                raise DownloadError(
                    f"download failed: HTTP {response.status_code} for {model_file.filename}"
                )
            append = bool(resume_from) and response.status_code == 206
            written = resume_from if append else 0
            with open(tmp, "ab" if append else "wb") as out:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if cancel is not None and cancel():
                        if on_progress is not None:
                            on_progress(done_before + written, total)
                        return False
                    if not chunk:
                        continue
                    out.write(chunk)
                    written += len(chunk)
                    if on_progress is not None:
                        on_progress(done_before + written, total)
    except requests.RequestException as exc:
        raise DownloadError(f"download failed for {model_file.filename}: {exc}") from exc

    size = tmp.stat().st_size
    if size < expected:
        raise DownloadError(
            f"{model_file.filename} stopped at {size} of {expected} bytes. "
            "The partial download was kept - try again to resume it."
        )
    if size > expected:
        tmp.unlink(missing_ok=True)
        raise DownloadError(
            f"{model_file.filename} came to {size} bytes but {expected} were expected, "
            "so it was discarded - try again to download it fresh."
        )
    tmp.replace(dest)
    return True
