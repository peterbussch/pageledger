"""Quality evidence construction for PageLedger extraction results."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from .aligner import ALIGNABLE_FORMATS

_INSTRUCTION_MARKERS = (
    "<think>",
    "</think>",
    "<|channel",
    "<|im_start|>",
    "<|im_end|>",
    "[INST]",
    "[/INST]",
)


def _build_quality_entry(
    *,
    schema_version: str,
    page: dict[str, Any],
    source: Path,
    result: Any,
    adapter: Any,
    parent_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = _quality_text(result.content)
    character_count = len(text)
    token_lengths = _alphabetic_token_lengths(text)
    word_count = len(token_lengths)
    warnings: list[str] = []
    if character_count == 0:
        warnings.append("empty_text")
    elif character_count < 10:
        warnings.append("short_text")
    text_quality = _text_quality_metrics(
        text,
        character_count=character_count,
        token_lengths=token_lengths,
    )
    shape_warnings = _text_quality_warnings(text_quality)
    if result.format in ALIGNABLE_FORMATS:
        # The symbol/shape heuristics are calibrated on prose. Structured
        # payloads are full of pipes, braces, and short numeric tokens by
        # construction — flagging them would be noise, not evidence.
        shape_warnings = [
            warning
            for warning in shape_warnings
            if warning
            not in {"suspicious_symbol_density", "fragmented_text", "joined_text"}
        ]
    warnings.extend(shape_warnings)
    output_integrity, integrity_warnings = _output_integrity(text, parent_quality)
    warnings.extend(integrity_warnings)
    confidence_detail = getattr(result, "confidence_detail", None)
    if _has_low_confidence_tail(confidence_detail):
        warnings.append("low_confidence")
    embedded = _embedded_text_quality(source, page["page_number"], adapter)
    delta: dict[str, Any] | None = None
    if embedded is not None:
        embedded_chars = len(embedded)
        char_delta = character_count - embedded_chars
        ratio = None if embedded_chars == 0 else round(character_count / embedded_chars, 4)
        delta = {
            "embedded_character_count": embedded_chars,
            "character_delta": char_delta,
            "character_ratio": ratio,
        }
        if embedded_chars > 0 and (ratio is not None and (ratio < 0.5 or ratio > 1.8)):
            warnings.append("suspicious_embedded_text_delta")
    return {
        "schema_version": schema_version,
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "adapter": adapter.name,
        "character_count": character_count,
        "word_count": word_count,
        "confidence": result.confidence,
        "confidence_detail": confidence_detail,
        "warnings": warnings,
        "text_quality": text_quality,
        "embedded_text_comparison": delta,
        "output_integrity": output_integrity,
    }


def _has_low_confidence_tail(detail: Any) -> bool:
    """True when engine-native word confidences show a weak tail.

    A quarter of the words under confidence 60 flags the page; a mean can
    hide one illegible paragraph on an otherwise clean page. Requires 10+
    words — less is not enough evidence to warn on.
    """
    if not isinstance(detail, dict):
        return False
    ratio = detail.get("below_60_ratio")
    word_count = detail.get("word_count")
    return (
        isinstance(ratio, (int, float))
        and isinstance(word_count, int)
        and word_count >= 10
        and ratio >= 0.25
    )


def _quality_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _output_integrity(
    text: str, parent_quality: dict[str, Any] | None
) -> tuple[dict[str, Any], list[str]]:
    """Return conservative chat-leak and rerun-size evidence for one page."""
    folded = text.casefold()
    markers = [marker for marker in _INSTRUCTION_MARKERS if marker.casefold() in folded]
    warnings = ["instruction_echo"] if markers else []

    parent_count: int | None = None
    if parent_quality is not None:
        candidate = parent_quality.get("character_count")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            parent_count = candidate

    character_delta: int | None = None
    character_ratio: float | None = None
    if parent_count is not None:
        character_delta = len(text) - parent_count
        if parent_count > 0:
            raw_ratio = len(text) / parent_count
            character_ratio = round(raw_ratio, 4)
            if raw_ratio >= 4.0 and character_delta >= 1000:
                warnings.append("output_inflation")
        elif character_delta >= 1000:
            warnings.append("output_inflation")

    return (
        {
            "instruction_markers": markers,
            "parent_character_count": parent_count,
            "character_delta": character_delta,
            "character_ratio": character_ratio,
        },
        warnings,
    )


# Letters abolished by the 1918 Russian orthographic reform. \u0456 is deliberately
# absent: it is standard modern Ukrainian and Belarusian.
_PREREFORM_LETTERS = frozenset("\u0463\u0462\u0473\u0472\u0475\u0474")
# Word-final hard sign \u2014 mandatory before 1918, absent from modern Russian.
# OCR models trained on modern text destroy the abolished letters but keep \u044a,
# so this is the pre-reform signal that survives extraction.
_TERMINAL_HARD_SIGN = re.compile(r"[\u044a\u042a](?![^\W\d_])")


def _alphabetic_token_lengths(text: str) -> list[int]:
    r"""Lengths of Unicode letter tokens, including combining marks.

    Python's ``\w`` does not include combining marks, so it splits many Indic
    words into one-character fragments. Join controls preserve a token but do
    not contribute to its measured length.
    """
    lengths: list[int] = []
    current_length = 0
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("L") or (category.startswith("M") and current_length):
            current_length += 1
        elif char in {"\u200c", "\u200d"} and current_length:
            continue
        elif current_length:
            lengths.append(current_length)
            current_length = 0
    if current_length:
        lengths.append(current_length)
    return lengths


def _text_quality_metrics(
    text: str,
    *,
    character_count: int,
    token_lengths: list[int] | None = None,
) -> dict[str, Any]:
    replacement_character_count = text.count("\ufffd")
    control_character_count = sum(
        1 for char in text if ord(char) < 32 and char not in {"\n", "\r", "\t", "\f"}
    )
    suspicious_symbol_count = sum(1 for char in text if _is_suspicious_symbol(char))
    if token_lengths is None:
        token_lengths = _alphabetic_token_lengths(text)
    alpha_token_count = len(token_lengths)
    letter_count = sum(1 for char in text if unicodedata.category(char).startswith("L"))
    latin_letter_count = sum(
        1
        for char in text
        if unicodedata.category(char).startswith("L")
        and "LATIN" in unicodedata.name(char, "")
    )
    return {
        "replacement_character_count": replacement_character_count,
        "control_character_count": control_character_count,
        "suspicious_symbol_count": suspicious_symbol_count,
        "suspicious_symbol_ratio": (
            0.0
            if character_count == 0
            else round(suspicious_symbol_count / character_count, 4)
        ),
        # Lexical shape of the output. Language-neutral evidence: sort pages
        # by mean_token_length to find fragment noise. These metrics cannot
        # detect word-level misrecognition ("matericl" for "material") \u2014
        # that needs a dictionary or model, which PageLedger does not ship.
        "alpha_token_count": alpha_token_count,
        "mean_token_length": (
            None
            if alpha_token_count == 0
            else round(sum(token_lengths) / alpha_token_count, 2)
        ),
        "max_token_length": max(token_lengths, default=0),
        "short_token_ratio": (
            None
            if alpha_token_count == 0
            else round(
                sum(1 for length in token_lengths if length <= 2) / alpha_token_count, 4
            )
        ),
        "whitespace_character_ratio": (
            0.0
            if character_count == 0
            else round(sum(char.isspace() for char in text) / character_count, 4)
        ),
        "latin_letter_ratio": (
            0.0 if letter_count == 0 else round(latin_letter_count / letter_count, 4)
        ),
        "prereform_letter_count": sum(1 for char in text if char in _PREREFORM_LETTERS),
        "terminal_hard_sign_count": len(_TERMINAL_HARD_SIGN.findall(text)),
    }


def _text_quality_warnings(metrics: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if metrics["replacement_character_count"] > 0:
        warnings.append("replacement_characters")
    if metrics["control_character_count"] > 0:
        warnings.append("control_characters")
    if metrics["suspicious_symbol_ratio"] >= 0.03 and metrics["suspicious_symbol_count"] >= 5:
        warnings.append("suspicious_symbol_density")
    mean_token_length = metrics["mean_token_length"]
    if (
        mean_token_length is not None
        and mean_token_length < 3.0
        and metrics["alpha_token_count"] >= 20
    ):
        # Real prose in tested corpora sits above 4; OCR fragment noise
        # ("l| ||| l|l ll") collapses toward 1.
        warnings.append("fragmented_text")
    if (
        mean_token_length is not None
        and mean_token_length >= 10.0
        and metrics["max_token_length"] >= 80
        and metrics["alpha_token_count"] >= 20
        and metrics["whitespace_character_ratio"] <= 0.03
        and metrics["latin_letter_ratio"] >= 0.8
    ):
        # Lost word boundaries in Latin-script hidden OCR create long joined
        # tokens with almost no whitespace. The multi-signal guard avoids
        # treating ordinary long words or unsegmented non-Latin scripts as
        # evidence of corruption.
        warnings.append("joined_text")
    if metrics["prereform_letter_count"] >= 2 or (
        metrics["alpha_token_count"] >= 20
        and metrics["terminal_hard_sign_count"] >= 2
        and metrics["terminal_hard_sign_count"] >= metrics["alpha_token_count"] / 100
    ):
        # Pre-1918 Russian orthography: the configured OCR model is probably
        # mismatched with the page. Measured on an 1850 gubernia review:
        # 21 terminal hard signs per 100 tokens vs 0.00 in modern text.
        warnings.append("historical_orthography")
    return warnings


def _is_suspicious_symbol(char: str) -> bool:
    if char in {"_", "|", "\\", "/", "{", "}", "[", "]", "•"}:
        return True
    if char.isalnum() or char.isspace():
        return False
    if unicodedata.category(char)[0] in {"L", "M", "N", "P", "Z"}:
        return False
    if char in ".,;:!?()'\"-$%&+=*#@<>":
        return False
    if char in "«»„“”‘’‚—–…·§№°":
        # Common typography and symbols, not extraction garble.
        return False
    return not char.isascii()


def _embedded_text_quality(source: Path, page_number: int, adapter: Any) -> str | None:
    if source.suffix.lower() != ".pdf":
        return None
    if "embedded_text" in getattr(adapter, "capabilities", ()):
        return None
    try:
        from .adapters import _pdf_page_text

        return _pdf_page_text(source, page_number)
    except Exception:
        return None
