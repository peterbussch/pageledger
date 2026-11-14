"""The vision adapter reading a page in tiles: one image and one request per planned box."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from pageledger import vision
from pageledger.adapters import AdapterFailure
from pageledger.image_evidence import validate_input_evidence, validate_tile_text
from pageledger.layout import GRID
from pageledger.tiling import plan_sha256, plan_tiles

STUB_PDFTOPPM = """\
#!{python}
import json, os, struct, sys
args = sys.argv[1:]
if args == ["-v"]:
    print("pdftoppm version stub", file=sys.stderr)
    sys.exit(0)
with open(os.environ["STUB_PDFTOPPM_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
calls = len(open(os.environ["STUB_PDFTOPPM_LOG"]).read().splitlines())
if "-W" in args:
    width, height = int(args[args.index("-W") + 1]), int(args[args.index("-H") + 1])
else:  # a whole page, -scale-to N
    width = height = int(args[args.index("-scale-to") + 1])
if calls > int(os.environ.get("STUB_PDFTOPPM_GOOD", "999")):
    width -= int(os.environ.get("STUB_PDFTOPPM_SHORT", "0"))
jpeg = (
    b"\\xff\\xd8\\xff\\xc0" + struct.pack(">H B H H B", 17, 8, height, width, 3)
    + b"\\x01\\x11\\x00\\x02\\x11\\x00\\x03\\x11\\x00"
    + b"\\xff\\xda\\x00\\x0c\\x03\\x01\\x00\\x02\\x11\\x03\\x11\\x00\\x3f\\x00\\x00\\xff\\xd9"
)
open(args[-1] + ".jpg", "wb").write(jpeg)
"""


def stub_pdftoppm(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a pdftoppm first on PATH that writes a header-only JPEG of the asked size."""
    directory.mkdir(exist_ok=True)
    script = directory / "pdftoppm"
    script.write_text(STUB_PDFTOPPM.format(python=sys.executable))
    script.chmod(0o755)
    log = directory / "pdftoppm.log"
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_PDFTOPPM_LOG", str(log))
    return log


def column_layout(columns: int = 3, lines: int = 30) -> dict:
    """Numbered lines in ``columns`` columns, each line 1/60 of the page tall."""
    width = GRID // columns
    items = []
    for column in range(columns):
        for line in range(lines):
            y0 = 20_000 + line * 30_000
            items.append(
                {
                    "box": [
                        column * width + 10_000,
                        y0,
                        (column + 1) * width - 10_000,
                        y0 + 16_000,
                    ],
                    "text": f"{column + 1}.{line + 1:02d} entry",
                }
            )
    return {"units": "page_millionths", "level": "line", "items": items, "source": {}}


def column_plan(**settings) -> dict:
    options = {"min_line_pixels": 20, "max_tiles": 24, "max_image_side": 1024, **settings}
    return plan_tiles(column_layout(), page_width=600.0, page_height=800.0, **options)


def reply(text: str, *, model: str = "served", finish: str = "stop", tokens: int = 5) -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"total_tokens": tokens},
    }


