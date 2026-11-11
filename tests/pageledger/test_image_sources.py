"""Page images as a source: one image file or a folder of them, read as one document."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import jsonschema
import pytest
import yaml
from test_image_evidence import image_descriptor
from test_vision_adapter import endpoint  # noqa: F401 (the fake vision endpoint fixture)

from pageledger import image_source, runner
from pageledger.adapters import (
    ExtractionResult,
    PageLedgerDiagnostic,
    native_page_pixels,
    render_warnings,
)
from pageledger.image_source import (
    KIND,
    _png_chunks,
    carrier,
    carrier_sha256,
    manifest_changes,
    manifest_sha256,
    read_image_source,
    write_carrier,
)
from pageledger.processing import process, verify_job
from pageledger.replay import ReplayError, bundle_run
from pageledger.runner import rerun, resume, run
from pageledger.verify import verify_run

FIXTURES = Path(__file__).parents[1] / "fixtures" / "images"
SCHEMAS = Path(__file__).parents[2] / "schemas"
TEXT = "The synthetic document contains complete readable prose for this source page. " * 8
PROSE = {"page_types": {"prose": {"default_action": "transcribe_text"}}}


def png(width=32, height=16, *, color=2, depth=8, left=None, right=None, extra=b"", interlace=0):
    """A PNG whose left half is one pixel value and right half another."""
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    left = left if left is not None else bytes(channels * depth // 8)
    right = right if right is not None else b"\xff" * (channels * depth // 8)
    row = b"".join(left if x < width // 2 else right for x in range(width))
    raw = b"".join(b"\x00" + row for _ in range(height))

    def chunk(kind, body):
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    header = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + extra
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def phys(ppm):
    body = struct.pack(">IIB", ppm, ppm, 1)
    return struct.pack(">I", 9) + b"pHYs" + body + struct.pack(">I", zlib.crc32(b"pHYs" + body))


def jpeg(name="page-gray.jpg", *, units=None, density=None, exif=None):
    """A fixture JPEG, optionally with a JFIF density and an EXIF block after the JFIF header."""
    data = bytearray((FIXTURES / name).read_bytes())
    assert data[6:11] == b"JFIF\x00"
    if units is not None:
        data[13] = units
        data[14:18] = struct.pack(">HH", density, density)
    if exif is not None:
        data[20:20] = b"\xff\xe1" + struct.pack(">H", len(exif) + 8) + b"Exif\x00\x00" + exif
    return bytes(data)


def exif(orientation=1, ppi=None):
    """A big-endian EXIF block with an orientation and, optionally, a resolution in inches."""
    entries = [(0x0112, 3, 1, struct.pack(">HH", orientation, 0))]
    values = b""
    if ppi is not None:
        start = 8 + 2 + 12 * 4 + 4
        entries += [
            (0x011A, 5, 1, struct.pack(">I", start)),
            (0x011B, 5, 1, struct.pack(">I", start + 8)),
            (0x0128, 3, 1, struct.pack(">HH", 2, 0)),
        ]
        values = struct.pack(">IIII", ppi, 1, ppi, 1)
    body = b"".join(
        struct.pack(">HHI", tag, kind, count) + value for tag, kind, count, value in entries
    )
    return (
        b"MM\x00\x2a"
        + struct.pack(">I", 8)
        + struct.pack(">H", len(entries))
        + body
        + b"\0" * 4
        + values
    )


@pytest.fixture
def scans(tmp_path):
    folder = tmp_path / "scans"
    folder.mkdir()
    (folder / "page-10.png").write_bytes(png(left=b"\x00\x00\xff"))
    (folder / "page-2.png").write_bytes(png(color=6, left=b"\x00\x00\x00\xff", right=b"\0" * 4))
    (folder / "page-1.jpg").write_bytes(jpeg("page-rgb.jpg"))
    (folder / ".DS_Store").write_bytes(b"\0")
    return folder


class StubOCR:
    name = "stub_ocr"
    version = "1"
    deterministic = True
    input_types: tuple[str, ...] = ("pdf",)
    output_types = ("text",)
    capabilities: tuple[str, ...] = ("ocr", "local")

    def __init__(self, warnings=(), evidence_root=None):
        self.seen: list[tuple[Path, int]] = []
        self.warnings = list(warnings)
        self.evidence_root = evidence_root

    def supports(self, action):
        return action == "transcribe_text"

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        self.seen.append((Path(source), page_number))
        evidence = None
        if self.evidence_root is not None:
            evidence = image_descriptor(self.evidence_root, Path(source), page_number, prompt or "")
        return ExtractionResult(
            TEXT,
            "text",
            None,
            "gemini-test-returned" if evidence else "stub",
            list(self.warnings),
            {"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
            input_evidence=evidence,
        )


@pytest.fixture
def stub(monkeypatch):
    adapters: dict[str, StubOCR] = {"stub": StubOCR()}
    real = runner.load_adapter

    def load(name, options=None):
        return adapters[name] if name in adapters else real(name, options)

    monkeypatch.setattr(runner, "load_adapter", load)
    return adapters


def config(tmp_path, adapter="stub", **run):
    path = tmp_path / "config.yml"
    data = {"schema_version": "0.1", "run": {"adapter": adapter, **run}, "taxonomy": PROSE}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def validate(instance, schema):
    jsonschema.validate(instance, json.loads((SCHEMAS / f"{schema}.schema.json").read_text()))


# -- The source: order, identity, refusals ---------------------------------------


def test_a_folder_is_one_document_in_natural_order(scans):
    source = read_image_source(scans)
    assert [page.name for page in source.pages] == ["page-1.jpg", "page-2.png", "page-10.png"]
    assert source.skipped == (".DS_Store",)
    assert source.sha256 == manifest_sha256(source.manifest())
    first = source.manifest()[0]
    data = (scans / "page-1.jpg").read_bytes()
    assert first == {
        "name": "page-1.jpg",
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "width": 32,
        "height": 16,
    }


def test_identity_changes_with_any_image_and_names_the_change(scans):
    before = read_image_source(scans)
    (scans / "page-2.png").write_bytes(png(color=6, left=b"\x00\x00\x00\x80", right=b"\0" * 4))
    changed = read_image_source(scans)
    assert changed.sha256 != before.sha256
    assert manifest_changes(before.manifest(), changed.manifest()) == ["changed: page-2.png"]
    (scans / "page-10.png").rename(scans / "page-3.png")
    (scans / "page-11.png").write_bytes(png())
    later = read_image_source(scans).manifest()
    assert manifest_changes(changed.manifest(), later) == [
        "missing: page-10.png",
        "added: page-3.png",
        "added: page-11.png",
    ]
    assert manifest_changes(later, later[::-1]) == ["reordered"]


@pytest.mark.parametrize(
    ("name", "content", "code", "hint"),
    [
        ("page-3.tif", b"II*\x00", "unsupported_image", "magick page.tif page.png"),
        ("page-3.jp2", b"\x00\x00\x00\x0cjP  ", "unsupported_image", "opj_decompress"),
        ("page-3.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "malformed_image", "damaged"),
        ("page-3.png", png(interlace=1), "unsupported_image", "-interlace none"),
        ("page-3.jpg", jpeg(exif=exif(orientation=2)), "unsupported_image", "-auto-orient"),
    ],
)
def test_an_image_pageledger_cannot_carry_faithfully_is_refused(scans, name, content, code, hint):
    (scans / name).write_bytes(content)
    with pytest.raises(PageLedgerDiagnostic) as error:
        read_image_source(scans)
    assert error.value.code == code
    assert hint in error.value.message


def test_anything_else_in_a_folder_of_page_images_is_refused(scans):
    (scans / "notes.txt").write_text("not a page")
    with pytest.raises(ValueError, match="'notes.txt' is not a page image"):
        read_image_source(scans)
    (scans / "notes.txt").unlink()
    (scans / "thumbnails").mkdir()
    with pytest.raises(ValueError, match="'thumbnails' is not one"):
        read_image_source(scans)


def test_a_mixed_folder_is_refused_by_run_before_any_call(scans, stub, tmp_path):
    (scans / "notes.txt").write_text("not a page")
    with pytest.raises(ValueError, match="notes.txt"):
        run(inputs=[scans], config_path=config(tmp_path), out_dir=tmp_path / "out", dry_run=False)
    assert stub["stub"].seen == []
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("data", "ppi", "basis", "rotation"),
    [
        (jpeg(units=1, density=150), (150.0, 150.0), "jfif", 0),
        (jpeg(units=2, density=59), (149.86, 149.86), "jfif", 0),
        (jpeg(exif=exif(orientation=6, ppi=200)), (200.0, 200.0), "exif", 90),
        (
            jpeg(units=1, density=150, exif=exif(orientation=8, ppi=200)),
            (150.0, 150.0),
            "jfif",
            270,
        ),
        (jpeg(), (300.0, 300.0), "default", 0),
        (jpeg(units=1, density=5), (300.0, 300.0), "default", 0),
        (png(extra=phys(11811)), (299.9994, 299.9994), "png_phys", 0),
        (png(), (300.0, 300.0), "default", 0),
    ],
)
def test_page_size_comes_from_the_images_own_density(tmp_path, data, ppi, basis, rotation):
    path = tmp_path / ("page.png" if data.startswith(b"\x89PNG") else "page.jpg")
    path.write_bytes(data)
    page = read_image_source(path).pages[0]
    assert (page.ppi, page.ppi_basis, page.rotation) == (ppi, basis, rotation)


# -- The carrier ------------------------------------------------------------------


def _pdf(path):
    pypdf = pytest.importorskip("pypdf")
    return pypdf.PdfReader(str(path), strict=True)


def test_the_carrier_holds_each_images_own_bytes(tmp_path):
    folder = tmp_path / "scans"
    folder.mkdir()
    (folder / "page-1.jpg").write_bytes(jpeg("page-gray.jpg", units=1, density=150))
    (folder / "page-2.png").write_bytes(png())
    (folder / "page-3.png").write_bytes(png(color=6, left=b"\0\0\0\xff", right=b"\0" * 4))
    (folder / "page-4.jpg").write_bytes(jpeg("page-rgb.jpg", exif=exif(orientation=6)))
    source = read_image_source(folder)
    out = tmp_path / "carrier.pdf"
    with out.open("wb") as handle:
        write_carrier(source, handle)
    assert hashlib.sha256(out.read_bytes()).hexdigest() == carrier_sha256(source)
    reader = _pdf(out)
    assert len(reader.pages) == 4
    images = [page["/Resources"]["/XObject"]["/Im0"].get_object() for page in reader.pages]
    assert [float(value) for value in reader.pages[0].mediabox[2:]] == [15.36, 7.68]
    assert [float(value) for value in reader.pages[1].mediabox[2:]] == [7.68, 3.84]
    assert images[0]["/Filter"] == "/DCTDecode" and images[0]["/ColorSpace"] == "/DeviceGray"
    assert images[0]._data == (folder / "page-1.jpg").read_bytes()
    assert images[1]["/Filter"] == "/FlateDecode" and images[1]["/DecodeParms"]["/Predictor"] == 15
    assert images[1]._data == _png_chunks((folder / "page-2.png").read_bytes(), "x")["data"]
    assert "/SMask" in images[2] and images[2]["/DecodeParms"]["/Colors"] == 3
    assert images[3]._data == (folder / "page-4.jpg").read_bytes()
    assert reader.pages[3]["/Rotate"] == 90


def test_pages_a_run_does_not_read_stay_empty_in_the_carrier(scans, tmp_path):
    source = read_image_source(scans)
    out = tmp_path / "carrier.pdf"
    with out.open("wb") as handle:
        write_carrier(source, handle, pages=[2])
    reader = _pdf(out)
    assert len(reader.pages) == 3
    assert ["/Resources" in page for page in reader.pages] == [False, True, False]
    assert carrier_sha256(source, [2]) != carrier_sha256(source)
    assert carrier_sha256(source, [2]) == carrier_sha256(read_image_source(scans), [2])


def _decoded(path):
    chunks = _png_chunks(path.read_bytes(), path.name)
    step = {0: 1, 2: 3, 4: 2, 6: 4}[chunks["color"]]
    row = chunks["width"] * step
    pixels = _unfilter(zlib.decompress(chunks["data"]), chunks["height"], row, step)
    middle = chunks["height"] // 2 * row
    return (
        chunks["width"],
        pixels[middle + step : middle + 2 * step],
        pixels[middle + row - 2 * step : middle + row - step],
    )


def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _png_rows(width, height, color, depth, rows, extra=b""):
    header = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0)
    raw = b"".join(rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + extra
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def _filtered(pixels, row, step, kinds):
    """PNG rows of ``pixels`` filtered with each of ``kinds`` in turn."""
    out, previous = [], bytes(row)
    for index in range(len(pixels) // row):
        line = pixels[index * row : (index + 1) * row]
        kind = kinds[index % len(kinds)]
        encoded = bytearray(row)
        for i in range(row):
            a = line[i - step] if i >= step else 0
            b, c = previous[i], previous[i - step] if i >= step else 0
            predictor = (0, a, b, (a + b) >> 1, _paeth(a, b, c))[kind]
            encoded[i] = (line[i] - predictor) & 0xFF
        out.append(bytes([kind]) + bytes(encoded))
        previous = line
    return out


def _unfilter(raw, height, row, step):
    """Undo PNG's row filters, as a reader does."""
    out, previous = bytearray(), bytearray(row)
    for index in range(height):
        start = index * (row + 1)
        kind, line = raw[start], bytearray(raw[start + 1 : start + 1 + row])
        for i in range(row):
            a = line[i - step] if i >= step else 0
            b, c = previous[i], previous[i - step] if i >= step else 0
            line[i] = (line[i] + (0, a, b, (a + b) >> 1, _paeth(a, b, c))[kind]) & 0xFF
        out += line
        previous = line
    return bytes(out)


