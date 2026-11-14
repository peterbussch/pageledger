"""A document job that reads dense pages in tiles, end to end, without a model.

The source is a generated two-page PDF of three columns of numbered lines. A stub
literal engine reads it with a layout; the real vision adapter reads the planned
tiles through a stub ``pdftoppm`` (which logs each crop) and a stub endpoint that
answers with the lines inside the crop it was sent.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from test_vision_tiles import stub_pdftoppm

from pageledger import adapters, runner, vision
from pageledger.adapters import ExtractionResult, TextAdapter
from pageledger.checkpoint import read_record, write_record
from pageledger.export import export_job
from pageledger.layout import GRID
from pageledger.processing import (
    adjudicate_job,
    adjudication_packets,
    process,
    resume_job,
    verify_job,
)
from pageledger.tiling import project_witness

pytest.importorskip("pypdf")  # a document job reads the PDF's page list with it
pytestmark = pytest.mark.skipif(shutil.which("pdfinfo") is None, reason="needs pdfinfo")

WIDTH, HEIGHT = 612, 792
COLUMNS, LINES = 3, 40
MISREAD = ("2.05 entry", "2.05 entrv")


def lines(page: int) -> list[tuple[float, float, float, float, str]]:
    """Each printed line's box in points from the top-left corner, and its text."""
    out = []
    for column in range(COLUMNS):
        for line in range(LINES):
            x0, y0 = 36 + column * 190, 60 + line * 15
            out.append((x0, y0, x0 + 150, y0 + 10, f"{column + 1}.{line + 1:02d} entry p{page}"))
    return out


def columns_pdf(path: Path, pages: int = 2) -> Path:
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    for page in range(1, pages + 1):
        content = (
            b"BT /F1 9 Tf "
            + b" ".join(
                b"1 0 0 1 %d %d Tm (%s) Tj" % (x0, HEIGHT - y1, text.encode())
                for x0, _, _, y1, text in lines(page)
            )
            + b" ET"
        )
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
            b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> "
            b">> >> >>" % (WIDTH, HEIGHT, len(objects) + 2)
        )
        kids.append(len(objects))
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        pages,
    )
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


class Literal(TextAdapter):
    """The OCR reads every line, with its box. The text layer reads nothing, unless
    ``text_layer`` is set: then it is an earlier OCR (as ALTO is) whose boxes lie
    that many points below the print."""

    name = "literal"
    stage = "local_ocr"
    input_types = ("pdf",)
    text_layer: float | None = None

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.capabilities = ("local", "layout")

    def page_count(self, source):
        return 2

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        printed = lines(page_number)
        shift = 0 if self.stage == "local_ocr" else self.text_layer
        if shift is None:
            return ExtractionResult("", "text", None, "stub-text", [], {"pages": 1})
        layout = {
            "units": "page_millionths",
            "level": "line",
            "items": [
                {
                    "box": [
                        round(x0 * GRID / WIDTH),
                        round((y0 + shift) * GRID / HEIGHT),
                        round(x1 * GRID / WIDTH),
                        round((y1 + shift) * GRID / HEIGHT),
                    ],
                    "text": text,
                }
                for x0, y0, x1, y1, text in printed
            ],
            "source": {},
        }
        return ExtractionResult(
            "\n".join(text for *_, text in printed),
            "text",
            None,
            f"stub-{self.stage}",
            [] if self.stage == "local_ocr" else ["ocr_text_layer"],
            {"pages": 1},
            layout=layout,
        )


