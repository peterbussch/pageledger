"""Quality evidence construction for PageLedger extraction results."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from functools import lru_cache
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
_PROSE_ONLY_WARNINGS = frozenset(
    {"suspicious_symbol_density", "fragmented_text", "joined_text", "digits_only_text"}
)
# Model loops. On 287 outputs of 24 transcribed pages by 11 engines, these
# limits flag 16 pages from the local vision models, every one a loop on
# inspection, and nothing from classic OCR, hosted models or the reference.
# Tables that repeat a label stay under the 30% share. A repeated tail is one
# unit of up to 200 characters, 20 times over.
_LOOP_MIN_IDENTICAL_LINES = 20
_LOOP_MIN_IDENTICAL_SHARE = 0.30
_LOOP_MIN_TAIL_REPEATS = 20
_LOOP_MAX_TAIL_UNIT = 200
_LOOP_MIN_LETTER_RUN = 40
# Declared-language checks judge only pages with enough letters. A page is in
# the wrong script when fewer than half its letters are in the declared one. It
# has lost its pre-reform spelling when 300 or more Cyrillic letters include no
# abolished letter and at most one word in a hundred ends in a hard sign.
_LANGUAGE_MIN_LETTERS = 200
_SCRIPT_MIN_SHARE = 0.5
_PREREFORM_MIN_LETTERS = 300
_FINAL_HARD_SIGN_MIN_SHARE = 0.01


def _build_quality_entry(
    *,
    schema_version: str,
    page: dict[str, Any],
    source: Path,
    result: Any,
    adapter: Any,
    parent_quality: dict[str, Any] | None = None,
    language: dict[str, str] | None = None,
) -> dict[str, Any]:
    text = _quality_text(result.content)
    character_count = len(text)
    tokens = _alphabetic_tokens(text)
    token_lengths = [len(token) for token in tokens]
    word_count = len(token_lengths)
    # Adapter-native warnings are quality evidence, not provenance-only notes.
    # Preserve them verbatim so a backend can surface partial or structurally
    # incomplete output to grading and the audit queue.
    warnings: list[str] = list(result.warnings)
    if not any(character.isalnum() for character in text):
        warnings.append("empty_text")
    elif character_count < 10:
        warnings.append("short_text")
    text_quality = _text_quality_metrics(
        text,
        character_count=character_count,
        token_lengths=token_lengths,
    )
    text_quality.update(_content_coverage_metrics(text, tokens))
    repetition_metrics, repetition_warnings = _repetition_evidence(text)
    text_quality.update(repetition_metrics)
    shape_warnings = _text_quality_warnings(text_quality)
    if result.format in ALIGNABLE_FORMATS:
        # The symbol/shape heuristics are calibrated on prose. Structured
        # payloads are full of pipes, braces, and short numeric tokens by
        # construction — flagging them would be noise, not evidence.
        shape_warnings = [
            warning for warning in shape_warnings if warning not in _PROSE_ONLY_WARNINGS
        ]
    warnings.extend(shape_warnings)
    warnings.extend(repetition_warnings)
    if language:
        warnings.extend(_declared_language_warnings(text, language))
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
    warnings = list(dict.fromkeys(warnings))
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


def _alphabetic_tokens(text: str) -> list[str]:
    r"""Unicode letter tokens, including combining marks.

    Python's ``\w`` does not include combining marks, so it splits many Indic
    words into one-character fragments. Join controls preserve a token but are
    left out of it, so they do not count toward its length.
    """
    tokens: list[str] = []
    current: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("L") or (category.startswith("M") and current):
            current.append(char)
        elif char in {"\u200c", "\u200d"} and current:
            continue
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _alphabetic_token_lengths(text: str) -> list[int]:
    return [len(token) for token in _alphabetic_tokens(text)]


_LOOKALIKE_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK")
_PRIVATE_USE = re.compile("[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]")


@lru_cache(maxsize=4096)
def _lookalike_script(char: str) -> str | None:
    name = unicodedata.name(char, "")
    return next((script for script in _LOOKALIKE_SCRIPTS if name.startswith(script)), None)


def _private_use_count(text: str) -> int:
    """Private Use Area code points, except one opening a line before a space.

    That exception is a bullet or icon glyph from a symbol font, common and
    harmless in word-processor PDFs. Elsewhere a private-use code point is a
    character the text layer lost, such as an old-style digit or a ligature.
    """
    count = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        for match in _PRIVATE_USE.finditer(stripped):
            bullet = match.start() == 0 and not stripped[1:2].strip()
            count += not bullet
    return count


def _content_coverage_metrics(text: str, tokens: list[str]) -> dict[str, Any]:
    """Counts for text layers that are present but carry little of the page."""
    mixed = sum(
        1
        for token in tokens
        if len({script for char in token if (script := _lookalike_script(char))}) > 1
    )
    return {
        "letter_count": sum(1 for char in text if unicodedata.category(char).startswith("L")),
        "digit_count": sum(1 for char in text if char.isdecimal()),
        "mixed_script_token_ratio": 0.0 if not tokens else round(mixed / len(tokens), 4),
        "private_use_count": _private_use_count(text),
    }


def _repetition_evidence(text: str) -> tuple[dict[str, int], list[str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    identical = max(
        (count for line, count in Counter(lines).items() if not _is_rule_line(line)), default=0
    )
    tail = _repeated_tail_length(text)
    letter_run = max(
        (
            len(match.group())
            for match in re.finditer(r"(.)\1+", text)
            if unicodedata.category(match.group(1)).startswith("L")
        ),
        default=0,
    )
    looping = (
        identical >= max(_LOOP_MIN_IDENTICAL_LINES, _LOOP_MIN_IDENTICAL_SHARE * len(lines))
        or tail > 0
        or letter_run >= _LOOP_MIN_LETTER_RUN
    )
    metrics = {
        "largest_identical_line_count": identical,
        "longest_repeated_tail_length": tail,
        "longest_letter_run": letter_run,
    }
    return metrics, ["repetition_loop"] if looping else []


def _is_rule_line(line: str) -> bool:
    """Dot leaders and rules, which tables legitimately repeat."""
    compact = line.replace(" ", "")
    return bool(compact) and all(char in ".·_-=—–" for char in compact)


def _repeated_tail_length(text: str) -> int:
    """Length of the longest ending made of one unit repeated 20 or more times."""
    tail = text.rstrip()
    if not tail or _is_rule_line(tail.splitlines()[-1]):
        return 0
    longest = 0
    for size in range(1, min(len(tail) // _LOOP_MIN_TAIL_REPEATS, _LOOP_MAX_TAIL_UNIT) + 1):
        unit, start = tail[-size:], len(tail) - size
        while start >= size and tail[start - size : start] == unit:
            start -= size
        if len(tail) - start >= _LOOP_MIN_TAIL_REPEATS * size:
            longest = max(longest, len(tail) - start)
    return longest


def _declared_language_warnings(text: str, language: dict[str, str]) -> list[str]:
    letters = [char for char in text if unicodedata.category(char).startswith("L")]
    if len(letters) < _LANGUAGE_MIN_LETTERS:
        return []
    warnings = []
    if _in_script(letters, language["script"]) < _SCRIPT_MIN_SHARE * len(letters):
        warnings.append("script_mismatch")
    words = len(_alphabetic_tokens(text))
    if (
        language.get("orthography") == "prereform"
        and _in_script(letters, "Cyrillic") >= _PREREFORM_MIN_LETTERS
        and not any(char in _PREREFORM_LETTERS for char in text)
        and len(_TERMINAL_HARD_SIGN.findall(text)) <= max(1, _FINAL_HARD_SIGN_MIN_SHARE * words)
    ):
        warnings.append("historical_letters_lost")
    return warnings


def _in_script(letters: list[str], script: str) -> int:
    prefix = f"{script.upper()} "
    return sum(unicodedata.name(char, "").startswith(prefix) for char in letters)


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
        if unicodedata.category(char).startswith("L") and "LATIN" in unicodedata.name(char, "")
    )
    return {
        "replacement_character_count": replacement_character_count,
        "control_character_count": control_character_count,
        "suspicious_symbol_count": suspicious_symbol_count,
        "suspicious_symbol_ratio": (
            0.0 if character_count == 0 else round(suspicious_symbol_count / character_count, 4)
        ),
        # Lexical shape of the output. Language-neutral evidence: sort pages
        # by mean_token_length to find fragment noise. These metrics cannot
        # detect word-level misrecognition ("matericl" for "material") \u2014
        # that needs a dictionary or model, which PageLedger does not ship.
        "alpha_token_count": alpha_token_count,
        "mean_token_length": (
            None if alpha_token_count == 0 else round(sum(token_lengths) / alpha_token_count, 2)
        ),
        "max_token_length": max(token_lengths, default=0),
        "short_token_ratio": (
            None
            if alpha_token_count == 0
            else round(sum(1 for length in token_lengths if length <= 2) / alpha_token_count, 4)
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
    # The three rules below were measured on 5,702 sampled pages from 1,500
    # PDFs and on 24 transcribed pages; see the warning table in
    # docs/provenance-spec.md.
    letters, digits = metrics["letter_count"], metrics["digit_count"]
    if digits >= 20 and letters < 0.05 * (letters + digits):
        # A table whose text layer kept the digits and lost the words. In the
        # survey every such page came from one Internet Archive derivative
        # (LuraDocument); transcribed tables have at least 20% letters.
        warnings.append("digits_only_text")
    if metrics["alpha_token_count"] >= 20 and metrics["mixed_script_token_ratio"] >= 0.05:
        # Latin look-alikes inside Cyrillic words, or the reverse.
        warnings.append("mixed_script_tokens")
    if metrics["private_use_count"] >= 3:
        warnings.append("private_use_characters")
    return warnings


_REPEATED_TEXT_MAX_CHARACTERS = 200
_REPEATED_TEXT_MIN_PAGES = 3


def repeated_text_key(content: Any) -> str | None:
    """A short page's normalized text, for finding text repeated across pages."""
    text = " ".join(unicodedata.normalize("NFC", _quality_text(content)).split())
    if len(text) >= _REPEATED_TEXT_MAX_CHARACTERS or not any(char.isalnum() for char in text):
        return None
    return text


def mark_repeated_page_text(entries: list[dict[str, Any]], keys: dict[str, str]) -> None:
    """Warn on pages whose entire short text recurs on three or more pages of a run.

    Stamp-only text layers repeat a scanner's or website's watermark on every
    page and none of the page's content. Each such page alone looks clean.
    """
    counts = Counter(keys.values())
    for entry in entries:
        key = keys.get(entry["page_id"])
        if key is not None and counts[key] >= _REPEATED_TEXT_MIN_PAGES:
            if "repeated_page_text" not in entry["warnings"]:
                entry["warnings"].append("repeated_page_text")


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