def _alpha_png(width, height, pixels, kinds=(1, 2, 3, 4, 0)):
    return _png_rows(width, height, 6, 8, _filtered(pixels, width * 4, 4, list(kinds)))


def test_png_row_filters_decode_to_the_pixels_they_encode(tmp_path):
    width, height, step = 16, 8, 4
    pixels = bytes(
        (x * 13 + y * 7 + c * 29) & 0xFF
        for y in range(height)
        for x in range(width)
        for c in range(step)
    )
    data = _alpha_png(width, height, pixels)
    chunks = _png_chunks(data, "x")
    assert _unfilter(zlib.decompress(chunks["data"]), height, width * step, step) == pixels
    (tmp_path / "page.png").write_bytes(data)
    out = tmp_path / "carrier.pdf"
    with out.open("wb") as handle:
        write_carrier(read_image_source(tmp_path / "page.png"), handle)
    image = _pdf(out).pages[0]["/Resources"]["/XObject"]["/Im0"].get_object()
    mask = image["/SMask"].get_object()
    assert image["/DecodeParms"]["/Colors"] == 3 and mask["/DecodeParms"]["/Colors"] == 1
    # Each half keeps PNG's filtered rows; uncompressed, so no zlib build changes the bytes.
    colour = _unfilter(zlib.decompress(image._data), height, width * 3, 3)
    assert colour == bytes(b for i, b in enumerate(pixels) if i % 4 != 3)
    assert _unfilter(zlib.decompress(mask._data), height, width, 1) == pixels[3::4]
    assert zlib.decompress(image._data) in image._data


