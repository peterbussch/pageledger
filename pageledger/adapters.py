"""Small built-in extractor adapters and the pagination seam."""

from __future__ import annotations

import difflib
import importlib
import importlib.metadata
import importlib.util
import logging
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO, ClassVar, Literal

from ._version import __version__

# The classic plain-text page break. Splitting on it lets a single text file
# carry multiple pages without any new dependency.
PAGE_DELIMITER = "\f"


@dataclass(frozen=True)
class ExtractionResult:
    content: str | dict[str, Any] | list[dict[str, Any]]
    format: Literal["text", "markdown", "json", "csv", "markdown_table"]
    confidence: float | None
    model: str | None
    warnings: list[str]
    # Canonical usage schema (the page is the required, portable unit):
    #   {"pages": int, "tokens": int|None,
    #    "compute_seconds": float|None, "cost_usd": float|None}
    usage: dict[str, Any]
    # Optional engine-native confidence evidence (e.g. Tesseract per-word
    # confidences). Shape is adapter-defined; recorded, never interpreted as
    # calibrated probability.
    confidence_detail: dict[str, Any] | None = None
    input_evidence: dict[str, Any] | None = None
    # Where the engine found the page's text (pageledger.layout); optional.
    layout: dict[str, Any] | None = None


ADAPTER_FAILURE_CODES = frozenset(
    {
        "MODEL_TIMEOUT",
        "MODEL_HTTP_ERROR",
        "MODEL_QUOTA",
        "MODEL_OUTPUT_TRUNCATED",
        "MODEL_CONTENT_FILTERED",
        "MODEL_RECITATION",
        "MODEL_NETWORK_ERROR",
        "MODEL_INVALID_RESPONSE",
        "MODEL_EMPTY_RESPONSE",
        "MODEL_UNAVAILABLE",
        "IMAGE_RENDER_ERROR",
        "IMAGE_EVIDENCE_INVALID",
    }
)


class PageLedgerDiagnostic(RuntimeError):
    """A setup, input or engine problem that PageLedger itself detected.

    The message is written by PageLedger and never includes adapter output,
    source text or credentials, so it is shown to the user without redaction.
    """

    CODES = frozenset(
        {
            "missing_binary",
            "missing_language_pack",
            "missing_crypto_dependency",
            "unsupported_encryption",
            "malformed_pdf",
            "render_limit",
            "engine_timeout",
            "unsupported_image",
            "malformed_image",
            "unreadable_text",
        }
    )

    def __init__(self, code: str, message: str) -> None:
        if code not in self.CODES:
            raise ValueError(f"Unknown PageLedger diagnostic code: {code}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class AdapterFailure(RuntimeError):
    """Safe, terminal failure; partial output is evidence, never a completion."""

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        partial_result: ExtractionResult | None = None,
    ):
        if not isinstance(code, str) or code not in ADAPTER_FAILURE_CODES:
            raise ValueError("Unsupported adapter failure code")
        if http_status is not None and (
            type(http_status) is not int or not 100 <= http_status <= 599
        ):
            raise ValueError("Adapter failure HTTP status must be 100..599 or null")
        if partial_result is not None and not isinstance(partial_result, ExtractionResult):
            raise ValueError("Adapter failure partial_result must be ExtractionResult or null")
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.partial_result = partial_result


PDF_ADAPTER_NAMES = {"pdf_text", "pdf"}

# Built-in adapters that only accept PDF input (pdf_ocr renders pages itself,
# so it does not need the pypdf extra for extraction).
PDF_ONLY_ADAPTER_NAMES = PDF_ADAPTER_NAMES | {"pdf_ocr"}

_ADAPTER_META_CHECKS: list[tuple[str, type | tuple[type, ...], str]] = [
    ("name", str, "must be a string"),
    ("version", str, "must be a version string"),
    ("deterministic", bool, "must be a bool"),
    ("input_types", (tuple, list), "must be a tuple or list of strings"),
    ("output_types", (tuple, list), "must be a tuple or list of strings"),
    ("capabilities", (tuple, list), "must be a tuple or list of capability strings"),
]


def paginate(source: Path, *, allow_pdf: bool = False) -> int:
    """Return the page count for a source.

    The page is PageLedger's canonical unit of work — the one metric every
    backend shares. Decodable text is split on the form-feed page break;
    PDF page counts are available through the optional ``pypdf`` extra during
    execution. Anything else is treated as a single opaque page.
    """
    if source.suffix.lower() == ".pdf":
        if not allow_pdf:
            return 1
        return _pdf_page_count(source)
    try:
        return len(text_pages(source))
    except (PageLedgerDiagnostic, OSError):
        return 1


def text_pages(source: Path) -> list[str]:
    """A UTF-8 text file's pages, split on the form feed.

    The bytes are decoded as they are, so CR and CRLF line endings stay in the page
    text; a leading byte-order mark is an encoding marker, not text. A form feed ends
    a page, so one at the very end (with nothing but white space after it, as OCR
    tools and exports write) does not start another, empty page.
    """
    try:
        text = source.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PageLedgerDiagnostic(
            "unreadable_text",
            f"{source.name} is not UTF-8 text (byte {exc.start} cannot be read as UTF-8); "
            "save it as UTF-8 and read it again",
        ) from None
    pages = text.split(PAGE_DELIMITER)
    if len(pages) > 1 and not pages[-1].strip():
        pages.pop()
    return pages


def adapter_page_count(adapter: Any, source: Path) -> int:
    """Return page count using an adapter hook when available."""
    hook = getattr(adapter, "page_count", None)
    if callable(hook):
        count = hook(source)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError(
                f"Adapter '{adapter.name}' page_count(source) must return a positive integer"
            )
        return count
    return paginate(source, allow_pdf=getattr(adapter, "name", None) in PDF_ADAPTER_NAMES)


@dataclass(frozen=True)
class TextAdapter:
    name: ClassVar[str] = "text"
    version: ClassVar[str] = "0.2"
    deterministic: ClassVar[bool] = True
    input_types: ClassVar[tuple[str, ...]] = ("text",)
    output_types: ClassVar[tuple[str, ...]] = ("text",)
    capabilities: ClassVar[tuple[str, ...]] = ("embedded_text", "local")

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def reproducibility_profile(self) -> dict[str, object]:
        return {"materials": []}

    def page_count(self, source: Path) -> int:
        # Undecodable text counts as one page; reading it then fails with the reason.
        return paginate(source, allow_pdf=False)

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
            raise ValueError(f"Text adapter does not support action: {action}")

        _ = page_id, prompt
        pages = text_pages(source)
        if page_number < 1 or page_number > len(pages):
            raise ValueError(f"page_number {page_number} out of range for {source}")
        return ExtractionResult(
            content=pages[page_number - 1],
            format="text",
            confidence=None,
            model=None,
            warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
        )


