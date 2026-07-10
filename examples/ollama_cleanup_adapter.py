"""Tesseract OCR followed by cleanup through an Ollama model.

This example wraps PageLedger's built-in 'pdf_ocr' adapter, then sends the
OCR text to Ollama's '/api/generate' endpoint. It uses only the Python
standard library. Ollama itself is optional software and is not a PageLedger
dependency.

    pageledger rerun runs/tesseract --config cleanup.yml \
        --out runs/cleaned --adapter-path examples

with cleanup.yml:

    run:
      adapter: ollama_cleanup_adapter:OllamaCleanupAdapter
      adapter_options:
        model: gemma3:12b
        base_url: http://127.0.0.1:11434
        lang: eng
        max_tokens: 8192

The adapter strips reasoning blocks with the same guard used by
local_llm_cleanup_adapter.py. An unterminated reasoning block becomes empty
text so PageLedger grades it as failed evidence instead of accepting a
reasoning transcript as OCR.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from urllib.request import Request, urlopen

from local_llm_cleanup_adapter import strip_thought_blocks

from pageledger.adapters import ExtractionResult, PdfOcrAdapter

PROMPT = """You are correcting OCR output from a scanned document.
Fix character-level recognition errors, join words broken across line
breaks, and remove obvious scanner noise. Do not paraphrase, summarize,
translate, or add content. Preserve line structure where it is meaningful.
Return only the corrected text.

OCR OUTPUT:
{text}"""


class OllamaCleanupAdapter:
    """Tesseract OCR followed by character cleanup through Ollama."""

    name = "ollama_cleanup"
    version = "0.1"
    deterministic = False
    input_types = ("pdf",)
    output_types = ("text",)
    capabilities = ("ocr", "cleanup", "local")

    def __init__(
        self,
        model: str = "gemma3:12b",
        base_url: str = "http://127.0.0.1:11434",
        lang: str = "eng",
        dpi: int = 300,
        max_tokens: int = 8192,
        temperature: float = 0.1,
        timeout_seconds: float = 300,
    ) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("run.adapter_options.model must be a non-empty string")
        if not isinstance(base_url, str) or not base_url:
            raise ValueError("run.adapter_options.base_url must be a non-empty string")
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
            raise ValueError("run.adapter_options.max_tokens must be a positive integer")
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature)
            or temperature < 0
        ):
            raise ValueError(
                "run.adapter_options.temperature must be a finite non-negative number"
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError(
                "run.adapter_options.timeout_seconds must be a finite positive number"
            )
        self._ocr = PdfOcrAdapter(dpi=dpi, lang=lang)
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = float(temperature)
        self.timeout_seconds = float(timeout_seconds)

    def supports(self, action: str) -> bool:
        return self._ocr.supports(action)

    def page_count(self, source: Path) -> int:
        return self._ocr.page_count(source)

    def _generate(self, prompt: str) -> tuple[str, int | None]:
        payload = json.dumps(
            {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {
                    "num_predict": self.max_tokens,
                    "temperature": self.temperature,
                },
            }
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read()
        try:
            result = json.loads(body)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Ollama returned invalid JSON") from exc
        completion = result.get("response") if isinstance(result, dict) else None
        if not isinstance(completion, str):
            raise RuntimeError("Ollama response did not contain text")

        token_counts: list[int] = []
        for field in ("prompt_eval_count", "eval_count"):
            value = result.get(field)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RuntimeError(f"Ollama returned invalid {field}")
            token_counts.append(value)
        return completion, sum(token_counts) if token_counts else None

    def extract(
        self,
        source: Path,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        ocr_result = self._ocr.extract(
            source,
            page_id=page_id,
            page_number=page_number,
            action=action,
            prompt=prompt,
        )
        started = time.perf_counter()
        if prompt:
            cleanup_prompt = prompt.replace("{text}", str(ocr_result.content))
            if "{text}" not in prompt:
                cleanup_prompt = f"{cleanup_prompt.rstrip()}\n\nOCR OUTPUT:\n{ocr_result.content}"
        else:
            cleanup_prompt = PROMPT.format(text=ocr_result.content)
        completion, tokens = self._generate(cleanup_prompt)
        cleaned, had_thoughts = strip_thought_blocks(completion)

        warnings = list(ocr_result.warnings)
        if had_thoughts:
            warnings.append("thought_block_stripped")
        usage = dict(ocr_result.usage)
        usage["tokens"] = tokens
        usage["compute_seconds"] = round(
            (usage.get("compute_seconds") or 0.0)
            + (time.perf_counter() - started),
            3,
        )
        return ExtractionResult(
            content=cleaned,
            format="text",
            confidence=None,
            model=f"{ocr_result.model} + ollama:{self.model}",
            warnings=warnings,
            usage=usage,
            confidence_detail=ocr_result.confidence_detail,
        )