@pytest.fixture
def tiled_job(tmp_path, monkeypatch):
    source = columns_pdf(tmp_path / "columns.pdf")
    log = stub_pdftoppm(tmp_path / "bin", monkeypatch)
    real = adapters.load_adapter

    def load(name, options=None):
        if name in {"vision", "pdf_text"}:
            return real(name, options)
        return Literal("local_ocr" if name == "stub_ocr" else "local_text")

    monkeypatch.setattr(runner, "load_adapter", load)

    def answer(self, body):
        args = json.loads(log.read_text().splitlines()[-1])
        if "-r" in args:
            dpi = float(args[args.index("-r") + 1])
            x, y, w, h = (
                int(args[args.index(flag) + 1]) * 72 / dpi for flag in ("-x", "-y", "-W", "-H")
            )
        else:  # the whole page
            x, y, w, h = 0, 0, WIDTH, HEIGHT
        page = int(args[args.index("-f") + 1])
        inside = [
            text.replace(*MISREAD) if text.startswith(MISREAD[0]) else text
            for x0, y0, x1, y1, text in lines(page)
            if x <= (x0 + x1) / 2 < x + w and y <= (y0 + y1) / 2 < y + h
        ]
        return {
            "model": "stub-reader",
            "choices": [{"message": {"content": "\n".join(inside)}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 3},
        }

    monkeypatch.setattr(vision.VisionAdapter, "_post", answer)
    config = tmp_path / "config.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "taxonomy": {"page_types": {"prose": {"default_action": "transcribe_text"}}},
                "processing": {
                    "local_text": {"adapter": "stub_text"},
                    "local_ocr": {"adapter": "stub_ocr"},
                    "image": {
                        "adapter": "vision",
                        "adapter_options": {
                            "base_url": "http://127.0.0.1:9/v1",
                            "model": "stub-reader",
                            "max_image_side": 1024,
                        },
                    },
                    "escalate_on": ["hold", "always"],
                    "contest": {"rules": "latin-print-0.1"},
                    "tiling": {"stages": ["image"], "layout": "local_ocr"},
                    "limits": {
                        "max_image_pages": 4,
                        "max_attempt_pages": 20,
                        "max_image_requests": 12,
                    },
                },
            }
        )
    )
    return source, config, tmp_path / "job", log


def test_a_dense_page_is_read_in_tiles_contested_settled_moved_and_exported(tiled_job, tmp_path):
    source, config, root, log = tiled_job
    first = process(source=source, config_path=config, out_dir=root)
    # Page 1 takes nine tiles; page 2's nine more would pass max_image_requests (12).
    assert first["status"] == "paused_budget"
    job = read_record(root / "job.json")
    assert job["halt_reason"] == "budget:max_image_requests"
    assert job["usage"]["image_requests"] == 9
    assert len(log.read_text().splitlines()) == 9

    resumed = resume_job(root, raise_limits={"max_image_requests": 30})
    assert resumed["status"] == "completed"
    job = read_record(root / "job.json")
    assert job["usage"]["image_requests"] == 18
    assert job["usage"]["image_calls"] == 2
    tiled = [stage for stage in job["stages"] if stage["stage"] == "image"]
    assert [stage["tiles"]["count"] for stage in tiled] == [9, 9]

    page = job["pages"][0]
    reading = next(a for a in page["attempts"] if a["stage"] == "image")
    assert reading["input_evidence"]["kind"] == "page_tiles"
    assert reading["tiling"]["failed"] == [] and reading["tiling"]["edge_cut"] == []
    assert not {"tile_failed", "tile_edge_cut"} & set(reading["warnings"])
    ocr = next(a for a in page["attempts"] if a["stage"] == "local_ocr")
    assert reading["tiling"]["layout_attempt"] == ocr["attempt_id"]
    assert ocr["layout_sha256"]

    # The witness is the OCR regrouped tile by tile, so the only span is the misread.
    contested = page["contested"]
    projected = (root / contested["witness_projection"]["artifact"]).read_text()
    record = json.loads((root / contested["artifact"]).read_text())
    assert record["witness"]["projection"]["outside_items"] == 0
    assert [(s["base"], s["witness"]) for s in record["spans"]] == [("entrv", "entry")]
    assert projected.splitlines()[:2] == ["1.01 entry p1", "1.02 entry p1"]
    assert page["disposition"] == "contested"
    assert verify_job(root)["status"] == "pass"

    packets = tmp_path / "packets"
    adjudication_packets(root, packets)
    packet = json.loads((packets / f"{page['page_id']}.json").read_text())
    assert packet["image"] is None
    [span] = packet["spans"]
    [image] = packet["images"]
    assert image["tile_id"] == span["tile_id"] == "t04"
    assert (packets / image["path"]).read_bytes() == (
        root / reading["run_path"] / f"evidence/{page['page_id']}-t04.jpg"
    ).read_bytes()
    answers = tmp_path / "answers"
    answers.mkdir()
    (answers / f"{page['page_id']}.decisions.json").write_text(
        json.dumps(
            {
                "page_id": page["page_id"],
                "contested_sha256": packet["contested_sha256"],
                "decisions": [{"span_id": span["span_id"], "text": "entry", "confidence": "high"}],
            }
        )
    )
    adjudicate_job(root, answers, reviewer="agent:test")
    job = read_record(root / "job.json")
    assert job["pages"][0]["disposition"] == "adjudicated_text"
    assert job["pages"][0]["adjudications"][0]["image_sha256"] is None
    assert verify_job(root)["status"] == "pass"

    _validate(root / "job.json", "job.schema.json")
    _validate(root / "document.json", "document.schema.json")
    _validate(root / contested["artifact"], "contested.schema.json")
    _validate(packets / f"{page['page_id']}.json", "adjudication-packet.schema.json")
    stage_run = root / reading["run_path"]
    for line in (stage_run / "provenance.jsonl").read_text().splitlines():
        _validate_value(json.loads(line), "provenance-line.schema.json")
    for path in (stage_run / ".checkpoint/pages").glob("*.json"):
        _validate(path, "checkpoint-page.schema.json")

    moved = tmp_path / "moved"
    shutil.copytree(root, moved)
    result = verify_job(moved)
    assert result["status"] == "pass", result
    assert export_job(moved, tmp_path / "book.txt", format="txt") >= 1
    assert "2.05 entry p1" in (tmp_path / "book.txt").read_text()


SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _validate_value(value, schema: str) -> None:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    def retrieve(uri: str):
        name = uri.rsplit("/", 1)[-1]
        return Resource.from_contents(json.loads((SCHEMAS / name).read_text()))

    registry = Registry(retrieve=retrieve)
    validator = Draft202012Validator(json.loads((SCHEMAS / schema).read_text()), registry=registry)
    validator.validate(value)


def _validate(path: Path, schema: str) -> None:
    _validate_value(json.loads(path.read_text()), schema)


def _completed(tiled_job) -> tuple[Path, dict]:
    source, config, root, _ = tiled_job
    data = yaml.safe_load(config.read_text())
    data["processing"]["limits"]["max_image_requests"] = 30
    config.write_text(yaml.safe_dump(data))
    assert process(source=source, config_path=config, out_dir=root)["status"] == "completed"
    assert verify_job(root)["status"] == "pass"
    job = read_record(root / "job.json")
    return root, job


def test_a_batch_of_tiled_pages_counts_every_page_s_tiles_against_the_limit(tiled_job):
    source, config, root, log = tiled_job
    data = yaml.safe_load(config.read_text())
    data["processing"]["batch"] = {"pages": 2, "concurrent": 1}
    config.write_text(yaml.safe_dump(data))

    # Both pages fit one batch, but their 18 tiles do not fit 12 requests: only page 1 starts.
    assert process(source=source, config_path=config, out_dir=root)["status"] == "paused_budget"
    job = read_record(root / "job.json")
    assert job["halt_reason"] == "budget:max_image_requests"
    assert job["usage"]["image_requests"] == 9
    assert len(log.read_text().splitlines()) == 9

    # Exactly enough for page 2's nine tiles.
    resumed = resume_job(root, raise_limits={"max_image_requests": 18})
    assert resumed["status"] == "completed"
    job = read_record(root / "job.json")
    assert job["usage"]["image_requests"] == 18
    assert [stage["tiles"]["count"] for stage in job["stages"] if stage["stage"] == "image"] == [
        9,
        9,
    ]
    assert verify_job(root)["status"] == "pass"


def test_a_changed_tile_image_fails_verification(tiled_job):
    root, job = _completed(tiled_job)
    reading = next(a for a in job["pages"][0]["attempts"] if a["stage"] == "image")
    tile = root / reading["run_path"] / reading["input_evidence"]["tiles"][1]["artifact"]
    tile.write_bytes(tile.read_bytes()[:-2] + b"\x00\xff\xd9")
    result = verify_job(root)
    assert result["status"] == "fail"
    assert "tile t02: artifact bytes or SHA-256 changed" in result["error"]


def test_a_changed_plan_fails_verification(tiled_job):
    root, job = _completed(tiled_job)
    stage = next(s for s in job["stages"] if s["stage"] == "image")
    path = root / stage["config_path"]
    config = yaml.safe_load(path.read_text())
    config["run"]["adapter_options"]["tiles"]["tiles"][0]["heading"] = "edited"
    path.write_text(yaml.safe_dump(config))
    result = verify_job(root)
    assert result["status"] == "fail"
    assert "attempt configuration changed" in result["error"]


def test_a_plan_the_retained_layout_no_longer_gives_is_refused(tiled_job, monkeypatch):
    import pageledger.processing as processing

    root, _ = _completed(tiled_job)
    planned = processing.plan_tiles

    def another_version(layout, **settings):
        plan = planned(layout, **settings)
        plan["tiles"][0]["edge_cut"] = not plan["tiles"][0]["edge_cut"]
        return plan

    monkeypatch.setattr(processing, "plan_tiles", another_version)
    result = verify_job(root)
    assert result["status"] == "fail"
    assert "cannot be planned again from the retained layout" in result["error"]


