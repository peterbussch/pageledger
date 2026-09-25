"""macOS Apple Vision OCR adapter for post-1918 printed material.

Build the helper with ``swiftc examples/apple_vision_ocr.swift -o apple_vision_ocr``.
Apple Vision modernizes historical letters; do not use it for pre-reform print.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from pageledger.adapters import ExtractionResult, ocr_pdf_page_count


@dataclass(frozen=True)
class AppleVisionAdapter:
    helper: str = "apple_vision_ocr"
    languages: str = "ru-RU"
    max_side: int = 3000
    name: ClassVar[str] = "apple-vision-ocr"
    version: ClassVar[str] = "example-0.1"
    deterministic: ClassVar[bool] = False
    input_types: ClassVar[tuple[str, ...]] = ("pdf",)
    output_types: ClassVar[tuple[str, ...]] = ("text",)
    capabilities: ClassVar[tuple[str, ...]] = ("ocr", "page_image")

    def __post_init__(self) -> None:
        if type(self.max_side) is not int or not 256 <= self.max_side <= 10000:
            raise ValueError(
                "run.adapter_options.max_side must be an integer between 256 and 10000"
            )

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def preflight(self, sources: list[Path]) -> None:
        _ = sources
        if shutil.which("pdftoppm") is None:
            raise ValueError("pdftoppm is required; install Poppler")
        if shutil.which(self.helper) is None and not Path(self.helper).is_file():
            raise ValueError(
                "Apple Vision helper is missing; build it with: swiftc examples/apple_vision_ocr.swift -o apple_vision_ocr"
            )

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
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="pageledger-vision-") as tmp:
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
            completed = subprocess.run(
                [self.helper, self.languages, str(image)],
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
        records = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        if not records:
            raise RuntimeError("Apple Vision helper returned no result")
        if records[0].get("error"):
            raise RuntimeError(str(records[0]["error"]))
        lines = []
        for record in records:
            for line in record.get("lines", []):
                box = line.get("box", [0, 0, 0, 0])
                lines.append(
                    {
                        "text": line["text"],
                        "score": float(line.get("confidence", 0)),
                        "cx": float(box[0]) + float(box[2]) / 2,
                        "cy": 1 - float(box[1]) - float(box[3]) / 2,
                        "h": float(box[3]),
                    }
                )
        text = "\n".join(" | ".join(item["text"] for item in row) for row in _rows(lines))
        return ExtractionResult(
            content=text,
            format="text",
            confidence=None,
            model=(
                f"Apple Vision revision {records[0].get('revision', 'unknown')}; "
                f"macOS {platform.mac_ver()[0]}; post-1918 print only"
            ),
            warnings=[],
            usage={
                "pages": 1,
                "tokens": None,
                "compute_seconds": round(time.perf_counter() - started, 3),
                "cost_usd": None,
            },
        )


def _rows(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not lines:
        return []
    heights = sorted(item["h"] for item in lines)
    tolerance = heights[len(heights) // 2] * 0.55
    rows: list[list[dict[str, Any]]] = []
    for item in sorted(lines, key=lambda line: line["cy"]):
        if rows and abs(rows[-1][0]["cy"] - item["cy"]) <= tolerance:
            rows[-1].append(item)
        else:
            rows.append([item])
    return [sorted(row, key=lambda item: item["cx"]) for row in rows]
