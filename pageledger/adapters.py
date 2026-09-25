"""Small built-in extractor adapters and the pagination seam."""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import math
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, Literal

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


ADAPTER_FAILURE_CODES = frozenset(
    {
        "MODEL_TIMEOUT",
        "MODEL_HTTP_ERROR",
        "MODEL_QUOTA",
        "MODEL_OUTPUT_TRUNCATED",
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
        content = source.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return 1
    return content.count(PAGE_DELIMITER) + 1


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
    version: ClassVar[str] = "0.1"
    deterministic: ClassVar[bool] = True
    input_types: ClassVar[tuple[str, ...]] = ("text",)
    output_types: ClassVar[tuple[str, ...]] = ("text",)
    capabilities: ClassVar[tuple[str, ...]] = ("embedded_text", "local")

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def reproducibility_profile(self) -> dict[str, object]:
        return {"materials": []}

    def page_count(self, source: Path) -> int:
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
        pages = source.read_text(encoding="utf-8").split(PAGE_DELIMITER)
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
        return len(self._document_text(source))

    def _document_text(self, source: Path) -> tuple[str, ...]:
        pages = _read_once(self._documents, source, _pdf_page_texts)
        if not pages:
            raise ValueError("PDF source has no pages")
        return pages

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
        pages = self._document_text(source)
        if type(page_number) is not int or not 1 <= page_number <= len(pages):
            raise ValueError(f"page_number {page_number} out of range for {source}")
        text = pages[page_number - 1]
        return ExtractionResult(
            content=text,
            format="text",
            confidence=None,
            model=_pypdf_model_string(),
            warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
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
    version: ClassVar[str] = "0.1"
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
            pdftoppm, source, page_number, target_dpi, self.max_render_pixels
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
            output_prefix = tmp_path / "ocr"
            _run_ocr_command(
                [tesseract, str(images[0]), str(output_prefix), "-l", self.lang, "txt", "tsv"],
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
            warnings=["render_dpi_capped"] if capped else [],
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


def _check_tesseract_langs(tesseract: str, lang: str) -> None:
    installed = _tesseract_installed_langs(tesseract)
    if installed is None:
        return
    missing = [part for part in lang.split("+") if part not in installed]
    if missing:
        raise PageLedgerDiagnostic(
            "missing_language_pack",
            f"Tesseract language pack(s) not installed: {', '.join(missing)}. "
            f"Installed: {', '.join(sorted(installed))}. "
            "Install the missing traineddata or change run.adapter_options.lang; "
            "'pageledger doctor' lists available OCR languages.",
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
    if importlib.util.find_spec("pypdf") is None:
        return requested_dpi
    try:
        page_ppis = _read_once(cache, source, _page_image_ppis)
    except (OSError, PageLedgerDiagnostic):
        return requested_dpi
    ppi = page_ppis[page_number - 1] if page_number <= len(page_ppis) else 0.0
    return max(requested_dpi, min(round(ppi), _MAX_NATIVE_DPI))


def _page_image_ppis(source: Path) -> tuple[float, ...]:
    """Each page's largest embedded image, in pixels per inch of the declared page.

    Treating the image as covering the whole page can only underestimate its
    resolution. Image data is never decoded.
    """
    with source.open("rb") as handle, _reading_pdf(source) as open_pdf:
        return tuple(_largest_image_ppi(page) for page in open_pdf(handle).pages)


def _largest_image_ppi(page: Any) -> float:
    width = float(page.mediabox.width) / 72
    height = float(page.mediabox.height) / 72
    resources = page["/Resources"] if "/Resources" in page else {}
    xobjects = resources["/XObject"] if "/XObject" in resources else {}
    ppi = 0.0
    if width > 0 and height > 0:
        for name in xobjects:
            xobject = xobjects[name]
            if xobject.get("/Subtype") == "/Image":
                ppi = max(ppi, int(xobject["/Width"]) / width, int(xobject["/Height"]) / height)
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
            f"The pdf_ocr adapter needs '{name}' on PATH; install {package}. "
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


def _pdf_page_texts(source: Path) -> tuple[str, ...]:
    with source.open("rb") as handle, _reading_pdf(source) as open_pdf:
        return tuple(page.extract_text() or "" for page in open_pdf(handle).pages)


def _pdf_page_text(source: Path, page_number: int) -> str:
    with source.open("rb") as handle, _reading_pdf(source) as open_pdf:
        pages = open_pdf(handle).pages
        in_range = 1 <= page_number <= len(pages)
        text = (pages[page_number - 1].extract_text() or "") if in_range else None
    if text is None:
        raise ValueError(f"page_number {page_number} out of range for {source}")
    return text


def load_adapter(name: str, options: dict[str, Any] | None = None) -> Any:
    opts = dict(options or {})
    if name == "text":
        adapter = _construct_builtin(TextAdapter, name, opts)
    elif name == "pdf_ocr":
        adapter = _construct_builtin(PdfOcrAdapter, name, opts)
    elif name in PDF_ADAPTER_NAMES:
        adapter = _construct_builtin(PdfTextAdapter, name, opts)
    elif ":" in name:
        adapter = _load_custom_adapter(name, opts)
    else:
        valid = ", ".join(["text", "pdf_text", "pdf_ocr", "module.path:object"])
        raise ValueError(f"Unsupported adapter '{name}'. Valid adapters: {valid}")
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
    candidate = _import_object(spec, description="Custom adapter")

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


def _import_object(spec: str, *, description: str) -> Any:
    """Import a ``module.path:object`` without deciding how to construct it."""
    if ":" not in spec:
        raise ValueError(f"{description} specs must use module.path:object")
    module_name, object_path = spec.split(":", 1)
    if not module_name or not object_path:
        raise ValueError(f"{description} specs must use module.path:object")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ValueError(f"Could not import {description.lower()} module '{module_name}'") from exc

    candidate: Any = module
    try:
        for attr in object_path.split("."):
            if not attr:
                raise AttributeError(object_path)
            candidate = getattr(candidate, attr)
    except AttributeError as exc:
        raise ValueError(
            f"{description} object '{object_path}' not found in {module_name}"
        ) from exc
    return candidate


def _opts_phrase(opts: dict[str, Any]) -> str:
    if not opts:
        return "no arguments"
    return f"run.adapter_options {sorted(opts)}"


def _looks_like_adapter(value: Any) -> bool:
    return callable(getattr(value, "supports", None)) and callable(getattr(value, "extract", None))
