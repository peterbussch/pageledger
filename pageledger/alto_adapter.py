"""The built-in ``alto`` adapter: OCR a library published beside its scans, read as a page's text layer.

ALTO is an XML format for OCR output: every word (``String``) with its text, its box
on the page and usually a confidence. Libraries such as the Library of Congress
publish one ALTO file per page image. This adapter reads the file that belongs to
a page and returns its words as one reading, so the institution's OCR enters a
document job as a witness with its own provenance.

Configure with ``adapter_options``: ``dir``, the folder that holds the ALTO files,
and ``name``, a template for one page's file name inside it (default
``{page:04d}.xml``; ``{page}`` is the 1-based page number, with an optional
zero-padded width such as ``{page:04d}``). For a page-image source ``{stem}`` is
the page image's file name without its extension, so ``{stem}.xml`` finds
``0081.xml`` for ``0081.jpg``; a PDF's pages have no names, so it is refused there.

The text is built from words, never from ``TextLine`` grouping, because ALTO lines
can run across columns. Words are kept as printed (``CONTENT``, never
``SUBS_CONTENT``); a ``HYP`` element adds its printed hyphen to the word before
it. A row that reads right to left (it holds Hebrew or Arabic-script letters and
the file lists its words leftward across the page) is written from its
right-hand end, its words in the file's order, which ALTO gives in reading
order; the column order stays left to right. Text is not Unicode-normalised, as with every other adapter; comparisons
normalise when they compare.
"""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import string
import subprocess
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from statistics import mean
from typing import Any, ClassVar
from xml.parsers import expat

from .adapters import ExtractionResult, ocr_pdf_page_count
from .layout import GRID, UNITS, grid_box, validate_layout
from .rapidocr_adapter import _row_groups, _xy_cut, right_to_left

MAX_BYTES = 32 * 1024 * 1024
# Several times the words of a dense newspaper page. Column order costs more
# than linear time in the words, so a file is not allowed to ask for millions.
MAX_WORDS = 20_000
MEASUREMENT_UNITS = ("pixel", "mm10", "inch1200")
# The ALTO page and the PDF page may differ in shape by this share before the
# boxes are taken to describe some other image.
ASPECT_TOLERANCE = 0.02
DEFAULT_NAME = "{page:04d}.xml"
_FIELDS = ("page", "stem")
_NO_STEM = (
    "alto name uses {{stem}}, the page image's file name, but {source} is not a page image "
    "or a folder of page images; a PDF's pages have no file names of their own, so use "
    "{{page}} for a PDF"
)
_NUMBERED = (
    "alto name {template} finds each page's ALTO file by its position in the document, but "
    "the page images in {source} carry numbers of their own that are not their positions "
    "({example}), as in a IIIF snapshot with a canvas missing or a folder with a page left "
    "out. {{page}} would read another page's file. If the ALTO files are named after the "
    "images, use name: '{{stem}}.xml'; if they really are numbered by page position, name "
    "the images so that their numbers are their page numbers"
)
_PAGE_SPEC = re.compile(r"(0?[1-9])?d?\Z")
_CC = re.compile(r"[0-9](\s*[0-9])*\Z")


def numbered_apart(names: list[str]) -> str | None:
    """The first page image whose own number, the last run of digits in its name, is not its page."""
    for position, name in enumerate(names, start=1):
        numbers = re.findall(r"[0-9]+", PurePosixPath(name).stem)
        if numbers and int(numbers[-1]) != position:
            return f"'{name}' is page {position}"
    return None


class AltoInvalid(ValueError):
    """The file is not a single-page ALTO document this adapter can read."""


@dataclass(frozen=True)
class AltoWord:
    text: str
    # (HPOS, VPOS, HPOS + WIDTH, VPOS + HEIGHT) in the file's unit; None when the
    # file gives no complete box.
    box: tuple[float, float, float, float] | None
    confidence: float | None
    line: int


@dataclass(frozen=True)
class AltoPage:
    words: tuple[AltoWord, ...]
    width: float | None
    height: float | None
    unit: str | None
    software: str | None


