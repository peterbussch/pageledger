from __future__ import annotations

import copy
import random

import pytest

from pageledger.layout import GRID
from pageledger.tiling import (
    centres_inside,
    check_edges,
    plan_sha256,
    plan_tiles,
    project_witness,
    validate_plan,
)

SETTINGS = {"min_line_pixels": 32, "max_tiles": 24, "max_image_side": 2400}


def page(items, width, height, level="line"):
    """A layout from boxes given in points of a ``width`` x ``height`` page."""
    return {
        "units": "page_millionths",
        "level": level,
        "items": [
            {
                "box": [
                    round(x0 / width * GRID),
                    round(y0 / height * GRID),
                    round(x1 / width * GRID),
                    round(y1 / height * GRID),
                ],
                "text": text,
            }
            for x0, y0, x1, y1, text in items
        ],
        "source": {},
    }


def plan(layout, width, height, **overrides):
    options = {**SETTINGS, "max_dpi": None, "layout_attempt": "a1", **overrides}
    return plan_tiles(layout, page_width=width, page_height=height, **options)


def rect(tile):
    return tile["x"], tile["y"], tile["x"] + tile["width"], tile["y"] + tile["height"]


def directory(columns=6, lines=140, width=648.0, height=864.0, seed=7, heading=True):
    """A telephone-directory page: a heading across the page, then dense columns."""
    rng = random.Random(seed)
    items = []
    if heading:
        items.append((40, 20, 608, 32, "WASHINGTON TELEPHONE DIRECTORY"))
    pitch = (height - 34 - 50) / lines
    column_width = width / columns
    for column in range(columns):
        left = column * column_width + 6
        for line in range(lines):
            top = 50 + line * pitch
            indent = 8 if rng.random() < 0.3 else 0
            right = left + rng.uniform(60, column_width - 12)
            text = f"c{column + 1}l{line + 1}"
            items.append((left + indent, top, right, top + pitch * 0.83, text))
    return items


def test_six_column_directory_page_cuts_in_gutters():
    width, height = 648.0, 864.0
    items = directory()
    result = plan(page(items, width, height), width, height, max_dpi=600)
    validate_plan(result)
    tiles = result["tiles"]
    band, columns = tiles[0], tiles[1:]
    assert band["band"] and band["column"] is None and band["width"] == width
    assert band["y"] == 0 and 32 < band["height"] < 50
    assert {tile["column"] for tile in columns} == {1, 2, 3, 4, 5, 6}
    # 823 points of column under a 345-point piece limit: three pieces per column.
    assert len(tiles) == 1 + 6 * 3
    line_height = result["line_height"]
    limit = line_height * SETTINGS["max_image_side"] / SETTINGS["min_line_pixels"]
    for tile in columns:
        assert tile["height"] <= limit + 0.001
        assert not tile["edge_cut"] and not tile["small_text"]
        assert tile["layout_items"] > 0
    for column in range(1, 7):
        pieces = [tile["height"] for tile in columns if tile["column"] == column]
        assert all(height >= 0.75 * limit for height in pieces[:-1])
    for tile in tiles:
        x0, _, x1, _ = rect(tile)
        for edge in (x0, x1):
            if 0 < edge < width:
                column = round(edge / 108)
                ends = [i[2] for i in items[1:] if i[0] < column * 108]
                starts = [i[0] for i in items[1:] if i[0] > column * 108]
                assert max(ends) < edge < min(starts)
    assert sum(tile["layout_items"] for tile in tiles) == len(items)
    assert check_edges(result, [page(items, width, height)]) == []


def test_six_column_page_from_a_low_resolution_source_is_small_text():
    width, height = 648.0, 864.0
    result = plan(page(directory(), width, height), width, height, max_dpi=300)
    columns = [tile for tile in result["tiles"] if not tile["band"]]
    # A 4.6-point line at 300 dpi is 19 pixels, whatever the cut: pieces fit
    # max_image_side at native resolution instead (2400 * 72 / 300 = 576 points).
    assert all(tile["small_text"] for tile in columns)
    assert all(tile["height"] <= 576 for tile in columns)
    assert len(columns) == 12


