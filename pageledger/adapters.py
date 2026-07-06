"""Small built-in extractor adapters and the pagination seam."""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass
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


def load_adapter(name: str) -> Any:
    if name == "text":
        adapter = TextAdapter()
    elif name in PDF_ADAPTER_NAMES:
        adapter = PdfTextAdapter()
    elif ":" in name:
        adapter = _load_custom_adapter(name)
    else:
        valid = ", ".join(["text", "pdf_text", "module.path:object"])
        raise ValueError(f"Unsupported adapter '{name}'. Valid adapters: {valid}")
    _validate_adapter_metadata(adapter)
    return adapter


def _validate_adapter_metadata(adapter: Any) -> None:
    """Validate that an adapter exposes required metadata fields with correct types."""
    prefix = f"Adapter '{getattr(adapter, 'name', 'unknown')}':"

    for attr, expected_type, description in _ADAPTER_META_CHECKS:
        value = getattr(adapter, attr, None)
        if value is None:
            raise ValueError(f"{prefix} missing required attribute '{attr}'")
        if not isinstance(value, expected_type):
            type_name = getattr(expected_type, "__name__", str(expected_type))
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


def _load_custom_adapter(spec: str) -> Any:
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
            adapter = adapter()
        except TypeError as exc:
            raise ValueError(
                f"Custom adapter '{spec}' is a class but could not be constructed with no arguments"
            ) from exc
    elif not _looks_like_adapter(adapter) and callable(adapter):
        try:
            adapter = adapter()
        except TypeError as exc:
            raise ValueError(
                f"Custom adapter '{spec}' is callable but could not be constructed with no arguments"
            ) from exc
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


def _looks_like_adapter(value: Any) -> bool:
    return callable(getattr(value, "supports", None)) and callable(getattr(value, "extract", None))
