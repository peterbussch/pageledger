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
# Words the other engine read elsewhere count as moved only in runs this long, or
# alone on their line: a single moved word may be a model's moved negation.
MOVED_RUN = 2

_LATIN_LOOKALIKES = "ABCEHKMOPTXYaceopxyijs"
_CYRILLIC_LOOKALIKES = "АВСЕНКМОРТХУасеорхуіјѕ"
# A rule set's name versions the whole contest procedure (tokens, alignment, span
# kinds and rules), so any change that alters the spans takes a new name: receipts
# bind to the spans they answer. Glyph pairs are (literal, reader) and belong to one
# engine: RapidOCR's confusions are not Tesseract's. These are the pairs RapidOCR's
# Cyrillic model made at least twice in the October 2026 dogfood (Большаков,
# Revzin), checked against the page image; not ё for е, which print uses in
# linguistic examples.
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
        if any(match.start() < end and start < match.end() for start, end in skip or ()):
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


def _alone(text: str, start: int, end: int) -> bool:
    """Nothing else on the line: a page number or a short heading."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    return not re.search(r"\w", text[line_start:start] + text[end:line_end])


def _moved_runs(
    left: list[int], right: list[int], a: list[str], b: list[str]
) -> list[tuple[int, int]]:
    """Take from both lists the runs of at least MOVED_RUN words read in another place.

    A run never crosses a gap in either reading, so words from separate places
    cannot add up to a run. Returns the matched (base, witness) word pairs.
    """
    matched: list[tuple[int, int]] = []
    while True:
        keys_a, at_a = _keys(left, a, "a")
        keys_b, at_b = _keys(right, b, "b")
        match = SequenceMatcher(None, keys_a, keys_b, autojunk=False).find_longest_match(
            0, len(keys_a), 0, len(keys_b)
        )
        if match.size < MOVED_RUN:
            return matched
        taken_a = set(at_a[match.a : match.a + match.size])
        taken_b = set(at_b[match.b : match.b + match.size])
        matched.extend(
            zip(
                at_a[match.a : match.a + match.size],
                at_b[match.b : match.b + match.size],
                strict=True,
            )
        )
        left[:] = [i for i in left if i not in taken_a]
        right[:] = [j for j in right if j not in taken_b]


def _keys(indices: list[int], words: list[str], side: str) -> tuple[list[str], list[int]]:
    """Words by index, with a marker that matches nothing wherever the indices jump."""
    keys: list[str] = []
    at: list[int] = []
    for position, index in enumerate(indices):
        if position and index != indices[position - 1] + 1:
            keys.append(f"\x01{side}{position}")
            at.append(-1)
        keys.append(words[index])
        at.append(index)
    return keys, at


def _runs(indices: list[int]) -> list[list[int]]:
    """Consecutive indices grouped together."""
    runs: list[list[int]] = []
    for index in indices:
        if runs and runs[-1][-1] == index - 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def _in_order(
    bases: list[int], wits: list[int], score: Callable[[int, int], float]
) -> list[tuple[int, int]]:
    """The similar pairs that keep both readings in order, with the most similarity."""
    rows, cols = len(bases), len(wits)
    best = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    for r in range(rows - 1, -1, -1):
        for c in range(cols - 1, -1, -1):
            value = score(bases[r], wits[c])
            best[r][c] = max(
                best[r + 1][c],
                best[r][c + 1],
                value + best[r + 1][c + 1] if value else 0.0,
            )
    pairs, r, c = [], 0, 0
    while r < rows and c < cols:
        value = score(bases[r], wits[c])
        if value and best[r][c] == value + best[r + 1][c + 1]:
            pairs.append((bases[r], wits[c]))
            r, c = r + 1, c + 1
        elif best[r][c] == best[r + 1][c]:
            r += 1
        else:
            c += 1
    return pairs


def contest(
    base: str,
    witness: str,
    *,
    rules: str,
    engine: str,
    known: Callable[[str], bool] | None,
) -> list[dict[str, Any]]:
    """Spans where the witness differs from the base, in base offsets, rule-settled where safe.

    Each span's ``base`` and ``witness`` are the exact characters each engine read
    there, so either one, given as the answer, rebuilds that reading.
    """
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
    words = [t[2] for t in stream]
    read = [t[2] for t in other]
    # Where in the base each witness word sits, for anchoring words only it has.
    after: dict[int, int] = {}
    block_of_base: dict[int, int] = {}
    block_of_witness: dict[int, int] = {}
    block_region: dict[int, int] = {}
    left: list[int] = []
    right: list[int] = []
    matcher = SequenceMatcher(None, words, read, autojunk=False)
    for block, (tag, i1, i2, j1, j2) in enumerate(matcher.get_opcodes()):
        if tag == "equal":
            after.update((j1 + k, stream[i1 + k][1]) for k in range(i2 - i1))
            continue
        for index in range(i1, i2):
            region = stream[index][3]
            if region is None:
                left.append(index)
                block_of_base[index] = block
            else:
                block_region.setdefault(block, region)
        for index in range(j1, j2):
            right.append(index)
            block_of_witness[index] = block

    def distance(i: int, j: int) -> int:
        return abs(block_of_base[i] - block_of_witness[j])

    def anchor(j: int) -> int:
        """The base offset after the base word matched to the witness word before j."""
        earlier = [k for k in after if k < j]
        return after[max(earlier)] if earlier else 0

    def moved_here(i: int, j: int) -> bool:
        """The witness read j right after the base word before i."""
        return anchor(j) == (stream[i - 1][1] if i else 0)

    for i, j in _moved_runs(left, right, words, read):
        after[j] = stream[i][1]
    for i in list(left):
        same = [
            j
            for j in right
            if read[j] == words[i]
            and _alone(base, *stream[i][:2])
            and _alone(witness, *other[j][:2])
        ]
        if same:
            j = min(same, key=lambda j: distance(i, j))
            left.remove(i)
            right.remove(j)
            after[j] = stream[i][1]

    # A word one reading hyphenates across a line break and the other reads as its
    # two halves is one word, when the halves sit together or the first one still
    # ends in a hyphen. Any other split or merge changes the text.
    def hyphen_after(text: str, end: int) -> bool:
        return text[end : end + 1] in ("-", "\u2010", "\u00ad")

    for i in list(left):
        parts = [
            unicodedata.normalize("NFC", x) for x in _BREAK.split(base[slice(*stream[i][:2])], 1)
        ]
        if len(parts) != 2:
            continue
        for j in right:
            if read[j] == parts[0] and j + 1 in right and read[j + 1] == parts[1]:
                left.remove(i)
                right.remove(j)
                right.remove(j + 1)
                after[j] = after[j + 1] = stream[i][1]
                break
            if read[j] == parts[0] and hyphen_after(witness, other[j][1]):
                second = next((k for k in right if k != j and read[k] == parts[1]), None)
                if second is not None:
                    left.remove(i)
                    right.remove(j)
                    right.remove(second)
                    after[j] = after[second] = stream[i][1]
                    break
    for j in list(right):
        parts = [
            unicodedata.normalize("NFC", x) for x in _BREAK.split(witness[slice(*other[j][:2])], 1)
        ]
        if len(parts) == 2:
            first = next(
                (
                    i
                    for i in left
                    if words[i] == parts[0] and i + 1 in left and words[i + 1] == parts[1]
                ),
                None,
            )
            if first is not None:
                right.remove(j)
                left.remove(first)
                left.remove(first + 1)
                after[j] = stream[first + 1][1]

    def similarity(i: int, j: int) -> float:
        # The same word in another place is a single moved word: a deletion here
        # and an insertion there, never a span that changes nothing.
        if read[j] == words[i]:
            return 0.0
        similar = SequenceMatcher(None, _fold(read[j]), _fold(words[i]), autojunk=False)
        if similar.real_quick_ratio() < SIMILAR or similar.quick_ratio() < SIMILAR:
            return 0.0
        ratio = similar.ratio()
        return ratio if ratio >= SIMILAR else 0.0

    paired: list[tuple[int, int]] = []

    def pair(i: int, j: int) -> None:
        paired.append((i, j))
        left.remove(i)
        right.remove(j)
        after[j] = stream[i][1]

    # A word the witness read in another block but at this very place: column order
    # moved its neighbours, and the engine misread it.
    for i in list(left):
        best = max(
            (j for j in right if block_of_witness[j] != block_of_base[i] and moved_here(i, j)),
            key=lambda j: similarity(i, j),
            default=None,
        )
        if best is not None and similarity(i, best):
            pair(i, best)
    # Within a block, words pair in order: a replaced phrase never crosses itself.
    for block in sorted({block_of_base[i] for i in left} & {block_of_witness[j] for j in right}):
        bases = [i for i in left if block_of_base[i] == block]
        wits = [j for j in right if block_of_witness[j] == block]
        for i, j in _in_order(bases, wits, similarity):
            pair(i, j)

    spans: list[dict[str, Any]] = [
        {
            "kind": _kind(read[j], words[i], known, pairs),
            "start": stream[i][0],
            "end": stream[i][1],
            "base": base[slice(*stream[i][:2])],
            "witness": witness[slice(*other[j][:2])],
        }
        for i, j in paired
    ]

    def raw(indices: list[int]) -> str:
        """The witness's own characters for consecutive words; a space between separate ones."""
        return " ".join(witness[other[g[0]][0] : other[g[-1]][1]] for g in _runs(indices))

    # What is left in a block between two pairs is one place to check: the base's
    # words there, and the witness's. Words only the witness has go where it read
    # them, after the base word matched to the witness word before them.
    absorbed: dict[int, list[int]] = {}
    for block in sorted({block_of_base[i] for i in left} | {block_of_witness[j] for j in right}):
        in_block = [(i, j) for i, j in paired if block_of_base[i] == block]
        bases = [i for i in left if block_of_base[i] == block]
        wits = [j for j in right if block_of_witness[j] == block]
        if block in block_region:
            absorbed[block_region[block]] = wits
            wits = []
        for segment in range(len(in_block) + 1):
            here = [i for i in bases if sum(p < i for p, _ in in_block) == segment]
            there = [j for j in wits if sum(q < j for _, q in in_block) == segment]
            runs = _runs(here)
            for number, run in enumerate(runs):
                start, end = stream[run[0]][0], stream[run[-1]][1]
                spans.append(
                    {
                        "kind": "word",
                        "start": start,
                        "end": end,
                        "base": base[start:end],
                        "witness": raw(there) if number == 0 else "",
                    }
                )
            if not runs:
                for group in _runs(there):
                    at = anchor(group[0])
                    spans.append(
                        {"kind": "word", "start": at, "end": at, "base": "", "witness": raw(group)}
                    )
    for number, (start, end) in enumerate(regions):
        spans.append(
            {
                "kind": "region",
                "start": start,
                "end": end,
                "base": base[start:end],
                "witness": raw(absorbed.get(number, [])),
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