@dataclass(frozen=True)
class PdfTextAdapter:
    _documents: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    _pages: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    name: ClassVar[str] = "pdf_text"
    version: ClassVar[str] = "0.1"
    deterministic: ClassVar[bool] = True
    input_types: ClassVar[tuple[str, ...]] = ("pdf",)
    output_types: ClassVar[tuple[str, ...]] = ("text",)
    capabilities: ClassVar[tuple[str, ...]] = ("embedded_text", "local")

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def reproducibility_profile(self) -> dict[str, object]:
        from .replay import _ProfileUnavailable, package_material

        try:
            material = package_material("pypdf")
        except (OSError, RuntimeError, ValueError) as exc:
            raise _ProfileUnavailable("pdf_text material is unavailable") from exc
        return {"materials": [material]}

    def page_count(self, source: Path) -> int:
        # From the page tree: a sample of a long PDF reads only its own pages' text.
        count = _read_once(self._documents, source, _pdf_page_count)
        if not count:
            raise ValueError("PDF source has no pages")
        return count

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
            raise ValueError(f"PDF text adapter does not support action: {action}")

        _ = page_id, prompt
        from .image_source import carrier_page

        if carrier_page(source, page_number) is not None:
            # A carrier PageLedger wrote for page images has nothing but the images.
            return ExtractionResult(
                content="",
                format="text",
                confidence=None,
                model="page image; no text layer",
                warnings=["no_text_layer"],
                usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
            )
        if type(page_number) is not int or not 1 <= page_number <= self.page_count(source):
            raise ValueError(f"page_number {page_number} out of range for {source}")
        text, ocr_layer = _read_once(
            self._pages.setdefault(page_number, {}),
            source,
            lambda path: _pdf_page_reading(path, page_number),
        )
        layer = bool(ocr_layer) and bool(text.strip())
        return ExtractionResult(
            content=text,
            format="text",
            confidence=None,
            model=_pypdf_model_string(),
            # The layer is an earlier OCR of a scan, not text the author typeset.
            warnings=["ocr_text_layer"] if layer else [],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
            confidence_detail={"ocr_text_layer": ocr_layer} if layer else None,
        )


_LANG_PATTERN = re.compile(r"^[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*$")

# Generous per-page ceilings; a page that takes longer than this is stuck.
_RENDER_TIMEOUT_SECONDS = 120
_OCR_TIMEOUT_SECONDS = 300
_PROBE_DPI = 10
# Scans rarely exceed this resolution; a higher estimate means odd page metadata.
_MAX_NATIVE_DPI = 1200
_MIN_RENDER_DPI = 72
_DEFAULT_MAX_RENDER_PIXELS = 60_000_000


