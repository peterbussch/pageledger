"""Lexicons for the rough trigger: how much of a reading is words a dictionary knows.

OCR that reads two columns as one, or garbles italics, writes non-words that no
warning catches; a falling share of known words is the signal. Install with
``pip install 'pageledger[ru]'`` for the Russian pymorphy3 dictionary.
"""

from __future__ import annotations

import importlib.metadata
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# Cyrillic words of three letters or more. Shorter tokens are mostly
# prepositions and abbreviations and say little about the reading.
_WORD = re.compile(r"[А-Яа-яЁё]{3,}")
# A word hyphenated across a line break, rejoined before it is judged.
_HYPHENATED = re.compile(r"([А-Яа-яЁё])[-‐]\s*\n\s*([а-яё])")
# Pages with fewer words are not judged: a title page proves nothing.
MIN_WORDS = 20
_UNKNOWN_SAMPLE = 15


@dataclass(frozen=True)
class Lexicon:
    identity: dict[str, str]
    analyzer: Any

    def known(self, word: str) -> bool:
        return bool(self.analyzer.word_is_known(word.lower()))


_LOADED: dict[str, Lexicon] = {}


def load_lexicon(config: dict[str, Any]) -> Lexicon:
    """The configured lexicon, loaded once per process."""
    key = f"{config['provider']}:{config['language']}"
    if key not in _LOADED:
        try:
            import pymorphy3
        except ImportError as exc:
            raise ValueError(
                "processing.lexicon needs pymorphy3; install it with pip install 'pageledger[ru]'"
            ) from exc
        _LOADED[key] = Lexicon(
            identity={
                "provider": "pymorphy3",
                "language": "ru",
                "version": importlib.metadata.version("pymorphy3"),
                "dictionary": importlib.metadata.version("pymorphy3-dicts-ru"),
            },
            analyzer=pymorphy3.MorphAnalyzer(lang="ru"),
        )
    return _LOADED[key]


def roughness(text: str, lexicon: Lexicon) -> dict[str, Any] | None:
    """Share of known words in a reading, with a sample of the unknown ones."""
    joined = _HYPHENATED.sub(r"\1\2", unicodedata.normalize("NFC", text))
    words = _WORD.findall(joined)
    if len(words) < MIN_WORDS:
        return None
    unknown = [word for word in words if not lexicon.known(word)]
    return {
        "known_share": round(1 - len(unknown) / len(words), 4),
        "words": len(words),
        "unknown": unknown[:_UNKNOWN_SAMPLE],
    }
