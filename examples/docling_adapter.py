"""Optional local Docling adapter for layout-aware PDF extraction.

Docling is intentionally installed as a machine tool, not as a PageLedger
dependency::

    uv tool install docling

The standard pipeline invokes that executable once per source document, reads
Docling's structured JSON, and caches page-level Markdown for the life of the
PageLedger run. VLM mode converts only the requested page so a selective rerun
does not process the rest of a large source. Both modes preserve PageLedger's
page-denominated artifacts and usage accounting.

Configure the standard layout/OCR/table pipeline in ``pageledger.yml``::

    run:
      adapter: docling_adapter:DoclingAdapter
      adapter_options:
        pipeline: standard

Then load the example module explicitly::

    pageledger run report.pdf --config pageledger.yml \
      --adapter-path examples --out runs/docling-standard

Or opt into the dogfooded local VLM preset in ``pageledger.yml``::

    run:
      adapter: docling_adapter:DoclingAdapter
      adapter_options:
        pipeline: vlm
        vlm_model: smoldocling

Remote services and external plugins are never enabled by this adapter. Model
assets may be downloaded by Docling on first use, so prewarm the machine tool
before an offline run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pageledger.adapters import ExtractionResult, ocr_pdf_page_count

_ACTIONS = frozenset({"transcribe_text", "extract_table", "vlm_table"})
_LOCAL_VLM_PRESETS = frozenset({"smoldocling"})
_VERSION_PATTERN = re.compile(r"^Docling version:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class _DocumentBatch:
    pages: dict[int, str]
    warnings: dict[int, list[str]]
    compute_seconds_per_page: float
    model: str


@dataclass
class DoclingAdapter:
    """Run Docling locally and expose its result through PageLedger's protocol."""

    pipeline: str = "standard"
    vlm_model: str = "smoldocling"
    timeout_seconds: int = 1800
    num_threads: int = 4
    executable: str = "docling"

    name: ClassVar[str] = "docling-local"
    version: ClassVar[str] = "example-0.1"
    deterministic: ClassVar[bool] = False
    input_types: ClassVar[tuple[str, ...]] = ("pdf",)
    output_types: ClassVar[tuple[str, ...]] = ("markdown",)
    capabilities: ClassVar[tuple[str, ...]] = (
        "ocr",
        "layout",
        "tables",
        "vlm",
        "local",
    )

    _batches: dict[tuple[Path, int, int, int], _DocumentBatch] = field(
        default_factory=dict, init=False, repr=False
    )
    _versions: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.pipeline not in {"standard", "vlm"}:
            raise ValueError("run.adapter_options.pipeline must be 'standard' or 'vlm'")
        if self.pipeline == "vlm" and self.vlm_model not in _LOCAL_VLM_PRESETS:
            raise ValueError(
                "run.adapter_options.vlm_model must be the dogfooded local VLM "
                "preset: smoldocling"
            )
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or not 60 <= self.timeout_seconds <= 7200
        ):
            raise ValueError(
                "run.adapter_options.timeout_seconds must be an integer between 60 and 7200"
            )
        if (
            not isinstance(self.num_threads, int)
            or isinstance(self.num_threads, bool)
            or not 1 <= self.num_threads <= 64
        ):
            raise ValueError(
                "run.adapter_options.num_threads must be an integer between 1 and 64"
            )
        if not isinstance(self.executable, str) or not self.executable.strip():
            raise ValueError("run.adapter_options.executable must be a non-empty string")

    def supports(self, action: str) -> bool:
        return action in _ACTIONS

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
            raise ValueError(f"{self.name} does not support action: {action}")
        if source.suffix.lower() != ".pdf":
            raise ValueError(f"{self.name} only reads PDF inputs: {source}")
        _ = page_id, prompt

        resolved = source.expanduser().resolve()
        stat = resolved.stat()
        page_scope = page_number if self.pipeline == "vlm" else 0
        cache_key = (resolved, stat.st_size, stat.st_mtime_ns, page_scope)
        batch = self._batches.get(cache_key)
        if batch is None:
            batch = self._convert_document(
                resolved,
                page_number=page_number if self.pipeline == "vlm" else None,
            )
            self._batches[cache_key] = batch
        if page_number not in batch.pages:
            available = f"1-{max(batch.pages)}" if batch.pages else "none"
            raise ValueError(
                f"page_number {page_number} is absent from Docling output for {source}; "
                f"available pages: {available}"
            )

        content = batch.pages[page_number]
        warnings = list(batch.warnings.get(page_number, []))
        if not content.strip():
            warnings.append("empty_docling_output")
        warnings = list(dict.fromkeys(warnings))
        return ExtractionResult(
            content=content,
            format="markdown",
            confidence=None,
            model=batch.model,
            warnings=warnings,
            usage={
                "pages": 1,
                "tokens": None,
                "compute_seconds": batch.compute_seconds_per_page,
                "cost_usd": None,
            },
        )

    def _convert_document(
        self, source: Path, *, page_number: int | None
    ) -> _DocumentBatch:
        executable = shutil.which(self.executable)
        if executable is None:
            raise RuntimeError(
                "Docling is not on PATH; install the machine tool with "
                "'uv tool install docling'"
            )
        docling_version = self._docling_version(executable)
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="pageledger-docling-") as tmp:
            output_dir = Path(tmp)
            output_format = "json" if self.pipeline == "standard" else "md"
            command = [
                executable,
                str(source),
                "--from",
                "pdf",
                "--to",
                output_format,
                "--pipeline",
                self.pipeline,
                "--image-export-mode",
                "placeholder",
                "--document-timeout",
                str(self.timeout_seconds),
                "--num-threads",
                str(self.num_threads),
                "--output",
                str(output_dir),
                "--quiet",
                "--abort-on-error",
            ]
            if self.pipeline == "standard":
                command.extend(
                    [
                        "--ocr",
                        "--tables",
                        "--ocr-engine",
                        "auto",
                        "--table-mode",
                        "accurate",
                    ]
                )
                model = (
                    f"docling {docling_version}; pipeline=standard; ocr=auto; "
                    "tables=accurate; batch=document"
                )
            else:
                if page_number is None:
                    raise RuntimeError("VLM conversion requires a requested page number")
                command.extend(
                    [
                        "--vlm-model",
                        self.vlm_model,
                        "--page-range",
                        f"{page_number}-{page_number}",
                    ]
                )
                model = (
                    f"docling {docling_version}; pipeline=vlm; "
                    f"vlm_model={self.vlm_model}; batch=page"
                )

            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds + 180,
            )
            if self.pipeline == "vlm":
                outputs = sorted(output_dir.glob("*.md"))
                if len(outputs) != 1:
                    raise RuntimeError(
                        "Docling must emit exactly one Markdown page; "
                        f"found {len(outputs)}"
                    )
                try:
                    content = outputs[0].read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise RuntimeError("Docling emitted unreadable Markdown output") from exc
                if page_number is None:
                    raise RuntimeError("VLM conversion lost its requested page number")
                pages = {page_number: content}
                page_warnings = {page_number: ["docling_vlm_uncalibrated"]}
            else:
                outputs = sorted(output_dir.glob("*.json"))
                if len(outputs) != 1:
                    raise RuntimeError(
                        "Docling must emit exactly one JSON document; "
                        f"found {len(outputs)}"
                    )
                try:
                    document = json.loads(outputs[0].read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("Docling emitted unreadable JSON output") from exc
                pages, page_warnings = _render_document_pages(document)

        elapsed = time.perf_counter() - started
        per_page = round(elapsed / len(pages), 3)
        return _DocumentBatch(
            pages=pages,
            warnings=page_warnings,
            compute_seconds_per_page=per_page,
            model=model,
        )

    def _docling_version(self, executable: str) -> str:
        cached = self._versions.get(executable)
        if cached is not None:
            return cached
        result = subprocess.run(
            [executable, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        match = _VERSION_PATTERN.search(result.stdout)
        if match is None:
            raise RuntimeError("Could not determine the installed Docling version")
        version = match.group(1)
        self._versions[executable] = version
        return version


def _render_document_pages(document: Any) -> tuple[dict[int, str], dict[int, list[str]]]:
    if not isinstance(document, dict):
        raise RuntimeError("Docling JSON root must be an object")
    page_map = document.get("pages")
    if not isinstance(page_map, dict) or not page_map:
        raise RuntimeError("Docling JSON must contain a non-empty pages mapping")
    try:
        page_numbers = sorted(int(value) for value in page_map)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Docling pages mapping contains a non-integer key") from exc
    if page_numbers != list(range(1, max(page_numbers) + 1)):
        raise RuntimeError("Docling pages mapping is not contiguous from page 1")
    body = document.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("children"), list):
        raise RuntimeError("Docling JSON must contain body.children")

    rendered: dict[int, str] = {}
    warnings_by_page: dict[int, list[str]] = {}
    for page_number in page_numbers:
        emitted: set[str] = set()
        blocks: list[str] = []
        warnings: list[str] = []
        for child in body["children"]:
            blocks.extend(
                _render_reference(document, child, page_number, emitted, warnings)
            )
        rendered[page_number] = "\n\n".join(
            block.strip() for block in blocks if block.strip()
        )
        warnings_by_page[page_number] = list(dict.fromkeys(warnings))
    return rendered, warnings_by_page


def _render_reference(
    document: dict[str, Any],
    reference: Any,
    page_number: int,
    emitted: set[str],
    warnings: list[str],
) -> list[str]:
    if not isinstance(reference, dict) or not isinstance(reference.get("$ref"), str):
        return []
    ref = reference["$ref"]
    if ref in emitted:
        return []
    emitted.add(ref)
    item = _resolve_reference(document, ref)
    blocks: list[str] = []

    if ref.startswith("#/tables/") and _belongs_to_page(item, page_number):
        for caption in item.get("captions", []):
            blocks.extend(
                _render_reference(document, caption, page_number, emitted, warnings)
            )
        table = _render_table(item)
        if table == "*[Table without cells]*":
            warnings.append("docling_table_without_cells")
        blocks.append(table)
    elif ref.startswith("#/pictures/") and _belongs_to_page(item, page_number):
        for caption in item.get("captions", []):
            blocks.extend(
                _render_reference(document, caption, page_number, emitted, warnings)
            )
        warnings.append("docling_picture_not_transcribed")
        blocks.append("*[Picture not transcribed]*")
    elif isinstance(item.get("text"), str) and _belongs_to_page(item, page_number):
        blocks.append(_render_text_item(item))

    children = item.get("children", [])
    if isinstance(children, list):
        for child in children:
            blocks.extend(
                _render_reference(document, child, page_number, emitted, warnings)
            )
    return blocks


def _resolve_reference(document: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise RuntimeError(f"Unsupported Docling reference: {ref}")
    current: Any = document
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        try:
            current = current[int(part)] if isinstance(current, list) else current[part]
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Broken Docling reference: {ref}") from exc
    if not isinstance(current, dict):
        raise RuntimeError(f"Docling reference does not resolve to an object: {ref}")
    return current


def _belongs_to_page(item: dict[str, Any], page_number: int) -> bool:
    provenance = item.get("prov")
    return isinstance(provenance, list) and any(
        isinstance(entry, dict) and entry.get("page_no") == page_number
        for entry in provenance
    )


def _render_text_item(item: dict[str, Any]) -> str:
    text = item["text"].strip()
    label = item.get("label")
    if label == "title":
        return f"# {text}"
    if label == "section_header":
        raw_level = item.get("level", 2)
        level = raw_level if isinstance(raw_level, int) and not isinstance(raw_level, bool) else 2
        return f"{'#' * max(1, min(level, 6))} {text}"
    if label == "list_item":
        return f"- {text}"
    if label == "code":
        return f"```\n{text}\n```"
    if label == "caption":
        return f"*{text}*"
    return text


def _render_table(item: dict[str, Any]) -> str:
    data = item.get("data")
    grid = data.get("grid") if isinstance(data, dict) else None
    if not isinstance(grid, list) or not grid:
        return "*[Table without cells]*"
    rows: list[list[str]] = []
    width = 0
    for raw_row in grid:
        if not isinstance(raw_row, list):
            continue
        row = [
            _markdown_cell(cell.get("text", "") if isinstance(cell, dict) else "")
            for cell in raw_row
        ]
        width = max(width, len(row))
        rows.append(row)
    if not rows or width == 0:
        return "*[Table without cells]*"
    padded = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(padded[0]) + " |"]
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
    return "\n".join(lines)


def _markdown_cell(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


ADAPTER = DoclingAdapter()
