"""Example PageLedger adapter: Tesseract OCR over pages rendered by pdftoppm.

Use with:

    PYTHONPATH=examples pageledger run input.pdf --config pageledger.yml --out run

and in pageledger.yml:

    run:
      adapter: tesseract_pdftoppm_adapter:TesseractPdftoppmAdapter
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from pageledger.adapters import ExtractionResult, pdf_page_count


@dataclass(frozen=True)
class TesseractPdftoppmAdapter:
    name: str = "tesseract-pdftoppm"
    version: str = "example"
    deterministic: bool = True
    input_types: tuple[str, ...] = ("pdf",)
    output_types: tuple[str, ...] = ("text",)
    capabilities: tuple[str, ...] = ("ocr", "local")

    def supports(self, action: str) -> bool:
        return action == "transcribe_text"

    def page_count(self, source: Path) -> int:
        return pdf_page_count(source)

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
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            prefix = tmp_path / "page"
            subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-r",
                    "200",
                    "-png",
                    str(source),
                    str(prefix),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            images = sorted(tmp_path.glob("page-*.png"))
            if not images:
                raise RuntimeError("pdftoppm did not render an image")
            output_prefix = tmp_path / "ocr"
            subprocess.run(
                ["tesseract", str(images[0]), str(output_prefix), "-l", "eng"],
                check=True,
                capture_output=True,
                text=True,
            )
            text = output_prefix.with_suffix(".txt").read_text(
                encoding="utf-8",
                errors="replace",
            )
        return ExtractionResult(
            content=text,
            format="text",
            confidence=None,
            model="tesseract",
            warnings=[],
            usage={
                "pages": 1,
                "tokens": None,
                "compute_seconds": round(time.perf_counter() - started, 3),
                "cost_usd": None,
            },
        )