def test_plan_is_deterministic_and_pinned():
    width, height = 648.0, 864.0
    layout = page(directory(columns=3, lines=60, seed=3), width, height)
    first = plan(layout, width, height, max_dpi=600)
    assert first == plan(copy.deepcopy(layout), width, height, max_dpi=600)
    assert plan_sha256(first) == plan_sha256(copy.deepcopy(first))
    # A change to the planner's geometry changes stored plans; bump plan_version.
    assert plan_sha256(first) == PINNED


PINNED = "48f0c62ec370c07b5b141fa3815570f1235964565cce6a80cf841bfffe7de598"


def test_no_layout_or_no_items_is_one_full_page_tile():
    for layout in (None, page([], 612, 792)):
        result = plan(layout, 612, 792)
        validate_plan(result)
        assert result["line_height"] is None
        [tile] = result["tiles"]
        assert rect(tile) == (0, 0, 612, 792)
        assert tile["small_text"] is None and tile["layout_items"] == 0
        assert not tile["band"] and tile["column"] is None
    assert plan(None, 612, 792)["layout"] is None
    assert plan(page([], 612, 792), 612, 792)["layout"]["attempt"] == "a1"


def test_one_item_and_a_single_column():
    one = plan(page([(100, 100, 300, 112, "Title")], 612, 792), 612, 792)
    assert len(one["tiles"]) == 1 and one["tiles"][0]["column"] == 1
    lines = [(72, 72 + i * 14, 540, 84 + i * 14, f"line {i}") for i in range(48)]
    single = plan(page(lines, 612, 792), 612, 792)
    validate_plan(single)
    assert {tile["column"] for tile in single["tiles"]} == {1}
    assert all(tile["x"] == 0 and tile["width"] == 612 for tile in single["tiles"])
    assert not any(tile["edge_cut"] for tile in single["tiles"])


def test_dense_lines_with_no_whitespace_are_edge_cut():
    lines = [(72, 72 + i * 10, 540, 84 + i * 10, f"line {i}") for i in range(70)]
    result = plan(page(lines, 612, 792), 612, 792, min_line_pixels=200)
    validate_plan(result)
    assert len(result["tiles"]) > 1
    assert any(tile["edge_cut"] for tile in result["tiles"])
    flagged = [tile["tile_id"] for tile in result["tiles"] if tile["edge_cut"]]
    assert check_edges(result, [page(lines, 612, 792)]) == flagged


def test_max_tiles_raises_the_piece_height_and_marks_small_text():
    width, height = 648.0, 864.0
    layout = page(directory(), width, height)
    result = plan(layout, width, height, max_dpi=600, max_tiles=13)
    validate_plan(result)
    assert len(result["tiles"]) <= 13
    assert {tile["column"] for tile in result["tiles"] if not tile["band"]} == set(range(1, 7))
    assert any(tile["small_text"] for tile in result["tiles"])
    assert not any(tile["edge_cut"] for tile in result["tiles"])


def test_max_tiles_below_the_column_count_merges_columns():
    width, height = 648.0, 864.0
    result = plan(page(directory(), width, height), width, height, max_dpi=600, max_tiles=4)
    validate_plan(result)
    assert len(result["tiles"]) <= 4
    assert not any(tile["edge_cut"] for tile in result["tiles"])
    assert plan(page(directory(), width, height), width, height, max_tiles=1)["tiles"][0][
        "width"
    ] == pytest.approx(width)


def block_column():
    """One directory column: unindented block headings over indented entries."""
    items, y = [], 40.0
    blocks = [("Embassy of the USSR—", 70), ("Embassy of Venezuela—", 70), ("Soler Juan", 0)]
    for heading, entries in blocks:
        items.append((50, y, 200, y + 8, heading))
        y += 10
        for n in range(entries):
            items.append((62, y, 220, y + 8, f"{heading[:-1]} entry {n + 1}"))
            y += 10
    return items