def test_a_layout_that_no_longer_hashes_to_its_record_fails_verification(tiled_job):
    root, job = _completed(tiled_job)
    ocr = next(a for a in job["pages"][0]["attempts"] if a["stage"] == "local_ocr")
    path = root / ocr["run_path"] / ".checkpoint/pages" / f"{ocr['page_id']}.json"
    record = read_record(path)
    record["result"]["layout"]["items"][0]["box"][2] += 1
    write_record(path, record)
    result = verify_job(root)
    assert result["status"] == "fail"
    assert "no longer matches its provenance hash" in result["error"]


def test_a_failed_tile_holds_the_page_and_names_the_tile(tiled_job, monkeypatch):
    original = vision.VisionAdapter._post
    calls = []

    def truncate_third(self, body):
        calls.append(1)
        reply = original(self, body)
        if len(calls) == 3:
            reply["choices"][0]["finish_reason"] = "length"
        return reply

    monkeypatch.setattr(vision.VisionAdapter, "_post", truncate_third)
    root, job = _completed(tiled_job)
    page = job["pages"][0]
    reading = next(a for a in page["attempts"] if a["stage"] == "image")
    assert reading["tiling"]["failed"] == ["t03"]
    assert "tile_failed" in reading["warnings"]
    assert "coverage_defect" in page["review_reasons"]
    report = (root / "report.md").read_text()
    assert "t03" in report


def _timing_out(monkeypatch, *calls_that_fail):
    from pageledger.adapters import AdapterFailure

    original = vision.VisionAdapter._post
    calls = []

    def post(self, body):
        calls.append(1)
        if len(calls) in calls_that_fail:
            raise AdapterFailure("MODEL_TIMEOUT")
        return original(self, body)

    monkeypatch.setattr(vision.VisionAdapter, "_post", post)


def test_a_tile_that_times_out_holds_its_page_with_the_paid_tiles_and_the_job_goes_on(
    tiled_job, monkeypatch
):
    _timing_out(monkeypatch, 3)
    root, job = _completed(tiled_job)
    assert job["usage"]["image_requests"] == 3 + 9
    first, second = job["pages"]
    reading = next(a for a in first["attempts"] if a["stage"] == "image")
    assert reading["outcome"] == "failed"
    assert [t["outcome"] for t in reading["input_evidence"]["tiles"]] == [
        "completed",
        "completed",
        "failed",
    ]
    assert reading["tiling"]["sent"] == 3 and reading["tiling"]["failed"] == ["t03"]
    # The paid tiles stay as evidence; the partial reading is never selected.
    assert (root / reading["raw_artifact"]).read_text().count("entry p1") > 0
    assert first["disposition"] == "provider_failure"
    assert first["next_action"] == "review"
    assert first["selected_attempt"] != reading["attempt_id"]
    assert [s["stage_id"] for s in job["stages"] if s["stage"] == "image"] == [
        "image-0001",
        "image-0002",
    ]
    assert second["disposition"] != "provider_failure"
    report = (root / "report.md").read_text()
    assert "tiles: 9 planned, 3 sent and paid for, 2 read, failed: t03" in report
    assert verify_job(root)["status"] == "pass"
    # Nothing is bought again without the user: resuming sends no tile.
    rendered = len(tiled_job[3].read_text().splitlines())
    resume_job(root)
    assert len(tiled_job[3].read_text().splitlines()) == rendered


def test_a_tiled_page_whose_first_tile_times_out_is_read_once_more(tiled_job, monkeypatch):
    _timing_out(monkeypatch, 1)
    root, job = _completed(tiled_job)
    stages = [s["stage_id"] for s in job["stages"] if s["stage"] == "image"]
    assert stages == ["image-0001", "image-0001-retry-1", "image-0002"]
    assert job["usage"]["image_requests"] == 1 + 9 + 9
    assert job["pages"][0]["selected_attempt"].startswith("image-0001-retry-1")
    assert verify_job(root)["status"] == "pass"


