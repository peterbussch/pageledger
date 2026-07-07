"""OCR + local-LLM cleanup: a free middle tier between Tesseract and a cloud VLM.

Runs the built-in ``pdf_ocr`` adapter, then asks a locally served language
model to fix character-level OCR errors ("ClA—<ontrolled" → "CIA-controlled",
noise fragments removed). Costs nothing but compute time, keeps the document
on your machine, and reports real token usage into the ledger.

This example targets ``mlx_lm`` (Apple Silicon). mlx_lm is NOT a PageLedger
dependency and never will be; this file is an extension pattern. The same
shape works with llama.cpp, Ollama, or vLLM — swap ``_generate``.

    pip install mlx-lm
    pageledger rerun runs/tesseract --config cleanup.yml \\
        --out runs/cleaned --adapter-path examples

with ``cleanup.yml``:

    run:
      adapter: local_llm_cleanup_adapter:LocalLlmCleanupAdapter
      adapter_options:
        model: mlx-community/gemma-4-26b-a4b-it-4bit
        lang: eng
        max_tokens: 8192

Two failure modes we hit in testing, both of which the ledger caught and
this adapter now guards against:

1. Thinking leak. Reasoning models can emit their thought process before
   the answer. Without stripping, ~8,000-character "transcriptions" of
   model reasoning landed in raw/ and passed every quality heuristic.
   The adapter strips thought blocks and records a
   ``thought_block_stripped`` warning into provenance when it does.
2. Token starvation. With max_tokens set low (2048), the model spent the
   whole budget thinking and the stripped answer was one character long.
   The ``short_text`` quality warning flagged every page. Give reasoning
   models room (8192+) or use a non-reasoning model.

A page the model cannot clean confidently should end up back in the review
queue via quality warnings, not silently degraded. Cleanup output is model
output: compare-runs against the plain OCR run before trusting it.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from pageledger.adapters import ExtractionResult, PdfOcrAdapter

PROMPT = """You are correcting OCR output from a scanned document.
Fix character-level recognition errors, join words broken across line
breaks, and remove obvious scanner noise. Do not paraphrase, summarize,
translate, or add content. Preserve line structure where it is meaningful.
Return only the corrected text.

OCR OUTPUT:
{text}"""

# Common reasoning-model markers. Extend for your model's chat template.
THOUGHT_BLOCK = re.compile(
    r"<think>.*?</think>|<\|channel\|>thought.*?<\|channel\|>final",
    re.DOTALL,
)


def strip_thought_blocks(text: str) -> tuple[str, bool]:
    stripped = THOUGHT_BLOCK.sub("", text)
    return stripped.strip(), stripped != text


class LocalLlmCleanupAdapter:
    """Tesseract OCR followed by local-LLM character cleanup."""

    name = "local_llm_cleanup"
    version = "0.1"
    deterministic = False  # sampling; pin temperature for near-determinism
    input_types = ("pdf",)
    output_types = ("text",)
    capabilities = ("ocr", "cleanup", "local")

    def __init__(
        self,
        model: str = "mlx-community/gemma-4-26b-a4b-it-4bit",
        lang: str = "eng",
        dpi: int = 300,
        max_tokens: int = 8192,
        temperature: float = 0.1,
    ) -> None:
        self._ocr = PdfOcrAdapter(dpi=dpi, lang=lang)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._llm = None  # loaded on first page, not at config validation

    def supports(self, action: str) -> bool:
        return self._ocr.supports(action)

    def page_count(self, source: Path) -> int:
        return self._ocr.page_count(source)

    def _generate(self, prompt: str) -> tuple[str, int]:
        """Return (completion, token_count). Swap this for other runtimes."""
        from mlx_lm import generate, load
        from mlx_lm.sample_utils import make_sampler

        if self._llm is None:
            self._llm = load(self.model)
        model, tokenizer = self._llm
        messages = [{"role": "user", "content": prompt}]
        templated = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True
        )
        completion = generate(
            model,
            tokenizer,
            templated,
            max_tokens=self.max_tokens,
            sampler=make_sampler(temp=self.temperature),
        )
        tokens = len(tokenizer.encode(completion)) + len(templated)
        return completion, tokens

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
        completion, tokens = self._generate(PROMPT.format(text=ocr_result.content))
        cleaned, had_thoughts = strip_thought_blocks(completion)

        warnings: list[Any] = list(ocr_result.warnings)
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
            confidence=None,  # the model does not report a usable confidence
            model=f"{ocr_result.model} + {self.model}",
            warnings=warnings,
            usage=usage,
            confidence_detail=ocr_result.confidence_detail,
        )