@pytest.mark.parametrize(
    ("raw", "what"),
    [
        # A 2 x 2 image whose data inflates to 5 MB: refused after 2 rows' worth.
        (b"\x00" * 5_000_000, "longer"),
        (b"\x00" * 9, "shorter"),
        (b"\x05" + b"\x00" * 8 + b"\x00" * 9, "unknown row filter"),
    ],
    ids=["bomb", "short", "filter"],
)
def test_alpha_png_data_must_match_its_header(tmp_path, raw, what):
    (tmp_path / "page.png").write_bytes(_png_rows(2, 2, 6, 8, [raw]))
    source = read_image_source(tmp_path / "page.png")
    with pytest.raises(PageLedgerDiagnostic, match=what) as error:
        carrier_sha256(source)
    assert error.value.code == "malformed_image"


def test_an_image_larger_than_any_render_is_refused_from_its_header(tmp_path, monkeypatch):
    monkeypatch.setattr(image_source, "MAX_IMAGE_PIXELS", 511)
    (tmp_path / "page.png").write_bytes(png(width=32, height=16))
    with pytest.raises(PageLedgerDiagnostic, match="32 x 16 pixels.*-resize") as error:
        read_image_source(tmp_path / "page.png")
    assert error.value.code == "unsupported_image"
    monkeypatch.setattr(image_source, "MAX_IMAGE_PIXELS", 512)
    assert read_image_source(tmp_path / "page.png").pages[0].width == 32


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="needs Poppler's pdftoppm")
def test_poppler_renders_each_colour_of_an_alpha_png_in_place(tmp_path):
    # Bands of red, green, blue and clear, every row filter in turn: samples taken
    # in the wrong order would mix the channels.
    bands = [b"\xc8\x1e\x1e\xff", b"\x1e\xc8\x1e\xff", b"\x1e\x1e\xc8\xff", b"\x00\x00\x00\x00"]
    width, height = 48, 16
    pixels = b"".join(bands[x // 12] for _ in range(height) for x in range(width))
    (tmp_path / "page.png").write_bytes(_alpha_png(width, height, pixels))
    with carrier(read_image_source(tmp_path / "page.png"), [1]) as (path, _):
        rendered = _render(path, 1, tmp_path / "r")
    chunks = _png_chunks(rendered.read_bytes(), "r")
    assert (chunks["width"], chunks["height"]) == (width, height)
    out = _unfilter(zlib.decompress(chunks["data"]), height, width * 3, 3)
    middle = height // 2 * width * 3
    expected = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (255, 255, 255)]
    for band, colour in enumerate(expected):
        at = middle + (band * 12 + 6) * 3
        assert all(abs(a - b) <= 8 for a, b in zip(out[at : at + 3], colour, strict=True)), band


def _shapes(folder):
    """Page images of the shapes PDF carries differently, and the colours each should render."""
    white = (255, 255, 255)
    shapes = {
        # 1-bit grey: left black, right white.
        "page-01.png": (_png_rows(32, 16, 0, 1, [b"\x00" + b"\x00\x00\xff\xff"] * 16), (0, 0, 0)),
        # 4-bit palette: index 0 red, index 1 white.
        "page-02.png": (
            _png_rows(
                32,
                16,
                3,
                4,
                [b"\x00" + b"\x00" * 8 + b"\x11" * 8] * 16,
                _chunk(b"PLTE", b"\xc8\x1e\x1e\xff\xff\xff"),
            ),
            (200, 30, 30),
        ),
        # 16-bit RGB: left blue.
        "page-03.png": (
            _png_rows(
                32, 16, 2, 16, [b"\x00" + b"\x00\x00\x00\x00\xff\xff" * 16 + b"\xff" * 96] * 16
            ),
            (0, 0, 255),
        ),
        # A transparent colour key on red: the left half shows the paper.
        "page-04.png": (
            _png_rows(
                32,
                16,
                2,
                8,
                [b"\x00" + b"\xc8\x1e\x1e" * 16 + b"\xff" * 48] * 16,
                _chunk(b"tRNS", struct.pack(">HHH", 200, 30, 30)),
            ),
            white,
        ),
        # Adobe CMYK JPEG, stored inverted. Poppler builds convert CMYK a little differently:
        # inverted, white would turn black and red cyan.
        "page-05.jpg": ((FIXTURES / "page-cmyk.jpg").read_bytes(), (200, 30, 30)),
    }
    for name, (data, _) in shapes.items():
        (folder / name).write_bytes(data)
    return [(left, white) for _, left in shapes.values()]


def _render(path, number, prefix):
    subprocess.run(
        ["pdftoppm", "-f", str(number), "-l", str(number), "-singlefile", "-r", "300", "-png"]
        + [str(path), str(prefix)],
        check=True,
        capture_output=True,
    )
    return prefix.with_suffix(".png")


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="needs Poppler's pdftoppm")
def test_poppler_renders_the_carrier_with_the_images_pixels(scans, tmp_path):
    expected = [
        ((200, 30, 30), (255, 255, 255)),
        ((0, 0, 0), (255, 255, 255)),
        ((0, 0, 255), (255, 255, 255)),
    ]
    shapes = tmp_path / "shapes"
    shapes.mkdir()
    more = _shapes(shapes)
    rotated = tmp_path / "rotated.jpg"
    rotated.write_bytes(jpeg("page-rgb.jpg", exif=exif(orientation=6)))
    for folder, colours in ((scans, expected), (shapes, more)):
        with carrier(read_image_source(folder), range(1, len(colours) + 1)) as (path, _):
            for number, (left, right) in enumerate(colours, start=1):
                width, first, last = _decoded(_render(path, number, tmp_path / f"r{number}"))
                assert width == 32
                # JPEG is lossy at its own edges; the carrier adds no loss of its own.
                close = 40 if folder == shapes and number == 5 else 8
                assert all(abs(a - b) <= close for a, b in zip(first, left, strict=True)), number
                assert tuple(last) == right
    with carrier(read_image_source(rotated), [1]) as (path, _):
        rendered = _render(path, 1, tmp_path / "rotated")
    chunks = _png_chunks(rendered.read_bytes(), "r")
    assert (chunks["width"], chunks["height"]) == (16, 32)
    # Orientation 6 turns the stored image a quarter clockwise: its red left half is now on top.
    pixels = _unfilter(zlib.decompress(chunks["data"]), 32, 16 * 3, 3)
    top, bottom = pixels[4 * 48 + 24 : 4 * 48 + 27], pixels[28 * 48 + 24 : 28 * 48 + 27]
    assert top[0] > 150 and top[1] < 80 and tuple(bottom) == (255, 255, 255)


