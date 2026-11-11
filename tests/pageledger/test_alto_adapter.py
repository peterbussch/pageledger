"""The alto adapter: an institution's OCR read as a page's inherited text layer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import sys
import zlib

import pytest
import yaml

from pageledger import adapters
from pageledger.alto_adapter import (
    MAX_BYTES,
    AltoAdapter,
    AltoInvalid,
    AltoPage,
    AltoWord,
    alto_name,
    alto_path,
    confidence_detail,
    page_layout,
    page_text,
    parse_alto,
    rendered_page_size,
)
from pageledger.checkpoint import read_record
from pageledger.layout import GRID, layout_sha256
from pageledger.processing import process, verify_job
from pageledger.runner import run
from pageledger.verify import verify_run

pypdf = pytest.importorskip("pypdf")
needs_pdfinfo = pytest.mark.skipif(shutil.which("pdfinfo") is None, reason="needs Poppler")

V2 = "http://www.loc.gov/standards/alto/ns-v2#"
V4 = "http://www.loc.gov/standards/alto/ns-v4#"
V2_SOFTWARE = (
    "<OCRProcessing ID='OCR_0'><ocrProcessingStep><processingSoftware>"
    "<softwareCreator>ABBYY</softwareCreator><softwareName>ABBYY FineReader</softwareName>"
    "<softwareVersion>8.0</softwareVersion></processingSoftware></ocrProcessingStep>"
    "</OCRProcessing>"
)
V4_SOFTWARE = (
    "<Processing ID='P1'><processingSoftware><softwareName>Tesseract</softwareName>"
    "<softwareVersion>5.3.0</softwareVersion></processingSoftware></Processing>"
)


def _string(text, x, y, w, h, extra=""):
    return f'<String CONTENT="{text}" HPOS="{x}" VPOS="{y}" WIDTH="{w}" HEIGHT="{h}" {extra}/>'


def alto(
    lines,
    *,
    ns=V4,
    unit="pixel",
    width=1000,
    height=1400,
    software=V4_SOFTWARE,
    pages=1,
    head="",
):
    body = "".join(f"<TextLine>{'<SP/>'.join(line)}</TextLine>" for line in lines)
    size = "" if width is None else f' WIDTH="{width}" HEIGHT="{height}"'
    page = (
        f'<Page ID="p1" PHYSICAL_IMG_NR="1"{size}><PrintSpace><TextBlock>{body}</TextBlock>'
        "</PrintSpace></Page>"
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>{head}<alto xmlns="{ns}"><Description>'
        f"<MeasurementUnit>{unit}</MeasurementUnit>{software}</Description>"
        f"<Layout>{page * pages}</Layout></alto>"
    ).encode()


def directory_page(scale=1.0, low="0.5", **kwargs):
    """A heading over two columns of entries; each TextLine runs across both columns."""
    s = scale
    lines = [
        [
            _string("DIRECTORY", 300 * s, 40 * s, 200 * s, 36 * s),
            _string("OF", 520 * s, 40 * s, 40 * s, 36 * s),
            _string("WASHINGTON", 575 * s, 40 * s, 240 * s, 36 * s),
        ]
    ]
    for i in range(8):
        y = (120 + 38 * i) * s
        lines.append(
            [
                _string(f"Smith{i}", 50 * s, y, 110 * s, 30 * s, 'WC="0.9"'),
                _string("John", 170 * s, y, 70 * s, 30 * s, 'WC="0.8"'),
                _string(f"Jones{i}", 550 * s, y, 110 * s, 30 * s, 'WC="0.95"'),
                _string("Mary", 670 * s, y, 70 * s, 30 * s, f'WC="{low}"'),
            ]
        )
    return alto(lines, width=1000 * s, height=1400 * s, **kwargs)


COLUMNS_TEXT = "\n".join(
    [
        "DIRECTORY OF WASHINGTON",
        *(f"Smith{i} John" for i in range(8)),
        *(f"Jones{i} Mary" for i in range(8)),
    ]
)


def test_words_not_lines_give_column_order_in_pixel_units():
    page = parse_alto(directory_page())
    assert page.unit == "pixel"
    assert (page.width, page.height) == (1000, 1400)
    assert page.software == "Tesseract 5.3.0"
    # TextLine order would read "Smith0 John Jones0 Mary" across the gutter.
    assert page_text(page) == COLUMNS_TEXT


def test_alto_v2_in_tenths_of_a_millimetre_reads_the_same():
    page = parse_alto(directory_page(scale=2.54, ns=V2, unit="mm10", software=V2_SOFTWARE))
    assert page.unit == "mm10"
    assert page.software == "ABBYY FineReader 8.0"
    assert page_text(page) == COLUMNS_TEXT


def test_hyphen_kept_as_printed_and_subs_content_ignored():
    lines = [
        [
            _string(
                "Pennsyl", 50, 100, 140, 30, 'SUBS_TYPE="HypPart1" SUBS_CONTENT="Pennsylvania"'
            ),
            '<HYP CONTENT="-" HPOS="190" VPOS="100" WIDTH="10"/>',
        ],
        [
            _string("vania", 50, 140, 100, 30, 'SUBS_TYPE="HypPart2" SUBS_CONTENT="Pennsylvania"'),
            _string("av", 160, 140, 40, 30),
        ],
    ]
    page = parse_alto(alto(lines))
    assert [word.text for word in page.words] == ["Pennsyl-", "vania", "av"]
    assert page_text(page) == "Pennsyl-\nvania av"


def test_confidence_from_wc_then_cc_then_none():
    lines = [
        [
            _string("sure", 50, 100, 80, 30, 'WC="1.0" CC="9999"'),
            _string("cc", 140, 100, 40, 30, 'CC="0 9"'),
            _string("bare", 190, 100, 80, 30),
            _string("bad", 280, 100, 60, 30, 'WC="7"'),
        ]
    ]
    page = parse_alto(alto(lines))
    assert [word.confidence for word in page.words] == [1.0, 0.5, None, None]
    assert confidence_detail(page) == {
        "word_count": 2,
        "mean": 75.0,
        "min": 50.0,
        "below_60_count": 1,
        "below_60_ratio": 0.5,
    }
    plain = parse_alto(alto([[_string("bare", 50, 100, 80, 30)]]))
    assert confidence_detail(plain) is None


def test_page_layout_is_word_level_and_bound_to_the_file():
    page = parse_alto(directory_page())
    layout, reason = page_layout(
        page, file="0001.xml", sha256="a" * 64, page_width=500, page_height=700
    )
    assert reason is None
    assert layout["level"] == "word"
    assert layout["items"][0] == {
        "box": [300_000, round(40 / 1400 * GRID), 500_000, round(76 / 1400 * GRID)],
        "text": "DIRECTORY",
    }
    assert [item["text"] for item in layout["items"][3:7]] == ["Smith0", "John", "Jones0", "Mary"]
    assert layout["source"] == {
        "file": "0001.xml",
        "sha256": "a" * 64,
        "software": "Tesseract 5.3.0",
        "measurement_unit": "pixel",
        "page_width": 1000.0,
        "page_height": 1400.0,
    }
    layout_sha256(layout)
    # The same page in tenths of a millimetre lands on the same grid.
    mm10 = parse_alto(directory_page(scale=2.54, unit="mm10"))
    other, _ = page_layout(mm10, file="0001.xml", sha256="a" * 64, page_width=500, page_height=700)
    assert [i["box"] for i in other["items"]] == [i["box"] for i in layout["items"]]


@pytest.mark.parametrize(
    "xml,size,reason",
    [
        (directory_page(), (700, 700), "shape differs"),
        (
            alto([[_string("Smith", 950, 100, 80, 30)]]),
            (500, 700),
            "outside the page",
        ),
        (alto([[_string("Smith", 50, 100, 80, 30)]], width=None), (500, 700), "no width"),
        (alto([[_string("Smith", 50, 100, 80, 30)]], width=0), (500, 700), "no width"),
        (alto([['<String CONTENT="Smith"/>']]), (500, 700), "no box"),
        (directory_page(), None, "size is unknown"),
    ],
)
def test_layout_is_withheld_when_the_geometry_does_not_fit(xml, size, reason):
    page = parse_alto(xml)
    width, height = size or (None, None)
    layout, why = page_layout(page, file="f", sha256="0" * 64, page_width=width, page_height=height)
    assert layout is None
    assert reason in why
    assert page_text(page)  # the text is kept


def test_a_small_shape_difference_is_tolerated():
    page = parse_alto(directory_page())
    layout, _ = page_layout(page, file="f", sha256="0" * 64, page_width=505, page_height=700)
    assert layout is not None


def _rows_page(rows):
    words = [
        AltoWord(text, (x, 100 + 38 * row, x + width, 130 + 38 * row), None, 0)
        for row, line in enumerate(rows)
        for text, x, width in line
    ]
    return AltoPage(tuple(words), 1000, 1400, "pixel", None)


@pytest.mark.parametrize("gutter,columns", [(40, True), (30, True), (25, False)])
def test_a_gutter_narrower_than_a_word_height_is_read_as_a_word_space(gutter, columns):
    rows = [
        [
            (f"left{i}", 50, 100),
            ("a", 160, 40),
            (f"right{i}", 200 + gutter, 100),
            ("b", 310 + gutter, 40),
        ]
        for i in range(6)
    ]
    left = [f"left{i} a" for i in range(6)]
    right = [f"right{i} b" for i in range(6)]
    by_rows = [f"left{i} a right{i} b" for i in range(6)]
    assert page_text(_rows_page(rows)).split("\n") == (left + right if columns else by_rows)


def test_a_tab_stop_inside_a_column_is_read_as_a_column():
    # A measured limit: names, then the numbers set at a tab stop to their right.
    rows = [
        [(f"Name{i}", 50, 80 + 20 * i), ("clk", 140 + 20 * i, 40), (f"ME5-12{i}", 400, 90)]
        for i in range(6)
    ]
    assert page_text(_rows_page(rows)).split("\n") == [
        *(f"Name{i} clk" for i in range(6)),
        *(f"ME5-12{i}" for i in range(6)),
    ]


def test_without_boxes_words_keep_the_file_order():
    page = parse_alto(
        alto([['<String CONTENT="b"/>', '<String CONTENT="a"/>'], ['<String CONTENT="c"/>']])
    )
    assert page_text(page) == "b a\nc"


@pytest.mark.parametrize(
    "data,message",
    [
        (directory_page(head='<!DOCTYPE alto [<!ENTITY a "aaaa">]>'), "DOCTYPE"),
        (
            '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE alto [<!ENTITY a "x">]>'
            f'<alto xmlns="{V4}"/>'.encode("utf-16"),
            "DOCTYPE",
        ),
        (directory_page(pages=2), "2 Page elements"),
        (alto([], pages=0), "0 Page elements"),
        (directory_page(unit="furlong"), "MeasurementUnit"),
        (b"<alto><Layout>", "not well-formed"),
        (b"<mets/>", "not alto"),
    ],
)
def test_refused_files(data, message):
    with pytest.raises(AltoInvalid, match=message):
        parse_alto(data)


def test_an_entity_bomb_is_refused_before_expansion():
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        + "".join(f'<!ENTITY lol{i} "{("&lol" + str(i - 1) + ";") * 10}">' for i in range(1, 10))
        + f']><alto xmlns="{V4}">&lol9;</alto>'
    )
    with pytest.raises(AltoInvalid, match="DOCTYPE"):
        parse_alto(bomb.encode().replace(b"&lol0;", b"&lol;"))


def test_name_templates(tmp_path):
    assert alto_name("{page:04d}.xml", 7) == "0007.xml"
    assert alto_name("reel_{page}.alto.xml", 12) == "reel_12.alto.xml"
    assert alto_name("{{page}}-{page:3}.xml", 5) == "{page}-  5.xml"
    assert alto_name("{stem}.xml", 1, stem="0251") == "0251.xml"
    assert alto_path(tmp_path, "sub/{page}.xml", 3) == (tmp_path / "sub" / "3.xml").resolve()
    for template in (
        "../{page}.xml",
        "/etc/{page}.xml",
        "{page.real}.xml",
        "{page[0]}.xml",
        "{page!r}.xml",
        "{0}.xml",
        "{}.xml",
        "{page:x}.xml",
        "{stem:>9}.xml",
        "a\\{page}.xml",
        "",
    ):
        with pytest.raises(ValueError):
            alto_name(template, 1)
    with pytest.raises(ValueError):
        alto_name("{stem}.xml", 1, stem="../x")
    with pytest.raises(ValueError):
        alto_name("{stem}.xml", 1)


def test_a_symlink_out_of_dir_is_refused(tmp_path):
    folder = tmp_path / "alto"
    folder.mkdir()
    (tmp_path / "secret.xml").write_bytes(directory_page())
    (folder / "1.xml").symlink_to(tmp_path / "secret.xml")
    with pytest.raises(ValueError, match="outside dir"):
        alto_path(folder, "{page}.xml", 1)
    source = _pdf(tmp_path / "reel.pdf", [(500, 700, 0)])
    result = AltoAdapter(dir=str(folder), name="{page}.xml").extract(
        source, page_id="p1", page_number=1, action="transcribe_text"
    )
    assert (result.content, result.warnings) == ("", ["alto_invalid"])
    assert result.model == "alto; file 1.xml outside dir"


def test_constructor_checks_options(tmp_path):
    with pytest.raises(ValueError, match="not a folder"):
        AltoAdapter(dir=str(tmp_path / "nowhere"))
    assert AltoAdapter(dir=str(tmp_path), name="{stem}.xml").uses_stem
    with pytest.raises(ValueError, match="without a format"):
        AltoAdapter(dir=str(tmp_path), name="{stem:4}.xml")
    with pytest.raises(ValueError, match="inside dir"):
        AltoAdapter(dir=str(tmp_path), name="../{page}.xml")
    with pytest.raises(ValueError, match="does not accept"):
        adapters.load_adapter("alto")
    adapter = adapters.load_adapter("alto", {"dir": str(tmp_path)})
    assert adapters.adapter_conformance_check(adapter) == []
    assert (adapter.name, adapter.template) == ("alto", "{page:04d}.xml")


def _pdf(path, sizes):
    writer = pypdf.PdfWriter()
    for width, height, rotate in sizes:
        page = writer.add_blank_page(width=width, height=height)
        if rotate:
            page.rotate(rotate)
    writer.write(path)
    return path


@needs_pdfinfo
def test_rendered_page_size_is_the_rotated_media_box(tmp_path):
    writer = pypdf.PdfWriter()
    first = writer.add_blank_page(width=600, height=800)
    first.cropbox = pypdf.generic.RectangleObject([10, 10, 300, 400])
    writer.add_blank_page(width=600, height=800).rotate(90)
    source = tmp_path / "boxes.pdf"
    writer.write(source)
    assert rendered_page_size(source, 1) == (600.0, 800.0)
    assert rendered_page_size(source, 2) == (800.0, 600.0)
    assert rendered_page_size(tmp_path / "missing.pdf", 1) is None


@needs_pdfinfo
def test_extract_reports_missing_invalid_and_mismatched_files(tmp_path):
    folder = tmp_path / "alto"
    folder.mkdir()
    source = _pdf(tmp_path / "reel.pdf", [(500, 700, 0)] * 5)
    (folder / "0001.xml").write_bytes(directory_page())
    (folder / "0003.xml").write_bytes(directory_page(pages=2))
    (folder / "0004.xml").write_bytes(
        alto([[_string("Smith", 50, 100, 80, 30)]], width=1400, height=1000, software="")
    )
    (folder / "0005.xml").write_bytes(b" " * (MAX_BYTES + 1))
    adapter = AltoAdapter(dir=str(folder))
    adapter.preflight([source])
    assert adapter.page_count(source) == 5

    def read(number):
        return adapter.extract(
            source, page_id=f"p{number:04d}", page_number=number, action="transcribe_text"
        )

    good = read(1)
    digest = hashlib.sha256((folder / "0001.xml").read_bytes()).hexdigest()
    assert good.content == COLUMNS_TEXT
    assert good.warnings == ["ocr_text_layer"]
    assert good.model == f"alto; file 0001.xml sha256:{digest}; software Tesseract 5.3.0"
    assert good.confidence_detail["word_count"] == 32
    assert good.confidence_detail["below_60_count"] == 8
    missing = read(2)
    assert (missing.content, missing.warnings) == ("", ["alto_missing"])
    assert missing.model == "alto; file 0002.xml missing"
    invalid = read(3)
    assert (invalid.content, invalid.warnings) == ("", ["alto_invalid"])
    assert invalid.model.endswith("; invalid")
    turned = read(4)
    assert turned.content == "Smith"
    assert turned.warnings == ["ocr_text_layer", "alto_geometry_mismatch"]
    assert turned.model.endswith("software not declared")
    assert read(5).warnings == ["alto_invalid"]
    with pytest.raises(ValueError, match="PDF"):
        adapter.page_count(tmp_path / "pages.txt")


STUB_OCR = """\
from pageledger.adapters import ExtractionResult, ocr_pdf_page_count

