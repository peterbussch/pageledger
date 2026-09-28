"""Bounded text comparison for independent document extraction attempts."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

MAX_TEXT_CHARS = 200_000
MAX_TOKENS = 10_000
MAX_SPANS = 20
_GROUPED_NUMBER = r"\d{1,3}(?:[ ,\u2009\u202f]\d{3})+"
_NUMBER_TOKEN = rf"(?:{_GROUPED_NUMBER}|\d+)(?:[.,]\d+)?"
_NUMBER = re.compile(rf"(?<!\w){_NUMBER_TOKEN}(?!\w)")
_TOKEN = re.compile(rf"{_NUMBER_TOKEN}|\w+", re.UNICODE)


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _context(tokens: list[str], start: int, end: int, radius: int = 5) -> str:
    return " ".join(tokens[max(0, start - radius) : min(len(tokens), end + radius)])


def compare_texts(left: str, right: str) -> dict:
    """Compare two texts and return bounded word-level differences and numbers."""
    if not isinstance(left, str) or not isinstance(right, str):
        raise ValueError("Attempt texts must be strings")
    truncated = len(left) > MAX_TEXT_CHARS or len(right) > MAX_TEXT_CHARS
    left_tokens = _TOKEN.findall(_normalise(left[:MAX_TEXT_CHARS]))
    right_tokens = _TOKEN.findall(_normalise(right[:MAX_TEXT_CHARS]))
    truncated = truncated or len(left_tokens) > MAX_TOKENS or len(right_tokens) > MAX_TOKENS
    left_tokens = left_tokens[:MAX_TOKENS]
    right_tokens = right_tokens[:MAX_TOKENS]
    matcher = SequenceMatcher(None, left_tokens, right_tokens, autojunk=False)
    spans: list[dict[str, str]] = []
    numeric: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if len(spans) < MAX_SPANS:
            spans.append(
                {"left": _context(left_tokens, i1, i2), "right": _context(right_tokens, j1, j2)}
            )
        left_numbers = [token for token in left_tokens[i1:i2] if _NUMBER.fullmatch(token)]
        right_numbers = [token for token in right_tokens[j1:j2] if _NUMBER.fullmatch(token)]
        for number, side, tokens, start, end in [
            *((n, "left", left_tokens, i1, i2) for n in left_numbers),
            *((n, "right", right_tokens, j1, j2) for n in right_numbers),
        ]:
            if len(numeric) >= MAX_SPANS:
                break
            numeric.append(
                {"number": number, "side": side, "context": _context(tokens, start, end)}
            )
    return {
        "agreement_ratio": matcher.ratio(),
        "spans": spans,
        "number_differences": numeric,
        "truncated": truncated,
    }
