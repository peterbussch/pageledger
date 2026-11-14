"""Tile plans: how to read a dense page as a set of smaller images.

A plan cuts one page into rectangles (tiles) along the whitespace of a layout
(``pageledger.layout``), so that each tile is rendered large enough for its text
to be legible and no cut passes through a known line. Every function here is
pure: the same layout and settings give the same plan, byte for byte.

Plan shape (``plan_version`` "0.1")::

    {"plan_version": "0.1",
     "layout": {"attempt": str | None, "sha256": hex, "level": "line" | "word"} | None,
     "page_box": {"units": "pdf_points", "width": float, "height": float},
     "settings": {"min_line_pixels": int, "max_tiles": int, "max_image_side": int},
     "line_height": float | None,
     "tiles": [{"tile_id": "t01", "x": float, "y": float, "width": float,
                "height": float, "column": int | None, "band": bool,
                "max_dpi": float | None, "continues_block": bool,
                "heading": str | None, "edge_cut": bool,
                "small_text": bool | None, "layout_items": int}, ...]}

Coordinates. ``page_box`` is the page box that the layout's render covered, in
PDF points, after ``/Rotate`` (for a page rotated by 90 or 270 degrees, width and
height are swapped relative to the unrotated box). The engines' renders use
``pdftoppm`` without ``-cropbox``, which renders the media box, so the page box is
the media box (``pdfinfo -box``), not the crop box that ``pdfinfo`` reports as
"Page size"; tiles must be rendered with the same choice. Tile boxes are in PDF
points of that rendered page with the origin at its top-left corner, ``x`` to the
right and ``y`` downward, resolved to 0.001 pt. To render tile ``t`` at ``dpi``
pixels per inch, pass ``-r dpi -x X -y Y -W W -H H`` with
``X = floor(t.x * dpi / 72)``, ``Y = floor(t.y * dpi / 72)``,
``W = ceil((t.x + t.width) * dpi / 72) - X`` and ``H`` likewise, so the rendered
crop contains the tile.

Tiles share edges, never overlap and together cover the whole page
(``validate_plan`` checks this exactly, in integer thousandths of a point), so
text the layout missed still lands in some tile. They are listed in reading
order: segments (full-width bands and the column regions between them) from
top to bottom, a segment's columns from left to right, and a column's pieces
from top to bottom. That order, like the column detection, assumes left-to-right print;
for right-to-left pages the geometry is still a valid partition but the tile
order and the indentation hints below read the columns in the wrong order. Text
taken from a layout (a heading, a projected witness) writes a row whose letters
are mostly right to left from its right-hand end.

Fields of a tile:

- ``column``: the column's number from the left (1-based) within its segment, or
  None for a ``band``: a full-width tile on a page that has columns elsewhere,
  such as a heading or running head. On a page without columns every tile is
  column 1. The single full-page tile of a plan without layout items has no
  column and is no band.
- ``max_dpi``: the page image's native resolution, above which rendering only
  upsamples; None for a page without one.
- ``layout_items``: how many layout items have their centre in the tile.
- ``edge_cut``: some layout item crosses one of the tile's edges inside the page
  by more than a quarter of ``line_height``: no clean cut existed there.
- ``small_text``: rendering the tile within ``max_image_side`` (and at most at
  ``max_dpi``) puts the median height of its column's lines, or of its band's,
  below ``min_line_pixels``. None when there is no layout to measure.
- ``continues_block``: the tile's first row is indented, so it continues a block
  whose heading lies above it. ``heading`` is then the text of the nearest
  unindented row before it in reading order within the same segment, so a block
  that runs from the foot of one column to the head of the next keeps its
  heading, but never across a band; None if there is none on this page. A row
  is indented when it starts more than half a line height (the column's median)
  right of the leftmost start of any line in its column segment. The heading is
  a record for review; it is not sent to a model.

``line_height`` is the median height of the layout's lines (for a word layout,
of the lines rebuilt from its words), in points.

How cuts are placed. The page is first split at horizontal whitespace taller than
half a line height, and consecutive blocks with the same gutters are joined again,
as ``rapidocr_adapter._xy_cut`` does, so two columns above a heading and three
below it each get their own gutters. Within a block, gutters are found as in
``rapidocr_adapter._gutters`` (the geometry is lifted here in integer arithmetic):
whitespace wider than a quarter line height in the horizontal projection, ignoring
lines wider than 0.6 of the text if needed, or else clusters of line starts. Lines
that cross a gutter by more than two line heights on each side make a full-width
band, bounded at the midpoints of the whitespace above and below. Each column segment is then cut into pieces no taller
than ``Hmax = h * max_image_side / min(min_line_pixels, h * max_dpi / 72)``, where
``h`` is the segment's median line height: the tallest tile whose lines still
render at ``min_line_pixels`` (or, for a low-resolution source, the tallest that
fits ``max_image_side`` at native resolution). A cut goes at the midpoint of the
whitespace in ``[0.75 Hmax, Hmax]`` below the piece's top, preferring the lowest
one above an unindented row (a block start), else the widest; failing that the
widest whitespace in ``[0.5 Hmax, Hmax]``; failing that exactly at ``Hmax``. If
the plan has more than ``max_tiles`` tiles, ``Hmax`` grows by a factor of 1.25
until it fits; once every column segment is a single piece, the narrowest pair of
adjacent columns is merged instead, and once no columns are left, the two
adjacent blocks with the least combined height. Merged columns show as
``small_text`` when their text no longer renders at ``min_line_pixels``. Word
layouts are first joined into lines: words in one row no further apart than a
quarter line height (the narrowest gutter) become one line, so joining never
bridges a gutter.

All geometry runs on integers (thousandths of a point) and exact fractions, so
the plan cannot differ between platforms; floats appear only in the output, as
integers divided by 1000. ``plan_sha256`` hashes the canonical JSON as
``layout_sha256`` does.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .layout import GRID, LEVELS, canonical_json, layout_sha256, validate_layout
from .rapidocr_adapter import right_to_left

PLAN_VERSION = "0.1"
MAX_TILES = 99
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_FIELDS = {"plan_version", "layout", "page_box", "settings", "line_height", "tiles"}
_TILE_FIELDS = {
    "tile_id",
    "x",
    "y",
    "width",
    "height",
    "column",
    "band",
    "max_dpi",
    "continues_block",
    "heading",
    "edge_cut",
    "small_text",
    "layout_items",
}


@dataclass(frozen=True)
class _Box:
    x0: int
    y0: int
    x1: int
    y1: int
    text: str
    index: int

    @property
    def cx2(self) -> int:
        return self.x0 + self.x1

    @property
    def cy2(self) -> int:
        return self.y0 + self.y1

    @property
    def height(self) -> int:
        return self.y1 - self.y0


Rect = tuple[int, int, int, int]


@dataclass
class _Tile:
    rect: Rect
    column: int | None
    segment: int
    reference: int
    margin: int = 0


def _invalid(message: str) -> ValueError:
    return ValueError(f"Tile plan invalid: {message}")


def _number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _milli(value: float) -> int:
    return round(Fraction(value) * 1000)


def _median(values: Sequence[int]) -> int:
    ordered = sorted(values)
    return max(1, ordered[len(ordered) // 2]) if ordered else 1


def _boxes(layout: Mapping[str, Any], width: int, height: int) -> list[_Box]:
    def scale(value: int, size: int) -> int:
        return (value * size + GRID // 2) // GRID

    return [
        _Box(
            scale(item["box"][0], width),
            scale(item["box"][1], height),
            scale(item["box"][2], width),
            scale(item["box"][3], height),
            item["text"],
            index,
        )
        for index, item in enumerate(layout["items"])
    ]


def _contains(rect: Rect, box: _Box, width: int, height: int) -> bool:
    x0, y0, x1, y1 = rect
    return (
        2 * x0 <= box.cx2
        and (box.cx2 < 2 * x1 or (x1 == width and box.cx2 == 2 * width))
        and 2 * y0 <= box.cy2
        and (box.cy2 < 2 * y1 or (y1 == height and box.cy2 == 2 * height))
    )


def _row_groups(boxes: Sequence[_Box], gap: int) -> list[list[_Box]]:
    rows: list[list[_Box]] = []
    for box in sorted(boxes, key=lambda b: (b.cy2, b.cx2, b.index)):
        if rows and 10 * abs(box.cy2 - rows[-1][0].cy2) <= 11 * gap:
            rows[-1].append(box)
        else:
            rows.append([box])
    return [sorted(row, key=lambda b: (b.cx2, b.index)) for row in rows]


def _row_text(row: Sequence[_Box]) -> str:
    """A row's text in its reading direction: from the right when it reads right to left."""
    in_layout_order = sorted(row, key=lambda box: box.index)
    ordered = reversed(row) if right_to_left(box.text for box in in_layout_order) else row
    return " ".join(box.text for box in ordered if box.text)


