from __future__ import annotations

import copy
import hashlib

import pytest

from pageledger.layout import GRID, grid_box, layout_sha256, validate_layout


def layout(items=None, level="line", source=None):
    return {
        "units": "page_millionths",
        "level": level,
        "items": [{"box": [100, 200, 5000, 900], "text": "Smith John"}] if items is None else items,
        "source": {"engine": "test"} if source is None else source,
    }


def test_valid_layouts_pass():
    validate_layout(layout())
    validate_layout(layout(items=[], level="word", source={}))
    validate_layout(layout(items=[{"box": [0, 0, GRID, GRID], "text": ""}]))


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(units="page_fraction"),
        lambda v: v.update(level="block"),
        lambda v: v.update(extra=1),
        lambda v: v.pop("source"),
        lambda v: v.update(source=[]),
        lambda v: v.update(source={"x": float("nan")}),
        lambda v: v.update(items={}),
        lambda v: v["items"][0].update(conf=90),
        lambda v: v["items"][0].update(text=None),
        lambda v: v["items"][0].update(box=[0.1, 0.2, 0.3, 0.4]),
        lambda v: v["items"][0].update(box=[1, 2, 3]),
        lambda v: v["items"][0].update(box=[True, 0, 3, 4]),
        lambda v: v["items"][0].update(box=[5, 0, 5, 4]),
        lambda v: v["items"][0].update(box=[0, 4, 5, 3]),
        lambda v: v["items"][0].update(box=[-1, 0, 5, 4]),
        lambda v: v["items"][0].update(box=[0, 0, GRID + 1, 4]),
    ],
)
def test_invalid_layouts_fail(change):
    value = layout()
    change(value)
    with pytest.raises(ValueError, match="Layout invalid"):
        validate_layout(value)


def test_digest_is_canonical_json():
    value = layout(source={"file": "0251.xml", "page_width": 2318.5, "name": "Сов"})
    expected = hashlib.sha256(
        (
            '{"items":[{"box":[100,200,5000,900],"text":"Smith John"}],"level":"line",'
            '"source":{"file":"0251.xml","name":"Сов","page_width":2318.5},'
            '"units":"page_millionths"}'
        ).encode()
    ).hexdigest()
    assert layout_sha256(value) == expected
    reordered = dict(reversed(list(value.items())))
    assert layout_sha256(reordered) == expected


def test_digest_changes_with_any_coordinate_or_text_or_order():
    base = layout(items=[{"box": [1, 2, 3, 4], "text": "a"}, {"box": [5, 6, 7, 8], "text": "b"}])
    digests = {layout_sha256(base)}
    for change in (
        lambda v: v["items"][0]["box"].__setitem__(2, 4),
        lambda v: v["items"][1].update(text="B"),
        lambda v: v["items"].reverse(),
        lambda v: v.update(level="word"),
    ):
        value = copy.deepcopy(base)
        change(value)
        digests.add(layout_sha256(value))
    assert len(digests) == 5


def test_grid_box_scales_rounds_and_clamps():
    assert grid_box(0, 0, 1600, 1079, 1600, 1079) == [0, 0, GRID, GRID]
    assert grid_box(400, 100.4, 800, 539.5, 1600, 1079) == [250000, 93049, 500000, 500000]
    assert grid_box(-3, -1, 1603, 1080, 1600, 1079) == [0, 0, GRID, GRID]
    with pytest.raises(ValueError):
        grid_box(0, 0, 1, 1, 0, 10)
