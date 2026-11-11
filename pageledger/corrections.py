"""A person's corrected text for a page, kept as a new reading beside the machine ones.

The corrected text is stored once, under a name made from its own hash, and a
review receipt binds it to the reading it was made from. Nothing is edited in
place: the machine readings stay as they were recorded.
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from pathlib import Path

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_WORDS = ("changed", "added", "removed")
# Share of a reading's words a correction may change before review --apply warns
# that it may be another page's text.
LARGE_CHANGE = 0.5


def normalize(text: str) -> str:
    """Line endings only: CRLF and lone CR become LF. Nothing else is touched."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def artifact_path(page_id: str, sha256: str) -> str:
    return f"corrections/{page_id}-{sha256}.txt"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def word_changes(before: str, after: str) -> dict[str, int]:
    """Words changed, added and removed, comparing exact whitespace-separated words."""
    old, new = before.split(), after.split()
    counts = dict.fromkeys(_WORDS, 0)
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        paired = min(i2 - i1, j2 - j1) if tag == "replace" else 0
        counts["changed"] += paired
        if tag in {"replace", "delete"}:
            counts["removed"] += i2 - i1 - paired
        if tag in {"replace", "insert"}:
            counts["added"] += j2 - j1 - paired
    return counts


def describe(words: dict[str, int]) -> str:
    changed = words["changed"]
    return (
        f"{changed} word{'' if changed == 1 else 's'} changed, "
        f"{words['added']} added, {words['removed']} removed"
    )


def large(words: dict[str, int], before: str) -> bool:
    """True when a correction changes more than half the words of the reading it started from."""
    return sum(words.values()) > LARGE_CHANGE * max(len(before.split()), 1)


def prepare(start: str, typed: str, page_number: int) -> tuple[str, dict[str, int]]:
    """The text to record and its word changes; refuses what is not a correction."""
    text = normalize(typed)
    if not text.strip():
        raise ValueError(
            f"The correction for page {page_number} is empty. If the page has no text, "
            "decide blank instead."
        )
    if text == normalize(start):
        raise ValueError(
            f"The correction for page {page_number} is identical to the reading it started "
            "from. If that reading is right, decide accept instead."
        )
    return text, word_changes(start, text)


def read_file(folder: Path, name: str, page_number: int) -> str:
    """A corrected text from a file beside the decisions file.

    The file must be UTF-8 and inside ``folder``. A byte-order mark at its start
    is how some editors mark a UTF-8 file, not part of the text, and is dropped.
    """
    folder = folder.resolve()
    relative = Path(name)
    path = (folder / relative).resolve()
    if not name or relative.is_absolute() or not path.is_relative_to(folder):
        raise ValueError(
            f"The correction file for page {page_number}, {name!r}, must be inside "
            f"{folder}, the folder of the decisions file"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Cannot read the correction file for page {page_number}: {exc}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"The correction file for page {page_number}, {name}, is not UTF-8; "
            "save it as UTF-8 and apply again"
        ) from exc
    return text.removeprefix("﻿")


def validate(correction: object, page_id: str) -> None:
    """The shape of a receipt's correction; its file and chain are checked with the job."""
    if (
        not isinstance(correction, dict)
        or set(correction) != {"artifact", "sha256", "from_sha256", "words"}
        or not isinstance(correction["sha256"], str)
        or not _HASH.fullmatch(correction["sha256"])
        or not isinstance(correction["from_sha256"], str)
        or not _HASH.fullmatch(correction["from_sha256"])
        or correction["sha256"] == correction["from_sha256"]
        or correction["artifact"] != artifact_path(page_id, correction["sha256"])
        or not isinstance(correction["words"], dict)
        or set(correction["words"]) != set(_WORDS)
        or any(type(n) is not int or n < 0 for n in correction["words"].values())
    ):
        raise ValueError("Review correction is invalid")


def chain(history: list[dict], page_id: str) -> list[tuple[dict, dict]]:
    """Each receipt decision with a correction, in order, refusing one with no recorded start.

    A correction starts from the reading its receipt names or from a correction
    recorded earlier for the same page.
    """
    known: set[str] = set()
    found = []
    for receipt in history:
        decision = next(d for d in receipt["decisions"] if d["page_id"] == page_id)
        correction = decision.get("correction")
        if correction is None:
            continue
        if correction["from_sha256"] not in known | {decision["output_sha256"]}:
            raise ValueError(
                f"The correction of {page_id} starts from a text this job does not record"
            )
        known.add(correction["sha256"])
        found.append((decision, correction))
    return found
