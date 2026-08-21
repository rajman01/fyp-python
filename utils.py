"""Geometry and text helpers shared by the plan generators."""

import re

from bs4 import BeautifulSoup
from ezdxf.tools.text import MTextEditor

#: Height of a <small> run relative to the surrounding text. Used for the
#: address / local government / state / scale lines under a plan title.
SUBTITLE_HEIGHT_FACTOR = 0.68


def polygon_orientation(coords) -> str:
    """Return 'CW' or 'CCW' for a polygon given as [(x1, y1), (x2, y2), ...]."""
    area = 0
    for i in range(len(coords)):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % len(coords)]
        area += (x2 - x1) * (y2 + y1)
    return "CW" if area > 0 else "CCW"


def line_normals(p1, p2, orientation: str = "CCW"):
    """Return the (inside, outside) normal vectors of a polygon edge."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    if orientation == "CCW":  # inside = left normal
        inside = (-dy, dx)
        outside = (dy, -dx)
    else:  # CW polygon
        inside = (dy, -dx)
        outside = (-dy, dx)
    return inside, outside


def line_direction(angle: float) -> str:
    """Reading direction of a line drawn at ``angle`` degrees (-180 to 180)."""
    if -90 <= angle <= 90:
        return "left → right"
    return "right → left"


# Label rotations at or past this angle flip by 180 degrees so they stay
# readable. 110 (not 90) is the drafting convention (e.g. Civil3D's
# readability bias): near-vertical lines on either side of 90 degrees all
# read bottom-to-top instead of flipping direction exactly at vertical.
READABILITY_BIAS = 110.0


def readable_angle(angle: float, bias: float = READABILITY_BIAS) -> float:
    """Return ``angle`` or its reciprocal so label text reads 'uphill'.

    The result lies in [bias - 180, bias): left-to-right for horizontal-ish
    lines and bottom-to-top for vertical-ish ones, with the flip boundary
    ``bias - 90`` degrees past vertical so lines that straddle 90 degrees
    do not read in opposite directions.
    """
    angle %= 360.0
    if bias <= angle < bias + 180.0:
        angle -= 180.0
    elif angle >= bias + 180.0:
        angle -= 360.0
    return angle


def format_number(num, mode: str = "tenth") -> str:
    """Zero-pad a number to 2 ('tenth') or 3 ('hundredth') digits."""
    if mode == "tenth":
        return f"{num:02d}" if isinstance(num, int) else str(num)
    if mode == "hundredth":
        return f"{num:03d}" if isinstance(num, int) else str(num)
    raise ValueError("mode must be either 'tenth' or 'hundredth'")


def html_to_mtext(html_text: str, font: str = "Times New Roman") -> str:
    """Convert a small subset of HTML (b/i/u/br/p) to DXF MText markup.

    MText has no standalone bold/italic toggles; both are expressed through
    font-change codes (``\\fFamily|b1|i0;``), so the target font family must
    be known here.
    """
    if not html_text:
        return ""

    soup = BeautifulSoup(html_text.replace("\n", " "), "html.parser")
    editor = MTextEditor()

    # Track nested bold/italic state so closing a tag restores the outer style.
    style = {"bold": 0, "italic": 0}

    def apply_font():
        editor.append(rf"\f{font}|b{min(style['bold'], 1)}|i{min(style['italic'], 1)};")

    def with_style(key: str, child):
        style[key] += 1
        apply_font()
        parse_tag(child)
        style[key] -= 1
        apply_font()

    def parse_tag(tag):
        for child in tag.children:
            if isinstance(child, str):  # plain text
                text = re.sub(r"\s+", " ", child)
                if text.strip():
                    editor.append(text)
            elif child.name in ("b", "strong"):
                with_style("bold", child)
            elif child.name in ("i", "em"):
                with_style("italic", child)
            elif child.name == "small":
                # MText height codes are relative to the current height, so the
                # closing code is the reciprocal rather than a fixed value.
                editor.append(rf"\H{SUBTITLE_HEIGHT_FACTOR}x;")
                parse_tag(child)
                editor.append(rf"\H{1 / SUBTITLE_HEIGHT_FACTOR:.4f}x;")
            elif child.name == "u":
                editor.append(MTextEditor.UNDERLINE_START)
                parse_tag(child)
                editor.append(MTextEditor.UNDERLINE_STOP)
            elif child.name == "br":
                editor.append(MTextEditor.NEW_LINE)
            elif child.name == "p":
                # paragraphs start on a new line, except the very first element
                prev = child.previous_sibling
                while prev is not None and str(prev).strip() == "":
                    prev = prev.previous_sibling
                if prev is not None:
                    editor.append(MTextEditor.NEW_LINE)
                parse_tag(child)
            else:
                parse_tag(child)

    parse_tag(soup)
    return str(editor).replace("\n", "")