TEXTS = {}
SECOND = {}
CALLS = []


class StubOCR:
    name = "stub_ocr"
    version = "1"
    deterministic = True
    input_types = ("pdf",)
    output_types = ("text",)
    capabilities = ("ocr", "local")
    texts = TEXTS

    def supports(self, action):
        return action == "transcribe_text"

    def page_count(self, source):
        return ocr_pdf_page_count(source)

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        CALLS.append((self.name, page_number))
        return ExtractionResult(
            content=self.texts[page_number],
            format="text",
            confidence=None,
            model=self.name,
            warnings=[],
            usage={"pages": 1},
        )


class SecondOCR(StubOCR):
    name = "second_ocr"
    texts = SECOND
"""


@pytest.fixture
def reel(tmp_path, monkeypatch):
    folder = tmp_path / "alto"
    folder.mkdir()
    (folder / "0001.xml").write_bytes(directory_page(low="0.9"))
    source = _pdf(tmp_path / "reel.pdf", [(500, 700, 0), (500, 700, 0)])
    module = f"stub_ocr_{os.getpid()}_{abs(hash(tmp_path)) % 10**8}"
    adapters_dir = tmp_path / "adapters"
    adapters_dir.mkdir()
    (adapters_dir / f"{module}.py").write_text(STUB_OCR, encoding="utf-8")
    monkeypatch.syspath_prepend(str(adapters_dir))
    __import__(module)
    config = tmp_path / "processing.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "processing": {
                    "local_text": {
                        "adapter": "alto",
                        "adapter_options": {"dir": str(folder), "name": "{page:04d}.xml"},
                    },
                    "local_ocr": {"adapter": f"{module}:StubOCR"},
                    "second_ocr": {"adapter": f"{module}:SecondOCR"},
                },
            }
        )
    )
    return source, config, folder, sys.modules[module]


OCR_PAGE_2 = "The second page, read by our own OCR, holds complete readable prose. " * 8
OTHER = "Completely different words from a fresh reading of this scanned page. " * 8


def _job(reel, tmp_path, ocr_page_1, second_page_1=None):
    source, config, folder, stub = reel
    stub.TEXTS.update({1: ocr_page_1, 2: OCR_PAGE_2})
    stub.SECOND.update({1: second_page_1 or ocr_page_1, 2: OCR_PAGE_2})
    out = tmp_path / "job"
    result = process(source=source, config_path=config, out_dir=out)
    assert result["status"] == "completed"
    assert verify_job(out)["status"] == "pass"
    return out, {page["page_number"]: page for page in read_record(out / "job.json")["pages"]}


@needs_pdfinfo
def test_alto_that_agrees_with_our_ocr_confirms_it(reel, tmp_path):
    _, pages = _job(reel, tmp_path, COLUMNS_TEXT)
    first = pages[1]
    alto_attempt, ocr_attempt = first["attempts"]
    assert alto_attempt["stage"] == "local_text"
    assert alto_attempt["warnings"] == ["ocr_text_layer"]
    assert [t["trigger"] for t in first["triggers"]] == ["ocr_layer"]
    assert first["selected_attempt"] == ocr_attempt["attempt_id"]
    assert first["disposition"] == "unreviewed_text"
    assert first["comparisons"][0]["agreement_ratio"] == 1.0
    # Confirmed by the ALTO reading, page 1 never reached the second OCR engine.
    assert ("second_ocr", 1) not in reel[3].CALLS
    # No ALTO for page 2: an empty reading, held blank until our OCR read text;
    # with nothing to confirm that OCR, the second engine read it too.
    second = pages[2]
    assert second["attempts"][0]["outcome"] == "completed"
    assert second["attempts"][0]["warnings"] == ["alto_missing", "empty_text"]
    assert [t["trigger"] for t in second["triggers"]] == ["uncorroborated"]
    assert second["selected_attempt"] == second["attempts"][1]["attempt_id"]
    assert second["disposition"] == "unreviewed_text"


@needs_pdfinfo
def test_alto_that_disagrees_is_set_aside_and_does_not_count_against_our_ocr(reel, tmp_path):
    _, pages = _job(reel, tmp_path, OTHER)
    first = pages[1]
    assert [a["stage"] for a in first["attempts"]] == ["local_text", "local_ocr", "second_ocr"]
    assert [t["trigger"] for t in first["triggers"]] == ["ocr_layer", "uncorroborated"]
    assert first["selected_attempt"] == first["attempts"][1]["attempt_id"]
    assert "engine_disagreement" not in first["review_reasons"]
    assert first["disposition"] == "unreviewed_text"


@needs_pdfinfo
def test_alto_held_for_low_confidence_cannot_confirm(reel, tmp_path):
    (reel[2] / "0001.xml").write_bytes(directory_page())  # a quarter of its words under 60
    _, pages = _job(reel, tmp_path, COLUMNS_TEXT)
    first = pages[1]
    assert first["attempts"][0]["warnings"] == ["ocr_text_layer", "low_confidence"]
    assert [t["trigger"] for t in first["triggers"]] == ["ocr_layer", "uncorroborated"]
    assert ("second_ocr", 1) in reel[3].CALLS


@needs_pdfinfo
def test_verification_does_not_reread_the_alto_file(reel, tmp_path):
    out, _ = _job(reel, tmp_path, COLUMNS_TEXT)
    (reel[2] / "0001.xml").write_bytes(directory_page(scale=1.0, software=V2_SOFTWARE))
    # The job records the file's digest in the model string, but cannot check the
    # file itself: it lives outside the source and the job.
    assert verify_job(out)["status"] == "pass"


@needs_pdfinfo
def test_a_plain_run_completes_and_queues_a_page_without_alto(reel, tmp_path):
    source, _, folder, _ = reel
    config = tmp_path / "run.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "taxonomy": {"page_types": {"prose": {"default_action": "transcribe_text"}}},
                "run": {"adapter": "alto", "adapter_options": {"dir": str(folder)}},
            }
        )
    )
    out = tmp_path / "run"
    run(inputs=[source], config_path=config, out_dir=out, dry_run=False)
    assert verify_run(out)["status"] == "pass"
    lines = [
        json.loads(line)
        for line in (out / "quality.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "ocr_text_layer" in lines[0]["warnings"]
    assert "alto_missing" in lines[1]["warnings"]
    queue = json.loads((out / "audit.json").read_text(encoding="utf-8"))["review_queue"]
    assert [(item["page_number"], item["reason"]) for item in queue] == [
        (1, "quality_warning"),
        (2, "quality_warning"),
    ]


# -- {stem}: ALTO files named after the page images -------------------------------


def _png(width, height):
    def chunk(kind, body):
        crc = struct.pack(">I", zlib.crc32(kind + body))
        return struct.pack(">I", len(body)) + kind + body + crc

    rows = b"".join(b"\x00" + b"\xff" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def scans(tmp_path):
    """Two page images that keep their reel numbers, and ALTO for the first in its own folder.

    The ALTO files cannot sit beside the images: a folder of page images holds
    nothing else.
    """
    folder = tmp_path / "scans"
    folder.mkdir()
    for name in ("0081.png", "0082.png"):
        (folder / name).write_bytes(_png(100, 140))
    library = tmp_path / "library-alto"
    library.mkdir()
    (library / "0081.xml").write_bytes(directory_page(low="0.9"))
    (library / "0001.xml").write_bytes(alto([[_string("Wrong", 10, 10, 50, 20)]]))
    return folder, library


@needs_pdfinfo
def test_stem_reads_the_alto_file_named_after_each_page_image_in_a_job(reel, scans, tmp_path):
    folder, library = scans
    _, config, _, stub = reel
    stub.TEXTS.update({1: COLUMNS_TEXT, 2: OCR_PAGE_2})
    stub.SECOND.update({1: COLUMNS_TEXT, 2: OCR_PAGE_2})
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["processing"]["local_text"]["adapter_options"] = {
        "dir": str(library),
        "name": "{stem}.xml",
    }
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    out = tmp_path / "job"
    assert process(source=folder, config_path=config, out_dir=out)["status"] == "completed"
    assert verify_job(out)["status"] == "pass"
    pages = {page["page_number"]: page for page in read_record(out / "job.json")["pages"]}
    # Page 1 of the job is 0081.png, so it reads 0081.xml and never 0001.xml.
    first, second = pages[1]["attempts"][0], pages[2]["attempts"][0]
    assert first["stage"] == "local_text" and first["warnings"] == ["ocr_text_layer"]
    assert pages[1]["comparisons"][0]["agreement_ratio"] == 1.0
    assert second["warnings"] == ["alto_missing", "empty_text"]
    models = [
        json.loads(line)["extractor"]["model"]
        for path in out.glob("attempts/*/provenance.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert any("file 0081.xml sha256:" in model for model in models)
    assert "alto; file 0082.xml missing" in models


@needs_pdfinfo
def test_stem_in_a_plain_run_of_page_images(scans, tmp_path):
    folder, library = scans
    config = tmp_path / "run.yml"
    options = {"dir": str(library), "name": "{stem}.xml"}
    config.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "taxonomy": {"page_types": {"prose": {"default_action": "transcribe_text"}}},
                "run": {"adapter": "alto", "adapter_options": options},
            }
        )
    )
    out = tmp_path / "run"
    run(inputs=[folder], config_path=config, out_dir=out, dry_run=False)
    assert verify_run(out)["status"] == "pass"
    lines = [json.loads(line) for line in (out / "quality.jsonl").read_text().splitlines()]
    assert lines[0]["warnings"] == ["ocr_text_layer"]
    assert "alto_missing" in lines[1]["warnings"]


def test_stem_on_a_pdf_is_refused_with_its_reason(reel, tmp_path):
    source, config, folder, _ = reel
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["processing"]["local_text"]["adapter_options"]["name"] = "{stem}.xml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match=r"^processing.local_text: alto name uses \{stem\}"):
        process(source=source, config_path=config, out_dir=tmp_path / "job")
    assert not (tmp_path / "job").exists()
    adapter = AltoAdapter(dir=str(folder), name="{stem}.xml")
    with pytest.raises(ValueError, match="a PDF's pages have no file names of their own"):
        adapter.preflight([source])
    with pytest.raises(ValueError, match=r"use \{page\} for a PDF"):
        adapter.extract(source, page_id="p0001", page_number=1, action="transcribe_text")