# -- Runs ---------------------------------------------------------------------------


def test_a_run_reads_a_folder_through_the_carrier_and_binds_each_page_to_its_image(
    scans, stub, tmp_path
):
    out = tmp_path / "out"
    result = run(inputs=[scans], config_path=config(tmp_path), out_dir=out, dry_run=False)
    assert result["skipped_inputs"] == [".DS_Store"]
    assert [number for _, number in stub["stub"].seen] == [1, 2, 3]
    assert {path.suffix for path, _ in stub["stub"].seen} == {".pdf"}
    assert not stub["stub"].seen[0][0].exists()
    manifest = json.loads((out / "manifest.json").read_text())
    validate(manifest, "manifest")
    (entry,) = manifest["inputs"]
    source = read_image_source(scans)
    assert entry["kind"] == KIND
    assert entry["sha256"] == source.sha256
    assert entry["images"] == source.manifest()
    assert entry["carrier"] == {
        "sha256": carrier_sha256(source, [1, 2, 3]),
        "derived": True,
        "pages": [1, 2, 3],
    }
    provenance = lines(out / "provenance.jsonl")
    for line in provenance:
        validate(line, "provenance-line")
        number = line["source"]["page_number"]
        image = line["source"]["image"]
        assert {key: image[key] for key in source.manifest()[0]} == source.manifest()[number - 1]
        assert line["source"]["native_pixels"] == {"width": 32, "height": 16}
        assert line["source"]["sha256"] == source.sha256
    report = verify_run(out)
    assert (report["status"], report["warnings"]) == ("pass", [])


