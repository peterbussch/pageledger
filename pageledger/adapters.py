"""Small built-in extractor adapters and the pagination seam."""

from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

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


@runtime_checkable
class AdapterProtocol(Protocol):
    name: str
    version: str
    deterministic: bool
    input_types: Sequence[str]
    output_types: Sequence[str]
    capabilities: Sequence[str]

    def supports(self, action: str) -> bool:
        ...

    def extract(
        self,
        source: Path,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        ...


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
            raise ValueError(f"Adapter '{adapter.name}' page_count(source) must return a positive integer")
        return count
    return paginate(source, allow_pdf=getattr(adapter, "name", None) in PDF_ADAPTER_NAMES)


def _page_text(source: Path, page_number: int) -> str:
    pages = source.read_text(encoding="utf-8").split(PAGE_DELIMITER)
    if page_number < 1 or page_number > len(pages):
        raise ValueError(f"page_number {page_number} out of range for {source}")
    return pages[page_number - 1]


@dataclass(frozen=True)
class TextAdapter:
    name: str = "text"
    version: str = "0.1"
    deterministic: bool = True
    input_types: tuple[str, ...] = ("text",)
    output_types: tuple[str, ...] = ("text",)
    capabilities: tuple[str, ...] = ("embedded_text", "local")

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

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
        return ExtractionResult(
            content=_page_text(source, page_number),
            format="text",
            confidence=None,
            model=None,
            warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
        )


@dataclass(frozen=True)
class PdfTextAdapter:
    name: str = "pdf_text"
    version: str = "0.1"
    deterministic: bool = True
    input_types: tuple[str, ...] = ("pdf",)
    output_types: tuple[str, ...] = ("text",)
    capabilities: tuple[str, ...] = ("embedded_text", "local")

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def page_count(self, source: Path) -> int:
        return paginate(source, allow_pdf=True)

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
        text = _pdf_page_text(source, page_number)
        return ExtractionResult(
            content=text,
            format="text",
            confidence=None,
            model=None,
            warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
        )


_LANG_PATTERN = re.compile(r"^[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*$")

# Generous per-page ceilings; a page that takes longer than this is stuck.
_RENDER_TIMEOUT_SECONDS = 120
_OCR_TIMEOUT_SECONDS = 300


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
    name: str = "pdf_ocr"
    version: str = "0.1"
    deterministic: bool = True
    input_types: tuple[str, ...] = ("pdf",)
    output_types: tuple[str, ...] = ("text",)
    capabilities: tuple[str, ...] = ("ocr", "local")

    def __post_init__(self) -> None:
        if not isinstance(self.dpi, int) or isinstance(self.dpi, bool):
            raise ValueError("run.adapter_options.dpi must be an integer")
        if not 50 <= self.dpi <= 1200:
            raise ValueError("run.adapter_options.dpi must be between 50 and 1200")
        if not isinstance(self.lang, str) or not _LANG_PATTERN.match(self.lang):
            raise ValueError(
                "run.adapter_options.lang must be a Tesseract language code "
                "such as 'eng' or 'eng+deu'"
            )

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

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

        started = time.perf_counter()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            prefix = tmp_path / "page"
            _run_ocr_command(
                [
                    pdftoppm,
                    "-f", str(page_number),
                    "-l", str(page_number),
                    "-r", str(self.dpi),
                    "-png",
                    str(source),
                    str(prefix),
                ],
                timeout=_RENDER_TIMEOUT_SECONDS,
                context=f"pdftoppm failed rendering page {page_number} of {source}",
            )
            images = sorted(tmp_path.glob("page-*.png"))
            if not images:
                raise RuntimeError(
                    f"pdftoppm produced no image for page {page_number} of {source}"
                )
            output_prefix = tmp_path / "ocr"
            _run_ocr_command(
                [tesseract, str(images[0]), str(output_prefix), "-l", self.lang],
                timeout=_OCR_TIMEOUT_SECONDS,
                context=f"tesseract failed on page {page_number} of {source}",
            )
            text = output_prefix.with_suffix(".txt").read_text(
                encoding="utf-8", errors="replace"
            )
        return ExtractionResult(
            content=text,
            format="text",
            confidence=None,
            model=_tesseract_model_string(),
            warnings=[],
            usage={
                "pages": 1,
                "tokens": None,
                "compute_seconds": round(time.perf_counter() - started, 3),
                "cost_usd": None,
            },
        )


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(
            f"The pdf_ocr adapter needs '{name}' on PATH. "
            "Run 'pageledger doctor' for install hints."
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
        raise RuntimeError(f"{context}: timed out after {timeout}s") from exc
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


def _pdf_page_count(source: Path) -> int:
    PdfReader = _load_pypdf()
    with source.open("rb") as handle:
        reader = PdfReader(handle)
        return len(reader.pages)


def pdf_page_count(source: Path) -> int:
    """Return a PDF page count using the optional ``pageledger[pdf]`` dependency."""
    return _pdf_page_count(source)


def _pdf_page_text(source: Path, page_number: int) -> str:
    PdfReader = _load_pypdf()
    with source.open("rb") as handle:
        reader = PdfReader(handle)
        if page_number < 1 or page_number > len(reader.pages):
            raise ValueError(f"page_number {page_number} out of range for {source}")
        return reader.pages[page_number - 1].extract_text() or ""


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
    _validate_adapter_metadata(adapter)
    return adapter


def _construct_builtin(cls: type, name: str, opts: dict[str, Any]) -> Any:
    try:
        return cls(**opts)
    except TypeError as exc:
        raise ValueError(
            f"Adapter '{name}' does not accept run.adapter_options "
            f"{sorted(opts)}"
        ) from exc


def _validate_adapter_metadata(adapter: Any) -> None:
    """Validate that an adapter exposes required metadata fields with correct types."""
    prefix = f"Adapter '{getattr(adapter, 'name', 'unknown')}':"

    for attr, expected_type, description in _ADAPTER_META_CHECKS:
        value = getattr(adapter, attr, None)
        if value is None:
            raise ValueError(f"{prefix} missing required attribute '{attr}'")
        if not isinstance(value, expected_type):
            raise ValueError(f"{prefix} '{attr}' {description}, got {type(value).__name__}")

    for attr in ["input_types", "output_types", "capabilities"]:
        seq = getattr(adapter, attr)
        for i, item in enumerate(seq):
            if not isinstance(item, str):
                raise ValueError(f"{prefix} '{attr}' item {i} must be a string, got {type(item).__name__}")

    if not callable(getattr(adapter, "supports", None)):
        raise ValueError(f"{prefix} missing required method 'supports(action)'")
    if not callable(getattr(adapter, "extract", None)):
        raise ValueError(f"{prefix} missing required method 'extract(...)'")


def adapter_conformance_check(adapter: Any) -> list[str]:
    """Validate an adapter against the PageLedger protocol contract.

    Returns a list of conformance issues (empty list means the adapter passes).
    Adapter authors can call this from pytest or a script.
    """
    issues: list[str] = []

    for attr, expected_type, _desc in _ADAPTER_META_CHECKS:
        value = getattr(adapter, attr, None)
        if value is None:
            issues.append(f"Missing attribute '{attr}'")
        elif not isinstance(value, expected_type):
            issues.append(f"'{attr}' is {type(value).__name__}, expected {getattr(expected_type, '__name__', str(expected_type))}")

    for attr in ["input_types", "output_types", "capabilities"]:
        seq = getattr(adapter, attr, None)
        if isinstance(seq, (tuple, list)):
            for i, item in enumerate(seq):
                if not isinstance(item, str):
                    issues.append(f"'{attr}'[{i}] is {type(item).__name__}, expected str")

    if not callable(getattr(adapter, "supports", None)):
        issues.append("Missing method 'supports(action)'")
    if not callable(getattr(adapter, "extract", None)):
        issues.append("Missing method 'extract(...)'")

    page_count = getattr(adapter, "page_count", None)
    if page_count is not None and not callable(page_count):
        issues.append("'page_count' must be callable or absent")

    return issues


def _load_custom_adapter(spec: str, opts: dict[str, Any] | None = None) -> Any:
    opts = dict(opts or {})
    module_name, object_path = spec.split(":", 1)
    if not module_name or not object_path:
        raise ValueError("Custom adapter specs must use module.path:object")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ValueError(f"Could not import custom adapter module '{module_name}'") from exc

    candidate: Any = module
    try:
        for attr in object_path.split("."):
            if not attr:
                raise AttributeError(object_path)
            candidate = getattr(candidate, attr)
    except AttributeError as exc:
        raise ValueError(f"Custom adapter object '{object_path}' not found in {module_name}") from exc

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
        raise ValueError(
            f"Custom adapter '{spec}' must expose supports(action) and extract(...)"
        )

    defaults = {
        "name": spec,
        "version": "custom",
        "deterministic": False,
        "input_types": ("text", "pdf", "image"),
        "output_types": ("text",),
        "capabilities": ("custom",),
    }
    for attr, default in defaults.items():
        if not hasattr(adapter, attr):
            try:
                setattr(adapter, attr, default)
            except Exception:
                pass
    return adapter


def _opts_phrase(opts: dict[str, Any]) -> str:
    if not opts:
        return "no arguments"
    return f"run.adapter_options {sorted(opts)}"


def _looks_like_adapter(value: Any) -> bool:
    return callable(getattr(value, "supports", None)) and callable(getattr(value, "extract", None))
