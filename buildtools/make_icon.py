"""Generate the application icon at build time.

Drawn in code rather than committed as a binary so it stays in version control
as something reviewable, and so every size is generated consistently.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "resources" / "icons"
SIZES = (16, 32, 48, 64, 128, 256, 512, 1024)

# A document with a redaction bar across it: what the app does, at 16px.
SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="112" fill="#12141a"/>
  <path d="M150 96h150l64 64v256a24 24 0 0 1-24 24H150a24 24 0 0 1-24-24V120a24 24 0 0 1 24-24z"
        fill="#e7eaf0"/>
  <path d="M300 96l64 64h-64z" fill="#98a0b0"/>
  <rect x="164" y="196" width="120" height="14" rx="7" fill="#98a0b0"/>
  <rect x="164" y="286" width="180" height="14" rx="7" fill="#98a0b0"/>
  <rect x="164" y="322" width="140" height="14" rx="7" fill="#98a0b0"/>
  <rect x="156" y="228" width="200" height="34" rx="9" fill="#ff5c5c"/>
  <rect x="164" y="358" width="96" height="14" rx="7" fill="#5b8cff"/>
</svg>
"""


def write_svg() -> Path:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    path = ICON_DIR / "app.svg"
    path.write_text(SVG)
    return path


def write_rasters() -> list[Path]:
    """PNG/ICO/ICNS for the packaged app. Requires Pillow + cairosvg if present."""
    written: list[Path] = []
    try:
        import cairosvg
    except ImportError:
        return written
    for size in SIZES:
        target = ICON_DIR / f"app-{size}.png"
        cairosvg.svg2png(bytestring=SVG.encode(), write_to=str(target),
                         output_width=size, output_height=size)
        written.append(target)
    try:
        from PIL import Image

        images = [Image.open(ICON_DIR / f"app-{s}.png") for s in (16, 32, 48, 64, 128, 256)]
        ico = ICON_DIR / "app.ico"
        images[0].save(ico, format="ICO", sizes=[(i.width, i.height) for i in images])
        written.append(ico)
    except ImportError:
        pass
    return written


def main() -> int:
    print("icon:", write_svg())
    for path in write_rasters():
        print("icon:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