def test_block_headings_follow_indentation():
    items = block_column()
    result = plan(page(items, 612, 1600), 612, 1600)
    validate_plan(result)
    tiles = result["tiles"]
    assert len(tiles) > 2
    assert tiles[0]["continues_block"] is False and tiles[0]["heading"] is None
    continued = [tile for tile in tiles if tile["continues_block"]]
    assert continued
    for tile in continued:
        first = min(
            (i for i in items if tile["y"] <= (i[1] + i[3]) / 2 < tile["y"] + tile["height"]),
            key=lambda i: i[1],
        )
        expected = "Embassy of the USSR—" if "USSR" in first[4] else "Embassy of Venezuela—"
        assert tile["heading"] == expected
    for tile in tiles:
        if not tile["continues_block"] and tile["layout_items"]:
            assert tile["heading"] is None


def test_cuts_prefer_block_starts():
    items = []
    y = 40.0
    for block in range(40):
        items.append((50, y, 200, y + 8, f"Block {block}"))
        y += 10
        for n in range(3):
            items.append((62, y, 220, y + 8, f"entry {block}.{n}"))
            y += 10
    result = plan(page(items, 612, 1700), 612, 1700)
    starts = {i[1] for i in items if i[0] == 50}
    for tile in result["tiles"][1:]:
        below = min(i[1] for i in items if i[1] >= tile["y"])
        assert below in starts
        assert tile["continues_block"] is False


def test_word_layout_rebuilds_lines():
    width, height = 648.0, 864.0
    words = []
    heading, *lines = directory(columns=4, lines=100, seed=11)
    words.append(heading)
    for x0, y0, x1, y1, text in lines:
        cursor = x0
        for n, word in enumerate((text, "nw", "CO-5")):
            right = cursor + (x1 - x0) / 3 - 2
            words.append((cursor, y0, right, y1, word if n else text))
            cursor = right + 2
    result = plan(page(words, width, height, level="word"), width, height, max_dpi=600)
    validate_plan(result)
    assert result["layout"]["level"] == "word"
    assert result["tiles"][0]["band"]
    assert {tile["column"] for tile in result["tiles"][1:]} == {1, 2, 3, 4}
    assert not any(tile["edge_cut"] for tile in result["tiles"])
    text, outside = project_witness(page(words, width, height, level="word"), result)
    assert outside == 0
    assert text.split("\n")[0] == "WASHINGTON TELEPHONE DIRECTORY"
    assert text.split("\n")[1] == "c1l1 nw CO-5"


def test_two_page_spread_cuts_the_middle_gutter():
    width = 1296.0
    left = directory(columns=4, lines=90, heading=False, seed=1)
    right = [(x0 + 648, y0, x1 + 648, y1, t) for x0, y0, x1, y1, t in left]
    result = plan(page(left + right, width, 864), width, 864, max_dpi=600)
    validate_plan(result)
    assert {tile["column"] for tile in result["tiles"]} == set(range(1, 9))
    assert any(tile["x"] + tile["width"] == pytest.approx(648, abs=6) for tile in result["tiles"])


def test_right_aligned_columns_are_still_a_partition_read_left_to_right():
    rng = random.Random(5)
    items = []
    for column in range(3):
        right = (column + 1) * 200 - 8
        for line in range(60):
            items.append((right - rng.uniform(60, 180), 40 + line * 12, right, 50 + line * 12, "ע"))
    result = plan(page(items, 600, 800), 600, 800)
    validate_plan(result)
    columns = [tile["column"] for tile in result["tiles"]]
    assert columns == sorted(columns) and set(columns) == {1, 2, 3}


def test_overlapping_items_keep_the_partition():
    items = [(100, 100, 400, 140, "a"), (120, 110, 420, 150, "b"), (90, 95, 410, 145, "c")]
    result = plan(page(items, 612, 792), 612, 792)
    validate_plan(result)
    assert sum(tile["layout_items"] for tile in result["tiles"]) == 3


def random_layout(rng):
    width = rng.choice([300.0, 612.0, 595.276, 1296.0])
    height = rng.choice([400.0, 792.0, 841.89])
    items = []
    for _ in range(rng.randrange(0, 400)):
        x0, y0 = rng.uniform(0, width - 2), rng.uniform(0, height - 2)
        w = rng.choice([rng.uniform(1, 40), rng.uniform(1, width - x0)])
        h = rng.uniform(1, 20)
        items.append((x0, y0, min(width, x0 + w), min(height, y0 + h), f"w{len(items)}"))
    level = rng.choice(["line", "word"])
    return page(items, width, height, level=level), width, height


