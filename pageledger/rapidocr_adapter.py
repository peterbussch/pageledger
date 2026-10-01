"""The built-in ``rapidocr`` adapter: RapidOCR 3 with a PP-OCRv5 recognizer you supply.

Install with ``pip install 'pageledger[rapidocr]'`` and point ``rec_model_dir`` at a
recognizer directory containing ``inference.onnx`` and ``keys.txt``, such as the
Cyrillic one, which keeps ѣ where RapidOCR's generic Russian model does not.
Without ``det_model_dir``, RapidOCR may download its default detection model on
first use.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from statistics import mean
from typing import Any, ClassVar

from .adapters import ExtractionResult, ocr_pdf_page_count


@dataclass(frozen=True)
class RapidOCRAdapter:
    rec_model_dir: str
    max_side: int = 3200
    det_model_dir: str | None = None
    reading_order: str = "rows"
    name: ClassVar[str] = "rapidocr"
    version: ClassVar[str] = "0.1"
    deterministic: ClassVar[bool] = True
    input_types: ClassVar[tuple[str, ...]] = ("pdf",)
    output_types: ClassVar[tuple[str, ...]] = ("text",)
    capabilities: ClassVar[tuple[str, ...]] = ("ocr", "page_image")

    def __post_init__(self) -> None:
        model_dir = Path(self.rec_model_dir).expanduser()
        if (
            not model_dir.is_dir()
            or not (model_dir / "inference.onnx").is_file()
            or not (model_dir / "keys.txt").is_file()
        ):
            raise ValueError(
                "rapidocr rec_model_dir must contain inference.onnx and keys.txt from a "
                "PP-OCRv5 recognizer"
            )
        if type(self.max_side) is not int or not 256 <= self.max_side <= 10000:
            raise ValueError(
                "run.adapter_options.max_side must be an integer between 256 and 10000"
            )
        if (
            self.det_model_dir is not None
            and not (Path(self.det_model_dir).expanduser() / "inference.onnx").is_file()
        ):
            raise ValueError("det_model_dir must contain inference.onnx for the detection model")
        if self.reading_order not in READING_ORDERS:
            raise ValueError("rapidocr reading_order must be rows or columns")
        self._rapidocr_module()

    @cached_property
    def _engine(self) -> Any:
        rapidocr = self._rapidocr_module()
        params: dict[str, Any] = {
            "Global.use_cls": False,
            "Global.max_side_len": self.max_side,
            "Global.log_level": "warning",
            "Rec.model_path": str(Path(self.rec_model_dir).expanduser() / "inference.onnx"),
            "Rec.rec_keys_path": str(Path(self.rec_model_dir).expanduser() / "keys.txt"),
            "Rec.ocr_version": rapidocr.OCRVersion.PPOCRV5,
        }
        if self.det_model_dir is not None:
            params["Det.model_path"] = str(Path(self.det_model_dir).expanduser() / "inference.onnx")
        return rapidocr.RapidOCR(params=params)

    @cached_property
    def _recognizer_sha256(self) -> str:
        model = Path(self.rec_model_dir).expanduser() / "inference.onnx"
        return hashlib.sha256(model.read_bytes()).hexdigest()[:12]

    @staticmethod
    def _rapidocr_module() -> Any:
        try:
            return importlib.import_module("rapidocr")
        except ImportError as exc:
            raise ValueError(
                "RapidOCR is missing; install it with pip install 'pageledger[rapidocr]'"
            ) from exc

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def preflight(self, sources: list[Path]) -> None:
        _ = sources
        if shutil.which("pdftoppm") is None:
            raise ValueError("pdftoppm is required; install Poppler")
        self._rapidocr_module()

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
        engine = self._engine
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="pageledger-rapidocr-") as tmp:
            prefix = Path(tmp) / "page"
            subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-singlefile",
                    "-scale-to",
                    str(self.max_side),
                    "-png",
                    str(source),
                    str(prefix),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            image = Path(tmp) / "page.png"
            if not image.is_file():
                raise RuntimeError(f"pdftoppm produced no image for page {page_number}")
            result = engine(str(image))
        lines = []
        boxes = result.boxes if result.boxes is not None else []
        for box, text, score in zip(boxes, result.txts or [], result.scores or [], strict=False):
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            lines.append(
                {
                    "text": text,
                    "score": float(score),
                    "cx": sum(xs) / 4,
                    "cy": sum(ys) / 4,
                    "h": max(ys) - min(ys),
                    "x0": min(xs),
                    "x1": max(xs),
                    "y0": min(ys),
                    "y1": max(ys),
                }
            )
        content = assemble(lines, reading_order=self.reading_order)
        scores = [line["score"] * 100 for line in lines]
        detail = (
            None
            if not scores
            else {
                "word_count": len(scores),
                "mean": round(mean(scores), 2),
                "min": round(min(scores), 2),
                "below_60_count": sum(score < 60 for score in scores),
                "below_60_ratio": round(sum(score < 60 for score in scores) / len(scores), 4),
            }
        )
        return ExtractionResult(
            content=content,
            format="text",
            confidence=None if not scores else round(mean(line["score"] for line in lines), 4),
            model=(
                f"rapidocr {importlib.metadata.version('rapidocr')}; rec PP-OCRv5 "
                f"sha256:{self._recognizer_sha256}; scale-to={self.max_side}"
                + ("; reading-order=columns" if self.reading_order == "columns" else "")
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


READING_ORDERS = ("rows", "columns")


def assemble(lines: list[dict[str, Any]], reading_order: str = "rows") -> str:
    """Join OCR lines in reading order.

    ``rows`` clusters lines into rows across the whole page, which keeps the
    cells of a table together. ``columns`` first cuts the page at whitespace
    (recursive XY-cut), so the columns of a journal page are read one after
    the other instead of line by line across the gutter.
    """
    if not lines:
        return ""
    if reading_order == "columns":
        heights = sorted(line["h"] for line in lines)
        gap = max(heights[len(heights) // 2], 1.0)
        return "\n".join(_rows(block) for block in _xy_cut(lines, gap))
    return _rows(lines)


def _gaps(spans: list[tuple[float, float]], minimum: float) -> list[float]:
    """Cut positions inside whitespace wider than ``minimum`` along one axis."""
    cuts, end = [], None
    for start, stop in sorted(spans):
        if end is not None and start - end > minimum:
            cuts.append((end + start) / 2)
        end = stop if end is None else max(end, stop)
    return cuts


def _split(lines: list[dict[str, Any]], axis: str, gap: float) -> list[list[dict[str, Any]]]:
    low, high, centre, size = (
        ("y0", "y1", "cy", 0.8 * gap) if axis == "y" else ("x0", "x1", "cx", gap)
    )
    edges = [float("-inf"), *_gaps([(line[low], line[high]) for line in lines], size), float("inf")]
    parts = [
        [line for line in lines if edges[i] < line[centre] <= edges[i + 1]]
        for i in range(len(edges) - 1)
    ]
    return [part for part in parts if part]


def _gutters(lines: list[dict[str, Any]], gap: float) -> list[float]:
    return _gaps([(line["x0"], line["x1"]) for line in lines], gap)


def _xy_cut(lines: list[dict[str, Any]], gap: float) -> list[list[dict[str, Any]]]:
    """Split at horizontal whitespace bands, then at vertical gutters.

    Consecutive bands with the same gutter are rejoined first, so a paragraph
    break that happens to fall at the same height in both columns does not
    make the page read left-top, right-top, left-bottom, right-bottom.
    """
    bands = _split(lines, "y", gap)
    merged: list[list[dict[str, Any]]] = []
    for band in bands:
        if merged:
            above, here = _gutters(merged[-1], gap), _gutters(band, gap)
            if above and here and all(any(abs(a - b) <= 2 * gap for b in above) for a in here):
                merged[-1] = merged[-1] + band
                continue
        merged.append(band)
    if len(merged) > 1:
        return [block for band in merged for block in _xy_cut(band, gap)]
    columns = _split(lines, "x", gap)
    if len(columns) > 1:
        return [block for column in columns for block in _xy_cut(column, gap)]
    return [lines]


def _rows(lines: list[dict[str, Any]]) -> str:
    """Cluster OCR lines into rows, preserving left-to-right order."""
    heights = sorted(line["h"] for line in lines)
    tolerance = heights[len(heights) // 2] * 0.55
    rows: list[list[dict[str, Any]]] = []
    for line in sorted(lines, key=lambda item: item["cy"]):
        if rows and abs(rows[-1][0]["cy"] - line["cy"]) <= tolerance:
            rows[-1].append(line)
        else:
            rows.append([line])
    return "\n".join(
        " | ".join(item["text"] for item in sorted(row, key=lambda item: item["cx"]))
        for row in rows
    )
