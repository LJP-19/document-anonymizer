"""Visual design tokens and the application stylesheet.

Kept in one place so the whole app shares a single type scale, spacing rhythm
and palette rather than accumulating ad-hoc widget styling.
"""

from __future__ import annotations

# Palette - a neutral dark canvas so the red replacement text and the amber
# review highlights are the only saturated colour in the window.
BG = "#12141a"
SURFACE = "#191c24"
SURFACE_2 = "#20242e"
SURFACE_3 = "#272c38"
BORDER = "#2e3441"
TEXT = "#e7eaf0"
TEXT_DIM = "#98a0b0"
TEXT_FAINT = "#6b7385"

ACCENT = "#5b8cff"
ACCENT_HOVER = "#6f9bff"
DANGER = "#ff5c5c"
WARN = "#f0b23c"
OK = "#3ecf8e"

# Detection highlight colours drawn over the page render.
HL_REDACT = (255, 92, 92, 58)
HL_REDACT_EDGE = (255, 92, 92, 200)
HL_REVIEW = (240, 178, 60, 58)
HL_REVIEW_EDGE = (240, 178, 60, 200)
HL_KEEP = (110, 120, 140, 40)
HL_KEEP_EDGE = (130, 140, 160, 150)
HL_FOCUS_EDGE = (91, 140, 255, 255)

TYPE_COLORS = {
    "PERSON": "#7aa2ff",
    "SSN": "#ff7a7a",
    "EIN": "#ff9f7a",
    "ITIN": "#ff9f7a",
    "EMAIL": "#7ad4ff",
    "PHONE": "#7ad4ff",
    "ADDRESS": "#9ee37d",
    "STREET": "#9ee37d",
    "CITY_STATE": "#9ee37d",
    "POSTAL_CODE": "#9ee37d",
    "BANK_ACCOUNT": "#ffc46b",
    "ROUTING_NUMBER": "#ffc46b",
    "CARD_NUMBER": "#ffc46b",
    "UNCLASSIFIED_GROUP_VALUE": "#b39ddb",
}
DEFAULT_TYPE_COLOR = "#9aa4b8"


def type_color(name: str) -> str:
    return TYPE_COLORS.get(name, DEFAULT_TYPE_COLOR)


def rgba(hex_color: str, alpha: float) -> str:
    """Qt stylesheets do not honour 8-digit hex alpha; they need rgba()."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Inter", "Segoe UI", "SF Pro Text", "Helvetica Neue", sans-serif;
    font-size: 13px;
}}

/* Labels must not paint the window background over their card. */
QLabel {{ background: transparent; }}

#Header {{
    background: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}
#DocTitle    {{ font-size: 15px; font-weight: 600; }}
#DocSubtitle {{ font-size: 12px; color: {TEXT_FAINT}; }}

#StatusPill {{
    background: {SURFACE_3};
    color: {TEXT_DIM};
    border: 1px solid {BORDER};
    border-radius: 11px;
    padding: 3px 12px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.4px;
}}

QPushButton {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 7px 14px;
    color: {TEXT};
}}
QPushButton:hover  {{ background: {SURFACE_3}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: {SURFACE}; }}

QPushButton#Primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #0b0e14;
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton#Primary:disabled {{ background: {SURFACE_2}; border-color: {BORDER}; color: {TEXT_FAINT}; }}

QPushButton#Ghost {{
    background: transparent;
    border: 1px solid transparent;
    color: {TEXT_DIM};
    padding: 5px 10px;
}}
QPushButton#Ghost:hover {{ background: {SURFACE_2}; color: {TEXT}; }}

#Sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}

QLineEdit {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 7px 10px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}

QPushButton#Chip {{
    background: transparent;
    border: 1px solid {BORDER};
    border-radius: 13px;
    padding: 4px 14px;
    color: {TEXT_DIM};
    font-size: 12px;
    /* Constant weight: switching to bold on :checked resizes the button and
       clips its own label. */
    font-weight: 600;
}}
QPushButton#Chip:hover {{ color: {TEXT}; border-color: {TEXT_FAINT}; }}
QPushButton#Chip:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: #0b0e14;
}}

#Card {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 9px;
}}
#Card:hover {{ border-color: {TEXT_FAINT}; }}
#Card[selected="true"] {{ border-color: {ACCENT}; background: {SURFACE_3}; }}
#CardValue {{ font-size: 13px; font-weight: 600; }}
#CardMeta  {{ font-size: 11px; color: {TEXT_FAINT}; }}
#CardReason {{ font-size: 11px; color: {WARN}; }}

#TypeBadge {{
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.4px;
    padding: 2px 7px;
    border-radius: 4px;
}}

#SectionLabel {{
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.8px;
    color: {TEXT_FAINT};
}}

#Toolbar {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
#Canvas  {{ background: #0c0e13; }}

QScrollArea {{ border: none; }}
QScrollBar:vertical   {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {SURFACE_3}; border-radius: 5px; min-height: 30px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: {BORDER}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; border: none; height: 0; width: 0;
}}

#Footer {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
#EmptyState {{ color: {TEXT_FAINT}; font-size: 13px; }}

QSpinBox {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 6px;
}}
QSlider::groove:horizontal {{ height: 3px; background: {BORDER}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT_DIM}; width: 13px; height: 13px;
    margin: -5px 0; border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT}; }}

QToolTip {{
    background: {SURFACE_3};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 5px 8px;
    border-radius: 6px;
}}
"""