def test_verify_run_names_the_image_that_changed_went_missing_or_was_added(scans, stub, tmp_path):
    out = tmp_path / "out"
    run(inputs=[scans], config_path=config(tmp_path), out_dir=out, dry_run=False)
    original = (scans / "page-2.png").read_bytes()
    (scans / "page-2.png").write_bytes(png())
    (warning,) = verify_run(out)["warnings"]
    assert (warning["code"], warning["changes"]) == ("source_changed", ["changed: page-2.png"])
    (scans / "page-2.png").write_bytes(original)
    (scans / "page-1.jpg").unlink()
    (scans / "page-0.jpg").write_bytes(jpeg())
    (warning,) = verify_run(out)["warnings"]
    assert warning["changes"] == ["missing: page-1.jpg", "added: page-0.jpg"]
    shutil.rmtree(scans)
    assert [item["code"] for item in verify_run(out)["warnings"]] == ["source_missing"]


def test_verify_run_rejects_page_image_records_that_disagree(scans, stub, tmp_path):
    out = tmp_path / "out"
    run(inputs=[scans], config_path=config(tmp_path), out_dir=out, dry_run=False)
    provenance = (out / "provenance.jsonl").read_text()
    (out / "provenance.jsonl").write_text(provenance.replace('"page-2.png"', '"page-9.png"', 1))
    codes = [error["code"] for error in verify_run(out, check_external_sources=False)["errors"]]
    assert codes == ["source_identity_mismatch"]
    (out / "provenance.jsonl").write_text(provenance)
    manifest = json.loads((out / "manifest.json").read_text())
    manifest["inputs"][0]["images"][0]["bytes"] += 1
    (out / "manifest.json").write_text(json.dumps(manifest))
    errors = verify_run(out, check_external_sources=False)["errors"]
    assert "source_identity_mismatch" in [error["code"] for error in errors]


def test_image_evidence_binds_to_the_carrier_and_a_resumable_run_verifies(scans, stub, tmp_path):
    out = tmp_path / "out"
    stub["stub"] = StubOCR(evidence_root=out)
    run(
        inputs=[scans],
        config_path=config(tmp_path),
        out_dir=out,
        dry_run=False,
        pages="2-3",
        resumable=True,
    )
    entry = json.loads((out / "manifest.json").read_text())["inputs"][0]
    assert entry["pages"] == "2-3"
    assert entry["carrier"]["sha256"] == carrier_sha256(read_image_source(scans), [2, 3])
    for line in lines(out / "provenance.jsonl"):
        assert line["input_evidence"]["source_sha256"] == entry["carrier"]["sha256"]
    checkpoint = json.loads((out / "checkpoint.json").read_text())
    validate(checkpoint, "checkpoint")
    assert checkpoint["payload"]["skipped_inputs"] == [".DS_Store"]
    assert verify_run(out)["status"] == "pass"
    assert resume(out)["resumed"] is True


def test_an_adapter_that_reads_images_gets_the_page_images_themselves(scans, stub, tmp_path):
    reader = StubOCR()
    reader.input_types = ("image",)
    stub["stub"] = reader
    out = tmp_path / "out"
    run(inputs=[scans], config_path=config(tmp_path), out_dir=out, dry_run=False)
    assert [path.name for path, _ in reader.seen] == ["page-1.jpg", "page-2.png", "page-10.png"]
    assert "carrier" not in json.loads((out / "manifest.json").read_text())["inputs"][0]
    assert verify_run(out)["status"] == "pass"


def test_a_single_image_is_a_one_page_document(stub, tmp_path):
    image = tmp_path / "plate.jpg"
    image.write_bytes(jpeg())
    out = tmp_path / "out"
    run(inputs=[image], config_path=config(tmp_path), out_dir=out, dry_run=False)
    (entry,) = json.loads((out / "manifest.json").read_text())["inputs"]
    assert (entry["kind"], entry["page_count"], entry["images"][0]["name"]) == (
        KIND,
        1,
        "plate.jpg",
    )
    assert verify_run(out)["status"] == "pass"


def test_a_dry_run_plans_pages_without_a_carrier(scans, tmp_path):
    out = tmp_path / "out"
    result = run(inputs=[scans], config_path=config(tmp_path), out_dir=out, dry_run=True)
    assert result["summary"]["pages_total"] == 3
    entry = json.loads((out / "manifest.json").read_text())["inputs"][0]
    assert entry["kind"] == KIND and "carrier" not in entry
    assert verify_run(out)["status"] == "pass"


def test_the_text_adapter_refuses_page_images(scans, tmp_path):
    with pytest.raises(ValueError, match="Adapter 'text' cannot read page images"):
        run(
            inputs=[scans],
            config_path=config(tmp_path, "text"),
            out_dir=tmp_path / "o",
            dry_run=False,
        )


def test_pdf_text_finds_no_text_layer_in_page_images_without_pypdf(scans, tmp_path, monkeypatch):
    import pageledger.adapters as adapters

    monkeypatch.setattr(adapters, "_load_pypdf", lambda: pytest.fail("pypdf was needed"))
    out = tmp_path / "out"
    run(inputs=[scans], config_path=config(tmp_path, "pdf_text"), out_dir=out, dry_run=False)
    assert all((out / "raw" / f"doc_0001_page_000{n}.txt").read_text() == "" for n in (1, 2, 3))
    assert all(
        line["warnings"][:2] == ["no_text_layer", "empty_text"]
        for line in lines(out / "quality.jsonl")
    )


def test_a_flagged_page_of_page_images_reruns(scans, stub, tmp_path):
    stub["stub"] = StubOCR(warnings=["render_upsampled"])
    parent, child = tmp_path / "parent", tmp_path / "child"
    run(inputs=[scans], config_path=config(tmp_path), out_dir=parent, dry_run=False)
    audit = json.loads((parent / "audit.json").read_text())
    assert {item["reason"] for item in audit["review_queue"]} == {"quality_warning"}
    stub["stub"] = StubOCR()
    rerun(parent_dir=parent, config_path=config(tmp_path), out_dir=child)
    entry = json.loads((child / "manifest.json").read_text())["inputs"][0]
    assert entry["sha256"] == read_image_source(scans).sha256
    assert verify_run(child)["status"] == "pass"
    from pageledger.compare import compare_runs

    report = compare_runs(parent, child)
    assert report["pages_compared"] == 3
    assert all("render_upsampled" in page["warnings_resolved"] for page in report["pages"])


