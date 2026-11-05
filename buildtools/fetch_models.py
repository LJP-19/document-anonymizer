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


def main() -> int:
    force = "--force" in sys.argv
    fetch_gliner(force=force)
    # The LLM auditor model is no longer fetched or bundled at build time
    # at all - removed after a real GitHub Releases 2 GB-per-asset limit
    # made bundling a model a genuine, recurring constraint (three model
    # sizes tried and reverted trying to fit under it). The user now
    # chooses and downloads a model themselves, on first launch, from a
    # catalog with no build-time size ceiling at all - see app/llm/ and
    # app/ui/model_picker.py. llama-cpp-python (the inference ENGINE, not
    # any specific model's weights) still gets installed and bundled by
    # build.py/the CI workflow, since the app always needs that to run
    # inference once a model is downloaded - only the model file itself
    # moved out of the build.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