def _template_parts(template: str) -> list[tuple[str, str | None, str]]:
    if type(template) is not str or not template:
        raise ValueError("alto name must be a non-empty template such as '{page:04d}.xml'")
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"alto name is not a valid template: {exc}") from exc
    parts = []
    for literal, field, spec, conversion in parsed:
        if field is not None:
            if field not in _FIELDS or conversion is not None:
                raise ValueError(
                    "alto name may only use the fields {page} and {stem}, without conversions"
                )
            if field == "page" and not _PAGE_SPEC.match(spec or ""):
                raise ValueError("alto name formats {page} only as a zero-padded width, e.g. 04d")
            if field == "stem" and spec:
                raise ValueError("alto name takes {stem} without a format")
        parts.append((literal, field, spec or ""))
    path = PurePosixPath("".join(literal + ("x" if field else "") for literal, field, _ in parts))
    if "\\" in template or path.is_absolute() or ".." in path.parts:
        raise ValueError("alto name must name a file inside dir")
    return parts


def alto_name(template: str, page: int, stem: str | None = None) -> str:
    """The file name ``template`` gives page ``page`` (1-based)."""
    if type(page) is not int or page < 1:
        raise ValueError("page must be a positive integer")
    out = []
    for literal, field, spec in _template_parts(template):
        out.append(literal)
        if field == "page":
            out.append(format(page, spec or "d"))
        elif field == "stem":
            if not stem or "/" in stem or "\\" in stem or stem in {".", ".."}:
                raise ValueError("alto {stem} needs the page image's file name without folders")
            out.append(stem)
    return "".join(out)


