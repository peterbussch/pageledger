"""Example PageLedger adapter: Tesseract TSV output clustered into a table.

Renders a PDF page with pdftoppm, runs Tesseract in TSV mode, and clusters
words into rows and columns by their pixel coordinates, emitting a
``markdown_table`` page that the schema aligner can consume. The first
detected row becomes the header.

This is *naive column clustering* — a demonstration of a structured-output
adapter, not a table-recognition engine. Merged cells, spanning headers,
and ragged columns will misalign; the point is that the aligner and the
grades then record that honestly. For real table work, wrap a dedicated
table extractor (Docling, a VLM, Textract) in the same shape.

Use with:

    PYTHONPATH=examples pageledger run input.pdf --config pageledger.yml --out run

and in pageledger.yml:

    run:
      adapter: tesseract_tsv_table_adapter:TesseractTsvTableAdapter
      adapter_options:
        dpi: 300
        lang: rus
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from pageledger.adapters import ExtractionResult, pdf_page_count

# Horizontal whitespace (pixels) treated as a column break. At 300 dpi one
# character is roughly 15-25 px, so 40 px is a deliberate gap, not kerning.
COLUMN_GAP_PX = 40


@dataclass(frozen=True)
class TesseractTsvTableAdapter:
    dpi: int = 300
    lang: str = "eng"
    name: str = "tesseract-tsv-table"
    version: str = "example"
    deterministic: bool = True
    input_types: tuple[str, ...] = ("pdf",)
    output_types: tuple[str, ...] = ("markdown_table",)
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
                    "-f", str(page_number),
                    "-l", str(page_number),
                    "-r", str(self.dpi),
                    "-png",
                    str(source),
                    str(prefix),
                ],
                check=True, capture_output=True, text=True,
            )
            images = sorted(tmp_path.glob("page-*.png"))
            if not images:
                raise RuntimeError("pdftoppm did not render an image")
            result = subprocess.run(
                ["tesseract", str(images[0]), "-", "-l", self.lang, "tsv"],
                check=True, capture_output=True, text=True,
            )

        words = _parse_tsv_words(result.stdout)
        table = _cluster_table(words)
        confidences = [word["conf"] for word in words]
        detail = None
        confidence = None
        if confidences:
            below_60 = sum(1 for value in confidences if value < 60)
            confidence = round(sum(confidences) / len(confidences) / 100, 4)
            detail = {
                "scale": "tesseract_word_conf_0_100",
                "word_count": len(confidences),
                "mean": round(sum(confidences) / len(confidences), 2),
                "min": min(confidences),
                "below_60_count": below_60,
                "below_60_ratio": round(below_60 / len(confidences), 4),
            }
        return ExtractionResult(
            content=table,
            format="markdown_table",
            confidence=confidence,
            model=f"tesseract:{self.lang}",
            warnings=[],
            usage={
                "pages": 1,
                "tokens": None,
                "compute_seconds": round(time.perf_counter() - started, 3),
                "cost_usd": None,
            },
            confidence_detail=detail,
        )


def _parse_tsv_words(tsv: str) -> list[dict]:
    """Extract recognized words (level 5, real confidence) from Tesseract TSV."""
    words: list[dict] = []
    lines = tsv.splitlines()
    if not lines:
        return words
    header = lines[0].split("\t")
    index = {column: position for position, column in enumerate(header)}
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != len(header) or fields[index["level"]] != "5":
            continue
        text = fields[index["text"]].strip()
        conf = float(fields[index["conf"]])
        if not text or conf < 0:
            continue
        words.append({
            "row_key": (
                int(fields[index["block_num"]]),
                int(fields[index["par_num"]]),
                int(fields[index["line_num"]]),
            ),
            "top": int(fields[index["top"]]),
            "left": int(fields[index["left"]]),
            "right": int(fields[index["left"]]) + int(fields[index["width"]]),
            "text": text,
            "conf": conf,
        })
    return words


def _cluster_table(words: list[dict]) -> str:
    """Render OCR words as a markdown table: TSV lines are rows, gaps are columns."""
    rows_by_key: dict[tuple, list[dict]] = {}
    for word in words:
        rows_by_key.setdefault(word["row_key"], []).append(word)

    rows: list[list[str]] = []
    for key in sorted(rows_by_key, key=lambda k: min(w["top"] for w in rows_by_key[k])):
        line_words = sorted(rows_by_key[key], key=lambda w: w["left"])
        cells: list[str] = []
        current = [line_words[0]]
        for word in line_words[1:]:
            if word["left"] - current[-1]["right"] > COLUMN_GAP_PX:
                cells.append(" ".join(w["text"] for w in current))
                current = [word]
            else:
                current.append(word)
        cells.append(" ".join(w["text"] for w in current))
        rows.append([cell.replace("|", "\\|") for cell in cells])

    if not rows:
        return ""
    # Header = the densest of the first rows, not blindly the first line:
    # scanned tables open with captions ("Таблица 4") before the real
    # column headers. Lines above the chosen header are dropped — a naive
    # trade-off this example accepts and real adapters should not.
    header_index = max(
        range(min(len(rows), 8)),
        key=lambda position: sum(1 for cell in rows[position] if cell),
    )
    rows = rows[header_index:]
    width = max(len(row) for row in rows)
    lines = []
    for position, row in enumerate(rows):
        padded = row + [""] * (width - len(row))
        lines.append("| " + " | ".join(padded) + " |")
        if position == 0:
            lines.append("|" + " --- |" * width)
    return "\n".join(lines) + "\n"
