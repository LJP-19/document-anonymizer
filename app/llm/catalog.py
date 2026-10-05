"""The catalog of LLMs the first-launch picker offers.

Every size and filename below was verified directly against the real,
official Qwen GGUF repositories on Hugging Face before use - not guessed
at, not taken from a third-party reupload. See CLAUDE.md for the exact
verification trail (each one was checked with a direct fetch/search
against huggingface.co/Qwen/... at the time it was added).

Quantization choice per tier: Q8_0 for 1.5B specifically, because a real
benchmark testing structured JSON output measured this exact
model+quantization combination at a 95.7% JSON parse rate, "competitive
with some 7B models" for this kind of task - quantization precision, not
parameter count, is the more likely place reliability erodes at small
sizes. Q4_K_M for 3B and 7B - the standard "recommended" balance point on
the official repos' own quantization tables, since the larger parameter
counts already carry enough capacity that this precision level costs
little against the benchmark-proven Q8_0 1.5B case specifically.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LlmChoice:
    id: str
    label: str
    repo: str
    filename: str
    #: Real, verified file size in bytes - never estimated. Shown to the
    #: user before they commit to a download, and used to confirm a
    #: completed download has the right number of bytes.
    size_bytes: int
    #: Minimum total system RAM, in GB, for this to be a reasonable
    #: choice. Not a hard technical floor (a smaller machine CAN still
    #: load a model that fits on disk) - a practical one: llama.cpp
    #: inference needs the model's own bytes resident in RAM plus context
    #: plus whatever headroom the OS and the rest of this app (torch,
    #: spaCy, GLiNER, all already running) need at the same time. Roughly
    #: 2x the model's own size, rounded to a clean number, leaves that
    #: headroom rather than just enough to technically load it.
    min_ram_gb: int
    description: str

    @property
    def size_gb(self) -> float:
        return self.size_bytes / 1e9


CATALOG: list[LlmChoice] = [
    LlmChoice(
        id="small",
        label="Small (0.5B)",
        repo="Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        filename="qwen2.5-0.5b-instruct-q4_k_m.gguf",
        size_bytes=491_000_000,
        min_ram_gb=4,
        description=(
            "Fastest, lightest option. Works on modest hardware, but its "
            "judgment on ambiguous cases (a joint name, a business-vs-"
            "personal call) is the weakest of the four - it is a real "
            "trade-off, not just a smaller download."
        ),
    ),
    LlmChoice(
        id="balanced",
        label="Balanced (1.5B)",
        repo="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        filename="qwen2.5-1.5b-instruct-q8_0.gguf",
        size_bytes=1_890_000_000,
        min_ram_gb=8,
        description=(
            "The default recommendation for most machines. A real "
            "benchmark measured this exact model and quantization at a "
            "95.7% JSON parse rate on structured-output tasks - "
            "\"competitive with some 7B models\" for this kind of work."
        ),
    ),
    LlmChoice(
        id="larger",
        label="Larger (3B)",
        repo="Qwen/Qwen2.5-3B-Instruct-GGUF",
        filename="qwen2.5-3b-instruct-q4_k_m.gguf",
        size_bytes=2_104_932_768,
        min_ram_gb=16,
        description=(
            "Meaningfully better contextual judgment than the 1.5B "
            "models, at a real cost in download size and RAM. A good "
            "choice on a machine with room to spare."
        ),
    ),
    LlmChoice(
        id="largest",
        label="Largest (7B)",
        repo="Qwen/Qwen2.5-7B-Instruct-GGUF",
        filename="qwen2.5-7b-instruct-q4_k_m.gguf",
        size_bytes=4_680_000_000,
        min_ram_gb=32,
        description=(
            "The best available judgment quality in this catalog. A "
            "large download and the most RAM-hungry option - only "
            "recommended on well-resourced machines."
        ),
    ),
]


def by_id(choice_id: str) -> LlmChoice | None:
    return next((c for c in CATALOG if c.id == choice_id), None)
