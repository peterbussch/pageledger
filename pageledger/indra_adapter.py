"""The built-in ``indra`` adapter: Datalab's Indra OCR, a small CPU model with layout.

Install with ``pip install 'pageledger[indra]'``. Indra reads a page into lines
with boxes and confidences, groups them into labelled layout blocks and puts
them in reading order. Its code is Apache-2.0; its model weights use a modified
OpenRAIL-M licence (free for research, personal use and organizations under
$20M in funding or revenue, and not for use in competition with Datalab's API).
PageLedger never ships the weights: Indra downloads them (about 76 MB) on first
use, or reads them from ``model_dir`` or ``INDRA_OCR_MODEL_DIR``.

Indra is a literal engine, but not a purely visual one: when it is unsure of a
line it rescores the candidates with a character language model, which can
favour a common spelling over the printed one.
"""

from __future__ import annotations

import hashlib
import importlib
import time
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from statistics import mean
from typing import Any, ClassVar

from .adapters import ExtractionResult, ocr_pdf_page_count

INDRA_MODES = ("ocr", "auto")
INDRA_OUTPUTS = ("text", "markdown")
# The files that decide what Indra reads; their hashes identify the model set.
_MODEL_FILES = ("det.onnx", "rec.onnx", "order.onnx", "meta.json")


@dataclass(frozen=True)
class IndraAdapter:
    mode: str = "ocr"
    dpi: int = 200
    math: bool = False
    output: str = "text"
    threads: int = 4
    model_dir: str | None = None
    name: ClassVar[str] = "indra"
    version: ClassVar[str] = "0.1"
    deterministic: ClassVar[bool] = True
    input_types: ClassVar[tuple[str, ...]] = ("pdf",)
    output_types: ClassVar[tuple[str, ...]] = ("text", "markdown")
    capabilities: ClassVar[tuple[str, ...]] = ("ocr", "page_image", "layout", "local")

    def __post_init__(self) -> None:
        if self.mode not in INDRA_MODES:
            # "text" would hand back the PDF's own text layer, which local_text already reads.
            raise ValueError("indra mode must be ocr or auto")
        if self.output not in INDRA_OUTPUTS:
            raise ValueError("indra output must be text or markdown")
        if type(self.dpi) is not int or not 72 <= self.dpi <= 600:
            raise ValueError("indra dpi must be an integer between 72 and 600")
        if type(self.threads) is not int or not 1 <= self.threads <= 64:
            raise ValueError("indra threads must be an integer between 1 and 64")
        if type(self.math) is not bool:
            raise ValueError("indra math must be true or false")
        self._indra_module()

    @staticmethod
    def _indra_module() -> Any:
        try:
            return importlib.import_module("indra_ocr")
        except ImportError as exc:
            raise ValueError(
                "Indra OCR is missing; install it with pip install 'pageledger[indra]'"
            ) from exc

    @cached_property
    def _models(self) -> Path:
        """The model folder; Indra downloads it on first use when none is given."""
        indra = self._indra_module()
        return Path(indra.model_dir(self.model_dir, math=self.math))

    @cached_property
    def _models_sha256(self) -> str:
        digest = hashlib.sha256()
        for name in _MODEL_FILES:
            path = self._models / name
            if path.is_file():
                digest.update(name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()[:12]

    @cached_property
    def _converter(self) -> Any:
        indra = self._indra_module()
        return indra.load(
            path=str(self._models),
            threads=self.threads,
            dpi=float(self.dpi),
            mode=self.mode,
            math=self.math,
        )

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def preflight(self, sources: list[Path]) -> None:
        _ = sources
        self._indra_module()
        importlib.import_module("pypdfium2")

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
        _ = page_id, prompt
        converter = self._converter
        pdfium = importlib.import_module("pypdfium2")
        started = time.perf_counter()
        document = pdfium.PdfDocument(str(source))
        try:
            if not 1 <= page_number <= len(document):
                raise ValueError(f"page_number {page_number} out of range for {source}")
            page = converter.pdf_page(str(source), document, page_number - 1)
        finally:
            document.close()
        detected = list(page.lines)
        lines = [line for line in detected if line.text.strip()]
        markdown = self.output == "markdown" and page.markdown is not None
        content = page.markdown if markdown else "\n".join(line.text for line in lines)
        # A line Indra found but could not read counts as unread, confidence 0. On degraded
        # typescript and cursive it leaves up to half its lines empty, and dropping them
        # would hide the one sign that the reading is missing text.
        scores = [float(line.conf) * 100 if line.text.strip() else 0.0 for line in detected]
        detail: dict[str, Any] | None = None
        if scores:
            detail = {
                # Indra scores lines, not words; the names match the other engines'.
                "word_count": len(scores),
                "empty_lines": len(detected) - len(lines),
                "mean": round(mean(scores), 2),
                "min": round(min(scores), 2),
                "below_60_count": sum(score < 60 for score in scores),
                "below_60_ratio": round(sum(score < 60 for score in scores) / len(scores), 4),
                "unit": "line",
            }
        layout = {
            "source": page.source,
            "note": page.note,
            "rotation": page.rotation,
            "skew": round(float(page.skew), 2),
            "vertical": bool(page.vertical),
            "blocks": [
                {"label": block["label"], "bbox": [round(float(v), 1) for v in block["bbox"]]}
                for block in page.blocks
            ],
        }
        detail = {**(detail or {}), "layout": layout}
        if page.source == "error":
            raise RuntimeError(f"Indra could not read page {page_number}: {page.note}")
        return ExtractionResult(
            content=content,
            format="markdown" if markdown else "text",
            confidence=None if not scores else round(mean(scores) / 100, 4),
            model=(
                f"indra-ocr {self._indra_module().__version__}; "
                f"models sha256:{self._models_sha256}; mode={self.mode}; dpi={self.dpi}"
                + ("; math" if self.math else "")
                + f"; source={page.source}"
            ),
            warnings=[],
            usage={
                "pages": 1,
                "tokens": None,
                "compute_seconds": round(time.perf_counter() - started, 3),
                "cost_usd": None,
            },
            confidence_detail=detail,
        )