def test_a_retried_tiled_page_reads_its_plan_again_and_pays_for_every_tile(tiled_job, monkeypatch):
    from pageledger.adapters import AdapterFailure

    original = vision.VisionAdapter.extract
    calls = []

    def fail_first(self, source, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise AdapterFailure("MODEL_TIMEOUT")
        return original(self, source, **kwargs)

    monkeypatch.setattr(vision.VisionAdapter, "extract", fail_first)
    root, job = _completed(tiled_job)
    first, retry = [s for s in job["stages"] if s["stage"] == "image"][:2]
    assert retry["stage_id"] == f"{first['stage_id']}-retry-1"
    assert retry["tiles"] == first["tiles"]
    # The failed request counts once, the retry once per tile, and page 2 the same.
    assert job["usage"]["image_requests"] == 1 + 9 + 9
    assert job["pages"][0]["attempts"][-1]["attempt_id"].startswith(retry["stage_id"])


def test_projection_matches_the_reading_order_of_the_tiles():
    layout = Literal("local_ocr").extract(Path("x"), page_id="p", page_number=1, action="t").layout
    from pageledger.tiling import plan_tiles

    plan = plan_tiles(
        layout,
        page_width=WIDTH,
        page_height=HEIGHT,
        min_line_pixels=32,
        max_tiles=24,
        max_image_side=1024,
    )
    text, outside = project_witness(layout, plan)
    assert outside == 0
    assert text.splitlines() == [line[-1] for line in lines(1)]


@pytest.mark.parametrize("shift, rejected", [(0.0, False), (7.0, True)])
def test_an_earlier_ocr_layout_is_cut_along_only_where_the_jobs_ocr_confirms_it(
    tiled_job, monkeypatch, shift, rejected
):
    monkeypatch.setattr(Literal, "text_layer", shift)
    source, config, root, _ = tiled_job
    data = yaml.safe_load(config.read_text())
    data["processing"]["tiling"]["layout"] = "local_text"
    data["processing"]["limits"]["max_image_requests"] = 30
    config.write_text(yaml.safe_dump(data))
    assert process(source=source, config_path=config, out_dir=root, pages="1")["status"] == (
        "completed"
    )
    page = read_record(root / "job.json")["pages"][0]
    reading = next(a for a in page["attempts"] if a["stage"] == "image")
    cut_along = reading["tiling"]["layout_attempt"]
    assert cut_along.startswith("local_ocr" if rejected else "local_text")
    assert ("alto_layout_rejected" in reading["warnings"]) is rejected
    # Boxes set aside do not make the cuts look bad.
    assert reading["tiling"]["edge_cut"] == []
    assert verify_job(root)["status"] == "pass"


def test_tiling_needs_a_request_limit_and_capable_adapters(tiled_job):
    source, config, root, _ = tiled_job
    data = yaml.safe_load(config.read_text())
    del data["processing"]["limits"]["max_image_requests"]
    config.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="max_image_requests"):
        process(source=source, config_path=config, out_dir=root)
    data["processing"]["limits"]["max_image_requests"] = 12
    data["processing"]["image"]["adapter_options"]["tiles"] = {}
    config.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="processing owns the adapter tiles"):
        process(source=source, config_path=config, out_dir=root)
    del data["processing"]["image"]["adapter_options"]["tiles"]
    data["processing"]["tiling"]["layout"] = "local_text"
    data["processing"]["local_text"]["adapter"] = "pdf_text"
    config.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="reports no layout"):
        process(source=source, config_path=config, out_dir=root)


def _policy(**changes):
    from pageledger.processing_config import processing_config

    processing = {
        "local_ocr": {"adapter": "rapidocr"},
        "image": {"adapter": "vision"},
        "tiling": {"stages": ["image"], "layout": "local_ocr"},
        "limits": {"max_image_pages": 4, "max_image_requests": 40},
        **changes,
    }
    return processing_config({"processing": processing}, pdf=True)


def test_tiling_settings_are_checked_and_completed_with_defaults():
    policy = _policy()
    assert policy["tiling"] == {
        "stages": ["image"],
        "layout": "local_ocr",
        "min_line_pixels": 32,
        "max_tiles": 24,
    }
    assert policy["limits"]["max_image_requests"] == 40
    untiled = {"tiling": None, "limits": {"max_image_pages": 4}}
    assert "tiling" not in _policy(**untiled)
    assert "max_image_requests" not in _policy(**untiled)["limits"]
    for tiling, message in (
        ({"stages": ["second_opinion"], "layout": "local_ocr"}, "configured image stages"),
        ({"stages": ["image"], "layout": "image"}, "tiling.layout"),
        ({"stages": ["image"], "layout": "local_ocr", "max_tiles": 100}, "max_tiles"),
        ({"stages": ["image"], "layout": "local_ocr", "min_line_pixels": 0}, "min_line_pixels"),
        ({"stages": ["image"], "layout": "local_ocr", "grid": True}, "takes stages"),
    ):
        with pytest.raises(ValueError, match=message):
            _policy(tiling=tiling)
    with pytest.raises(ValueError, match="needs processing.tiling"):
        _policy(tiling=None)
    with pytest.raises(ValueError, match="max_image_requests"):
        _policy(limits={"max_image_pages": 4})