@dataclass(frozen=True)
class PdfOcrAdapter:
    """OCR scanned PDFs with Tesseract, one page at a time.

    Renders each page with ``pdftoppm`` and reads it with ``tesseract``.
    Both binaries must be installed separately (``pageledger doctor`` checks
    for them). Configure via ``run.adapter_options``: ``dpi`` (default 300)
    and ``lang`` (Tesseract language codes, e.g. ``eng`` or ``eng+deu``).
    """

    dpi: int = 300
    lang: str = "eng"
    max_render_pixels: int = _DEFAULT_MAX_RENDER_PIXELS
    _image_ppis: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    name: ClassVar[str] = "pdf_ocr"
    version: ClassVar[str] = "0.2"
    deterministic: ClassVar[bool] = True
    input_types: ClassVar[tuple[str, ...]] = ("pdf",)
    output_types: ClassVar[tuple[str, ...]] = ("text",)
    capabilities: ClassVar[tuple[str, ...]] = ("ocr", "local")

    def __post_init__(self) -> None:
        if not isinstance(self.dpi, int) or isinstance(self.dpi, bool):
            raise ValueError("run.adapter_options.dpi must be an integer")
        if not 50 <= self.dpi <= 1200:
            raise ValueError("run.adapter_options.dpi must be between 50 and 1200")
        if (
            not isinstance(self.max_render_pixels, int)
            or isinstance(self.max_render_pixels, bool)
            or not 1_000_000 <= self.max_render_pixels <= 400_000_000
        ):
            raise ValueError(
                "run.adapter_options.max_render_pixels must be an integer "
                "between 1000000 and 400000000"
            )
        if not isinstance(self.lang, str) or not _LANG_PATTERN.match(self.lang):
            raise ValueError(
                "run.adapter_options.lang must be a Tesseract language code "
                "such as 'eng' or 'eng+deu'"
            )

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def reproducibility_profile(self) -> dict[str, object]:
        from .replay import _ProfileUnavailable, binary_material, model_material

        try:
            tesseract = _require_binary("tesseract")
            pdftoppm = _require_binary("pdftoppm")
            data_dir = _tesseract_data_dir(tesseract)
            materials: list[dict[str, str]] = [
                binary_material("tesseract", tesseract, _tesseract_model_string()),
                binary_material("pdftoppm", pdftoppm, _pdftoppm_model_string()),
            ]
            for language in self.lang.split("+"):
                traineddata = data_dir / f"{language}.traineddata"
                materials.append(
                    model_material(
                        f"tesseract:{language}.traineddata",
                        traineddata,
                    )
                )
        except (OSError, RuntimeError, ValueError) as exc:
            raise _ProfileUnavailable("pdf_ocr material is unavailable") from exc
        return {"materials": materials}

    def preflight(self, sources: list[Path]) -> None:
        """Refuse before any page runs when the OCR tools or languages are missing."""
        _ = sources
        _require_binary("pdftoppm")
        tesseract = _require_binary("tesseract")
        _check_tesseract_langs(tesseract, self.lang)

    def page_count(self, source: Path) -> int:
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
            raise ValueError(f"PDF OCR adapter does not support action: {action}")
        _ = page_id, prompt
        pdftoppm = _require_binary("pdftoppm")
        tesseract = _require_binary("tesseract")
        _check_tesseract_langs(tesseract, self.lang)

        started = time.perf_counter()
        target_dpi = _native_image_dpi(source, page_number, self.dpi, self._image_ppis)
        render_dpi, capped = _render_dpi(
            pdftoppm,
            source,
            page_number,
            target_dpi,
            self.max_render_pixels,
            _render_floor(source, page_number),
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            prefix = tmp_path / "page"
            _run_ocr_command(
                [
                    pdftoppm,
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-r",
                    str(render_dpi),
                    "-png",
                    str(source),
                    str(prefix),
                ],
                timeout=_RENDER_TIMEOUT_SECONDS,
                context=f"pdftoppm failed rendering page {page_number} of {source}",
            )
            images = sorted(tmp_path.glob("page-*.png"))
            if not images:
                raise RuntimeError(f"pdftoppm produced no image for page {page_number} of {source}")
            rendered = _png_size(images[0])
            output_prefix = tmp_path / "ocr"
            _run_ocr_command(
                # Some Tesseract releases end the text with a form feed, PageLedger's page
                # break; an empty separator keeps the page's text the same across releases.
                [
                    tesseract,
                    str(images[0]),
                    str(output_prefix),
                    "-l",
                    self.lang,
                    "-c",
                    "page_separator=",
                    "txt",
                    "tsv",
                ],
                timeout=_OCR_TIMEOUT_SECONDS,
                context=f"tesseract failed on page {page_number} of {source}",
            )
            text = output_prefix.with_suffix(".txt").read_text(encoding="utf-8", errors="replace")
            confidence, confidence_detail = _tesseract_word_confidence(
                output_prefix.with_suffix(".tsv")
            )
        return ExtractionResult(
            content=text,
            format="text",
            confidence=confidence,
            model=(
                f"{_tesseract_model_string()}; {_pdftoppm_model_string()}; "
                f"{_dpi_note(render_dpi, self.dpi, raised=target_dpi > self.dpi)}; "
                f"lang={self.lang}"
            ),
            warnings=(["render_dpi_capped"] if capped else [])
            + render_warnings(source, page_number, pixels=rendered),
            usage={
                "pages": 1,
                "tokens": None,
                "compute_seconds": round(time.perf_counter() - started, 3),
                "cost_usd": None,
            },
            confidence_detail=confidence_detail,
        )


@lru_cache(maxsize=8)
def _tesseract_installed_langs(tesseract: str) -> frozenset[str] | None:
    """Installed language packs per ``tesseract --list-langs``.

    Returns None when the listing fails or cannot be parsed — an unreadable
    listing must never block extraction.
    """
    try:
        proc = subprocess.run(
            [tesseract, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    # First line is a "List of available languages..." banner on every
    # Tesseract version we know; keep only plausible language codes below it.
    langs = frozenset(
        line.strip()
        for line in (proc.stdout or "").splitlines()[1:]
        if line.strip() and _LANG_PATTERN.match(line.strip())
    )
    return langs or None


def _tesseract_data_dir(tesseract: str) -> Path:
    """Resolve Tesseract's data directory from its language-list banner."""
    try:
        proc = subprocess.run(
            [tesseract, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Cannot resolve Tesseract data directory") from exc
    if proc.returncode != 0:
        raise ValueError("Cannot resolve Tesseract data directory")
    first_line = (proc.stdout or "").splitlines()[:1]
    if not first_line:
        raise ValueError("Cannot resolve Tesseract data directory")
    banner = first_line[0]
    match = re.search(r"[\"']([^\"']+)[\"']", banner)
    if match is None:
        match = re.search(r"\bin\s+(.+?)(?:\s+\(\d+\))?$", banner)
    if match is None:
        raise ValueError("Cannot resolve Tesseract data directory")
    data_dir = Path(match.group(1).strip())
    if not data_dir.is_dir():
        raise ValueError(f"Cannot locate Tesseract data directory '{data_dir}'")
    return data_dir


# What people type for a language, and the three-letter code Tesseract wants.
_LANGUAGE_CODES = {
    "english": "eng",
    "en": "eng",
    "russian": "rus",
    "ru": "rus",
    "french": "fra",
    "fr": "fra",
    "german": "deu",
    "de": "deu",
    "spanish": "spa",
    "es": "spa",
    "italian": "ita",
    "it": "ita",
    "dutch": "nld",
    "nl": "nld",
    "ukrainian": "ukr",
    "uk": "ukr",
    "polish": "pol",
    "pl": "pol",
}


# Models people ask for by name that Tesseract does not ship, and where their recipe is.
_COMMUNITY_MODELS = {
    "orus": (
        "a community model for Russian printed before 1918",
        f"https://github.com/peterbussch/pageledger/blob/v{__version__}/docs/engine-recipes.md"
        "#tesseract-with-the-community-orus-model-macos-and-linux",
    ),
}


def _check_tesseract_langs(tesseract: str, lang: str) -> None:
    installed = _tesseract_installed_langs(tesseract)
    if installed is None:
        return
    missing = [part for part in lang.split("+") if part not in installed]
    known = [part for part in missing if part in _COMMUNITY_MODELS]
    if known:
        part = known[0]
        about, recipe = _COMMUNITY_MODELS[part]
        raise PageLedgerDiagnostic(
            "missing_language_pack",
            f"Tesseract's {part} model is not installed. {part} is {about}, not one of "
            f"Tesseract's stock languages. How to install it: {recipe}",
        )
    if missing:
        suggestions: list[str] = []
        for part in missing:
            mapped = _LANGUAGE_CODES.get(part.casefold())
            candidates = (
                [mapped]
                if mapped in installed
                else difflib.get_close_matches(part.casefold(), sorted(installed), n=2, cutoff=0.55)
            )
            for candidate in candidates:
                if candidate not in suggestions:
                    suggestions.append(candidate)
        available = sorted(installed)
        shown = ", ".join(available[:8])
        if len(available) > 8:
            shown += ", …"
        suggestion_text = f" Did you mean {', '.join(suggestions)}?" if suggestions else ""
        raise PageLedgerDiagnostic(
            "missing_language_pack",
            f"Tesseract language pack(s) not installed: {', '.join(missing)}."
            f"{suggestion_text} Tesseract uses three-letter language codes. "
            f"Installed: {len(available)} codes, including {shown}. "
            "Run `pageledger doctor` for the full list. Install the missing traineddata, "
            "or set --lang on the command line, run.adapter_options.lang for a run config, "
            "or processing.local_ocr.adapter_options.lang for a job config.",
        )


# Words with Tesseract confidence below this are counted into the
# low-confidence tail that quality signals inspect.
_LOW_WORD_CONFIDENCE = 60.0


def _tesseract_word_confidence(
    tsv_path: Path,
) -> tuple[float | None, dict[str, Any] | None]:
    """Summarize per-word confidences from Tesseract TSV output.

    Returns ``(confidence, detail)`` where confidence is the mean word
    confidence scaled to 0–1. Any parse problem yields ``(None, None)`` —
    confidence is optional evidence, never worth failing a page over.
    """
    try:
        lines = tsv_path.read_text(encoding="utf-8", errors="replace").splitlines()
        header = lines[0].split("\t")
        conf_col = header.index("conf")
        text_col = header.index("text")
        level_col = header.index("level")
        confidences = []
        for line in lines[1:]:
            fields = line.split("\t")
            if len(fields) <= max(conf_col, text_col, level_col):
                continue
            # Level 5 rows are words; other levels carry conf -1.
            if fields[level_col] != "5" or not fields[text_col].strip():
                continue
            conf = float(fields[conf_col])
            if conf < 0:
                continue
            confidences.append(conf)
    except (OSError, ValueError, IndexError):
        return None, None
    if not confidences:
        return None, None
    mean = sum(confidences) / len(confidences)
    below = sum(1 for conf in confidences if conf < _LOW_WORD_CONFIDENCE)
    detail = {
        "scale": "tesseract_word_confidence_0_100",
        "word_count": len(confidences),
        "mean": round(mean, 2),
        "min": round(min(confidences), 2),
        "below_60_count": below,
        "below_60_ratio": round(below / len(confidences), 4),
    }
    return round(mean / 100, 4), detail


_BINARY_PACKAGES = {"tesseract": "Tesseract", "pdftoppm": "Poppler", "pdfinfo": "Poppler"}


def _png_size(path: Path) -> tuple[int, int] | None:
    """Width and height from a PNG header, or None if the file is not a PNG."""
    try:
        with path.open("rb") as fh:
            header = fh.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def _probe_size_at_10dpi(pdftoppm: str, source: Path, page: int) -> tuple[int, int] | None:
    """Render one page at 10 DPI to learn its true, rotated size cheaply."""
    with tempfile.TemporaryDirectory() as tmp:
        _run_ocr_command(
            [
                pdftoppm,
                "-f",
                str(page),
                "-l",
                str(page),
                "-r",
                str(_PROBE_DPI),
                "-png",
                str(source),
                str(Path(tmp) / "probe"),
            ],
            timeout=_RENDER_TIMEOUT_SECONDS,
            context=f"pdftoppm failed probing page {page} of {source}",
        )
        images = sorted(Path(tmp).glob("*.png"))
        return _png_size(images[0]) if images else None


def _render_dpi(
    pdftoppm: str,
    source: Path,
    page: int,
    dpi: int,
    max_pixels: int,
    min_dpi: int = _MIN_RENDER_DPI,
) -> tuple[int, bool]:
    """DPI to render at so a page stays within max_pixels, and whether it was lowered.

    Some scans declare pages meters wide; rendering them at a fixed 300 DPI can
    need hundreds of megapixels. The DPI is only ever lowered here. An
    unreadable probe keeps the requested DPI.
    """
    size = _probe_size_at_10dpi(pdftoppm, source, page)
    if size is None:
        return dpi, False
    width10, height10 = size
    pixels = (width10 * dpi / _PROBE_DPI) * (height10 * dpi / _PROBE_DPI)
    if pixels <= max_pixels:
        return dpi, False
    capped = int(dpi * math.sqrt(max_pixels / pixels))
    if capped < min_dpi:
        raise PageLedgerDiagnostic(
            "render_limit",
            f"page {page} would need {capped} DPI to stay within {max_pixels:,} pixels; "
            "raise run.adapter_options.max_render_pixels or split the page",
        )
    return capped, True


def _native_image_dpi(source: Path, page_number: int, requested_dpi: int, cache: dict) -> int:
    """The DPI that renders the page's largest embedded image at its own resolution.

    Some scans are filed as tiny declared pages, which a fixed DPI downsamples.
    Without pypdf, or when pypdf cannot read the file, the requested DPI stands;
    pdftoppm then renders the page or refuses it with its own error.
    """
    from .image_source import carrier_page

    image = carrier_page(source, page_number)
    if image is not None:
        return max(requested_dpi, min(round(max(image.ppi)), _MAX_NATIVE_DPI))
    if importlib.util.find_spec("pypdf") is None:
        return requested_dpi
    try:
        page_ppis = _read_once(cache, source, _page_image_ppis)
    except (OSError, PageLedgerDiagnostic):
        return requested_dpi
    ppi = page_ppis[page_number - 1] if page_number <= len(page_ppis) else 0.0
    return max(requested_dpi, min(round(ppi), _MAX_NATIVE_DPI))


def _render_floor(source: Path, page_number: int) -> int:
    """The lowest DPI a page may be rendered at to fit the pixel limit.

    A page image rendered near its own resolution is legible whatever its declared
    density, so its floor is half that resolution rather than the fixed minimum.
    """
    from .image_source import carrier_page

    image = carrier_page(source, page_number)
    if image is None:
        return _MIN_RENDER_DPI
    return min(_MIN_RENDER_DPI, max(1, int(min(image.ppi) / 2)))


# A render with more than this many times the source image's pixels along the
# long side is upsampled; 300 DPI on a 200 ppi scan is routine and stays quiet.
UPSAMPLE_FACTOR = 1.5
# A PDF page's largest image is its scan only when it has the page's proportions.
_SCAN_ASPECT_TOLERANCE = 0.03


def native_page_pixels(source: Path, page_number: int) -> tuple[int, int, float] | None:
    """Pixel width and height of the image that is the page, and the page's long side in inches.

    For a page image that is the image itself. For a PDF page it is the largest
    embedded image when that image has the page's proportions, so that it is
    plausibly the scan; figures and logos on a born-digital page are not. None
    when unknown, including PDFs without the pypdf extra. Image data is never decoded.
    """
    from .image_source import carrier_page

    image = carrier_page(source, page_number)
    if image is not None:
        width, height = image.points
        return image.width, image.height, max(width, height) / 72
    if source.suffix.lower() != ".pdf" or importlib.util.find_spec("pypdf") is None:
        return None
    try:
        scans = _pdf_scans(str(source.absolute()), _file_identity(source))
    except (OSError, PageLedgerDiagnostic):
        return None
    return scans[page_number - 1] if 1 <= page_number <= len(scans) else None


@lru_cache(maxsize=8)
def _pdf_scans(path: str, identity: tuple[int, ...]) -> tuple[tuple[int, int, float] | None, ...]:
    _ = identity
    source = Path(path)
    with source.open("rb") as handle, _reading_pdf(source) as open_pdf:
        return tuple(_page_scan(page) for page in open_pdf(handle).pages)


def _page_scan(page: Any) -> tuple[int, int, float] | None:
    width, height = float(page.cropbox.width), float(page.cropbox.height)
    resources = page["/Resources"] if "/Resources" in page else {}
    xobjects = resources["/XObject"] if "/XObject" in resources else {}
    sizes = [
        (int(xobjects[name]["/Width"]), int(xobjects[name]["/Height"]))
        for name in xobjects
        if xobjects[name].get("/Subtype") == "/Image"
    ]
    if width <= 0 or height <= 0 or not sizes:
        return None
    image = max(sizes, key=lambda size: size[0] * size[1])
    if min(image) <= 0:
        return None
    page_shape = min(width, height) / max(width, height)
    if abs(min(image) / max(image) - page_shape) > _SCAN_ASPECT_TOLERANCE * page_shape:
        return None
    return image[0], image[1], max(width, height) / 72


def render_warnings(
    source: Path,
    page_number: int,
    *,
    pixels: tuple[int, int] | None = None,
    dpi: float | None = None,
) -> list[str]:
    """``render_upsampled`` when a page was rendered well beyond its source image's pixels.

    Give the rendered image's size, or the DPI it was rendered at. Engines that
    render pages call this; a custom adapter may too.
    """
    native = native_page_pixels(source, page_number)
    if native is None:
        return []
    if pixels is not None:
        rendered = float(max(pixels))
    elif dpi is not None:
        rendered = native[2] * dpi
    else:
        return []
    return ["render_upsampled"] if rendered > max(native[:2]) * UPSAMPLE_FACTOR else []


def _page_image_ppis(source: Path) -> tuple[float, ...]:
    """Each page's largest embedded image, in pixels per inch of the declared page.

    Treating the image as covering the whole page can only underestimate its
    resolution. Image data is never decoded.
    """
    with source.open("rb") as handle, _reading_pdf(source) as open_pdf:
        return tuple(_largest_image_ppi(page) for page in open_pdf(handle).pages)


def _largest_image_ppi(page: Any) -> float:
    """Only an image with the page's proportions can be its scan; a logo or figure,
    however sharp, must not raise the render DPI of a born-digital page."""
    width = float(page.mediabox.width) / 72
    height = float(page.mediabox.height) / 72
    resources = page["/Resources"] if "/Resources" in page else {}
    xobjects = resources["/XObject"] if "/XObject" in resources else {}
    ppi = 0.0
    if width > 0 and height > 0:
        page_shape = min(width, height) / max(width, height)
        for name in xobjects:
            xobject = xobjects[name]
            if xobject.get("/Subtype") != "/Image":
                continue
            image_width, image_height = int(xobject["/Width"]), int(xobject["/Height"])
            if min(image_width, image_height) <= 0:
                continue
            shape = min(image_width, image_height) / max(image_width, image_height)
            if abs(shape - page_shape) > _SCAN_ASPECT_TOLERANCE * page_shape:
                continue
            ppi = max(ppi, image_width / width, image_height / height)
    return ppi


def _dpi_note(render_dpi: int, requested_dpi: int, *, raised: bool) -> str:
    if render_dpi == requested_dpi:
        return f"dpi={render_dpi}"
    reason = ", native image" if raised else ""
    return f"dpi={render_dpi} (requested {requested_dpi}{reason})"


def _file_identity(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _read_once(cache: dict, source: Path, read: Callable[[Path], Any]) -> Any:
    """Read a PDF once per adapter lifetime, refusing a source that changes meanwhile."""
    source = source.absolute()
    identity = _file_identity(source)
    cached = cache.get(source)
    if cached is not None:
        if cached[0] != identity:
            raise ValueError("PDF source changed during adapter lifetime")
        return cached[1]
    value = read(source)
    if _file_identity(source) != identity:
        raise ValueError("PDF source changed while reading")
    cache[source] = (identity, value)
    return value


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        package = _BINARY_PACKAGES.get(name, name)
        raise PageLedgerDiagnostic(
            "missing_binary",
            f"PageLedger needs '{name}' on PATH to read this page; install {package}. "
            "Run 'pageledger doctor' for install hints.",
        )
    return path


def _run_ocr_command(argv: list[str], *, timeout: int, context: str) -> None:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PageLedgerDiagnostic(
            "engine_timeout", f"{context}: timed out after {timeout}s"
        ) from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise RuntimeError(f"{context} (exit {proc.returncode}){detail}")


@lru_cache(maxsize=1)
def _tesseract_model_string() -> str:
    path = shutil.which("tesseract")
    if path is None:
        return "tesseract"
    try:
        proc = subprocess.run(
            [path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "tesseract"
    first_line = (proc.stdout or "").splitlines()[:1]
    if first_line and first_line[0].strip():
        return first_line[0].strip()
    return "tesseract"


@lru_cache(maxsize=1)
def _pypdf_model_string() -> str:
    """Return the installed embedded-text backend identity."""
    try:
        return f"pypdf {importlib.metadata.version('pypdf')}"
    except importlib.metadata.PackageNotFoundError:
        return "pypdf"


@lru_cache(maxsize=1)
def _pdftoppm_model_string() -> str:
    """Return the Poppler renderer identity without making extraction depend on it."""
    path = shutil.which("pdftoppm")
    if path is None:
        return "pdftoppm"
    try:
        proc = subprocess.run(
            [path, "-v"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "pdftoppm"
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    first_line = output.splitlines()[:1]
    if first_line and first_line[0].strip():
        return first_line[0].strip()
    return "pdftoppm"


def ocr_pdf_page_count(source: Path) -> int:
    """Page count for pdf_ocr: pdfinfo when available, else the pypdf extra."""
    count = _pdf_page_count_pdfinfo(source)
    if count is not None:
        return count
    try:
        return _pdf_page_count(source)
    except ValueError as exc:
        raise ValueError(
            f"Cannot count pages in {source}: install poppler (pdfinfo) "
            "or the optional dependency pageledger[pdf]"
        ) from exc


def _pdf_page_count_pdfinfo(source: Path) -> int | None:
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo is None:
        return None
    try:
        proc = subprocess.run(
            [pdfinfo, str(source)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _load_pypdf() -> Any:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise ValueError(
            "PDF support requires the optional dependency: install pageledger[pdf]"
        ) from exc
    return PdfReader


_PDF_READ_ERRORS = (
    OSError,
    ValueError,
    KeyError,
    IndexError,
    TypeError,
    AttributeError,
    RecursionError,
)

_PYPDF_REPAIR_NOTICES = ("Ignoring wrong pointing object", "Xref table not zero-indexed")


@contextmanager
def capture_pypdf_repairs() -> Iterator[None]:
    """For one command, count pypdf's routine repair notices and print a single line.

    pypdf logs one line for every object it repairs while reading a damaged PDF, which
    buried a four-page job under 95 lines. Its other warnings still reach stderr. This is
    logging configuration, so the command line applies it; library callers keep pypdf's own.
    """
    repaired = fonts = 0
    other = logging.StreamHandler()

    class _Notices(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            nonlocal repaired, fonts
            message = record.getMessage()
            if message.startswith(_PYPDF_REPAIR_NOTICES):
                repaired += 1
            elif message.startswith("fontTools is required"):
                # Newer pypdf prints the whole font dictionary with each of these.
                fonts += 1
            else:
                other.emit(record)

    logger = logging.getLogger("pypdf")
    handler, propagate = _Notices(), logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        if repaired:
            print(f"pypdf repaired {repaired} objects while reading PDF files.", file=sys.stderr)
        if fonts:
            print(
                f"pypdf could not fully decode {fonts} fonts without the fontTools package; "
                "their text may read differently.",
                file=sys.stderr,
            )


@contextmanager
def _reading_pdf(source: Path) -> Iterator[Callable[..., Any]]:
    """Yield a pypdf opener for source; failures in the block become typed diagnostics.

    pypdf tries the empty password itself, so a PDF with only an owner password
    (print or copy restrictions) opens normally. Keep PageLedger's own checks
    outside the block: every exception inside it is blamed on the file.
    """
    PdfReader = _load_pypdf()
    from pypdf.errors import DependencyError, FileNotDecryptedError, PyPdfError

    def open_pdf(stream: BinaryIO, *, strict: bool = False) -> Any:
        try:
            return PdfReader(stream, strict=strict)
        except NotImplementedError as exc:
            raise PageLedgerDiagnostic(
                "unsupported_encryption",
                f"{source.name} uses an encryption handler that pypdf cannot read "
                "(Internet Archive lending copies are one example). Use an unencrypted copy.",
            ) from exc

    try:
        yield open_pdf
    except DependencyError as exc:
        raise PageLedgerDiagnostic(
            "missing_crypto_dependency",
            f"{source.name} is AES-encrypted and pypdf needs the 'cryptography' package "
            "to read it. Reinstall pageledger[pdf], which includes it.",
        ) from exc
    except FileNotDecryptedError as exc:
        raise PageLedgerDiagnostic(
            "unsupported_encryption",
            f"{source.name} needs a password to open. PageLedger reads encrypted PDFs "
            "only when they open without one; use an unprotected copy.",
        ) from exc
    except (PyPdfError, *_PDF_READ_ERRORS) as exc:
        raise PageLedgerDiagnostic(
            "malformed_pdf",
            f"pypdf could not read {source.name} ({type(exc).__name__}); the file may "
            "be damaged, truncated or not a PDF. PageLedger does not repair sources; "
            "a PDF tool such as qpdf can often write a readable copy.",
        ) from exc


def _pdf_page_count(source: Path) -> int:
    with source.open("rb") as handle, _reading_pdf(source) as open_pdf:
        return len(open_pdf(handle).pages)


def pdf_page_count(source: Path) -> int:
    """Return a PDF page count using the optional ``pageledger[pdf]`` dependency."""
    return _pdf_page_count(source)


# The invisible font Tesseract, OCRmyPDF and the Internet Archive draw their OCR text in.
_OCR_LAYER_FONTS = (b"GlyphLessFont",)
# OCR software that names itself in a PDF's Producer or Creator. In the 2026-11
# dogfood, ABBYY FineReader 11, ABBYY Recognition Server and LuraDocument layers were
# drawn in ordinary fonts and passed as typeset text.
_OCR_PRODUCERS = re.compile(
    r"ABBYY|FineReader|LuraDocument|Tesseract|OCRmyPDF|OmniPage|Readiris|Paper Capture", re.I
)
# An image drawn over at least this share of the page's width and height covers the page.
_COVERS_PAGE = 0.9
_TEXT_SHOWING = (b"Tj", b"TJ", b"'", b'"')


def _pdf_page_reading(source: Path, page_number: int) -> tuple[str, dict | None]:
    """A page's text layer, and why that layer is an earlier OCR of a scanned image (or None).

    The evidence names each signal that fired: ``font`` (GlyphLessFont), ``invisible_text``
    (most text drawn invisibly on a page with a page-sized image), ``text_under_image`` (most
    text painted before an opaque page-sized image that hides it), ``producer`` (the
    document's Producer or Creator names OCR software and the page has a page-sized image).
    """
    with source.open("rb") as handle, _reading_pdf(source) as open_pdf:
        reader = open_pdf(handle)
        pages = reader.pages
        if not 1 <= page_number <= len(pages):
            raise ValueError(f"page_number {page_number} out of range for {source}")
        page = pages[page_number - 1]
        text = page.extract_text() or ""
        signals = ["font"] if _ocr_layer(page) else []
        drawing = _text_and_scan(page)
        producer = _ocr_producer(reader) if drawing["scan"] else None
        if drawing["invisible"]:
            signals.append("invisible_text")
        if drawing["under_image"]:
            signals.append("text_under_image")
        if producer:
            signals.append("producer")
        if not signals:
            return text, None
        return text, {"signals": signals, **({"producer": producer} if producer else {})}


def _ocr_producer(reader: Any) -> str | None:
    from pypdf.errors import PyPdfError

    try:
        meta = reader.metadata or {}
        names = [str(meta.get(key) or "").strip() for key in ("/Producer", "/Creator")]
    except (PyPdfError, AttributeError, KeyError, TypeError, ValueError):
        return None
    return next((name for name in names if _OCR_PRODUCERS.search(name)), None)


def _text_and_scan(page: Any) -> dict[str, bool]:
    """How the page draws its text relative to an image that covers the page.

    Walks the page's content and its forms without decoding any image. The image is a
    scan when, as placed, it covers the page; it hides text when it is opaque (no soft
    mask, mask or stencil), painted opaquely (the graphics state sets no alpha below 1,
    soft mask or blend mode other than Normal: a watermark is stamped that way) and
    painted after that text.
    """
    from pypdf.errors import PyPdfError
    from pypdf.generic import ContentStream

    try:
        width, height = float(page.cropbox.width), float(page.cropbox.height)
    except (PyPdfError, AttributeError, KeyError, TypeError, ValueError):
        # A page whose box cannot be read gives no signal; its text is still read.
        return {"scan": False, "invisible": False, "under_image": False}
    shown = {"all": 0, "invisible": 0, "covered": 0, "scan": False}

    def multiply(m: list[float], n: list[float]) -> list[float]:
        return [
            m[0] * n[0] + m[1] * n[2],
            m[0] * n[1] + m[1] * n[3],
            m[2] * n[0] + m[3] * n[2],
            m[2] * n[1] + m[3] * n[3],
            m[4] * n[0] + m[5] * n[2] + n[4],
            m[4] * n[1] + m[5] * n[3] + n[5],
        ]

    def covers(ctm: list[float]) -> bool:
        xs = [ctm[4], ctm[0] + ctm[4], ctm[2] + ctm[4], ctm[0] + ctm[2] + ctm[4]]
        ys = [ctm[5], ctm[1] + ctm[5], ctm[3] + ctm[5], ctm[1] + ctm[3] + ctm[5]]
        return (max(xs) - min(xs)) >= _COVERS_PAGE * width and (
            max(ys) - min(ys)
        ) >= _COVERS_PAGE * height

    def walk(
        stream: Any, resources: Any, ctm: list[float], mode: int, see: dict, depth: int
    ) -> None:
        resources = resources.get_object() if resources is not None else {}
        xobjects = resources.get("/XObject") or {}
        states = resources.get("/ExtGState") or {}
        stack: list[tuple[list[float], int, dict]] = []
        for operands, operator in ContentStream(stream, page.pdf).operations:
            if operator == b"q":
                stack.append((ctm, mode, see))
            elif operator == b"Q" and stack:
                ctm, mode, see = stack.pop()
            elif operator == b"gs" and operands and operands[0] in states:
                see = _see_through(states[operands[0]].get_object(), see)
            elif operator == b"cm" and len(operands) == 6:
                ctm = multiply([float(x) for x in operands], ctm)
            elif operator == b"Tr" and operands:
                mode = int(operands[0])
            elif operator in _TEXT_SHOWING:
                shown["all"] += 1
                shown["invisible"] += mode in (3, 7)
            elif operator == b"Do" and operands and operands[0] in xobjects:
                xobject = xobjects[operands[0]].get_object()
                if xobject.get("/Subtype") == "/Form" and depth < 3:
                    matrix = [float(x) for x in xobject.get("/Matrix", [1, 0, 0, 1, 0, 0])]
                    walk(
                        xobject,
                        xobject.get("/Resources"),
                        multiply(matrix, ctm),
                        mode,
                        see,
                        depth + 1,
                    )
                elif xobject.get("/Subtype") == "/Image" and covers(ctm):
                    shown["scan"] = True
                    opaque = (
                        not any(see.values())
                        and not any(key in xobject for key in ("/SMask", "/Mask"))
                        and not xobject.get("/ImageMask", False)
                    )
                    if opaque:
                        shown["covered"] = shown["all"]

    try:
        contents = page.get_contents()
        if contents is not None:
            walk(contents, page.get("/Resources"), [1, 0, 0, 1, 0, 0], 0, {}, 0)
    except (
        PyPdfError,
        RecursionError,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        IndexError,
    ):
        # A drawing the walk cannot follow gives no signal; the page's text is still read.
        return {"scan": False, "invisible": False, "under_image": False}
    total = shown["all"]
    return {
        "scan": bool(shown["scan"]),
        "invisible": bool(shown["scan"]) and total > 0 and 2 * shown["invisible"] > total,
        "under_image": total > 0 and 2 * shown["covered"] > total,
    }


def _see_through(state: Any, see: dict[str, bool]) -> dict[str, bool]:
    """Which of the graphics state's transparency settings let what lies under show.

    An ExtGState changes only the keys it sets. Alpha below 1 (fill ``/ca`` or stroke
    ``/CA``), a soft mask or a blend mode other than Normal lets the text beneath show
    through, as a watermark stamped over a typeset page does.
    """
    see = dict(see)
    for key in ("/ca", "/CA"):
        if key in state:
            see[key] = float(state[key]) < 1
    if "/SMask" in state:
        see["/SMask"] = state["/SMask"] != "/None"
    if "/BM" in state:
        mode = state["/BM"]
        mode = mode[0] if isinstance(mode, list) and mode else mode
        see["/BM"] = mode not in ("/Normal", "/Compatible")
    return see


def _ocr_layer(page: Any) -> bool:
    """True when the page draws text in a known OCR font, at page level or in its forms."""
    seen: set[int] = set()

    def fonts(resources: Any, depth: int) -> bool:
        resources = resources.get_object() if resources is not None else None
        if not resources or id(resources) in seen or depth > 3:
            return False
        seen.add(id(resources))
        for font in (resources.get("/Font") or {}).values():
            name = str(font.get_object().get("/BaseFont", "")).encode()
            if any(marker in name for marker in _OCR_LAYER_FONTS):
                return True
        for xobject in (resources.get("/XObject") or {}).values():
            xobject = xobject.get_object()
            if xobject.get("/Subtype") == "/Form" and fonts(xobject.get("/Resources"), depth + 1):
                return True
        return False

    try:
        return fonts(page.get("/Resources"), 0)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _pdf_page_text(source: Path, page_number: int) -> str:
    with source.open("rb") as handle, _reading_pdf(source) as open_pdf:
        pages = open_pdf(handle).pages
        in_range = 1 <= page_number <= len(pages)
        text = (pages[page_number - 1].extract_text() or "") if in_range else None
    if text is None:
        raise ValueError(f"page_number {page_number} out of range for {source}")
    return text


BUILTIN_ADAPTERS = ("text", "pdf_text", "pdf_ocr", "vision", "rapidocr", "indra", "alto")


def check_adapter_name(name: str) -> None:
    """Refuse a name that is neither a built-in adapter nor a module.path:object spec,
    without importing or constructing anything."""
    if name in BUILTIN_ADAPTERS or name in PDF_ADAPTER_NAMES or ":" in name:
        return
    valid = ", ".join([*BUILTIN_ADAPTERS, "module.path:object"])
    raise ValueError(f"Unsupported adapter '{name}'. Valid adapters: {valid}")


def load_adapter(name: str, options: dict[str, Any] | None = None) -> Any:
    opts = dict(options or {})
    if _RECORDS:
        check_named_endpoint(name, opts, _RECORDS[-1][0], _ENDPOINTS[-1])
    if name == "text":
        adapter = _construct_builtin(TextAdapter, name, opts)
    elif name == "vision":
        from .vision import VisionAdapter

        adapter = _construct_builtin(VisionAdapter, name, opts)
    elif name == "rapidocr":
        from .rapidocr_adapter import RapidOCRAdapter

        adapter = _construct_builtin(RapidOCRAdapter, name, opts)
    elif name == "indra":
        from .indra_adapter import IndraAdapter

        adapter = _construct_builtin(IndraAdapter, name, opts)
    elif name == "alto":
        from .alto_adapter import AltoAdapter

        adapter = _construct_builtin(AltoAdapter, name, opts)
    elif name == "pdf_ocr":
        adapter = _construct_builtin(PdfOcrAdapter, name, opts)
    elif name in PDF_ADAPTER_NAMES:
        adapter = _construct_builtin(PdfTextAdapter, name, opts)
    elif ":" in name:
        adapter = _load_custom_adapter(name, opts)
    else:
        check_adapter_name(name)
    issues = _adapter_contract_issues(adapter)
    if issues:
        prefix = f"Adapter '{getattr(adapter, 'name', 'unknown')}':"
        raise ValueError(f"{prefix} {issues[0]}")
    return adapter


def _construct_builtin(cls: type, name: str, opts: dict[str, Any]) -> Any:
    try:
        return cls(**opts)
    except TypeError as exc:
        raise ValueError(
            f"Adapter '{name}' does not accept run.adapter_options {sorted(opts)}"
        ) from exc


def adapter_conformance_check(adapter: Any) -> list[str]:
    """Validate an adapter against the PageLedger protocol contract.

    Returns a list of conformance issues (empty list means the adapter passes).
    Adapter authors can call this from pytest or a script.
    """
    return _adapter_contract_issues(adapter)


def _adapter_contract_issues(adapter: Any) -> list[str]:
    """Collect protocol issues without invoking adapter code."""
    issues: list[str] = []
    for attr, expected_type, description in _ADAPTER_META_CHECKS:
        value = getattr(adapter, attr, None)
        if value is None:
            issues.append(f"missing required attribute '{attr}'")
        elif not isinstance(value, expected_type):
            issues.append(f"'{attr}' {description}, got {type(value).__name__}")

    for attr in ("input_types", "output_types", "capabilities"):
        value = getattr(adapter, attr, None)
        if isinstance(value, (tuple, list)):
            for index, item in enumerate(value):
                if not isinstance(item, str):
                    issues.append(
                        f"'{attr}' item {index} must be a string, got {type(item).__name__}"
                    )

    if not callable(getattr(adapter, "supports", None)):
        issues.append("missing required method 'supports(action)'")
    if not callable(getattr(adapter, "extract", None)):
        issues.append("missing required method 'extract(...)'")

    page_count = getattr(adapter, "page_count", None)
    if page_count is not None and not callable(page_count):
        issues.append("'page_count' must be callable or absent")
    reproducibility_profile = getattr(adapter, "reproducibility_profile", None)
    if reproducibility_profile is not None and not callable(reproducibility_profile):
        issues.append("'reproducibility_profile' must be callable or absent")
    return issues


def _load_custom_adapter(spec: str, opts: dict[str, Any] | None = None) -> Any:
    opts = dict(opts or {})
    candidate = _import_object(spec, description="Custom adapter", methods=ADAPTER_METHODS)

    adapter = candidate
    if isinstance(adapter, type):
        try:
            adapter = adapter(**opts)
        except TypeError as exc:
            raise ValueError(
                f"Custom adapter '{spec}' is a class but could not be constructed "
                f"with {_opts_phrase(opts)}"
            ) from exc
    elif not _looks_like_adapter(adapter) and callable(adapter):
        try:
            adapter = adapter(**opts)
        except TypeError as exc:
            raise ValueError(
                f"Custom adapter '{spec}' is callable but could not be constructed "
                f"with {_opts_phrase(opts)}"
            ) from exc
    elif opts:
        raise ValueError(
            f"Custom adapter '{spec}' is already an instance; "
            "run.adapter_options require a class or factory"
        )
    if not _looks_like_adapter(adapter):
        raise ValueError(f"Custom adapter '{spec}' must expose supports(action) and extract(...)")

    return adapter


class UntrustedCode(ValueError):
    """A job, run or bundle read from disk names code the user has not allowed."""


# While a record read from disk is continued (resume, replay), every module:object it
# leads PageLedger to import must live in the --adapter-path folder the user named.
# Process-wide rather than a contextvar, so a job's worker threads see it too.
_RECORDS: list[tuple[str, Path, Path | None]] = []
# The model servers the user named for the record being continued (--trust-endpoint).
_ENDPOINTS: list[frozenset[str]] = []


@contextmanager
def named_by_record(
    what: str,
    record: Path,
    adapter_path: Path | None,
    endpoints: Iterable[str] | None = None,
) -> Iterator[None]:
    """Hold custom code that ``record`` (a job, run or bundle) names to ``adapter_path``,
    and the servers it sends pages to to ``endpoints`` (inherited when None)."""
    _RECORDS.append((what, record.expanduser().resolve(), adapter_path))
    _ENDPOINTS.append(trusted_endpoints(endpoints))
    try:
        yield
    finally:
        _RECORDS.pop()
        _ENDPOINTS.pop()


def trusted_endpoints(endpoints: Iterable[str] | None) -> frozenset[str]:
    """The servers the user named, or those of the record being continued around this one."""
    if endpoints is None:
        return _ENDPOINTS[-1] if _ENDPOINTS else frozenset()
    return frozenset(url.rstrip("/") for url in endpoints)


def check_named_endpoint(
    name: str, options: dict[str, Any], what: str, endpoints: frozenset[str]
) -> None:
    """Refuse a record whose vision adapter sends pages (and the API key its settings
    name) to a server off this machine that the user did not name with --trust-endpoint."""
    if name != "vision":
        return
    base_url = options.get("base_url")
    if not isinstance(base_url, str):
        return  # the adapter refuses it when built
    from .vision import _on_this_machine

    host = urllib.parse.urlsplit(base_url).hostname
    if (host and _on_this_machine(host)) or base_url.rstrip("/") in endpoints:
        return
    raise UntrustedCode(
        f"This {what} sends page images, and any API key its settings name, to {base_url}, "
        "a server it names itself. PageLedger sends them there only if you name it: if you "
        f"trust that server, pass --trust-endpoint {base_url}."
    )


def check_named_code(spec: str, what: str, record: Path, adapter_path: Path | None) -> None:
    """Refuse a ``module:object`` that a record names unless its module is in the folder
    the user passed as --adapter-path, and not in the record itself. Built-in adapter
    names pass. Nothing is imported."""
    if ":" not in spec:
        return
    module_name = spec.split(":", 1)[0]
    top = module_name.split(".")[0]
    folder = adapter_path.expanduser().resolve() if adapter_path is not None else None
    if folder is not None:
        _refuse_package_root(folder, what)
        if top and _module_inside(top, folder, record.resolve()):
            return
    where = f"; it is not in the --adapter-path folder {folder}" if folder is not None else ""
    raise UntrustedCode(
        f"This {what} names its own adapter, {spec}, which would run Python code from "
        f"outside PageLedger{where}. PageLedger runs such code only from a folder you name: "
        f"if you trust where this {what} came from and that adapter, pass --adapter-path "
        "with the folder of the adapter's own code (for an installed adapter, its package "
        "folder inside site-packages, never site-packages itself)."
    )


def _package_roots() -> set[Path]:
    """Where Python keeps the standard library and installed packages."""
    import site
    import sysconfig

    places = [
        sysconfig.get_paths().get(key) for key in ("stdlib", "platstdlib", "purelib", "platlib")
    ]
    try:
        places += site.getsitepackages()
    except AttributeError:
        pass
    places.append(site.getusersitepackages())
    return {Path(place).resolve() for place in places if place}


def _refuse_package_root(folder: Path, what: str) -> None:
    """A received record could reach any module in such a folder, so it is never trusted
    whole: name the folder of the one adapter instead."""
    roots = _package_roots()
    if folder.name in {"site-packages", "dist-packages"} or any(
        root.is_relative_to(folder) for root in roots
    ):
        raise UntrustedCode(
            f"--adapter-path {folder} holds Python's own library or installed packages, any of "
            f"which this {what} could then name. Pass the folder of the adapter's own code "
            "instead (for an installed adapter, its package folder inside site-packages)."
        )


# What an object named by a received record must offer before PageLedger builds it:
# only an adapter class or object is called, never a plain function.
ADAPTER_METHODS = ("supports", "extract")
HOOK_METHODS = ("classify_page",)


def check_recorded_names(
    adapters: Iterable[tuple[str, dict[str, Any]]],
    hooks: Iterable[str],
    what: str,
    record: Path,
    adapter_path: Path | None,
    endpoints: Iterable[str] | None = None,
) -> None:
    """Every refusal a record can meet, before anything in it changes: each named
    ``module:object`` is resolved to the end and must be an adapter (or hook) defined in
    the --adapter-path folder, and each model server off this machine must be named.
    Only code inside the trusted folder is imported; nothing is constructed or called."""
    if adapter_path is not None:
        folder = adapter_path.expanduser().resolve()
        _refuse_package_root(folder, what)
        # The trusted folder is where the run will import from, as --adapter-path does.
        if folder.is_dir() and str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    with named_by_record(what, record, adapter_path, endpoints):
        named = trusted_endpoints(None)
        for name, options in adapters:
            check_named_endpoint(name, options, what, named)
            if ":" in name:
                _import_object(name, description="Custom adapter", methods=ADAPTER_METHODS)
        for hook in hooks:
            if ":" in hook:
                _import_object(hook, description="Classifier hook", methods=HOOK_METHODS)


def _module_inside(top: str, folder: Path, record: Path) -> bool:
    """Whether importing the top-level module ``top`` loads it from inside ``folder``."""
    entry = str(folder)
    added = entry not in sys.path
    if added:
        sys.path.insert(0, entry)
    try:
        found = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return False
    finally:
        if added:
            sys.path.remove(entry)
    if found is None:
        return False
    places = (
        [found.origin]
        if found.origin and found.has_location
        else list(found.submodule_search_locations or [])
    )
    if not places:
        return False
    for place in places:
        resolved = Path(place).resolve()
        if not resolved.is_relative_to(folder) or resolved.is_relative_to(record):
            return False
    return True


def _import_object(spec: str, *, description: str, methods: tuple[str, ...] = ()) -> Any:
    """Import a ``module.path:object`` without deciding how to construct it."""
    if ":" not in spec:
        raise ValueError(f"{description} specs must use module.path:object")
    module_name, object_path = spec.split(":", 1)
    if not module_name or not object_path:
        raise ValueError(f"{description} specs must use module.path:object")
    for record in _RECORDS:
        check_named_code(spec, *record)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ValueError(
            f"Could not import {description.lower()} module '{module_name}': "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    # The import may have resolved elsewhere on sys.path; check what actually loaded.
    for record in _RECORDS:
        check_named_code(spec, *record)

    candidate: Any = module
    try:
        for attr in object_path.split("."):
            if not attr:
                raise AttributeError(object_path)
            candidate = getattr(candidate, attr)
            # A module in the trusted folder may import anything; under a record the
            # object named must be defined there, not merely reachable from there.
            for what, record_root, trusted in _RECORDS:
                if not _defined_inside(candidate, record_root, trusted):
                    raise UntrustedCode(
                        f"This {what} names {spec}, which reaches code defined outside the "
                        "--adapter-path folder; PageLedger refuses to run it."
                    )
    except AttributeError as exc:
        raise ValueError(
            f"{description} object '{object_path}' not found in {module_name}"
        ) from exc
    if _RECORDS and not all(callable(getattr(candidate, name, None)) for name in methods):
        raise UntrustedCode(
            f"This {_RECORDS[-1][0]} names {spec}, which is not an adapter class or object: "
            "from a received record PageLedger builds only a class with "
            + " and ".join(f"{name}()" for name in methods)
            + ", never a plain function."
        )
    return candidate


def _defined_inside(value: Any, record: Path, adapter_path: Path | None) -> bool:
    if adapter_path is None:
        return False
    name: Any = value.__name__ if isinstance(value, ModuleType) else None
    if name is None:
        name = getattr(value, "__module__", None)
        if not isinstance(name, str):
            name = type(value).__module__
    defining = sys.modules.get(name)
    location = getattr(defining, "__file__", None)
    if not isinstance(location, str):
        return False
    resolved = Path(location).resolve()
    folder = adapter_path.expanduser().resolve()
    return resolved.is_relative_to(folder) and not resolved.is_relative_to(record)


def _opts_phrase(opts: dict[str, Any]) -> str:
    if not opts:
        return "no arguments"
    return f"run.adapter_options {sorted(opts)}"


def _looks_like_adapter(value: Any) -> bool:
    return callable(getattr(value, "supports", None)) and callable(getattr(value, "extract", None))