class Replies:
    """Stands in for VisionAdapter._post: answers each request in turn."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.bodies: list[dict] = []

    def __call__(self, body: bytes) -> dict:
        self.bodies.append(json.loads(body))
        answer = self.answers.pop(0)
        if isinstance(answer, AdapterFailure):
            raise answer
        return answer


@pytest.fixture
def tiled(tmp_path, monkeypatch):
    log = stub_pdftoppm(tmp_path / "bin", monkeypatch)
    source = tmp_path / "page.pdf"
    source.write_bytes(b"%PDF-1.4 stand-in")
    run = tmp_path / "run"
    run.mkdir()
    plan = column_plan()

    def read(answers, plan=plan, page_id="p1", **options):
        adapter = vision.VisionAdapter(
            base_url="http://127.0.0.1:9/v1",
            model="requested",
            max_image_side=plan["settings"]["max_image_side"],
            evidence_dir=str(run / "evidence"),
            tiles=plan,
            **options,
        )
        replies = Replies(answers)
        monkeypatch.setattr(adapter, "_post", replies)
        result = adapter.extract(
            source, page_id=page_id, page_number=1, action="transcribe_text", prompt="Read."
        )
        return result, replies

    return read, plan, run, source, log


def test_each_tile_is_its_own_image_and_request_and_the_texts_join_in_plan_order(tiled):
    read, plan, run, source, log = tiled
    count = len(plan["tiles"])
    assert count > 3
    result, replies = read([reply(f"text {n}") for n in range(count)])
    assert result.content == "\n".join(f"text {n}" for n in range(count))
    assert result.model == "served"
    assert result.usage["tokens"] == 5 * count
    evidence = result.input_evidence
    assert evidence["kind"] == "page_tiles"
    assert evidence["plan_sha256"] == plan_sha256(plan)
    assert [tile["tile_id"] for tile in evidence["tiles"]] == [t["tile_id"] for t in plan["tiles"]]
    validate_input_evidence(evidence, root=run, page_number=1)
    validate_tile_text(evidence, result.content)
    # The same prompt goes with every tile, so the page's prompt hash holds for all.
    assert {body["messages"][0]["content"][0]["text"] for body in replies.bodies} == {"Read."}
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == count
    for tile, planned, args in zip(evidence["tiles"], plan["tiles"], calls, strict=True):
        assert (run / tile["artifact"]).is_file()
        assert tile["artifact"] == f"evidence/p1-{planned['tile_id']}.jpg"
        assert {"-r", "-x", "-y", "-W", "-H", "-jpeg"} <= set(args)
        assert "-scale-to" not in args
        dpi = float(args[args.index("-r") + 1])
        assert max(tile["width"], tile["height"]) <= 1024
        crop, pixel = tile["crop"], 72 / dpi
        for start, size, key in (("x", "width", "x"), ("y", "height", "y")):
            assert abs(crop[start] - planned[key]) <= pixel
            assert abs(crop[start] + crop[size] - planned[key] - planned[size]) <= pixel


def test_a_tile_the_model_will_not_read_is_failed_and_the_page_goes_on(tiled):
    read, plan, run, _, _ = tiled
    count = len(plan["tiles"])
    answers = [reply(f"text {n}") for n in range(count)]
    answers[1] = reply("cut off", finish="length")
    result, _ = read(answers)
    tiles = result.input_evidence["tiles"]
    assert tiles[1]["outcome"] == "failed"
    assert tiles[1]["failure_code"] == "MODEL_OUTPUT_TRUNCATED"
    assert tiles[1]["text_offset"] is None
    assert "cut off" not in result.content
    assert result.content.startswith("text 0\ntext 2\n")
    validate_tile_text(result.input_evidence, result.content)


def test_an_empty_tile_is_read_as_empty_only_where_the_layout_found_nothing(tiled):
    read, plan, *_ = tiled
    answers = [reply(f"text {n}") for n in range(len(plan["tiles"]))]
    answers[0] = reply("")
    result, _ = read(answers)
    assert result.input_evidence["tiles"][0]["failure_code"] == "MODEL_EMPTY_RESPONSE"

    # The top half of one column: the tile below it holds no line.
    layout = column_layout(columns=1, lines=12)
    sparse = plan_tiles(
        layout,
        page_width=600.0,
        page_height=800.0,
        min_line_pixels=20,
        max_tiles=24,
        max_image_side=256,
    )
    blank = [n for n, tile in enumerate(sparse["tiles"]) if not tile["layout_items"]]
    assert blank
    answers = [reply("" if n in blank else f"text {n}") for n in range(len(sparse["tiles"]))]
    result, _ = read(answers, plan=sparse, page_id="p2")
    assert all(tile["outcome"] == "completed" for tile in result.input_evidence["tiles"])

    # Without a layout nothing says a tile is blank, and a page that reads empty fails.
    whole = plan_tiles(
        None,
        page_width=600.0,
        page_height=800.0,
        min_line_pixels=20,
        max_tiles=24,
        max_image_side=1024,
    )
    with pytest.raises(AdapterFailure) as caught:
        read([reply("")], plan=whole, page_id="p3")
    assert caught.value.code == "MODEL_EMPTY_RESPONSE"


def test_a_quota_failure_stops_the_page_with_the_tiles_read_so_far(tiled):
    read, plan, run, _, log = tiled
    answers = [reply("text 0"), AdapterFailure("MODEL_QUOTA", http_status=429), reply("never")]
    with pytest.raises(AdapterFailure) as caught:
        read(answers)
    assert caught.value.code == "MODEL_QUOTA"
    partial = caught.value.partial_result
    assert partial.content == "text 0"
    tiles = partial.input_evidence["tiles"]
    assert [(t["tile_id"], t["outcome"], t["failure_code"]) for t in tiles] == [
        ("t01", "completed", None),
        ("t02", "failed", "MODEL_QUOTA"),
    ]
    # Tiles never sent are neither rendered nor listed; the plan lists them all.
    assert len(log.read_text().splitlines()) == 2
    validate_input_evidence(partial.input_evidence, root=run)
    validate_tile_text(partial.input_evidence, partial.content)


def test_tiles_served_by_different_models_fail_the_page(tiled):
    read, plan, _, _, _ = tiled
    answers = [reply(f"text {n}") for n in range(len(plan["tiles"]))]
    answers[2] = reply("text 2", model="other")
    with pytest.raises(AdapterFailure) as caught:
        read(answers)
    assert caught.value.code == "MODEL_INVALID_RESPONSE"


def test_a_render_of_the_wrong_size_is_a_render_error(tiled, monkeypatch):
    read, *_ = tiled
    monkeypatch.setenv("STUB_PDFTOPPM_SHORT", "5")
    monkeypatch.setenv("STUB_PDFTOPPM_GOOD", "0")
    with pytest.raises(AdapterFailure) as caught:
        read([])
    assert caught.value.code == "IMAGE_RENDER_ERROR"
    assert caught.value.partial_result is None


def test_a_render_failure_after_paid_tiles_keeps_them_as_a_partial(tiled, monkeypatch):
    read, _, run, _, _ = tiled
    monkeypatch.setenv("STUB_PDFTOPPM_SHORT", "5")
    monkeypatch.setenv("STUB_PDFTOPPM_GOOD", "2")
    with pytest.raises(AdapterFailure) as caught:
        read([reply("text 0"), reply("text 1")])
    assert caught.value.code == "IMAGE_RENDER_ERROR"
    partial = caught.value.partial_result
    assert [tile["tile_id"] for tile in partial.input_evidence["tiles"]] == ["t01", "t02"]
    assert partial.content == "text 0\ntext 1"
    validate_input_evidence(partial.input_evidence, root=run)


def test_text_that_does_not_match_its_tiles_is_refused(tiled):
    read, plan, *_ = tiled
    result, _ = read([reply(f"text {n}") for n in range(len(plan["tiles"]))])
    for changed in (result.content + "x", result.content.replace("text 1", "text 9", 1)):
        with pytest.raises(ValueError, match="Image evidence invalid"):
            validate_tile_text(result.input_evidence, changed)


def test_tiles_need_evidence_and_the_planned_image_size(tmp_path):
    plan = column_plan()
    base = {"base_url": "http://127.0.0.1:9/v1", "model": "m", "tiles": plan}
    with pytest.raises(ValueError, match="evidence_dir"):
        vision.VisionAdapter(**base, max_image_side=1024)
    evidence = str(tmp_path / "evidence")
    with pytest.raises(ValueError, match="max_image_side"):
        vision.VisionAdapter(**base, max_image_side=2048, evidence_dir=evidence)
    broken = {**plan, "tiles": plan["tiles"][1:]}
    with pytest.raises(ValueError, match="vision tiles"):
        vision.VisionAdapter(
            **{**base, "tiles": broken}, max_image_side=1024, evidence_dir=evidence
        )


# -- The crop conversion, checked on the real renderer ----------------------------

CORNERS = b"1 0 0 rg 0 80 20 20 re f 0 1 0 rg 180 80 20 20 re f 0 0 1 rg 0 0 20 20 re f 0 0 0 rg 180 0 20 20 re f"
COLOURS = {(255, 0, 0): "red", (0, 255, 0): "green", (0, 0, 255): "blue", (0, 0, 0): "black"}


def corner_pdf(path: Path, *, rotate: int = 0, origin: int = 0, cropbox: bool = False) -> Path:
    """A 200 x 100 pt page with a 20 pt square in each corner: red top left, green
    top right, blue bottom left, black bottom right (before /Rotate)."""
    o = origin
    content = b"1 0 0 1 %d %d cm " % (o, o) + CORNERS
    box = b"/MediaBox [%d %d %d %d]" % (o, o, o + 200, o + 100)
    if cropbox:
        box += b" /CropBox [%d %d %d %d]" % (o + 30, o + 10, o + 150, o + 90)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R " + box + b" /Rotate %d /Contents 4 0 R >>" % rotate,
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    data += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    data += b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path.write_bytes(bytes(data))
    return path


def _ppm(path: Path):
    data = path.read_bytes()
    _, width, height, _, pixels = data.split(maxsplit=4)
    width, height = int(width), int(height)

    def colour(x, y):
        index = (y * width + x) * 3
        return COLOURS.get(tuple(pixels[index : index + 3]), "other")

    return width, height, colour


real_poppler = pytest.mark.skipif(
    shutil.which("pdftoppm") is None or shutil.which("pdfinfo") is None,
    reason="needs Poppler (pdftoppm and pdfinfo)",
)


@real_poppler
@pytest.mark.parametrize(
    "rotate, page, corners",
    [
        (0, (200, 100), ("red", "green", "blue", "black")),
        (90, (100, 200), ("blue", "red", "black", "green")),
        (180, (200, 100), ("black", "blue", "green", "red")),
        (270, (100, 200), ("green", "black", "red", "blue")),
    ],
)
@pytest.mark.parametrize("origin", [0, 15])
@pytest.mark.parametrize("dpi", [None, 100.5])
def test_poppler_renders_each_planned_box_at_the_page_corners(
    tmp_path, monkeypatch, rotate, page, corners, origin, dpi
):
    """Tile boxes are in points of the page as rendered (media box, after /Rotate),
    from its top-left corner; ``-r -x -y -W -H`` crops exactly that box."""
    from pageledger.alto_adapter import rendered_page_size

    source = corner_pdf(tmp_path / "page.pdf", rotate=rotate, origin=origin)
    assert rendered_page_size(source, 1) == page
    _spy(tmp_path / "bin", monkeypatch)
    adapter = vision.VisionAdapter(base_url="http://127.0.0.1:9/v1", model="m", max_image_side=256)
    width, height = page
    boxes = [(0, 0), (width - 20, 0), (0, height - 20), (width - 20, height - 20)]
    for (x, y), expected in zip(boxes, corners, strict=True):
        tile = {"x": x, "y": y, "width": 20, "height": 20, "max_dpi": dpi}
        jpeg, used, _, crop = adapter._render_tile(source, 1, tile)
        w, h, colour = _ppm(tmp_path / "bin" / "out.ppm")
        assert (w, h) == vision.jpeg_dimensions(jpeg)
        assert colour(w // 2, h // 2) == expected
        assert {colour(2, 2), colour(w - 3, h - 3)} == {expected}
        assert abs(crop["x"] - x) <= 72 / used and abs(crop["y"] - y) <= 72 / used


@real_poppler
def test_poppler_renders_the_media_box_even_when_a_crop_box_is_smaller(tmp_path, monkeypatch):
    from pageledger.alto_adapter import rendered_page_size

    source = corner_pdf(tmp_path / "page.pdf", cropbox=True)
    assert rendered_page_size(source, 1) == (200, 100)
    _spy(tmp_path / "bin", monkeypatch)
    adapter = vision.VisionAdapter(base_url="http://127.0.0.1:9/v1", model="m", max_image_side=256)
    adapter._render_tile(source, 1, {"x": 180, "y": 80, "width": 20, "height": 20, "max_dpi": 72})
    w, h, colour = _ppm(tmp_path / "bin" / "out.ppm")
    assert (w, h) == (20, 20) and colour(10, 10) == "black"


def _spy(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real pdftoppm as asked, then again without -jpeg so the test can read
    the very crop's pixels (``out.ppm`` in ``directory``)."""
    real = shutil.which("pdftoppm")
    assert real is not None
    directory.mkdir()
    real_run = vision._run_ocr_command

    def run(argv, **kwargs):
        real_run(argv, **kwargs)
        plain = [a for a in argv[1:-1] if a not in ("-jpeg", "-jpegopt")]
        plain = [a for a in plain if not a.startswith("quality=")]
        subprocess.run([real, *plain, str(directory / "out")], check=True)

    monkeypatch.setattr(vision, "_run_ocr_command", run)


