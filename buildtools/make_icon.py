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


def _render_png(size: int, target: Path) -> None:
    """Rasterize the SVG. PyMuPDF (already a runtime dependency) renders SVG with
    no system libraries, so this works on every CI runner; cairosvg is used only
    if PyMuPDF is unavailable."""
    try:
        import pymupdf

        doc = pymupdf.open(stream=SVG.encode(), filetype="svg")
        page = doc[0]
        zoom = size / max(page.rect.width, page.rect.height)
        page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=True).save(str(target))
        doc.close()
    except ImportError:
        import cairosvg

        cairosvg.svg2png(bytestring=SVG.encode(), write_to=str(target), output_width=size, output_height=size)


def write_rasters() -> list[Path]:
    """PNG sizes, plus app.ico (Windows) and app.icns (macOS) via Pillow."""
    written: list[Path] = []
    for size in SIZES:
        target = ICON_DIR / f"app-{size}.png"
        _render_png(size, target)
        written.append(target)
    from PIL import Image

    ico = ICON_DIR / "app.ico"
    biggest = Image.open(ICON_DIR / "app-256.png")
    biggest.save(ico, format="ICO", sizes=[(s, s) for s in (16, 32, 48, 64, 128, 256)])
    written.append(ico)
    icns = ICON_DIR / "app.icns"
    Image.open(ICON_DIR / "app-1024.png").save(icns, format="ICNS")
    written.append(icns)
    return written


def main() -> int:
    print("icon:", write_svg())
    for path in write_rasters():
        print("icon:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
