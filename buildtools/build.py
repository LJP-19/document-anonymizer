"""Packaging entry point, shared by every platform (spec sections 5, 65, 77).

One script builds on all three OSes so the Windows and macOS builds can never
drift apart. Everything the application needs at runtime - the spaCy model, the
rule files - is collected into the bundle; nothing is downloaded when the user
runs the app (spec sections 4 and 67).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.version import APP_NAME, __version__  # noqa: E402

BUNDLE_NAME = "DocumentAnonymizer"
SPACY_MODEL = "en_core_web_sm"

# Packages that PyInstaller cannot discover by static analysis because spaCy
# resolves them through entry points and catalogue registries at runtime.
COLLECT_ALL = [
    SPACY_MODEL,
    "spacy",
    "spacy_legacy",
    "spacy_loggers",
    "thinc",
    "srsly",
    "catalogue",
    "cymem",
    "preshed",
    "blis",
    "murmurhash",
    "wasabi",
    "weasel",
    "confection",
    "faker",
]

HIDDEN_IMPORTS = [
    "pymupdf",
    "yaml",
    "app.ui.main_window",
]


def _data_arg(src: Path, dest: str) -> str:
    sep = ";" if os.name == "nt" else ":"
    return f"{src}{sep}{dest}"


def build(clean: bool = True) -> Path:
    dist = ROOT / "dist"
    work = ROOT / "build" / "pyinstaller"
    if clean:
        shutil.rmtree(dist, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--windowed",
        "--name",
        BUNDLE_NAME,
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(ROOT / "build"),
        "--add-data",
        _data_arg(ROOT / "resources" / "rules", "resources/rules"),
    ]
    for pkg in COLLECT_ALL:
        if importlib.util.find_spec(pkg) is None:
            # Better to fail here than to ship a bundle missing a runtime
            # dependency that only breaks on the user's machine.
            raise SystemExit(f"required package '{pkg}' is not installed in the build environment")
        cmd += ["--collect-all", pkg]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    if sys.platform == "darwin":
        cmd += ["--osx-bundle-identifier", "solutions.turnkeyfinancial.docanonymizer"]
    cmd.append(str(ROOT / "main.py"))

    print("running:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)
    return dist


def make_dmg(dist: Path) -> Path:
    """macOS only. Wraps the .app in a mountable disk image."""
    app = dist / f"{BUNDLE_NAME}.app"
    if not app.exists():
        raise FileNotFoundError(f"{app} was not produced by PyInstaller")
    dmg = dist / f"{BUNDLE_NAME}-macOS-v{__version__}.dmg"
    staging = dist / "dmg-staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    shutil.copytree(app, staging / app.name, symlinks=True)
    os.symlink("/Applications", staging / "Applications")
    subprocess.run(
        [
            "hdiutil", "create", "-volname", APP_NAME,
            "-srcfolder", str(staging), "-ov", "-format", "UDZO", str(dmg),
        ],
        check=True,
    )
    shutil.rmtree(staging, ignore_errors=True)
    return dmg


def rename_windows_exe(dist: Path) -> Path:
    exe = dist / BUNDLE_NAME / f"{BUNDLE_NAME}.exe"
    if not exe.exists():
        exe = dist / f"{BUNDLE_NAME}.exe"
    if not exe.exists():
        raise FileNotFoundError("PyInstaller did not produce an .exe")
    return exe


def verify_model_is_bundled() -> None:
    """Fail the build rather than ship an app that would need the network."""
    if importlib.util.find_spec(SPACY_MODEL) is None:
        raise SystemExit(
            f"{SPACY_MODEL} is not installed in the build environment. "
            "The packaged app must never download it at runtime (spec section 67)."
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dmg", action="store_true", help="also build a .dmg (macOS)")
    args = parser.parse_args()

    verify_model_is_bundled()
    dist = build()

    if sys.platform == "darwin" and args.dmg:
        print("DMG:", make_dmg(dist))
    elif sys.platform.startswith("win"):
        print("EXE:", rename_windows_exe(dist))
    print("version:", __version__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