def test_classify_and_bundle_refuse_page_images_plainly(scans, stub, tmp_path):
    from pageledger.classifier import classify
    from pageledger.cli import _config_template

    template = tmp_path / "classify.yml"
    template.write_text(_config_template("pdf_ocr"), encoding="utf-8")
    with pytest.raises(ValueError, match="no text layer to classify"):
        classify(inputs=[scans], config_path=template, out_path=tmp_path / "routes.yml")
    out = tmp_path / "out"
    run(inputs=[scans], config_path=config(tmp_path), out_dir=out, dry_run=False)
    with pytest.raises(ReplayError) as error:
        bundle_run(out, tmp_path / "bundle")
    assert error.value.code == "image_source_unsupported"


def test_a_run_of_page_images_classifies_from_run_and_routes_a_second_run(scans, stub, tmp_path):
    from pageledger.classifier import classify
    from pageledger.cli import _config_template

    template = tmp_path / "classify.yml"
    template.write_text(_config_template("stub"), encoding="utf-8")
    first, second = tmp_path / "first", tmp_path / "second"
    run(inputs=[scans], config_path=template, out_dir=first, dry_run=False)
    routes = tmp_path / "routes.yml"
    result = classify(inputs=[], config_path=template, out_path=routes, from_run=first)
    assert not any("Source" in warning for warning in result.get("warnings", []))
    document = yaml.safe_load(routes.read_text())["documents"][0]
    assert document["source_sha256"] == read_image_source(scans).sha256
    run(inputs=[scans], config_path=template, out_dir=second, dry_run=False, routes_path=routes)
    assert verify_run(second)["status"] == "pass"


# -- render_upsampled -----------------------------------------------------------------


def test_render_upsampled_flags_a_render_well_beyond_the_images_pixels(scans, tmp_path):
    source = read_image_source(scans)
    with carrier(source, [1]) as (path, _):
        assert native_page_pixels(path, 1)[:2] == (32, 16)
        assert render_warnings(path, 1, pixels=(48, 24)) == []
        assert render_warnings(path, 1, pixels=(49, 25)) == ["render_upsampled"]
        assert render_warnings(path, 1, dpi=300) == []
        assert render_warnings(path, 1, dpi=600) == ["render_upsampled"]
        scanned = tmp_path / "scanned.pdf"
        scanned.write_bytes(path.read_bytes())
    pytest.importorskip("pypdf")
    # The same PDF, no longer a registered carrier: a scan whose image fills the page.
    assert native_page_pixels(scanned, 1)[:2] == (32, 16)
    # A page of other proportions: its image is not plausibly the scan, so nothing is claimed.
    scanned.write_bytes(scanned.read_bytes().replace(b"[0 0 7.68 3.84]", b"[0 0 7.68 7.68]", 1))
    assert native_page_pixels(scanned, 1) is None
    assert render_warnings(scanned, 1, pixels=(4000, 4000)) == []


@pytest.mark.skipif(
    shutil.which("pdftoppm") is None or shutil.which("tesseract") is None,
    reason="needs Poppler and Tesseract",
)
def test_pdf_ocr_warns_when_it_renders_a_low_density_image_at_300_dpi(tmp_path):
    image = tmp_path / "page.jpg"
    image.write_bytes(jpeg(units=1, density=100))
    out = tmp_path / "out"
    run(inputs=[image], config_path=config(tmp_path, "pdf_ocr"), out_dir=out, dry_run=False)
    (line,) = lines(out / "quality.jsonl")
    assert "render_upsampled" in line["warnings"]


# -- Document jobs ----------------------------------------------------------------------


def _job(scans, stub, tmp_path, warnings=()):
    stub["stub"] = StubOCR(warnings=warnings)
    path = tmp_path / "job.yml"
    data = {
        "schema_version": "0.1",
        "processing": {"local_text": {"adapter": "pdf_text"}, "local_ocr": {"adapter": "stub"}},
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return process(source=scans, config_path=path, out_dir=tmp_path / "job")


def test_a_job_on_page_images_finds_no_text_layer_and_reads_them_with_ocr(scans, stub, tmp_path):
    assert _job(scans, stub, tmp_path)["status"] == "completed"
    root = tmp_path / "job"
    job = json.loads((root / "job.json").read_text())
    validate(job, "job")
    validate(json.loads((root / "document.json").read_text()), "document")
    payload = job["payload"]
    source = read_image_source(scans)
    assert payload["source"]["kind"] == KIND
    assert payload["source"]["images"] == source.manifest()
    assert payload["policy"]["local_ocr"]["adapter"] == "stub"
    for page, image in zip(payload["pages"], source.pages, strict=True):
        assert page["source_image_sha256"] == image.sha256
        assert page["disposition"] == "unreviewed_text"
        assert page["review_reasons"] == []
        assert page["selected_attempt"].startswith("local_ocr")
    report = (root / "report.md").read_text()
    assert "(<../scans/page-10.png>)" in report
    assert verify_job(root)["status"] == "pass"


def test_verify_job_names_a_changed_page_image(scans, stub, tmp_path):
    _job(scans, stub, tmp_path)
    (scans / "page-2.png").write_bytes(png())
    result = verify_job(tmp_path / "job")
    assert result["status"] == "fail"
    assert result["error"] == "Document job source changed or is missing: changed: page-2.png"


def test_render_upsampled_is_recorded_evidence_and_holds_nothing(scans, stub, tmp_path):
    _job(scans, stub, tmp_path, warnings=["render_upsampled"])
    payload = json.loads((tmp_path / "job" / "job.json").read_text())["payload"]
    for page in payload["pages"]:
        ocr = page["attempts"][-1]
        assert ocr["warnings"] == ["render_upsampled"]
        assert (page["disposition"], page["review_reasons"]) == ("unreviewed_text", [])
        assert page["selected_attempt"] == ocr["attempt_id"]


def test_a_job_refuses_the_text_adapter_for_page_images_before_it_starts(scans, tmp_path):
    path = tmp_path / "job.yml"
    data = {"schema_version": "0.1", "processing": {"local_text": {"adapter": "text"}}}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="text adapter cannot read page images"):
        process(source=scans, config_path=path, out_dir=tmp_path / "job")
    assert not (tmp_path / "job").exists()


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="needs Poppler's pdftoppm")
def test_the_vision_adapter_reads_page_images_and_says_when_it_upsampled(scans, endpoint, tmp_path):  # noqa: F811
    options = {"base_url": endpoint, "model": "requested", "max_image_side": 256}
    out = tmp_path / "out"
    run(
        inputs=[scans],
        config_path=config(tmp_path, "vision", adapter_options=options),
        out_dir=out,
        dry_run=False,
        pages="1",
    )
    (line,) = lines(out / "quality.jsonl")
    assert "render_upsampled" in line["warnings"]
    assert verify_run(out)["status"] == "pass"


