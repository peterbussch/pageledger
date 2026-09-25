"""Score engine transcriptions against reference transcriptions.

    python scripts/corpus/score.py REFERENCES ENGINE [--pages pages.json] [--json]

REFERENCES holds one `<page_id>.txt` per page. ENGINE holds engine output in
the same layout, or is a corpus run directory, whose selected text, time and
cost are then used. Every number is agreement with a reference that is itself
a reading, not accuracy.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pageledger.quality import _repetition_evidence

MODERN = str.maketrans("ѣѢіІѳѲѵѴ", "еЕиИфФиИ")
HISTORICAL = {"ѣ": "ѣѢ", "і": "іІ", "ѳ": "ѳѲ", "ѵ": "ѵѴ"}
FINAL_HARD = re.compile(r"ъ(?=[\s.,;:!?)\]»\"'—-]|$)")
DOT_LEADER = re.compile(r"(?:[.·…]\s*){3,}")
HANDWRITING = re.compile(r"\{hw:[^}]*\}")
NUMBER = re.compile(r"\d(?:[\d.,\u00a0 ]*\d)?")
SEPARATOR = re.compile(r"[.,\u00a0 ]")
# Some vision models write tables as HTML, often a whole table on one line.
ROW_END = re.compile(r"</tr\s*>|<br\s*/?>", re.IGNORECASE)
TAG = re.compile(r"</?[A-Za-z][^>]*>")
# Engine lines longer than this, such as whole blocks on one line, are matched
# in overlapping pieces, so every reference line can be found at bounded cost.
PIECE, OVERLAP = 400, 200


def clean(text: str) -> str:
    """NFC text without reader markup, table rules or dot leaders, on single spaces."""
    text = unicodedata.normalize("NFC", text)
    text = HANDWRITING.sub(" ", text).replace("[?]", "").replace("[illegible]", " ")
    return " ".join(DOT_LEADER.sub(" ", text.replace("|", " ")).split())


def plain(engine_text: str) -> str:
    """Engine text with HTML table rows as lines and every other tag as a space."""
    return html.unescape(TAG.sub(" ", ROW_END.sub("\n", engine_text)))


def normalize(text: str) -> str:
    """Cleaned text, case-folded, in modern letters, without word-final ъ."""
    return FINAL_HARD.sub("", clean(text).casefold().translate(MODERN))


def graphemes(text: str) -> list[str]:
    """Base characters with their combining marks (an approximation of UAX #29)."""
    clusters: list[str] = []
    for char in text:
        if clusters and (unicodedata.category(char).startswith("M") or char in "\u200c\u200d"):
            clusters[-1] += char
        else:
            clusters.append(char)
    return clusters


def edit_distance(left: Sequence[str], right: Sequence[str]) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def semiglobal(reference: str, window: str) -> tuple[int, str]:
    """The fewest grapheme edits turning the reference into any stretch of the window."""
    ref, target = graphemes(reference), graphemes(window)
    previous, starts = [0] * (len(target) + 1), list(range(len(target) + 1))
    for i, char in enumerate(ref, 1):
        current, current_starts = [i], [0]
        for j, other in enumerate(target, 1):
            substitute = previous[j - 1] + (char != other)
            delete, insert = previous[j] + 1, current[j - 1] + 1
            best = min(substitute, delete, insert)
            current.append(best)
            if best == substitute:
                current_starts.append(starts[j - 1])
            elif best == delete:
                current_starts.append(starts[j])
            else:
                current_starts.append(current_starts[j - 1])
        previous, starts = current, current_starts
    end = min(range(len(previous)), key=lambda j: (previous[j], -j))
    return previous[end], "".join(target[starts[end] : end])


def trigrams(text: str) -> set[str]:
    return {text[i : i + 3] for i in range(len(text) - 2)}


def pieces(lines: list[str]) -> list[str]:
    result = []
    for line in lines:
        if len(line) <= PIECE:
            result.append(line)
        else:
            steps = range(0, len(line) - OVERLAP, PIECE - OVERLAP)
            result += [line[start : start + PIECE] for start in steps]
    return result


def match(reference: str, lines: list[str], line_trigrams: list[set[str]]) -> tuple[int, str]:
    """The closest stretch of engine text to a reference line: (edits, matched text).

    The three lines sharing most trigrams with the reference are searched, each
    with its neighbours, by semi-global edit distance.
    """
    wanted = trigrams(reference)
    ranked = sorted(range(len(lines)), key=lambda i: -len(wanted & line_trigrams[i]))[:3]
    best = (len(graphemes(reference)), "")
    for i in ranked:
        found = semiglobal(reference, " ".join(lines[max(0, i - 1) : i + 2]))
        if found[0] < best[0]:
            best = found
    return best


def reference_numbers(raw: str) -> list[str]:
    """The digits of each number in each table cell of a reference line."""
    numbers = []
    for cell in raw.split("|"):
        cell = DOT_LEADER.sub(" ", HANDWRITING.sub(" ", cell).replace("[?]", ""))
        numbers += [re.sub(r"\D", "", found) for found in NUMBER.findall(cell)]
    return [number for number in numbers if number]


def digit_groups(text: str) -> set[str]:
    """Digit runs, alone and joined with up to two neighbours across one separator."""
    runs, current = [], []
    for index, part in enumerate(re.split(r"(\d+)", text)):
        if index % 2:
            current.append(part)
        elif not (current and SEPARATOR.fullmatch(part)):
            runs.append(current)
            current = []
    runs.append(current)
    return {
        "".join(run[start:end])
        for run in runs
        for start in range(len(run))
        for end in range(start + 1, min(len(run), start + 3) + 1)
    }


def historical_counts(text: str) -> dict[str, int]:
    counts = {
        letter: sum(text.count(form) for form in forms) for letter, forms in HISTORICAL.items()
    }
    counts["final ъ"] = len(FINAL_HARD.findall(text))
    return counts


def score_page(reference: str, engine: str) -> list[dict[str, Any]]:
    engine_lines = plain(engine).splitlines()
    diplomatic = pieces([line for line in map(clean, engine_lines) if line])
    normalized = pieces([line for line in map(normalize, engine_lines) if line])
    diplomatic_trigrams = [trigrams(line) for line in diplomatic]
    normalized_trigrams = [trigrams(line) for line in normalized]
    rows = []
    for raw in reference.splitlines():
        ref = clean(raw)
        if len(ref.replace(" ", "")) < 3:
            continue
        ref_normalized = normalize(raw)
        edits, matched = match(ref, diplomatic, diplomatic_trigrams)
        normalized_edits, normalized_matched = match(
            ref_normalized, normalized, normalized_trigrams
        )
        numbers, found = reference_numbers(raw), digit_groups(matched)
        letters, kept = historical_counts(ref), historical_counts(matched)
        rows.append(
            {
                "reference": raw,
                "matched": matched,
                "characters": len(ref),
                "character_edits": edit_distance(ref, matched),
                "graphemes": len(graphemes(ref)),
                "grapheme_edits": edits,
                "normalized_characters": len(ref_normalized),
                "normalized_character_edits": edit_distance(ref_normalized, normalized_matched),
                "normalized_graphemes": len(graphemes(ref_normalized)),
                "normalized_grapheme_edits": normalized_edits,
                "numbers": len(numbers),
                "numbers_found": sum(number in found for number in numbers),
                "historical": sum(letters.values()),
                "historical_kept": sum(min(n, kept[letter]) for letter, n in letters.items()),
                "historical_added": sum(max(0, kept[letter] - n) for letter, n in letters.items()),
            }
        )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def share(part: str, whole: str) -> float | None:
        total = sum(row[whole] for row in rows)
        return round(sum(row[part] for row in rows) / total, 4) if total else None

    def line_share(test) -> float | None:
        return round(sum(map(test, rows)) / len(rows), 4) if rows else None

    return {
        "lines": len(rows),
        "cer": share("character_edits", "characters"),
        "grapheme_cer": share("grapheme_edits", "graphemes"),
        "normalized_cer": share("normalized_character_edits", "normalized_characters"),
        "normalized_grapheme_cer": share("normalized_grapheme_edits", "normalized_graphemes"),
        "exact_lines": line_share(lambda row: row["grapheme_edits"] == 0),
        "missing_lines": line_share(lambda row: row["grapheme_edits"] > row["graphemes"] / 2),
        "numbers_found": share("numbers_found", "numbers"),
        "historical_kept": share("historical_kept", "historical"),
        "historical_added": sum(row["historical_added"] for row in rows),
    }


def read_engine(path: Path) -> dict[str, dict[str, Any]]:
    """Each page's text, and for a corpus run its seconds and reported cost."""
    if not (path / "results.jsonl").is_file():
        return {
            page.stem: {"text": page.read_text(encoding="utf-8"), "seconds": None, "cost_usd": None}
            for page in sorted(path.glob("*.txt"))
        }
    pages = {}
    for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        selected = row["selected_output"]
        costs = [
            a["usage"]["cost_usd"]
            for a in row["attempts"]
            if a["usage"].get("cost_usd") is not None
        ]
        pages[f"{row['item_id']}_p{row['page_number']:04d}"] = {
            "text": (path / selected).read_text(encoding="utf-8") if selected else None,
            "seconds": sum(attempt["seconds"] or 0 for attempt in row["attempts"]),
            "cost_usd": sum(costs) if costs else None,
        }
    return pages


def score(
    references: dict[str, str], engine: dict[str, dict[str, Any]], strata: dict[str, str]
) -> dict[str, Any]:
    pages, rows_by_stratum = {}, {}
    for page_id, reference in sorted(references.items()):
        if page_id not in engine:
            continue
        text = engine[page_id]["text"]
        rows = score_page(reference, text or "")
        rows_by_stratum.setdefault(strata.get(page_id, "unassigned"), []).extend(rows)
        pages[page_id] = {
            **summarize(rows),
            "no_text": text is None,
            "loop": bool(text and _repetition_evidence(text)[1]),
            "seconds": engine[page_id]["seconds"],
            "cost_usd": engine[page_id]["cost_usd"],
            "rows": rows,
        }
    all_rows = [row for page in pages.values() for row in page["rows"]]
    costs = [page["cost_usd"] for page in pages.values() if page["cost_usd"] is not None]
    seconds = [page["seconds"] for page in pages.values() if page["seconds"] is not None]
    worst = sorted(pages, key=lambda page_id: -(pages[page_id]["cer"] or 0))[:5]
    return {
        "basis": "against reference",
        "overall": {
            **summarize(all_rows),
            "pages": len(pages),
            "reference_pages_not_read": len(references.keys() - engine.keys()),
            "pages_without_text": sum(page["no_text"] for page in pages.values()),
            "loops": sum(page["loop"] for page in pages.values()),
            "seconds": round(sum(seconds), 1) if seconds else None,
            "cost_usd": round(sum(costs), 4) if costs else None,
        },
        "strata": {name: summarize(rows) for name, rows in sorted(rows_by_stratum.items())},
        "worst_pages": [{"page_id": page_id, "cer": pages[page_id]["cer"]} for page_id in worst],
        "pages": pages,
    }


def markdown(report: dict[str, Any]) -> str:
    columns = ("cer", "normalized_cer", "exact_lines", "missing_lines", "numbers_found")
    header = "| {} | CER | Normalized CER | Exact lines | Missing lines | Numbers found |"
    lines = ["All scores are against the reference, not accuracy.", ""]
    for title, groups in (
        ("Overall", {"all pages": report["overall"]}),
        ("By stratum", report["strata"]),
    ):
        lines += [f"## {title}", "", header.format(""), "|---|---:|---:|---:|---:|---:|"]
        lines += [
            f"| {name} | " + " | ".join(str(group[column]) for column in columns) + " |"
            for name, group in groups.items()
        ]
        lines.append("")
    overall = report["overall"]
    lines.append(
        f"{overall['pages']} pages scored, {overall['pages_without_text']} without text, "
        f"{overall['loops']} loops, {overall['reference_pages_not_read']} reference pages not "
        f"read. Worst pages: "
        + ", ".join(f"{page['page_id']} ({page['cer']})" for page in report["worst_pages"])
        + "."
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("references", type=Path)
    parser.add_argument("engine", type=Path, help="Directory of <page_id>.txt, or a corpus run")
    parser.add_argument("--pages", type=Path, help="JSON list of pages with their stratum")
    parser.add_argument("--split", choices=("dev", "holdout"))
    parser.add_argument("--splits", type=Path, help="JSON object: document id -> dev or holdout")
    parser.add_argument("--json", action="store_true", help="Write JSON, with every line")
    args = parser.parse_args(argv)
    if args.split and not args.splits:
        parser.error("--split requires --splits FILE")
    references = {
        page.stem: page.read_text(encoding="utf-8") for page in args.references.glob("*.txt")
    }
    if args.split:
        splits = json.loads(args.splits.read_text(encoding="utf-8"))
        references = {
            page_id: text
            for page_id, text in references.items()
            if splits.get(page_id.rsplit("_p", 1)[0]) == args.split
        }
    strata = (
        {page["page_id"]: page["stratum"] for page in json.loads(args.pages.read_text())}
        if args.pages
        else {}
    )
    report = score(references, read_engine(args.engine), strata)
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
