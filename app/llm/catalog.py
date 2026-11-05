"""The catalog of LLMs the first-launch picker offers.

Every repo, filename, byte size and revision below was read from the
Hugging Face API for the official Qwen GGUF repositories - not typed from
the rounded "1.89 GB" figures the web pages show.

That distinction is a real, shipped bug, not a hypothetical: the first
version of this catalog used rounded sizes (e.g. 1_890_000_000), and the
first real Windows download of the 1.5B model arrived complete at
1,894,532,128 bytes and was rejected as "incomplete or corrupted". The same
first version also listed the 7B model as one file that does not exist
(HTTP 404): the official repo ships it as two shards. Hence:

- sizes are exact (tests/test_llm_catalog_live.py re-checks them against the
  live API whenever the network is reachable, and the offline test rejects
  any "round" size);
- each entry is pinned to a commit (`revision`), so the bytes behind a
  filename cannot change under users the way `main` can;
- an entry is a tuple of files, because some models are split. llama.cpp
  loads a split model when pointed at its FIRST shard, so `filename` is the
  first file and `size_bytes` is the total.

Quantization: Q8_0 for 1.5B because a benchmark of structured-JSON output
measured that model+quantization at a 95.7% JSON parse rate; Q4_K_M for the
others, the standard balance point on the official repos' own tables.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelFile:
    filename: str
    #: Exact size in bytes - never rounded, never estimated.
    size_bytes: int


@dataclass(frozen=True)
class LlmChoice:
    id: str
    label: str
    repo: str
    #: A full commit hash, so the files cannot change after this was checked.
    revision: str
    files: tuple[ModelFile, ...]
    #: Minimum total system RAM, in GB, for this to be a reasonable choice:
    #: roughly 2x the model's own size, leaving room for context, the OS and
    #: the rest of this app (torch, spaCy and GLiNER are already running).
    min_ram_gb: int
    description: str

    @property
    def filename(self) -> str:
        """The file llama.cpp is pointed at: the only file for an ordinary
        model, the FIRST shard for a split one."""
        return self.files[0].filename

    @property
    def size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def size_gb(self) -> float:
        return self.size_bytes / 1e9


CATALOG: list[LlmChoice] = [
    LlmChoice(
        id="small",
        label="Small (0.5B)",
        repo="Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        revision="9217f5db79a29953eb74d5343926648285ec7e67",
        files=(ModelFile("qwen2.5-0.5b-instruct-q4_k_m.gguf", 491_400_032),),
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
        revision="91cad51170dc346986eccefdc2dd33a9da36ead9",
        files=(ModelFile("qwen2.5-1.5b-instruct-q8_0.gguf", 1_894_532_128),),
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
        revision="7dabda4d13d513e3e842b20f0d435c732f172cbe",
        files=(ModelFile("qwen2.5-3b-instruct-q4_k_m.gguf", 2_104_932_768),),
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
        revision="bb5d59e06d9551d752d08b292a50eb208b07ab1f",
        # Two shards: the official repo does not ship a single-file Q4_K_M.
        files=(
            ModelFile("qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf", 3_993_201_344),
            ModelFile("qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf", 689_872_288),
        ),
        min_ram_gb=32,
        description=(
            "The best available judgment quality in this catalog. A "
            "large download (two files) and the most RAM-hungry option - "
            "only recommended on well-resourced machines."
        ),
    ),
]


def by_id(choice_id: str) -> LlmChoice | None:
    return next((c for c in CATALOG if c.id == choice_id), None)