# -- Recovery, readers and moved folders ---------------------------------------------------


def _interrupted(scans, stub, tmp_path):
    out = tmp_path / "out"
    stub["stub"] = StubOCR(evidence_root=out)

    def interrupt(completed_phase, _duration):
        if completed_phase == "page_log_control":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(
            inputs=[scans],
            config_path=config(tmp_path),
            out_dir=out,
            dry_run=False,
            resumable=True,
            _phase_observer=interrupt,
        )
    assert [number for _, number in stub["stub"].seen] == [1]
    return out


def test_an_interrupted_run_of_page_images_resumes_on_the_same_carrier(scans, stub, tmp_path):
    out = _interrupted(scans, stub, tmp_path)
    recorded = json.loads((out / "checkpoint.json").read_text())["payload"]["inputs"][0]["carrier"]
    assert resume(out)["status"] == "completed"
    assert [number for _, number in stub["stub"].seen] == [1, 2, 3]
    assert json.loads((out / "manifest.json").read_text())["inputs"][0]["carrier"] == recorded
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    registry = Registry().with_resources(
        (path.name, Resource.from_contents(json.loads(path.read_text())))
        for path in SCHEMAS.glob("*.schema.json")
    )
    page_schema = json.loads((SCHEMAS / "checkpoint-page.schema.json").read_text())
    for record in (out / ".checkpoint" / "pages").glob("*.json"):
        Draft202012Validator(page_schema, registry=registry).validate(
            json.loads(record.read_text())
        )
    for line in lines(out / "provenance.jsonl"):
        assert line["input_evidence"]["source_sha256"] == recorded["sha256"]
    assert verify_run(out)["status"] == "pass"


def test_resume_refuses_a_carrier_that_is_not_the_recorded_one(scans, stub, tmp_path, monkeypatch):
    out = _interrupted(scans, stub, tmp_path)
    original = runner.carrier

    def other(source, pages):
        with original(source, pages) as (path, _sha256):
            yield path, "0" * 64

    from contextlib import contextmanager

    monkeypatch.setattr(runner, "carrier", contextmanager(other))
    with pytest.raises(ValueError, match="carrier differs from the recorded one"):
        resume(out)
    assert [number for _, number in stub["stub"].seen] == [1]


def test_a_job_with_an_image_stage_binds_each_readers_evidence_to_its_carrier(
    scans, monkeypatch, tmp_path
):
    from pageledger.export import export_job

    real = runner.load_adapter

    def load(name, options=None):
        if name == "ocr":
            return StubOCR(warnings=["coverage_defect"])
        if name == "reader":
            evidence = (options or {}).get("evidence_dir")
            reader = StubOCR(evidence_root=Path(evidence).parent if evidence else None)
            reader.capabilities = ("generative",)
            return reader
        return real(name, options)

    monkeypatch.setattr(runner, "load_adapter", load)
    path = tmp_path / "job.yml"
    data = {
        "schema_version": "0.1",
        "processing": {
            "local_text": {"adapter": "pdf_text"},
            "local_ocr": {"adapter": "ocr"},
            "image": {"adapter": "reader"},
            "limits": {"max_image_pages": 3},
            "batch": {"pages": 3, "concurrent": 2},
        },
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    root = tmp_path / "job"
    process(source=scans, config_path=path, out_dir=root)
    payload = json.loads((root / "job.json").read_text())["payload"]
    for page in payload["pages"]:
        attempt = next(item for item in page["attempts"] if item["stage"] == "image")
        child = json.loads((root / attempt["run_path"] / "manifest.json").read_text())
        carrier_sha = child["inputs"][0]["carrier"]["sha256"]
        assert child["inputs"][0]["carrier"]["pages"] == [page["page_number"]]
        assert attempt["input_evidence"]["source_sha256"] == carrier_sha
        assert carrier_sha != payload["source"]["sha256"]
    assert verify_job(root)["status"] == "pass"
    export_job(root, tmp_path / "book.tei", format="tei")
    assert 'facs="scans/page-10.png"' in (tmp_path / "book.tei").read_text()


def test_a_moved_folder_is_found_by_its_identity_through_the_source_index(
    scans, stub, tmp_path, monkeypatch
):
    out = tmp_path / "out"
    run(inputs=[scans], config_path=config(tmp_path), out_dir=out, dry_run=False)
    _job(scans, stub, tmp_path)
    identity = read_image_source(scans).sha256
    moved = tmp_path / "archive" / "scans"
    moved.parent.mkdir()
    scans.rename(moved)
    index = tmp_path / "index.tsv"
    index.write_text(f"{identity}\t{moved}\n", encoding="utf-8")
    monkeypatch.setenv("PAGELEDGER_SOURCE_INDEX", str(index))
    (warning,) = verify_run(out)["warnings"]
    assert (warning["code"], warning["located"]) == ("source_relocated", str(moved))
    assert verify_job(tmp_path / "job")["status"] == "pass"
    # A folder at the recorded path that no longer matches is passed over for the indexed one.
    scans.mkdir()
    (scans / "page-1.png").write_bytes(png())
    (warning,) = verify_run(out)["warnings"]
    assert warning["code"] == "source_relocated"


def test_a_carrier_that_does_not_rebuild_to_its_recorded_hash_is_reported(scans, stub, tmp_path):
    out = tmp_path / "out"
    run(inputs=[scans], config_path=config(tmp_path), out_dir=out, dry_run=False)
    manifest = json.loads((out / "manifest.json").read_text())
    manifest["inputs"][0]["carrier"]["sha256"] = "0" * 64
    (out / "manifest.json").write_text(json.dumps(manifest))
    report = verify_run(out)
    assert [item["code"] for item in report["warnings"]] == ["carrier_not_reproduced"]


def test_a_page_image_may_render_below_72_dpi_down_to_half_its_own_density(tmp_path):
    from pageledger.adapters import _render_floor

    for density, floor in ((100, 50), (300, 72)):
        image = tmp_path / f"page-{density}.jpg"
        image.write_bytes(jpeg(units=1, density=density))
        with carrier(read_image_source(image), [1]) as (path, _):
            assert _render_floor(path, 1) == floor
    assert _render_floor(tmp_path / "scan.pdf", 1) == 72


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="needs Poppler's pdftoppm")
def test_rapidocr_says_when_it_upsampled_a_page_image(scans, tmp_path, monkeypatch):
    from test_engine_examples import _engine_for, _fake_rapidocr, _recognizer

    from pageledger.adapters import load_adapter

    _fake_rapidocr(monkeypatch, _engine_for([]))
    recognizer = str(_recognizer(tmp_path / "rec"))
    image = tmp_path / "plate.png"
    image.write_bytes(png(600, 300))
    with carrier(read_image_source(image), [1]) as (path, _):
        for side, warnings in ((900, []), (901, ["render_upsampled"])):
            adapter = load_adapter("rapidocr", {"rec_model_dir": recognizer, "max_side": side})
            result = adapter.extract(path, page_id="p1", page_number=1, action="transcribe_text")
            assert result.warnings == warnings