def test_stub_jpeg_header_matches_the_reader():
    width, height = 7, 5
    jpeg = (
        b"\xff\xd8\xff\xc0"
        + struct.pack(">H B H H B", 17, 8, height, width, 3)
        + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        + b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00\x00\xff\xd9"
    )
    assert vision.jpeg_dimensions(jpeg) == (width, height)


def test_an_unusable_usage_number_on_one_tile_keeps_every_paid_tile(tiled):
    read, plan, run, _, _ = tiled
    count = len(plan["tiles"])
    answers = [reply(f"text {n}") for n in range(count)]
    # A JSON integer too large for a float, as a broken gateway might report.
    answers[-1]["usage"] = {"total_tokens": 5, "cost_usd": 10**400}
    result, _ = read(answers)
    assert result.content.startswith("text 0")
    assert result.usage["tokens"] == 5 * count and result.usage["cost_usd"] is None
    assert len(result.input_evidence["tiles"]) == count


def test_a_prompt_too_long_for_any_tile_is_refused_before_anything_is_paid(tiled, monkeypatch):
    _, plan, run, source, log = tiled
    adapter = vision.VisionAdapter(
        base_url="http://127.0.0.1:9/v1",
        model="requested",
        max_image_side=plan["settings"]["max_image_side"],
        evidence_dir=str(run / "evidence"),
        tiles=plan,
    )
    monkeypatch.setattr(adapter, "_post", lambda body: pytest.fail("nothing may be sent"))
    with pytest.raises(ValueError, match="prompt is too long"):
        adapter.extract(
            source, page_id="p1", page_number=1, action="transcribe_text", prompt="x" * 70_000
        )
    assert not log.exists() or not log.read_text()
