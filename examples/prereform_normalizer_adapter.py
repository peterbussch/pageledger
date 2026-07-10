"""OCR + pre-reform Russian orthography normalization, as one adapter.

Wraps the built-in ``pdf_ocr`` adapter and canonicalizes its output from
pre-1918 Russian orthography to modern spelling: ѣ→е, і→и, ѳ→ф, ѵ→и, and
word-final ъ removed while morphologically significant ъ (before vowels,
as in съѣздъ→съезд or объектъ→объект) is preserved.

Run it against a run produced by plain ``pdf_ocr`` — not instead of one:

    pageledger run scan.pdf --adapter pdf_ocr --out runs/original
    pageledger rerun runs/original --config normalized.yml \\
        --out runs/normalized --adapter-path examples

with ``normalized.yml`` naming this adapter:

    run:
      adapter: prereform_normalizer_adapter:PrereformNormalizerAdapter
      adapter_options:
        dpi: 300
        lang: rus

That keeps the un-normalized parent run as the original evidence
(canonicalization must preserve the original — Piotrowski, *NLP for
Historical Texts*, 2012, ch. 4) and `pageledger compare-runs` shows every
page the normalization touched. The character count of applied
replacements is recorded as a result warning so the rewrite is visible in
provenance, never silent.

Caveat: character rules cannot resolve the n:m cases (міръ "world" and
миръ "peace" both normalize to мир), and OCR models trained on modern
Russian rarely emit the abolished letters at all — check
``terminal_hard_sign_count`` in quality.jsonl to see what survived.
"""

from __future__ import annotations

from pathlib import Path

from pageledger.adapters import ExtractionResult, PdfOcrAdapter

# Letters abolished in 1918 and their modern equivalents.
PREREFORM_CHARS = {
    "ѣ": "е", "Ѣ": "Е",  # yat
    "і": "и", "І": "И",  # decimal i
    "ѳ": "ф", "Ѳ": "Ф",  # fita
    "ѵ": "и", "Ѵ": "И",  # izhitsa
}

# Modern vowels plus pre-reform yat, which normalizes to a vowel.
VOWELS = "аеёиоуыэюяАЕЁИОУЫЭЮЯѣѢ"


def normalize_orthography(text: str) -> tuple[str, int]:
    """Normalize pre-reform Russian text; returns (text, replacements).

    ъ is dropped only where it carries no meaning (word-final); before a
    vowel it marks a hard boundary and must stay ("съезд" and "сезд" are
    different words).
    """
    if not text:
        return text, 0
    replacements = 0
    kept: list[str] = []
    for index, char in enumerate(text):
        if char in ("ъ", "Ъ"):
            next_char = text[index + 1] if index + 1 < len(text) else ""
            if next_char and next_char in VOWELS:
                kept.append(char)
            else:
                replacements += 1
            continue
        replacement = PREREFORM_CHARS.get(char)
        if replacement is not None:
            kept.append(replacement)
            replacements += 1
        else:
            kept.append(char)
    return "".join(kept), replacements


class PrereformNormalizerAdapter:
    """pdf_ocr followed by pre-reform orthography canonicalization."""

    name = "prereform_normalizer"
    version = "0.1"
    deterministic = True
    input_types = ("pdf",)
    output_types = ("text",)
    capabilities = ("ocr", "normalization", "local")

    def __init__(self, dpi: int = 300, lang: str = "rus") -> None:
        self._ocr = PdfOcrAdapter(dpi=dpi, lang=lang)

    def supports(self, action: str) -> bool:
        return self._ocr.supports(action)

    def page_count(self, source: Path) -> int:
        return self._ocr.page_count(source)

    def extract(
        self,
        source: Path,
        *,
        page_id: str,
        page_number: int,
        action: str,
        prompt: str | None = None,
    ) -> ExtractionResult:
        result = self._ocr.extract(
            source,
            page_id=page_id,
            page_number=page_number,
            action=action,
            prompt=prompt,
        )
        normalized, replacements = normalize_orthography(str(result.content))
        warnings: list[str] = list(result.warnings)
        if replacements:
            warnings.append(f"prereform_normalization_applied:{replacements}")
        return ExtractionResult(
            content=normalized,
            format=result.format,
            confidence=result.confidence,
            model=f"{result.model} + prereform-normalizer 0.1",
            warnings=warnings,
            usage=result.usage,
            confidence_detail=result.confidence_detail,
        )