def _runs(boxes: list[_Box], level: str, gap: int) -> list[_Box]:
    """Lines for planning: a line layout as is, a word layout joined into lines."""
    if level == "line":
        return boxes
    runs = []
    for row in _row_groups(boxes, gap):
        words = sorted(row, key=lambda b: (b.x0, b.index))
        current = [words[0]]
        for word in words[1:]:
            if 4 * (word.x0 - max(w.x1 for w in current)) <= gap:
                current.append(word)
            else:
                runs.append(_join(current))
                current = [word]
        runs.append(_join(current))
    return runs


def _reading_order(words: list[_Box]) -> list[_Box]:
    """Words left to right, or in the layout's own order when that reads right to left."""
    in_layout_order = sorted(words, key=lambda word: word.index)
    return in_layout_order if right_to_left(word.text for word in in_layout_order) else words


def _join(words: list[_Box]) -> _Box:
    return _Box(
        min(w.x0 for w in words),
        min(w.y0 for w in words),
        max(w.x1 for w in words),
        max(w.y1 for w in words),
        " ".join(w.text for w in _reading_order(words) if w.text),
        min(w.index for w in words),
    )


def _gaps(spans: list[tuple[int, int]], gap: int, parts: int = 4) -> list[int]:
    """Cut positions inside whitespace wider than ``gap / parts``."""
    cuts: list[int] = []
    end: int | None = None
    for start, stop in sorted(spans):
        if end is not None and parts * (start - end) > gap:
            cuts.append((end + start) // 2)
        end = stop if end is None else max(end, stop)
    return cuts


def _gutters(lines: list[_Box], gap: int) -> list[int]:
    if not lines:
        # A band between cuts can hold no line centre when lines are a grid step tall.
        return []
    cuts = _gaps([(line.x0, line.x1) for line in lines], gap)
    if cuts:
        return cuts
    width = max(line.x1 for line in lines) - min(line.x0 for line in lines)
    narrow = [line for line in lines if 5 * (line.x1 - line.x0) < 3 * width]
    cuts = _gaps([(line.x0, line.x1) for line in narrow], gap)
    return cuts or _column_starts(narrow, gap)


def _column_starts(lines: list[_Box], gap: int) -> list[int]:
    if len(lines) < 10:
        return []
    starts = sorted(line.x0 for line in lines)
    clusters: list[list[int]] = [[starts[0]]]
    for x in starts[1:]:
        if x - clusters[-1][-1] <= gap:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    columns = [c for c in clusters if len(c) >= max(3, len(lines) // 5)]
    cuts = []
    for column in columns[1:]:
        start = column[0]
        left = [line for line in lines if 2 * line.x0 < 2 * start - gap]
        if left and 5 * sum(line.x1 <= start + gap for line in left) >= 4 * len(left):
            cuts.append((2 * start - gap) // 2)
    return cuts


@dataclass
class _Block:
    top: int
    bottom: int
    cuts: list[int]


def _cuts(lines: list[_Box], gap: int, width: int) -> list[int]:
    return sorted({c for c in _gutters(lines, gap) if 0 < c < width})


def _same_gutters(above: list[int], here: list[int], gap: int) -> bool:
    if not above or not here:
        return not above and not here
    return all(any(abs(a - b) <= 2 * gap for b in above) for a in here)


def _blocks(runs: list[_Box], gap: int, width: int, height: int) -> list[_Block]:
    edges = [y for y in _gaps([(r.y0, r.y1) for r in runs], gap, parts=2) if 0 < y < height]
    blocks: list[_Block] = []
    for top, bottom in zip([0, *edges], [*edges, height], strict=True):
        lines = [r for r in runs if _contains((0, top, width, bottom), r, width, height)]
        cuts = _cuts(lines, gap, width)
        if blocks and _same_gutters(blocks[-1].cuts, cuts, gap):
            block = blocks[-1]
            block.bottom = bottom
            merged = [r for r in runs if _contains((0, block.top, width, bottom), r, width, height)]
            block.cuts = _cuts(merged, gap, width)
        else:
            blocks.append(_Block(top, bottom, cuts))
    return blocks


def _segments(
    runs: list[_Box], cuts: list[int], gap: int, lo: int, hi: int
) -> list[tuple[int, int, bool]]:
    """Horizontal segments of a block, top to bottom, as (top, bottom, is_band)."""
    wide = sorted(
        (r for r in runs if any(r.x0 < c - 2 * gap and r.x1 > c + 2 * gap for c in cuts)),
        key=lambda r: (r.y0, r.y1, r.x0, r.index),
    )
    if not wide:
        return [(lo, hi, False)]
    wide_ids = {r.index for r in wide}
    narrow = [r for r in runs if r.index not in wide_ids]
    groups = [[wide[0]]]
    for run in wide[1:]:
        bottom = max(r.y1 for r in groups[-1])
        if any(2 * bottom < r.cy2 < 2 * run.y0 for r in narrow):
            groups.append([run])
        else:
            groups[-1].append(run)
    segments = []
    cursor = lo
    for group in groups:
        b0, b1 = min(r.y0 for r in group), max(r.y1 for r in group)
        members = [r for r in narrow if 2 * b0 <= r.cy2 <= 2 * b1]
        e0 = min([b0, *(r.y0 for r in members)])
        e1 = max([b1, *(r.y1 for r in members)])
        above = [r.y1 for r in narrow if r.cy2 < 2 * e0]
        below = [r.y0 for r in narrow if r.cy2 > 2 * e1]
        top = lo if not above else (max(above) + e0) // 2 if max(above) <= e0 else e0
        end = hi if not below else (e1 + min(below)) // 2 if min(below) >= e1 else e1
        top = max(top, cursor)
        end = max(end, top)
        if top > cursor:
            segments.append((cursor, top, False))
        if end > top:
            segments.append((top, end, True))
        cursor = end
    if cursor < hi:
        segments.append((cursor, hi, False))
    return segments


def _whitespace(spans: list[tuple[int, int]], lo: int, hi: int) -> list[tuple[int, int]]:
    out = []
    cursor = lo
    for start, stop in sorted(spans):
        if stop <= cursor:
            continue
        if start > cursor:
            out.append((cursor, min(start, hi)))
        cursor = max(cursor, stop)
        if cursor >= hi:
            break
    if cursor < hi:
        out.append((cursor, hi))
    return [(a, b) for a, b in out if b > a]


def _indented(row: Sequence[_Box], margin: int, gap: int) -> bool:
    return 2 * (min(box.x0 for box in row) - margin) > gap


def _pieces(
    lines: list[_Box], top: int, bottom: int, limit: int, gap: int, margin: int
) -> list[tuple[int, int]]:
    if not lines or bottom - top <= limit:
        return [(top, bottom)]
    rows = sorted(_row_groups(lines, gap), key=lambda row: min(b.y0 for b in row))
    tops = [(min(b.y0 for b in row), row) for row in rows]
    spans = [(line.y0, line.y1) for line in lines]

    def block_start(end: int) -> bool:
        row = next((row for start, row in tops if start >= end), None)
        return row is not None and not _indented(row, margin, gap)

    pieces = []
    start = top
    while bottom - start > limit:
        hi = start + limit
        space = _whitespace(spans, start, bottom)

        def window(lo: int, hi: int = hi, space: list[tuple[int, int]] = space) -> list[Any]:
            return [
                ((max(a, lo) + min(b, hi)) // 2, b - a, b)
                for a, b in space
                if min(b, hi) > max(a, lo)
            ]

        candidates = window(start + (3 * limit + 3) // 4)
        starts = [c for c in candidates if block_start(c[2])]
        if starts:
            cut = max(c[0] for c in starts)
        else:
            candidates = candidates or window(start + (limit + 1) // 2)
            cut = max(candidates, key=lambda c: (c[1], c[0]))[0] if candidates else hi
        pieces.append((start, cut))
        start = cut
    pieces.append((start, bottom))
    return pieces


def _limit(reference: int, factor: Fraction, settings: Mapping[str, Any], dpi: Any) -> int:
    target = Fraction(settings["min_line_pixels"])
    if dpi is not None:
        target = min(target, Fraction(reference) * Fraction(dpi) / 72000)
    return max(1, math.floor(Fraction(reference * settings["max_image_side"]) / target * factor))


def _build(
    runs: list[_Box],
    blocks: list[_Block],
    gap: int,
    width: int,
    height: int,
    factor: Fraction,
    settings: Mapping[str, Any],
    dpi: Any,
) -> tuple[list[_Tile], bool]:
    tiles: list[_Tile] = []
    split = False
    columns_elsewhere = any(block.cuts for block in blocks)
    segments = []
    for block in blocks:
        area = (0, block.top, width, block.bottom)
        lines = [r for r in runs if _contains(area, r, width, height)]
        for segment in _segments(lines, block.cuts, gap, block.top, block.bottom):
            segments.append((segment, block.cuts))
    for number, ((top, bottom, band), cuts) in enumerate(segments):
        inside = [r for r in runs if _contains((0, top, width, bottom), r, width, height)]
        xs = [0, width] if band else [0, *cuts, width]
        for column in range(len(xs) - 1):
            rect = (xs[column], top, xs[column + 1], bottom)
            lines = [r for r in inside if _contains(rect, r, width, height)]
            reference = _median([r.height for r in lines]) if lines else gap
            margin = min((r.x0 for r in lines), default=rect[0])
            limit = _limit(reference, factor, settings, dpi)
            pieces = _pieces(lines, top, bottom, limit, reference, margin)
            split = split or len(pieces) > 1
            label = None if len(xs) == 2 and columns_elsewhere else column + 1
            for a, b in pieces:
                tiles.append(_Tile((rect[0], a, rect[2], b), label, number, reference, margin))
    return tiles, split


def _fewer(blocks: list[_Block], width: int) -> list[_Block]:
    """The blocks with the narrowest pair of adjacent columns merged or, when no
    block has columns left, the two adjacent blocks of least combined height."""
    choices: list[tuple[int, int, int]] = []
    for b, block in enumerate(blocks):
        xs = [0, *block.cuts, width]
        choices.extend((xs[i + 2] - xs[i], b, i) for i in range(len(block.cuts)))
    if choices:
        _, b, i = min(choices)
        block = blocks[b]
        fewer = _Block(block.top, block.bottom, block.cuts[:i] + block.cuts[i + 1 :])
        return [*blocks[:b], fewer, *blocks[b + 1 :]]
    i = min(range(len(blocks) - 1), key=lambda i: (blocks[i + 1].bottom - blocks[i].top, i))
    return [*blocks[:i], _Block(blocks[i].top, blocks[i + 1].bottom, []), *blocks[i + 2 :]]


def _crossed(rect: Rect, boxes: Sequence[_Box], width: int, height: int, gap: int) -> bool:
    x0, y0, x1, y1 = rect
    for box in boxes:
        across = min(box.y1, y1) - max(box.y0, y0)
        if 4 * across > gap:
            for edge in (x0, x1):
                if 0 < edge < width and 4 * min(edge - box.x0, box.x1 - edge) > gap:
                    return True
        across = min(box.x1, x1) - max(box.x0, x0)
        if 4 * across > gap:
            for edge in (y0, y1):
                if 0 < edge < height and 4 * min(edge - box.y0, box.y1 - edge) > gap:
                    return True
    return False


def _small(tile: _Tile, settings: Mapping[str, Any], dpi: Any) -> bool:
    x0, y0, x1, y1 = tile.rect
    pixels = Fraction(tile.reference * settings["max_image_side"], max(x1 - x0, y1 - y0))
    if dpi is not None:
        pixels = min(pixels, Fraction(tile.reference) * Fraction(dpi) / 72000)
    return pixels < settings["min_line_pixels"]


def _positive_int(name: str, value: Any, maximum: int | None = None) -> None:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        bound = f" no more than {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be a positive integer{bound}")


def plan_tiles(
    layout: Mapping[str, Any] | None,
    *,
    page_width: float,
    page_height: float,
    min_line_pixels: int,
    max_tiles: int,
    max_image_side: int,
    max_dpi: float | None = None,
    layout_attempt: str | None = None,
) -> dict[str, Any]:
    """Plan the tiles for one page from its layout (None when there is none).

    ``page_width`` and ``page_height`` are the rendered page in PDF points (see the
    module docstring); ``max_dpi`` is the native resolution of the page image, if
    it has one; ``layout_attempt`` names the attempt the layout came from. Without
    a layout, or with one that has no items, the plan is one full-page tile.
    """
    for name, value in (("page_width", page_width), ("page_height", page_height)):
        if not _number(value) or _milli(value) < 1:
            raise ValueError(f"{name} must be a positive number of points")
    _positive_int("min_line_pixels", min_line_pixels)
    _positive_int("max_tiles", max_tiles, MAX_TILES)
    _positive_int("max_image_side", max_image_side)
    if max_dpi is not None and (not _number(max_dpi) or max_dpi <= 0):
        raise ValueError("max_dpi must be a positive number or None")
    if layout_attempt is not None and (type(layout_attempt) is not str or not layout_attempt):
        raise ValueError("layout_attempt must be a non-empty string or None")
    width, height = _milli(page_width), _milli(page_height)
    settings = {
        "min_line_pixels": min_line_pixels,
        "max_tiles": max_tiles,
        "max_image_side": max_image_side,
    }
    plan: dict[str, Any] = {
        "plan_version": PLAN_VERSION,
        "layout": None,
        "page_box": {"units": "pdf_points", "width": width / 1000, "height": height / 1000},
        "settings": settings,
        "line_height": None,
        "tiles": [],
    }
    boxes: list[_Box] = []
    if layout is not None:
        plan["layout"] = {
            "attempt": layout_attempt,
            "sha256": layout_sha256(layout),
            "level": layout["level"],
        }
        boxes = _boxes(layout, width, height)
    dpi = None if max_dpi is None else float(max_dpi)
    if not boxes:
        plan["tiles"] = [
            _tile_record(1, (0, 0, width, height), None, False, dpi, None, False, None, 0)
        ]
        return plan

    runs = _runs(boxes, layout["level"] if layout else "line", _median([b.height for b in boxes]))
    gap = _median([r.height for r in runs])
    plan["line_height"] = gap / 1000
    blocks = _blocks(runs, gap, width, height)
    factor = Fraction(1)
    while True:
        tiles, split = _build(runs, blocks, gap, width, height, factor, settings, dpi)
        if len(tiles) <= max_tiles:
            break
        if split:
            factor *= Fraction(5, 4)
        else:
            blocks = _fewer(blocks, width)

    last_heading: dict[int, str | None] = {}
    for number, tile in enumerate(tiles, 1):
        lines = [r for r in runs if _contains(tile.rect, r, width, height)]
        rows = _row_groups(lines, tile.reference)
        continues = heading = None
        if tile.column is not None:
            continues = bool(rows) and _indented(rows[0], tile.margin, tile.reference)
            heading = last_heading.get(tile.segment) if continues else None
            for row in rows:
                if not _indented(row, tile.margin, tile.reference):
                    last_heading[tile.segment] = _row_text(row)
        plan["tiles"].append(
            _tile_record(
                number,
                tile.rect,
                tile.column,
                tile.column is None,
                dpi,
                heading,
                bool(continues),
                _small(tile, settings, dpi),
                sum(_contains(tile.rect, box, width, height) for box in boxes),
                edge_cut=_crossed(tile.rect, boxes, width, height, gap),
            )
        )
    return plan


def _tile_record(
    number: int,
    rect: Rect,
    column: int | None,
    band: bool,
    dpi: float | None,
    heading: str | None,
    continues: bool,
    small: bool | None,
    items: int,
    *,
    edge_cut: bool = False,
) -> dict[str, Any]:
    x0, y0, x1, y1 = rect
    return {
        "tile_id": f"t{number:02d}",
        "x": x0 / 1000,
        "y": y0 / 1000,
        "width": (x1 - x0) / 1000,
        "height": (y1 - y0) / 1000,
        "column": column,
        "band": band,
        "max_dpi": dpi,
        "continues_block": continues,
        "heading": heading,
        "edge_cut": edge_cut,
        "small_text": small,
        "layout_items": items,
    }


def validate_plan(plan: Any) -> None:
    """Raise ``ValueError`` unless ``plan`` has the documented shape and its tiles
    exactly partition the page."""
    if not isinstance(plan, Mapping) or set(plan) != _PLAN_FIELDS:
        raise _invalid(f"must be an object with exactly {sorted(_PLAN_FIELDS)}")
    if plan["plan_version"] != PLAN_VERSION:
        raise _invalid(f"plan_version must be {PLAN_VERSION!r}")
    layout = plan["layout"]
    if layout is not None and (
        not isinstance(layout, Mapping)
        or set(layout) != {"attempt", "sha256", "level"}
        or not (layout["attempt"] is None or (type(layout["attempt"]) is str and layout["attempt"]))
        or not isinstance(layout["sha256"], str)
        or not _SHA256.fullmatch(layout["sha256"])
        or layout["level"] not in LEVELS
    ):
        raise _invalid("layout must be null or record attempt, sha256 and level")
    box = plan["page_box"]
    if (
        not isinstance(box, Mapping)
        or set(box) != {"units", "width", "height"}
        or box["units"] != "pdf_points"
        or not all(_number(box[key]) and _milli(box[key]) >= 1 for key in ("width", "height"))
    ):
        raise _invalid("page_box must be a positive box in pdf_points")
    settings = plan["settings"]
    if not isinstance(settings, Mapping) or set(settings) != {
        "min_line_pixels",
        "max_tiles",
        "max_image_side",
    }:
        raise _invalid("settings must record min_line_pixels, max_tiles and max_image_side")
    try:
        _positive_int("min_line_pixels", settings["min_line_pixels"])
        _positive_int("max_tiles", settings["max_tiles"], MAX_TILES)
        _positive_int("max_image_side", settings["max_image_side"])
    except ValueError as exc:
        raise _invalid(str(exc)) from exc
    if plan["line_height"] is not None and not (
        _number(plan["line_height"]) and plan["line_height"] > 0
    ):
        raise _invalid("line_height must be null or positive")
    tiles = plan["tiles"]
    if type(tiles) is not list or not 1 <= len(tiles) <= settings["max_tiles"]:
        raise _invalid("tiles must be a list of 1 to max_tiles tiles")
    rects = []
    for number, tile in enumerate(tiles, 1):
        if not isinstance(tile, Mapping) or set(tile) != _TILE_FIELDS:
            raise _invalid(f"tile {number} must have exactly {sorted(_TILE_FIELDS)}")
        if tile["tile_id"] != f"t{number:02d}":
            raise _invalid(f"tile {number} must be numbered t{number:02d}")
        if not all(_number(tile[key]) for key in ("x", "y", "width", "height")):
            raise _invalid(f"tile {number} box must be finite numbers")
        column, band = tile["column"], tile["band"]
        if type(band) is not bool or not (
            (column is None and (band or len(tiles) == 1))
            or (type(column) is int and column >= 1 and not band)
        ):
            raise _invalid(f"tile {number} column must be a positive integer, or null for a band")
        if tile["max_dpi"] is not None and not (_number(tile["max_dpi"]) and tile["max_dpi"] > 0):
            raise _invalid(f"tile {number} max_dpi must be null or positive")
        if (
            type(tile["continues_block"]) is not bool
            or type(tile["edge_cut"]) is not bool
            or tile["small_text"] not in (True, False, None)
            or type(tile["small_text"]) not in (bool, type(None))
        ):
            raise _invalid(f"tile {number} flags must be booleans")
        if tile["heading"] is not None and (
            type(tile["heading"]) is not str or not tile["continues_block"]
        ):
            raise _invalid(f"tile {number} heading must be null or text of a continued block")
        if type(tile["layout_items"]) is not int or tile["layout_items"] < 0:
            raise _invalid(f"tile {number} layout_items must be a count")
        x0, y0 = _milli(tile["x"]), _milli(tile["y"])
        rects.append((x0, y0, x0 + _milli(tile["width"]), y0 + _milli(tile["height"])))
    width, height = _milli(box["width"]), _milli(box["height"])
    for x0, y0, x1, y1 in rects:
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise _invalid("every tile must be a non-empty box inside the page")
    if sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in rects) != width * height:
        raise _invalid("tiles must cover the page exactly")
    for i, (ax0, ay0, ax1, ay1) in enumerate(rects):
        for bx0, by0, bx1, by1 in rects[i + 1 :]:
            if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                raise _invalid("tiles must not overlap")


def plan_sha256(plan: Any) -> str:
    validate_plan(plan)
    return hashlib.sha256(canonical_json(plan)).hexdigest()


def _geometry(plan: Mapping[str, Any]) -> tuple[int, int, list[Rect]]:
    validate_plan(plan)
    rects = []
    for tile in plan["tiles"]:
        x0, y0 = _milli(tile["x"]), _milli(tile["y"])
        rects.append((x0, y0, x0 + _milli(tile["width"]), y0 + _milli(tile["height"])))
    return _milli(plan["page_box"]["width"]), _milli(plan["page_box"]["height"]), rects


def check_edges(plan: Mapping[str, Any], layouts: Sequence[Mapping[str, Any]]) -> list[str]:
    """Tile ids, in plan order, whose edges inside the page an item of any of
    ``layouts`` crosses by more than a quarter of the plan's ``line_height``
    (along the edge and across it). The planning layout itself flags exactly the
    tiles marked ``edge_cut``."""
    width, height, rects = _geometry(plan)
    gap = 0 if plan["line_height"] is None else _milli(plan["line_height"])
    boxes = []
    for layout in layouts:
        validate_layout(layout)
        boxes.extend(_boxes(layout, width, height))
    return [
        tile["tile_id"]
        for tile, rect in zip(plan["tiles"], rects, strict=True)
        if _crossed(rect, boxes, width, height, gap)
    ]


def project_witness(layout: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[str, int]:
    """A literal reading's text regrouped in tile order, and the count of its items
    whose centre lies in no tile (0 for any valid plan).

    Each item goes to the tile containing its centre. Within a tile, items are
    grouped into rows by their vertical centres, rows are written top to bottom
    and a row's items left to right joined by single spaces. Tiles are joined by
    "\\n" in plan order; tiles without text add nothing.
    """
    validate_layout(layout)
    width, height, rects = _geometry(plan)
    boxes = _boxes(layout, width, height)
    gap = _median([box.height for box in boxes])
    placed: list[list[_Box]] = [[] for _ in rects]
    outside = 0
    for box in boxes:
        index = next(
            (i for i, rect in enumerate(rects) if _contains(rect, box, width, height)), None
        )
        if index is None:
            outside += 1
        else:
            placed[index].append(box)
    texts = []
    for tile_boxes in placed:
        rows = [_row_text(row) for row in _row_groups(tile_boxes, gap)]
        text = "\n".join(row for row in rows if row)
        if text:
            texts.append(text)
    return "\n".join(texts), outside


def centres_inside(layout: Mapping[str, Any], reference: Mapping[str, Any]) -> tuple[int, int]:
    """How many of ``layout``'s item centres lie inside some item box of
    ``reference``, and how many items ``layout`` has.

    Both are layouts of the same page, so this compares them on the layout grid.
    It is the cross-check of an institutional layout (ALTO words) against the
    job's own OCR lines: a low share means the boxes point elsewhere.
    """
    validate_layout(layout)
    validate_layout(reference)
    step = GRID // 1000
    buckets: dict[int, list[list[int]]] = {}
    for item in reference["items"]:
        box = item["box"]
        for bucket in range(box[1] // step, box[3] // step + 1):
            buckets.setdefault(bucket, []).append(box)
    inside = 0
    for item in layout["items"]:
        x0, y0, x1, y1 = item["box"]
        cx2, cy2 = x0 + x1, y0 + y1
        candidates = buckets.get(cy2 // 2 // step, [])
        inside += any(
            2 * b[0] <= cx2 <= 2 * b[2] and 2 * b[1] <= cy2 <= 2 * b[3] for b in candidates
        )
    return inside, len(layout["items"])