def alto_path(directory: Path, template: str, page: int, stem: str | None = None) -> Path:
    """The ALTO file for a page, refused if it resolves outside ``directory``."""
    root = directory.expanduser().resolve()
    path = (root / alto_name(template, page, stem)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("alto file resolves outside dir")
    return path


def read_alto(path: Path) -> bytes:
    """A file's bytes, read once and refused above ``MAX_BYTES``."""
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise AltoInvalid(f"over {MAX_BYTES // (1024 * 1024)} MiB")
    return data


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _refuse_declarations(data: bytes) -> None:
    """Refuse a document type or entity declaration in any encoding, before parsing.

    ALTO never needs a DTD, and a DTD is how entity expansion bombs and external
    entities get in. The byte search gives a clear reason for UTF-8 files; expat,
    which reads whatever encoding the file declares, catches the rest.
    """
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise AltoInvalid("contains a DOCTYPE or ENTITY declaration")

    def refuse(*_: Any) -> None:
        raise AltoInvalid("contains a DOCTYPE or ENTITY declaration")

    scanner = expat.ParserCreate()
    scanner.StartDoctypeDeclHandler = refuse
    scanner.EntityDeclHandler = refuse
    try:
        scanner.Parse(data, True)
    except expat.ExpatError as exc:
        raise AltoInvalid(f"not well-formed XML: {exc}") from exc


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _confidence(element: ElementTree.Element) -> float | None:
    # WC: 0 (unsure) to 1 (sure). CC: one digit per character, 0 (sure) to 9 (unsure).
    wc = _number(element.get("WC"))
    if wc is not None:
        return wc if 0 <= wc <= 1 else None
    cc = (element.get("CC") or "").strip()
    if cc and _CC.match(cc):
        digits = [int(c) for c in cc if c.isdigit()]
        return 1 - mean(digits) / 9
    return None


def _clean(value: str | None) -> str:
    text = "".join(c for c in (value or "") if c.isprintable())
    return " ".join(text.split())[:80]


def _software(root: ElementTree.Element) -> str | None:
    names = []
    for element in root.iter():
        if _local(element.tag) != "processingSoftware":
            continue
        parts = {_local(child.tag): _clean(child.text) for child in element}
        label = " ".join(p for p in (parts.get("softwareName"), parts.get("softwareVersion")) if p)
        if label and label not in names:
            names.append(label)
    return ", ".join(names) or None


def parse_alto(data: bytes) -> AltoPage:
    """Parse one ALTO file (versions 1 to 4) that holds exactly one page."""
    _refuse_declarations(data)
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise AltoInvalid(f"not well-formed XML: {exc}") from exc
    if _local(root.tag) != "alto":
        raise AltoInvalid("the root element is not alto")
    pages = [element for element in root.iter() if _local(element.tag) == "Page"]
    if len(pages) != 1:
        raise AltoInvalid(f"holds {len(pages)} Page elements; exactly one is read")
    units = [e for e in root.iter() if _local(e.tag) == "MeasurementUnit"]
    unit = (units[0].text or "").strip() if units else None
    if unit is not None and unit not in MEASUREMENT_UNITS:
        raise AltoInvalid(f"MeasurementUnit {unit!r} is not one of {', '.join(MEASUREMENT_UNITS)}")
    page = pages[0]
    width, height = _number(page.get("WIDTH")), _number(page.get("HEIGHT"))
    words: list[AltoWord] = []
    lines = (e for e in page.iter() if _local(e.tag) == "TextLine")
    for number, line in enumerate(lines):
        for child in line:
            kind = _local(child.tag)
            if kind == "HYP" and words and words[-1].line == number:
                hyphen = child.get("CONTENT", "")
                words[-1] = AltoWord(
                    words[-1].text + hyphen, words[-1].box, words[-1].confidence, number
                )
            elif kind == "String":
                text = child.get("CONTENT", "")
                if not text.strip():
                    continue
                x, y = _number(child.get("HPOS")), _number(child.get("VPOS"))
                w, h = _number(child.get("WIDTH")), _number(child.get("HEIGHT"))
                box = None
                # A box that is negative or overflows is no box: the page keeps file order.
                if x is not None and y is not None and w is not None and h is not None:
                    if w >= 0 and h >= 0 and math.isfinite(x + w) and math.isfinite(y + h):
                        box = (x, y, x + w, y + h)
                words.append(AltoWord(text, box, _confidence(child), number))
                if len(words) > MAX_WORDS:
                    raise AltoInvalid(f"holds more than {MAX_WORDS:,} words")
    return AltoPage(
        tuple(words),
        width if width and width > 0 else None,
        height if height and height > 0 else None,
        unit,
        _software(root),
    )


def page_text(page: AltoPage) -> str:
    """The page's words in reading order (see ``_page_text``)."""
    return _page_text(page)[0]


def _page_text(page: AltoPage) -> tuple[str, int]:
    """The page's words in reading order, and how many rows were written right to left.

    Words in one visual row are first joined into runs wherever they stand less
    than the median word height apart: a word space is narrower than that, a
    column gutter rarely is. Joining first keeps word spaces that happen to line
    up from row to row (a table, a directory) from being read as gutters. The
    runs are then cut into columns and bands as RapidOCR's ``columns`` reading
    order does, and each block is written row by row, its runs joined by spaces.
    Without a box for every word, the words keep the file's order, a line of the
    file per line.
    """
    words = list(page.words)
    if not words:
        return "", 0
    if any(word.box is None for word in words):
        lines: dict[int, list[str]] = {}
        for word in words:
            lines.setdefault(word.line, []).append(word.text)
        return "\n".join(" ".join(texts) for texts in lines.values()), 0
    items = [
        {**_item(word.text, *word.box), "index": index}
        for index, word in enumerate(words)
        if word.box is not None
    ]
    runs = _runs(items)
    heights = sorted(run["h"] for run in runs)
    gap = max(heights[len(heights) // 2], 1e-9)
    blocks: list[list[dict[str, Any]]] = []
    for block in _xy_cut(runs, gap):
        # A row the cut split at its word spaces is one row again.
        if blocks and len(_row_groups(blocks[-1] + block)) == 1:
            blocks[-1] = blocks[-1] + block
        else:
            blocks.append(block)
    rows, backwards = [], 0
    for block in blocks:
        for row in _row_groups(block):
            in_file_order = sorted(row, key=lambda item: item["index"])
            reverse = right_to_left([(item["text"], item["cx"]) for item in in_file_order])
            backwards += reverse
            ordered = sorted(row, key=lambda item: item["cx"], reverse=reverse)
            rows.append(" ".join(item["text"] for item in ordered))
    return "\n".join(rows), backwards


def _item(text: str, x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    return {
        "text": text,
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "cx": (x0 + x1) / 2,
        "cy": (y0 + y1) / 2,
        "h": y1 - y0,
    }


def _runs(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    words = [{"index": index, **word} for index, word in enumerate(words)]
    heights = sorted(word["h"] for word in words)
    gap = heights[len(heights) // 2]
    runs = []
    for row in _row_groups(words):
        ordered = sorted(row, key=lambda word: word["x0"])
        current, right = [ordered[0]], ordered[0]["x1"]
        for word in ordered[1:]:
            if word["x0"] - right < gap:
                current.append(word)
                right = max(right, word["x1"])
            else:
                runs.append(current)
                current, right = [word], word["x1"]
        runs.append(current)
    return [
        {
            **_item(
                " ".join(w["text"] for w in _in_file_order_if_right_to_left(run)),
                min(w["x0"] for w in run),
                min(w["y0"] for w in run),
                max(w["x1"] for w in run),
                max(w["y1"] for w in run),
            ),
            "index": min(w["index"] for w in run),
        }
        for run in runs
    ]


def _in_file_order_if_right_to_left(run: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A run's words left to right, or, when they read right to left, as the file lists them.

    The file's order is the reading order, which also keeps a Latin name inside a
    Hebrew line the right way round.
    """
    in_file_order = sorted(run, key=lambda word: word["index"])
    reads = right_to_left([(word["text"], word["cx"]) for word in in_file_order])
    return in_file_order if reads else run


def confidence_detail(page: AltoPage) -> dict[str, Any] | None:
    """Word confidences in RapidOCR's shape (percent), or None when the file gives none.

    Only scored words have a confidence; ``unscored_count`` says how many words the
    file gives no usable ``WC`` or ``CC`` for, so a mean over a few scored words is
    not mistaken for the page's.
    """
    scores = [word.confidence * 100 for word in page.words if word.confidence is not None]
    if not scores:
        return None
    low = sum(score < 60 for score in scores)
    return {
        "word_count": len(scores),
        "unscored_count": len(page.words) - len(scores),
        "mean": round(mean(scores), 2),
        "min": round(min(scores), 2),
        "below_60_count": low,
        "below_60_ratio": round(low / len(scores), 4),
    }


def page_layout(
    page: AltoPage,
    *,
    file: str,
    sha256: str,
    page_width: float | None,
    page_height: float | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """The page's word layout, or None and the reason it cannot be trusted.

    ``page_width`` and ``page_height`` are the PDF page as rendered (its media box
    after ``/Rotate``), in any unit. The ALTO page must declare its size, have the
    same shape within ``ASPECT_TOLERANCE``, and hold every word box inside it. A
    box at the wrong place inside the page passes; only a comparison with the
    job's own OCR can catch that.
    """
    if page.width is None or page.height is None:
        return None, "the ALTO page declares no width and height"
    if not (page_width and page_height and page_width > 0 and page_height > 0):
        return None, "the rendered page size is unknown"
    ratio = (page.width / page.height) / (page_width / page_height)
    if abs(ratio - 1) > ASPECT_TOLERANCE:
        return None, f"the ALTO page's shape differs from the page's by {abs(ratio - 1):.1%}"
    items = []
    for word in page.words:
        if word.box is None:
            return None, "a word has no box"
        x0, y0, x1, y1 = word.box
        if not (0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height):
            return None, f"the box of {word.text!r} is empty or outside the page"
        box = grid_box(x0, y0, x1, y1, page.width, page.height)
        if box[0] < box[2] <= GRID and box[1] < box[3] <= GRID:
            items.append({"box": box, "text": word.text})
    layout = {
        "units": UNITS,
        "level": "word",
        "items": items,
        "source": {
            "file": file,
            "sha256": sha256,
            "software": page.software,
            "measurement_unit": page.unit,
            "page_width": page.width,
            "page_height": page.height,
        },
    }
    validate_layout(layout)
    return layout, None


_BOX = re.compile(r"Page\s+(\d+)\s+MediaBox:\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)")
_ROT = re.compile(r"Page\s+(\d+)\s+rot:\s+(-?\d+)")


def rendered_page_size(source: Path, page_number: int) -> tuple[float, float] | None:
    """A PDF page's media box after ``/Rotate``, in points, from ``pdfinfo -box``.

    That is the page ``pdftoppm`` renders. None when ``pdfinfo`` cannot say.
    """
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo is None:
        return None
    try:
        proc = subprocess.run(
            [pdfinfo, "-f", str(page_number), "-l", str(page_number), "-box", str(source)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    box = next((m for m in _BOX.finditer(proc.stdout) if int(m.group(1)) == page_number), None)
    rot = next((m for m in _ROT.finditer(proc.stdout) if int(m.group(1)) == page_number), None)
    if proc.returncode != 0 or box is None:
        return None
    try:
        x0, y0, x1, y1 = (float(box.group(i)) for i in range(2, 6))
    except ValueError:
        return None
    width, height = abs(x1 - x0), abs(y1 - y0)
    if rot is not None and int(rot.group(2)) % 180 == 90:
        width, height = height, width
    return (width, height) if width > 0 and height > 0 else None


class AltoAdapter:
    name: ClassVar[str] = "alto"
    version: ClassVar[str] = "0.1"
    deterministic: ClassVar[bool] = True
    input_types: ClassVar[tuple[str, ...]] = ("pdf",)
    output_types: ClassVar[tuple[str, ...]] = ("text",)
    capabilities: ClassVar[tuple[str, ...]] = ("embedded_text", "local", "layout")

    def __init__(self, dir: str, name: str = DEFAULT_NAME) -> None:
        if type(dir) is not str or not dir:
            raise ValueError("alto dir must name the folder that holds the ALTO files")
        parts = _template_parts(name)
        if not Path(dir).expanduser().is_dir():
            raise ValueError(f"alto dir is not a folder: {dir}")
        self.directory = Path(dir).expanduser()
        self.template = name
        self.uses_stem = any(field == "stem" for _, field, _ in parts)

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def preflight(self, sources: list[Path]) -> None:
        from .image_source import is_image_source, page_names

        for source in sources:
            if self.uses_stem and not is_image_source(source):
                raise ValueError(_NO_STEM.format(source=source))
            if not self.uses_stem and is_image_source(source):
                self._check_positions(page_names(source), source)
        if shutil.which("pdfinfo") is None:
            raise ValueError("pdfinfo is required to check ALTO geometry; install Poppler")

    def _check_positions(self, names: list[str], source: Path) -> None:
        example = numbered_apart(names)
        if example is not None:
            raise ValueError(
                _NUMBERED.format(template=self.template, source=source, example=example)
            )

    def _stem(self, source: Path, page_number: int) -> str | None:
        """The page image's file name without its extension, when the template asks for it."""
        from .image_source import carrier_page, carrier_source

        if not self.uses_stem:
            images = carrier_source(source)
            if images is not None:
                self._check_positions([page.name for page in images.pages], images.path)
            return None

        image = carrier_page(source, page_number)
        if image is None:
            raise ValueError(_NO_STEM.format(source=source))
        return Path(image.name).stem

    def page_count(self, source: Path) -> int:
        if source.suffix.lower() != ".pdf":
            raise ValueError(f"alto reads PDF sources: {source}")
        return ocr_pdf_page_count(source)

    def extract(
        self,
        source: Path,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        if not self.supports(action):
            raise ValueError(f"{self.name} does not support action: {action}")
        _ = page_id, prompt
        stem = self._stem(source, page_number)
        file = alto_name(self.template, page_number, stem)
        try:
            path = alto_path(self.directory, self.template, page_number, stem)
        except ValueError:
            # Only a symbolic link can lead out of dir: the template was checked.
            return self._empty(f"alto; file {file} outside dir", "alto_invalid")
        if not path.is_file():
            return self._empty(f"alto; file {file} missing", "alto_missing")
        try:
            data = read_alto(path)
        except (AltoInvalid, OSError):
            return self._empty(f"alto; file {file} unreadable", "alto_invalid")
        digest = hashlib.sha256(data).hexdigest()
        try:
            page = parse_alto(data)
        except AltoInvalid:
            return self._empty(f"alto; file {file} sha256:{digest}; invalid", "alto_invalid")
        size = rendered_page_size(source, page_number)
        layout, _reason = page_layout(
            page,
            file=file,
            sha256=digest,
            page_width=size[0] if size else None,
            page_height=size[1] if size else None,
        )
        text, right_to_left_rows = _page_text(page)
        warnings = ["ocr_text_layer"] if text.strip() else []
        if layout is None:
            warnings.append("alto_geometry_mismatch")
        detail = confidence_detail(page)
        return ExtractionResult(
            content=text,
            format="text",
            # A page confidence only when the file scores every word.
            confidence=(
                None
                if detail is None or detail["unscored_count"]
                else round(detail["mean"] / 100, 4)
            ),
            model=(
                f"alto; file {file} sha256:{digest}; software {page.software or 'not declared'}"
                + (f"; right-to-left-rows={right_to_left_rows}" if right_to_left_rows else "")
            ),
            warnings=warnings,
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
            confidence_detail=detail,
            layout=layout,
        )

    def _empty(self, model: str, warning: str) -> ExtractionResult:
        return ExtractionResult(
            content="",
            format="text",
            confidence=None,
            model=model,
            warnings=[warning],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
        )
