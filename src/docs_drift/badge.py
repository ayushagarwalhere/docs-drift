"""Docs-health badges: a shields.io endpoint payload and an equivalent flat SVG.

A finding counts as stale exactly when `docs-drift check` would fail on it with
the default `--fail-on drift`: a drift verdict at or above the confidence threshold.
"""

import re
from collections.abc import Iterable
from typing import Any
from xml.sax.saxutils import escape

from docs_drift.models import Finding

DEFAULT_LABEL = "docs drift"
NOT_SCANNED = "not scanned"
MAX_LABEL_CHARS = 40
# shields.io named colours, so the endpoint JSON and the static SVG look the same.
COLORS = {
    "brightgreen": "#4c1",
    "green": "#97ca00",
    "yellow": "#dfb317",
    "orange": "#fe7d37",
    "red": "#e05d44",
    "lightgrey": "#9f9f9f",
}
_LABEL = re.compile(r"[^\x00-\x1f\x7f]{1,40}")
# Verdana 11px advance widths in tenths of a pixel, as used by shields' flat badges.
_WIDTHS = {
    " ": 39, "!": 43, '"': 51, "#": 90, "$": 70, "%": 120, "&": 79, "'": 30, "(": 49, ")": 49, "*": 70, "+": 90,
    ",": 40, "-": 49, ".": 40, "/": 49, ":": 49, ";": 49, "<": 90, "=": 90, ">": 90, "?": 60, "@": 110,
    "[": 49, "\\": 49, "]": 49, "^": 90, "_": 70, "`": 70, "{": 70, "|": 49, "}": 70, "~": 90,
    "A": 75, "B": 75, "C": 77, "D": 85, "E": 70, "F": 63, "G": 85, "H": 84, "I": 46, "J": 50, "K": 76, "L": 61,
    "M": 93, "N": 82, "O": 87, "P": 66, "Q": 87, "R": 77, "S": 75, "T": 68, "U": 81, "V": 75, "W": 110, "X": 75,
    "Y": 68, "Z": 75, "a": 66, "b": 68, "c": 58, "d": 68, "e": 65, "f": 38, "g": 68, "h": 69, "i": 30, "j": 38,
    "k": 65, "l": 30, "m": 106, "n": 69, "o": 67, "p": 68, "q": 68, "r": 47, "s": 57, "t": 43, "u": 69, "v": 65,
    "w": 89, "x": 65, "y": 65, "z": 58,
}  # fmt: skip
_DIGIT = 70
_FALLBACK = 75


def validate_label(label: str) -> str:
    if not _LABEL.fullmatch(label) or not label.strip():
        raise ValueError(f"Use a badge label of 1 to {MAX_LABEL_CHARS} printable characters.")
    return label


def stale_count(findings: Iterable[Finding], min_confidence: float) -> int:
    return sum(1 for finding in findings if finding.is_drift and finding.confidence >= min_confidence)


def color_for(stale: int | None) -> str:
    if stale is None:
        return "lightgrey"
    if stale == 0:
        return "brightgreen"
    if stale <= 3:  # noqa: PLR2004 - badge colour bands.
        return "yellow"
    if stale <= 9:  # noqa: PLR2004 - badge colour bands.
        return "orange"
    return "red"


def message_for(stale: int | None) -> str:
    return NOT_SCANNED if stale is None else f"{stale} stale"


def endpoint(stale: int | None, label: str = DEFAULT_LABEL) -> dict[str, Any]:
    """The shields.io endpoint schema: https://shields.io/badges/endpoint-badge."""
    return {"schemaVersion": 1, "label": label, "message": message_for(stale), "color": color_for(stale)}


def text_width(text: str) -> int:
    """Rendered width in tenths of a pixel; unknown glyphs use a wide fallback."""
    return sum(_DIGIT if character.isdigit() else _WIDTHS.get(character, _FALLBACK) for character in text)


def svg(stale: int | None, label: str = DEFAULT_LABEL) -> str:
    """A static flat badge equivalent to shields' `style=flat`, with every text node escaped."""
    message = message_for(stale)
    color = COLORS[color_for(stale)]
    label_text, message_text = text_width(label), text_width(message)
    # 5px padding on each side of each half, matching shields' flat layout.
    left, right = label_text // 10 + 10, message_text // 10 + 10
    total = left + right
    title = escape(f"{label}: {message}")
    attribute = escape(f"{label}: {message}", {'"': "&quot;"})
    label_x, message_x = left * 5, left * 10 + right * 5
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{attribute}">'
        f"<title>{title}</title>"
        '<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        '<stop offset="1" stop-opacity=".1"/></linearGradient>'
        f'<clipPath id="r"><rect width="{total}" height="20" rx="3" fill="#fff"/></clipPath>'
        f'<g clip-path="url(#r)"><rect width="{left}" height="20" fill="#555"/>'
        f'<rect x="{left}" width="{right}" height="20" fill="{color}"/>'
        f'<rect width="{total}" height="20" fill="url(#s)"/></g>'
        '<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" '
        'text-rendering="geometricPrecision" font-size="110">'
        f'<text aria-hidden="true" x="{label_x}" y="150" fill="#010101" fill-opacity=".3" transform="scale(.1)" '
        f'textLength="{label_text}">{escape(label)}</text>'
        f'<text x="{label_x}" y="140" transform="scale(.1)" fill="#fff" textLength="{label_text}">'
        f"{escape(label)}</text>"
        f'<text aria-hidden="true" x="{message_x}" y="150" fill="#010101" fill-opacity=".3" transform="scale(.1)" '
        f'textLength="{message_text}">{escape(message)}</text>'
        f'<text x="{message_x}" y="140" transform="scale(.1)" fill="#fff" textLength="{message_text}">'
        f"{escape(message)}</text></g></svg>"
    )
