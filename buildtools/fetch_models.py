"""Download the bundled local models (spec sections 67 and 79).

Run at build time and during development setup. The application itself never
downloads anything - the packaged app must work with the network switched off.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "resources" / "models"

GLINER_REPO = "knowledgator/gliner-pii-edge-v1.0"  # Apache-2.0
GLINER_DIR = MODELS / "gliner-pii"
GLINER_PATTERNS = [
    "gliner_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "onnx/model_quint8.onnx",
]


def fetch_gliner(force: bool = False) -> Path:
    target = GLINER_DIR / "onnx" / "model_quint8.onnx"
    if target.exists() and not force:
        print(f"gliner: already present ({target.stat().st_size / 1e6:.1f} MB)")
        return GLINER_DIR
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise SystemExit(
            "huggingface_hub is required to fetch models:\n"
            f"  {sys.executable} -m pip install huggingface_hub"
        )
    print(f"gliner: downloading {GLINER_REPO} ...")
    source = snapshot_download(GLINER_REPO, allow_patterns=GLINER_PATTERNS)
    GLINER_DIR.mkdir(parents=True, exist_ok=True)
    # Copy only the listed files. The shared HF cache may already hold the fp16
    # and fp32 graphs from another run; copying the whole snapshot would put
    # ~320 MB into the installer instead of ~50 MB.
    for relative in GLINER_PATTERNS:
        item = Path(source) / relative
        if not item.exists():
            continue
        destination = GLINER_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
    size = sum(f.stat().st_size for f in GLINER_DIR.rglob("*") if f.is_file())
    print(f"gliner: ready at {GLINER_DIR} ({size / 1e6:.1f} MB)")
    return GLINER_DIR


LLM_REPO = "Qwen/Qwen2.5-1.5B-Instruct-GGUF"  # Apache-2.0, official Qwen repo.
# The size ceiling was revised to "below 4 GB" and the request became: use
# Q8_0, not Q4_K_M, for the same 1.5B model. Real, confirmed CI build
# sizes for the base app with no LLM at all are 819 MB (macOS) and
# 1533 MB (Windows). Q8_0's size is confirmed directly from the official
# repo's own file tree (not a third-party reupload, not guessed): 1.89 GB.
# 1533 + 1890 = 3423 MB, comfortably under 4096 MB with a real ~670 MB
# margin. The reason to prefer Q8_0 over Q4_K_M at the same parameter
# count: a benchmark specifically testing structured JSON output (not
# just general quality) measured Qwen2.5-1.5B at Q8_0 with a 95.7% JSON
# parse rate, described as "competitive with some 7B models" for exactly
# this kind of task - Q4_K_M's lower precision is the more likely place
# for that reliability to erode, not the parameter count itself. Since
# this auditor now also uses grammar-constrained decoding (see
# app/detection/auditor.py), which makes malformed JSON structurally
# impossible regardless of quantization, the two changes are
# complementary rather than either alone solving the same problem.
LLM_FILE = "qwen2.5-1.5b-instruct-q8_0.gguf"
LLM_DIR = MODELS / "llm"


def fetch_llm(force: bool = False) -> Path:
    target = LLM_DIR / LLM_FILE
    if target.exists() and not force:
        print(f"llm: already present ({target.stat().st_size / 1e6:.0f} MB)")
        return LLM_DIR
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise SystemExit("huggingface_hub is required to fetch models")
    print(f"llm: downloading {LLM_REPO} ({LLM_FILE}) ...")
    source = hf_hub_download(LLM_REPO, LLM_FILE)
    LLM_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    print(f"llm: ready at {target} ({target.stat().st_size / 1e6:.0f} MB)")
    return LLM_DIR


def main() -> int:
    force = "--force" in sys.argv
    fetch_gliner(force=force)
    # Opt-in: 1.89 GB for a layer that is off by default.
    if "--with-llm" not in sys.argv:
        print("llm: skipped (pass --with-llm to include the audit model)")
        return 0
    # Deliberately NOT conditional on llama_cpp being importable. This used to
    # skip the download whenever the runtime had not been installed yet, which
    # depended entirely on the order of the CI steps - and silently produced a
    # build with no audit model.
    fetch_llm(force=force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
