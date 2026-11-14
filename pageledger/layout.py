"""The layout an engine reports for one page: where its text sits, and what it says.

A layout is a JSON object with exactly these keys::

    {"units": "page_millionths",
     "level": "line" | "word",
     "items": [{"box": [x0, y0, x1, y1], "text": "..."}, ...],
     "source": {...}}

``box`` holds four integers on a grid of ``GRID`` (one million) steps per page side:
``x`` runs from the left edge (0) to the right edge (``GRID``), ``y`` from the top
edge (0) to the bottom edge (``GRID``), with ``0 <= x0 < x1 <= GRID`` and
``0 <= y0 < y1 <= GRID``. The page is the page as rendered, after its ``/Rotate``,
so an engine that read a ``pdftoppm`` render divides its pixel boxes by the
render's width and height (``grid_box`` does this). A millionth of the page is
finer than any scan pixel: one pixel of a 9,500-pixel master is about 105 steps.

Integers rather than fractions keep equality and the digest honest: two layouts
are the same exactly when their digests are, with no rounding at hashing time.

``level`` says what an item is: a ``line`` (a detector's text line, possibly a
fragment of one) or a ``word`` (ALTO ``String``). ``text`` is the item's text as
the engine read it and may be empty. Items keep the engine's order. ``source`` is
a JSON object (it may be empty) describing where the layout came from, such as
an ALTO file and its digest; the digest covers it.

``layout_sha256`` is the SHA-256 of the canonical JSON: keys sorted, separators
``","`` and ``":"``, non-ASCII characters written as UTF-8 rather than escaped,
NaN and infinities refused. Floats can appear only inside ``source``; they are
written by Python's ``repr``, the shortest decimal that round-trips the IEEE-754
double, which is the same on every platform. Strings are hashed as given, without
Unicode normalisation.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

GRID = 1_000_000
UNITS = "page_millionths"
LEVELS = ("line", "word")
_FIELDS = {"units", "level", "items", "source"}


def _invalid(message: str) -> ValueError:
    return ValueError(f"Layout invalid: {message}")


def _json_value(value: Any, depth: int = 0) -> bool:
    if depth > 64:
        return False
    if value is None or type(value) in {str, bool, int}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_value(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json_value(item, depth + 1) for key, item in value.items())
    return False


def validate_layout(layout: Any) -> None:
    """Raise ``ValueError`` unless ``layout`` has exactly the documented shape."""
    if not isinstance(layout, Mapping) or set(layout) != _FIELDS:
        raise _invalid(f"must be an object with exactly {sorted(_FIELDS)}")
    if layout["units"] != UNITS:
        raise _invalid(f"units must be {UNITS!r}")
    if layout["level"] not in LEVELS:
        raise _invalid("level must be line or word")
    if type(layout["source"]) is not dict or not _json_value(layout["source"]):
        raise _invalid("source must be a JSON object")
    items = layout["items"]
    if type(items) is not list:
        raise _invalid("items must be a list")
    for number, item in enumerate(items, 1):
        if type(item) is not dict or set(item) != {"box", "text"}:
            raise _invalid(f"item {number} must have exactly box and text")
        if type(item["text"]) is not str:
            raise _invalid(f"item {number} text must be a string")
        box = item["box"]
        if type(box) is not list or len(box) != 4 or not all(type(value) is int for value in box):
            raise _invalid(f"item {number} box must be four integers")
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= GRID and 0 <= y0 < y1 <= GRID):
            raise _invalid(f"item {number} box must satisfy 0 <= x0 < x1 <= {GRID}, same for y")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def layout_sha256(layout: Any) -> str:
    validate_layout(layout)
    return hashlib.sha256(canonical_json(layout)).hexdigest()


def grid_box(x0: float, y0: float, x1: float, y1: float, width: float, height: float) -> list[int]:
    """A box in pixels of a ``width`` x ``height`` render, on the layout grid.

    Coordinates are rounded to the nearest step and clamped to the page, because a
    detector's padded box can reach past the image. Check boxes that must lie inside
    the page (ALTO) before calling this. A box that rounds to zero width or height
    fails ``validate_layout``; drop it rather than widen it.
    """
    if not (width > 0 and height > 0):
        raise ValueError("render width and height must be positive")

    def scale(value: float, size: float) -> int:
        return min(GRID, max(0, round(value / size * GRID)))

    return [scale(x0, width), scale(y0, height), scale(x1, width), scale(y1, height)]