def test_indra_says_when_it_upsampled_a_page_image_and_not_for_a_text_layer(
    scans, tmp_path, monkeypatch
):
    from test_indra_adapter import PAGE, _fake_indra

    from pageledger.adapters import load_adapter

    with carrier(read_image_source(scans), [1]) as (path, _):
        for source, dpi, warnings in (
            ("ocr", 300, []),
            ("ocr", 600, ["render_upsampled"]),
            ("text", 600, []),
        ):
            _fake_indra(monkeypatch, tmp_path, dataclasses.replace(PAGE, source=source))
            adapter = load_adapter("indra", {"dpi": dpi})
            result = adapter.extract(path, page_id="p1", page_number=1, action="transcribe_text")
            assert result.warnings == warnings


def test_review_resume_and_export_work_on_a_job_of_page_images(scans, stub, tmp_path):
    from pageledger.export import export_job
    from pageledger.processing import create_review_sheet, resume_job, review_job

    stub["stub"] = StubOCR()
    path = tmp_path / "job.yml"
    data = {
        "schema_version": "0.1",
        "processing": {
            "local_text": {"adapter": "pdf_text"},
            "local_ocr": {"adapter": "stub"},
            "limits": {"max_attempt_pages": 4},
        },
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    root = tmp_path / "job"
    assert process(source=scans, config_path=path, out_dir=root)["status"] == "paused_budget"
    assert resume_job(root, raise_limits={"max_attempt_pages": 6})["status"] == "completed"
    sheet = tmp_path / "review.csv"
    create_review_sheet(root, sheet)
    import csv

    with sheet.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[2]["source_page"].endswith("scans/page-10.png")
    with sheet.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({**row, "decision": "accept"} for row in rows)
    assert review_job(root, sheet, reviewer="Tester")["decisions"] == {"reviewed_text": 3}
    assert verify_job(root)["status"] == "pass"
    export_job(root, tmp_path / "book.md", format="md")
    assert "(<scans/page-2.png>)" in (tmp_path / "book.md").read_text()


# -- The review view --------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="needs Poppler")
def test_the_review_view_shows_each_pages_own_image_also_from_a_moved_job(scans, stub, tmp_path):
    from pageledger.review_html import write_review_html

    _job(scans, stub, tmp_path)
    for root, view in ((tmp_path / "job", "view"), (tmp_path / "deposit" / "job", "moved")):
        if not root.exists():
            shutil.copytree(tmp_path / "job", root)
        result = write_review_html(root, tmp_path / view)
        assert result["images"] == 3
        for row in ("p0001", "p0002", "p0003"):
            image = (tmp_path / view / "images" / f"{row}.jpg").read_bytes()
            assert image[:2] == b"\xff\xd8"
        page = (tmp_path / view / "index.html").read_text(encoding="utf-8")
        assert '<h2 id="p0003-title">Page 3 (page-10.png)</h2>' in page
        assert 'href="../scans/page-10.png">Page 3 (page-10.png) in the source</a>' in page
    assert write_review_html(tmp_path / "job", tmp_path / "none", images="none")["images"] == 0


def test_without_poppler_the_review_view_links_each_page_image(scans, stub, tmp_path, monkeypatch):
    from pageledger import review_html

    _job(scans, stub, tmp_path)
    monkeypatch.setattr(review_html.shutil, "which", lambda name: None)
    result = review_html.write_review_html(tmp_path / "job", tmp_path / "view")
    assert result["images"] == 0
    assert not (tmp_path / "view" / "images").exists()
    page = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    assert "No page image in this view." in page
    assert 'href="../scans/page-2.png">Page 2 (page-2.png) in the source</a>' in page
