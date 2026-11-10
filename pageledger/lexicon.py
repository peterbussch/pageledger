"""Lexicons for the rough trigger: how much of a reading is words a dictionary knows.

OCR that reads two columns as one, or garbles italics, writes non-words that no
warning catches; a falling share of known words is the signal. Install with
``pip install 'pageledger[ru]'`` for the Russian pymorphy3 dictionary, or name a
plain word list of any language.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Cyrillic words of three letters or more. Shorter tokens are mostly
# prepositions and abbreviations and say little about the reading.
_WORD = re.compile(r"[А-Яа-яЁё]{3,}")
# A word hyphenated across a line break, rejoined before it is judged.
_HYPHENATED = re.compile(r"([А-Яа-яЁё])[-‐]\s*\n\s*([а-яё])")
# The same for a word list, in any script: letters, with combining accents.
_LETTER = r"(?:[^\W\d_]|[̀-ͯ])"
_ANY_WORD = re.compile(rf"{_LETTER}{{3,}}")
_ANY_HYPHENATED = re.compile(rf"({_LETTER})[-‐]\s*\n\s*(?=[^\W\d_])")
# Pages with fewer words are not judged: a title page proves nothing.
MIN_WORDS = 20
_UNKNOWN_SAMPLE = 15


@dataclass(frozen=True)
class Lexicon:
    identity: dict[str, Any]
    known: Callable[[str], bool]
    words: Callable[[str], list[str]]


_LOADED: dict[str, Lexicon] = {}


def fold(word: str) -> str:
    """How a word list entry and a word read on the page are compared."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", word).casefold())


def load_lexicon(config: dict[str, Any]) -> Lexicon:
    """The configured lexicon, loaded once per process; a word list whenever it changes."""
    if config["provider"] == "wordlist":
        return _wordlist(Path(config["path"]).expanduser().resolve())
    key = f"{config['provider']}:{config['language']}"
    if key not in _LOADED:
        try:
            import pymorphy3
        except ImportError as exc:
            raise ValueError(
                "processing.lexicon needs pymorphy3; install it with pip install 'pageledger[ru]'"
            ) from exc
        analyzer = pymorphy3.MorphAnalyzer(lang="ru")
        _LOADED[key] = Lexicon(
            identity={
                "provider": "pymorphy3",
                "language": "ru",
                "version": importlib.metadata.version("pymorphy3"),
                "dictionary": importlib.metadata.version("pymorphy3-dicts-ru"),
            },
            known=lambda word: bool(analyzer.word_is_known(word.lower())),
            words=lambda text: _WORD.findall(
                _HYPHENATED.sub(r"\1\2", unicodedata.normalize("NFC", text))
            ),
        )
    return _LOADED[key]


def _wordlist(path: Path) -> Lexicon:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"processing.lexicon cannot read the word list {path}: {exc}") from exc
    sha256 = hashlib.sha256(data).hexdigest()
    key = f"wordlist:{path}:{sha256}"
    if key not in _LOADED:
        try:
            lines = data.decode("utf-8-sig").splitlines()
        except UnicodeDecodeError as exc:
            raise ValueError(f"The word list {path} is not UTF-8 text") from exc
        entries = frozenset(fold(line.strip()) for line in lines if line.strip())
        if not entries:
            raise ValueError(f"The word list {path} has no words")
        _LOADED[key] = Lexicon(
            identity={
                "provider": "wordlist",
                "path": str(path),
                "sha256": sha256,
                "bytes": len(data),
                "words": len(entries),
                "match": "nfc-casefold",
            },
            known=lambda word: fold(word) in entries,
            words=lambda text: _ANY_WORD.findall(_rejoin(unicodedata.normalize("NFC", text))),
        )
    return _LOADED[key]


def _rejoin(text: str) -> str:
    """Join a word hyphenated across a line break when the next line goes on in lower case."""
    return _ANY_HYPHENATED.sub(
        lambda m: m.group(1) if text[m.end()].islower() else m.group(0), text
    )


def roughness(text: str, lexicon: Lexicon) -> dict[str, Any] | None:
    """Share of known words in a reading, with a sample of the unknown ones."""
    words = lexicon.words(text)
    if len(words) < MIN_WORDS:
        return None
    unknown = [word for word in words if not lexicon.known(word)]
    return {
        "known_share": round(1 - len(unknown) / len(words), 4),
        "words": len(words),
        "unknown": unknown[:_UNKNOWN_SAMPLE],
    }