def test_partition_property_over_random_layouts():
    rng = random.Random(20261110)
    for _ in range(250):
        layout, width, height = random_layout(rng)
        options = {
            "min_line_pixels": rng.choice([8, 32, 120]),
            "max_tiles": rng.choice([1, 3, 24, 99]),
            "max_image_side": rng.choice([512, 2400]),
            "max_dpi": rng.choice([None, 150.0, 600.0]),
        }
        result = plan(layout, width, height, **options)
        validate_plan(result)
        tiles = result["tiles"]
        assert len(tiles) <= options["max_tiles"]
        assert sum(tile["layout_items"] for tile in tiles) == len(layout["items"])
        text, outside = project_witness(layout, result)
        assert outside == 0
        assert len(text.split()) == len(layout["items"])
        flagged = [tile["tile_id"] for tile in tiles if tile["edge_cut"]]
        assert check_edges(result, [layout]) == flagged


def test_project_witness_regroups_in_tile_order():
    width, height = 648.0, 864.0
    items = directory(columns=2, lines=40, seed=2)
    result = plan(page(items, width, height), width, height)
    # Rows read across the page would interleave the columns; the projection does not.
    text, outside = project_witness(page(items, width, height), result)
    lines = text.split("\n")
    assert outside == 0
    assert lines[0] == "WASHINGTON TELEPHONE DIRECTORY"
    assert lines[1:41] == [f"c1l{n}" for n in range(1, 41)]
    assert lines[41:] == [f"c2l{n}" for n in range(1, 41)]


def test_alto_words_misplaced_against_ocr_lines_are_caught():
    width, height = 648.0, 864.0
    ocr = directory(columns=6, lines=140)
    words = [(x0, y0, (x0 + x1) / 2, y1, t) for x0, y0, x1, y1, t in ocr]
    shifted = [
        (x0 + 54, y0 + 3, min(x1 + 54, width), y1 + 3, t)
        for x0, y0, x1, y1, t in words
        if x0 + 54 < width - 1
    ]
    lines = page(ocr, width, height)
    inside, total = centres_inside(page(words, width, height, "word"), lines)
    assert inside == total
    inside, total = centres_inside(page(shifted, width, height, "word"), lines)
    assert inside < 0.9 * total
    result = plan(page(shifted, width, height, "word"), width, height, max_dpi=600)
    assert check_edges(result, [lines])


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(plan_version="0.2"),
        lambda p: p.update(extra=True),
        lambda p: p["tiles"].pop(),
        lambda p: p["tiles"][0].update(tile_id="t02"),
        lambda p: p["tiles"][1].update(width=p["tiles"][1]["width"] + 1),
        lambda p: p["tiles"][1].update(x=p["tiles"][1]["x"] - 1),
        lambda p: p["tiles"][0].update(band=False),
        lambda p: p["tiles"][1].update(heading="x", continues_block=False),
        lambda p: p["tiles"][1].update(edge_cut=None),
        lambda p: p["tiles"][1].update(layout_items=-1),
        lambda p: p["tiles"][1].update(height=float("inf")),
        lambda p: p["settings"].update(max_tiles=100),
        lambda p: p["layout"].update(sha256="x"),
        lambda p: p["page_box"].update(units="pixels"),
    ],
)
def test_validate_plan_rejects_broken_plans(change):
    width, height = 648.0, 864.0
    result = plan(page(directory(columns=2, lines=40), width, height), width, height)
    validate_plan(result)
    change(result)
    with pytest.raises(ValueError, match="Tile plan invalid"):
        validate_plan(result)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_tiles": 0},
        {"max_tiles": 100},
        {"min_line_pixels": 0},
        {"max_image_side": 1.5},
        {"max_dpi": 0},
        {"layout_attempt": ""},
    ],
)
def test_plan_tiles_rejects_bad_settings(overrides):
    with pytest.raises(ValueError):
        plan(None, 612, 792, **overrides)
    with pytest.raises(ValueError):
        plan_tiles(None, page_width=0, page_height=792, **SETTINGS)
