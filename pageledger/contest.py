"""Contested spans: the places where a model's reading and a literal engine's differ.

A reader (a vision model) writes fluent text and silently corrects what it sees;
a literal engine (OCR, a text layer) misreads glyphs but does not invent words.
The reader's text is the base. Every place the two differ becomes a span
anchored in base offsets. Rules settle the spans whose answer follows from the
two readings alone; the rest stay open until someone checks them against the
page image.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from difflib import SequenceMatcher
from typing import Any

_PART = r"(?:[^\W_]|[̀-ͯ])+"
# A word, rejoined when the line ends in a hyphen and the next line goes on in lower case.
_TOKEN = re.compile(rf"{_PART}(?:[-‐­][ \t]*\n\s*(?=[a-zа-яё]){_PART})?")
_BREAK = re.compile(r"[-‐­][ \t]*\n\s*")
# What a reader writes that no literal engine can confirm word by word: formulas,
# figure lines, and its own marks of doubt.
_REGION = re.compile(
    r"\$\$.+?\$\$|\$[^$\n]+?\$|^\[Figure:[^\n]*\]$|\[illegible\]|\S+[ \t]*\[\?\]",
    re.DOTALL | re.MULTILINE,
)
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_LATIN = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")
SIMILAR = 0.6

_LATIN_LOOKALIKES = "ABCEHKMOPTXYaceopxyijs"
_CYRILLIC_LOOKALIKES = "АВСЕНКМОРТХУасеорхуіјѕ"
# Rule sets are data with a version, recorded in each job. Glyph pairs are
# (literal, reader) and belong to one engine: RapidOCR's confusions are not
# Tesseract's. These are the pairs RapidOCR's Cyrillic model made at least twice
# in the October 2026 dogfood (Большаков, Revzin), checked against the page image, except ё for е: print uses ё in linguistic examples.
RULE_SETS: dict[str, dict[str, Any]] = {
    "ru-print-0.1": {
        "glyph_pairs": {
            "rapidocr": [
                ("н", "п"),
                ("п", "н"),
                ("н", "и"),
                ("и", "н"),
                ("ш", "п"),
                ("п", "и"),
                ("и", "п"),
                ("с", "е"),
                ("ц", "щ"),
                ("щ", "ш"),
                ("т", "х"),
                ("й", "и"),
                ("к", "н"),
                ("д", "ц"),
                ("н", "м"),
                ("ш", "н"),
                ("r", "г"),
            ]
        }
    }
}


def _span_id(*parts: object) -> str:
    return "sp_" + hashlib.sha256("\x1f".join(map(str, parts)).encode("utf-8")).hexdigest()[:16]


def _tokens(text: str, skip: list[tuple[int, int]] | None = None) -> list[tuple[int, int, str]]:
    """(start, end, normalized word) for each word outside the skipped ranges."""
    tokens = []
    for match in _TOKEN.finditer(text):
        if any(start <= match.start() < end for start, end in skip or ()):
            continue
        word = unicodedata.normalize("NFC", _BREAK.sub("", match.group()))
        tokens.append((match.start(), match.end(), word))
    return tokens


_TO_CYRILLIC = str.maketrans(_LATIN_LOOKALIKES, _CYRILLIC_LOOKALIKES)
_TO_LATIN = str.maketrans(_CYRILLIC_LOOKALIKES, _LATIN_LOOKALIKES)


def _homoglyph(literal: str, reader: str) -> bool:
    """The literal mixes in look-alike letters of another script and otherwise equals the reader.

    A single letter proves nothing: a lone Latin B may stand for в as easily as В.
    """
    if len(literal) < 2:
        return False
    return (
        bool(_LATIN.search(literal))
        and not _LATIN.search(reader)
        and literal.translate(_TO_CYRILLIC) == reader
    ) or (
        bool(_CYRILLIC.search(literal))
        and not _CYRILLIC.search(reader)
        and literal.translate(_TO_LATIN) == reader
    )


def _fold(word: str) -> str:
    """A spelling to compare similarity on: HAYK and НАУК are the same shapes."""
    return word.translate(_TO_CYRILLIC).lower()


def _only_pairs(literal: str, reader: str, pairs: set[tuple[str, str]]) -> bool:
    changes = [
        (literal[i1:i2], reader[j1:j2])
        for tag, i1, i2, j1, j2 in SequenceMatcher(
            None, literal, reader, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    ]
    return bool(changes) and all(change in pairs for change in changes)


def _kind(
    literal: str, reader: str, known: Callable[[str], bool] | None, pairs: set[tuple[str, str]]
) -> str:
    if _DIGIT.search(literal) or _DIGIT.search(reader):
        return "number"
    if _homoglyph(literal, reader):
        return "homoglyph"
    if known is None or not (_CYRILLIC.search(literal) and _CYRILLIC.search(reader)):
        return "word"
    if known(reader) and not known(literal):
        # A non-word the engine's habits do not explain may be what the page prints.
        return "glyph" if _only_pairs(literal, reader, pairs) else "misprint_guard"
    return "word"


def contest(
    base: str,
    witness: str,
    *,
    rules: str,
    engine: str,
    known: Callable[[str], bool] | None,
) -> list[dict[str, Any]]:
    """Spans where the witness differs from the base, in base offsets, rule-settled where safe."""
    listed = RULE_SETS[rules]["glyph_pairs"].get(engine, ())
    # A pair holds in either case, never across cases: print's capitals are evidence.
    pairs = {*listed, *((a.upper(), b.upper()) for a, b in listed)}
    regions = [(m.start(), m.end()) for m in _REGION.finditer(base)]
    # One placeholder per region, so a formula aligns as a unit and not as letters.
    stream: list[tuple[int, int, str, int | None]] = sorted(
        [(s, e, w, None) for s, e, w in _tokens(base, regions)]
        + [(s, e, f"\x00{n}", n) for n, (s, e) in enumerate(regions)],
        key=lambda token: token[0],
    )
    other = _tokens(witness)
    matcher = SequenceMatcher(None, [t[2] for t in stream], [t[2] for t in other], autojunk=False)
    base_only: list[tuple[int, int]] = []  # (stream index, block)
    witness_only: list[tuple[int, int]] = []  # (witness index, block)
    block_region: dict[int, int] = {}
    block_anchor: dict[int, int] = {}
    for block, (tag, i1, i2, j1, j2) in enumerate(matcher.get_opcodes()):
        if tag == "equal":
            continue
        block_anchor[block] = stream[i1][0] if i1 < len(stream) else len(base)
        for index in range(i1, i2):
            region = stream[index][3]
            if region is None:
                base_only.append((index, block))
            else:
                block_region.setdefault(block, region)
        witness_only.extend((index, block) for index in range(j1, j2))

    # A word the other engine placed elsewhere on the page is reading order, not a difference.
    remaining = list(witness_only)
    unmatched = []
    for index, block in base_only:
        word = stream[index][2]
        same = [item for item in remaining if other[item[0]][2] == word]
        if same:
            remaining.remove(min(same, key=lambda item: abs(item[1] - block)))
        else:
            unmatched.append((index, block))

    # A word the base hyphenates across a line break, read by the other engine as its
    # two halves, is one word: column order often carries the second half elsewhere.
    # Any other split or merge changes the text and stays a difference.
    for index, block in list(unmatched):
        start, end = stream[index][:2]
        parts = [unicodedata.normalize("NFC", p) for p in _BREAK.split(base[start:end], 1)]
        if len(parts) != 2:
            continue
        first, second = (
            min(
                (item for item in remaining if other[item[0]][2] == part),
                key=lambda item: abs(item[1] - block),
                default=None,
            )
            for part in parts
        )
        if first and second and first != second:
            unmatched.remove((index, block))
            remaining.remove(first)
            remaining.remove(second)

    spans: list[dict[str, Any]] = []
    alone: list[tuple[int, int]] = []
    for index, block in unmatched:
        start, end, word = stream[index][:3]
        folded = _fold(word)
        best, best_key = None, None
        for item in remaining:
            matcher = SequenceMatcher(None, _fold(other[item[0]][2]), folded, autojunk=False)
            if matcher.real_quick_ratio() < SIMILAR or matcher.quick_ratio() < SIMILAR:
                continue
            key = (matcher.ratio(), -abs(item[1] - block))
            if key[0] >= SIMILAR and (best_key is None or key > best_key):
                best, best_key = item, key
        if best is None:
            alone.append((index, block))
            continue
        remaining.remove(best)
        literal = other[best[0]][2]
        spans.append(
            {
                "kind": _kind(literal, word, known, pairs),
                "start": start,
                "end": end,
                "base": base[start:end],
                "witness": literal,
            }
        )
    # Words with no counterpart become one span per run within a block, so a line
    # one engine dropped is one place to check, not one per word.
    leftover: dict[int, list[str]] = {}
    for index, block in remaining:
        leftover.setdefault(block, []).append(other[index][2])
    absorbed = {block_region[b]: leftover.pop(b) for b in list(leftover) if b in block_region}
    runs: list[list[tuple[int, int]]] = []
    for index, block in alone:
        if runs and runs[-1][-1] == (index - 1, block):
            runs[-1].append((index, block))
        else:
            runs.append([(index, block)])
    for run in runs:
        start, end = stream[run[0][0]][0], stream[run[-1][0]][1]
        witness = " ".join(leftover.pop(run[0][1], []))
        spans.append(
            {
                "kind": "word",
                "start": start,
                "end": end,
                "base": base[start:end],
                "witness": witness,
            }
        )
    for block, words in leftover.items():
        anchor = block_anchor[block]
        spans.append(
            {"kind": "word", "start": anchor, "end": anchor, "base": "", "witness": " ".join(words)}
        )
    for number, (start, end) in enumerate(regions):
        spans.append(
            {
                "kind": "region",
                "start": start,
                "end": end,
                "base": base[start:end],
                "witness": " ".join(absorbed.get(number, ())),
            }
        )

    spans.sort(key=lambda span: (span["start"], span["end"], span["witness"]))
    seen: dict[tuple, int] = {}
    for span in spans:
        identity = (span["start"], span["end"], span["base"], span["witness"])
        seen[identity] = seen.get(identity, 0) + 1
        span["span_id"] = _span_id(*identity, seen[identity])
        rule = {"homoglyph": "H", "glyph": "G"}.get(span["kind"])
        if rule:
            span.update(status="settled", settled_by=f"rule:{rules}/{rule}", text=span["base"])
        else:
            span["status"] = "open"
    return spans
