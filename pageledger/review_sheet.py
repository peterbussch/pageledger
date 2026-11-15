"""Spreadsheet review: one CSV row per page, bound to the evidence it was written from."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shlex
from pathlib import Path

from . import corrections
from .checkpoint import atomic_bytes
from .document_report import (
    _artifact_bytes,
    _engines,
    edition_published,
    empty_reading,
    reader_name,
    relative_path,
)
from .image_source import page_target
from .runner import _utc_now
from .wording import CORRECTION, DECISION_GROUPS, HOLDS, REVIEWED, sentence

# Beside every sheet: the decisions it takes, and the readings each page can use.
GUIDE = "review-decisions.txt"
_GUIDE_TITLE = "How to fill in {}"

FIELDS = (
    "row_id",
    "page_number",
    "source_page",
    "disposition",
    "attempt_summary",
    "review_reasons",
    "decision",
    "note",
)
# What a reviewer may type in `decision`, and the disposition each records.
# `use:ATTEMPT` accepts another completed attempt's text instead; `correct:FILE`
# records the corrected text in FILE as reviewed_text.
DECISIONS = {
    "accept": "reviewed_text",
    "blank": "reviewed_blank",
    "illustration": "illustration",
    "handwriting": "handwriting",
    "unreadable": "unreadable",
    "source_defect": "source_defect",
}
# A row of a decisions file, the JSON form of the sheet. `text` is a corrected
# text and `from` the attempt it starts from, when not the selected text.
DECISION_FIELDS = frozenset({"row_id", "page_number", "decision", "note", "text", "from"})
# A cell starting with one of these can be read as a spreadsheet formula. A dash
# is allowed in the reviewer's own note: notes such as "-1 word illegible" are
# ordinary. A cell PageLedger writes from a file name never starts with any of
# _CELL_PREFIXES.
_FORMULA_PREFIXES = ("=", "+", "@", "\t", "\r")
_CELL_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
# Cell separators of a CSV saved in another locale or format.
_SEPARATORS = ((";", "semicolons"), ("\t", "tabs"))


def binding_path(sheet: Path) -> Path:
    return sheet.with_name(sheet.name + ".binding.json")


def write_review_sheet(job: dict, sheet: Path, root: Path) -> dict:
    """Write the CSV and the sidecar that binds each row to its page's evidence.

    ``root`` is where the job is now, which may not be where it was written.
    """
    sheet = sheet.expanduser().resolve()
    root = root.resolve()
    if sheet.is_relative_to(root):
        raise ValueError(f"Write the review sheet outside the job's folder, not to {sheet}")
    for path in (sheet, binding_path(sheet)):
        if path.exists() or path.is_symlink():
            raise ValueError(
                f"{path} already exists; a filled-in sheet would be lost. Name another "
                "sheet, or remove it first"
            )
    guide = _guide_path(sheet)
    sheet.parent.mkdir(parents=True, exist_ok=True)
    source = relative_path(job["source"]["path"], sheet.parent)
    if source.startswith(_CELL_PREFIXES):
        # The same path, so that no spreadsheet reads a file name as a formula.
        source = "./" + source
    rows, bindings = [], {}
    for page in job["pages"]:
        row_id = _row_id(page)
        rows.append(
            {
                "row_id": row_id,
                "page_number": str(page["page_number"]),
                "source_page": source + "".join(page_target(job["source"], page["page_number"])),
                "disposition": _state(page),
                "attempt_summary": _summary(root, job, page, _selected(page)),
                "review_reasons": "; ".join(
                    f"{HOLDS.get(code, code)} ({code})" for code in page["review_reasons"]
                ),
                "decision": "",
                "note": "",
            }
        )
        bindings[row_id] = _binding(job, page)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    # The byte-order mark makes spreadsheet programs read the file as UTF-8.
    atomic_bytes(sheet, buffer.getvalue().encode("utf-8-sig"))
    binding = json.dumps({"schema_version": "0.1", "bindings": bindings}, indent=2) + "\n"
    atomic_bytes(binding_path(sheet), binding.encode("utf-8"))
    atomic_bytes(guide, _guide(job, sheet, root).encode("utf-8"))
    return {
        "out": str(sheet),
        "binding": str(binding_path(sheet)),
        "guide": str(guide),
        "rows": len(rows),
    }


def _guide_path(sheet: Path) -> Path:
    """Where the sheet's guide goes: review-decisions.txt, unless that file is another
    sheet's guide or someone's own file, which are left as they are."""
    for name in (GUIDE, f"{sheet.stem}-decisions.txt"):
        path = sheet.parent / name
        if not path.exists() and not path.is_symlink():
            return path
        try:
            with path.open(encoding="utf-8") as stream:
                first = stream.readline().rstrip("\n")
        except (OSError, UnicodeError):
            continue
        if path.is_file() and not path.is_symlink() and first == _GUIDE_TITLE.format(sheet.name):
            return path
    raise ValueError(
        f"{sheet.parent} already holds {GUIDE} and {sheet.stem}-decisions.txt; write the "
        "sheet in another folder"
    )


def _guide(job: dict, sheet: Path, root: Path) -> str:
    """What may go in the sheet's decision column, written beside it for a spreadsheet user."""
    command = (
        f"pageledger review {shlex.quote(str(root))} --apply {shlex.quote(sheet.name)} "
        '--reviewer "YOUR NAME"'
    )
    lines = [
        _GUIDE_TITLE.format(sheet.name),
        "",
        "Type one decision in each row's decision column, and a note if you like: the note",
        "is recorded as the reason. A row left empty leaves its page unreviewed. In a",
        'spreadsheet program, save the sheet as "CSV UTF-8 (comma delimited)": another kind',
        "of CSV can lose letters outside English or use semicolons. Then, in this folder,",
        "apply the sheet:",
        "",
        f"  {command}",
        "",
        "Add --dry-run to check it first without recording anything. Keep",
        f"{binding_path(sheet).name} beside the sheet: it ties each row to the page's text",
        "and its review as they were when the sheet was written, and a row is refused if",
        "the page has changed since.",
        "",
        "The decisions, and what each records:",
    ]
    for title, choices in DECISION_GROUPS:
        lines += ["", title]
        for token, _label, records in choices:
            shown = {"use:": "use:READING", "correct": "correct:FILE"}.get(token, token)
            lines.append(f"  {shown:<15} records {records}.")
            if token == "use:":
                lines.append(f"  {'':<15} READING is the ID of a reading listed below.")
            if token == "correct":
                lines.append(
                    f"  {'':<15} Save your corrected text in FILE, a text file beside the sheet."
                )
    offered = []
    for page in job["pages"]:
        for attempt in page["attempts"]:
            usable = _completed(page, attempt["attempt_id"]) and not _empty(attempt)
            if not usable or attempt["attempt_id"] == page["selected_attempt"]:
                continue
            name = _name(job, attempt)
            offered.append(f"  Page {page['page_number']}: use:{attempt['attempt_id']} ({name})")
    lines += ["", "Other readings each page can use instead of its selected text:"]
    lines += offered or ["  none"]
    return "\n".join(lines) + "\n"


def read_review_sheet(
    job: dict, sheet: Path, reviewer: str | None, root: Path
) -> tuple[dict, list[dict]]:
    """Check every row against the job, then return its decisions as a review receipt
    and the corrected texts its ``correct:FILE`` decisions name.

    A row with a blank decision, or a deleted row, leaves its page unreviewed.
    """
    sheet = sheet.expanduser().resolve()
    try:
        reader = csv.DictReader(io.StringIO(sheet.read_text(encoding="utf-8-sig"), newline=""))
        rows = list(reader)
    except UnicodeError as exc:
        raise ValueError(
            f"The review sheet is not UTF-8 ({exc}). Save it from the spreadsheet as "
            '"CSV UTF-8 (comma delimited)" and apply it again'
        ) from exc
    except (OSError, csv.Error) as exc:
        raise ValueError(f"Cannot read the review sheet: {exc}") from exc
    if reader.fieldnames != list(FIELDS):
        header = ",".join(reader.fieldnames or [])
        saved = next((name for mark, name in _SEPARATORS if mark in header), None)
        raise ValueError(
            f"Review sheet columns must be: {', '.join(FIELDS)}"
            + (
                f". This sheet separates them with {saved}: save it from the spreadsheet "
                'as "CSV UTF-8 (comma delimited)"'
                if saved
                else ""
            )
        )
    # Spreadsheet programs can save rows that are wholly empty below the data.
    # Numbered as the spreadsheet numbers them, the header being row 1.
    numbered = [
        (number, row)
        for number, row in enumerate(rows, start=2)
        if any(value != "" for value in row.values())
    ]
    rows = [row for _, row in numbered]
    pages = {_row_id(page): page for page in job["pages"]}
    expected_binding = binding_path(sheet)
    if expected_binding.is_file():
        binding_file = expected_binding
    else:
        matches, same_rows = [], []
        for candidate in sheet.parent.glob("*.binding.json"):
            try:
                proposed = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if _binding_matches_rows(proposed, rows, job, pages):
                matches.append(candidate)
            elif _binding_matches_rows(proposed, rows, job, pages, evidence=False):
                same_rows.append(candidate)
        if not matches and len(same_rows) == 1:
            # The sheet's own binding, written before the job last changed: the rows
            # below are then refused for what is wrong with them, not for a file name.
            matches = same_rows
        if len(matches) != 1:
            raise ValueError(
                f"Binding file {expected_binding.name} is missing or ambiguous. Keep the "
                f"sheet's binding beside it and name it {sheet.name}.binding.json."
            )
        binding_file = matches[0]
    try:
        binding = json.loads(binding_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read review sheet binding {binding_file}: {exc}") from exc
    if (
        not isinstance(binding, dict)
        or binding.get("schema_version") != "0.1"
        or not isinstance(binding.get("bindings"), dict)
    ):
        raise ValueError(f"Review sheet binding is invalid: {binding_file}")
    for number, row in numbered:
        if None in row or None in row.values():
            raise ValueError(f"Row {number} of the review sheet does not have {len(FIELDS)} cells")
        for column in ("decision", "note"):
            if row[column].startswith(_FORMULA_PREFIXES):
                raise ValueError(f"Review row {row['row_id']} has a formula in {column}")
    return _receipt(job, root, rows, binding["bindings"], reviewer, sheet.parent, sheet=True)


def read_review_decisions(
    job: dict, path: Path, data: dict, reviewer: str | None, root: Path
) -> tuple[dict, list[dict]]:
    """The JSON form of a review sheet, which the review view downloads.

    It carries the sheet's bindings and only the rows with a decision. A row's
    ``text`` can hold a corrected page, which a spreadsheet cell cannot carry safely.
    """
    path = path.expanduser().resolve()
    rows = data.get("rows")
    if (
        set(data) != {"schema_version", "bindings", "rows"}
        or data["schema_version"] != "0.1"
        or not isinstance(data["bindings"], dict)
        or not isinstance(rows, list)
    ):
        raise ValueError(
            "A decisions file holds schema_version 0.1, the sheet's bindings and a list of rows"
        )
    required = {"row_id", "page_number", "decision"}
    for index, row in enumerate(rows):
        if isinstance(row, dict) and type(row.get("page_number")) is int:
            # A file written by hand or by a script may give the page as a number.
            rows[index] = row = {**row, "page_number": str(row["page_number"])}
        if (
            not isinstance(row, dict)
            or not required <= set(row) <= DECISION_FIELDS
            or not all(isinstance(value, str) for value in row.values())
        ):
            optional = ", ".join(sorted(DECISION_FIELDS - required))
            raise ValueError(
                "Each row of a decisions file has row_id, page_number and decision, "
                f"and may have {optional}, all as text (page_number may be a number)"
            )
    return _receipt(job, root, rows, data["bindings"], reviewer, path.parent, sheet=False)


def _receipt(
    job: dict,
    root: Path,
    rows: list[dict],
    bindings: dict,
    reviewer: str | None,
    folder: Path,
    *,
    sheet: bool,
) -> tuple[dict, list[dict]]:
    pages = {_row_id(page): page for page in job["pages"]}
    if set(bindings) != set(pages):
        raise ValueError("Review sheet was written for a different set of pages")
    reviewer = reviewer or os.environ.get("PAGELEDGER_REVIEWER")
    reviewed_at = _utc_now()
    decisions, corrected, seen = [], [], set()
    for row in rows:
        row_id = row["row_id"]
        if row_id not in pages:
            raise ValueError(f"Unknown review row: {row_id}")
        if row_id in seen:
            raise ValueError(f"Duplicate review row: {row_id}")
        seen.add(row_id)
        page = pages[row_id]
        decided = bool(row["decision"].strip()) or row.get("text") is not None
        if not _bound(bindings[row_id], _binding(job, page), decided) or row["page_number"] != str(
            page["page_number"]
        ):
            raise ValueError(
                f"Review row {row_id} no longer matches the job: page "
                f"{page['page_number']} was read again, reviewed or corrected after this "
                "sheet was written (applying a sheet a second time does this too). "
                "Write a new review sheet"
            )
        token = row["decision"].strip()
        text, start = row.get("text"), row.get("from") or None
        if token == "correct" or token.startswith("correct:"):
            if token == "correct" and sheet:
                raise ValueError(
                    f"Review row {row_id}: a sheet cannot carry the corrected text. Save it "
                    "in a text file beside the sheet and write correct:FILE"
                )
            if (token == "correct") != (text is not None):
                raise ValueError(
                    f"Review row {row_id}: give the corrected text either in the row's text "
                    "with correct, or in a file with correct:FILE"
                )
            if text is None:
                text = corrections.read_file(
                    folder, token.removeprefix("correct:"), page["page_number"]
                )
            decision, item = _correction(root, page, row_id, text, start)
            corrected.append(item)
        elif text is not None or start is not None:
            raise ValueError(f"Review row {row_id}: text and from go with a correct decision")
        elif token:
            decision = _decision(page, row_id, token)
        else:
            continue
        reason = row.get("note", "").strip() or f"Review sheet decision: {token}"
        decisions.append(
            {**decision, "reason": reason, "reviewer": reviewer, "reviewed_at": reviewed_at}
        )
    if decisions and not reviewer:
        raise ValueError(
            "Name the reviewer with --reviewer NAME or the PAGELEDGER_REVIEWER variable"
        )
    receipt = {
        "schema_version": "0.1",
        "source_sha256": job["source"]["sha256"],
        "decisions": decisions,
    }
    return receipt, corrected


def _binding_matches_rows(
    binding: object, rows: list[dict[str, str]], job: dict, pages: dict, *, evidence: bool = True
) -> bool:
    """Whether a binding is this sheet's: the same rows and, with ``evidence``, pages
    that still are as the binding recorded them."""
    if (
        not isinstance(binding, dict)
        or binding.get("schema_version") != "0.1"
        or not isinstance(binding.get("bindings"), dict)
    ):
        return False
    bindings = binding["bindings"]
    if set(bindings) != set(pages) or len(rows) != len(pages):
        return False
    seen = set()
    for row in rows:
        row_id = row.get("row_id")
        page = pages.get(row_id)
        if (
            page is None
            or row_id in seen
            or (evidence and not _bound(bindings.get(row_id), _binding(job, page), False))
            or row.get("page_number") != str(page["page_number"])
        ):
            return False
        seen.add(row_id)
    return seen == set(pages)


def _decision(page: dict, row_id: str, token: str) -> dict:
    if token.startswith("use:"):
        attempt_id = token.removeprefix("use:")
        attempt = _completed(page, attempt_id)
        if attempt is None:
            raise ValueError(f"Review row {row_id}: no completed attempt {attempt_id}")
        disposition = "reviewed_text"
    elif token in DECISIONS:
        attempt, disposition = _selected(page), DECISIONS[token]
        if attempt is not None and token == "accept":
            corrected = _corrected(page)
            if corrected is not None:
                # Accepting a corrected page accepts the correction, its published text.
                return {
                    key: corrected[key]
                    for key in (
                        "page_id",
                        "page_number",
                        "disposition",
                        "selected_attempt",
                        "output_sha256",
                        "correction",
                    )
                }
            # Accepting a page with an edition accepts the edition, the published text.
            return {
                "page_id": page["page_id"],
                "page_number": page["page_number"],
                "disposition": disposition,
                "selected_attempt": attempt["attempt_id"],
                "output_sha256": _output_sha256(page, attempt),
            }
    else:
        choices = ", ".join([*DECISIONS, "use:ATTEMPT", "correct:FILE"])
        raise ValueError(f"Review row {row_id}: unknown decision {token!r}; use one of {choices}")
    if attempt is None and disposition == "reviewed_text":
        raise ValueError(f"Review row {row_id}: the page has no text to accept")
    return {
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "disposition": disposition,
        "selected_attempt": None if attempt is None else attempt["attempt_id"],
        "output_sha256": None if attempt is None else attempt["raw_sha256"],
    }


def _correction(
    root: Path, page: dict, row_id: str, typed: str, start: str | None
) -> tuple[dict, dict]:
    """A reviewed_text decision carrying a correction, and what review --apply reports of it.

    The correction starts from the attempt named by ``start``, or else from the
    page's selected text: a correction already recorded, an edition, or the attempt.
    """
    corrected = _corrected(page)
    if start is not None:
        attempt = _completed(page, start)
        if attempt is None:
            raise ValueError(f"Review row {row_id}: no completed attempt {start} to correct")
        base = (attempt["attempt_id"], attempt["raw_sha256"])
        path, from_sha256, label = attempt["raw_artifact"], attempt["raw_sha256"], start
    elif corrected is not None:
        base = (corrected["selected_attempt"], corrected["output_sha256"])
        path, from_sha256 = corrected["correction"]["artifact"], corrected["correction"]["sha256"]
        label = f"the correction by {corrected['reviewer']}"
    else:
        attempt = _selected(page)
        if attempt is None:
            raise ValueError(
                f"Review row {row_id}: the page has no selected text to correct; in a "
                "decisions file, name the attempt to start from in the row's from"
            )
        sha256 = _output_sha256(page, attempt)
        base, from_sha256, label = (attempt["attempt_id"], sha256), sha256, attempt["attempt_id"]
        edition = page.get("edition")
        path = attempt["raw_artifact"]
        if sha256 != attempt["raw_sha256"] and edition is not None:
            path, label = edition["artifact"], f"the edition of {label}"
    try:
        before = _artifact_bytes(root, path, from_sha256).decode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"Review row {row_id}: the reading to correct is not UTF-8") from exc
    text, words = corrections.prepare(before, typed, page["page_number"])
    sha256 = corrections.sha256_text(text)
    decision = {
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "disposition": "reviewed_text",
        "selected_attempt": base[0],
        "output_sha256": base[1],
        "correction": {
            "artifact": corrections.artifact_path(page["page_id"], sha256),
            "sha256": sha256,
            "from_sha256": from_sha256,
            "words": words,
        },
    }
    item = {
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "from": label,
        "text": text,
        "sha256": sha256,
        "words": words,
        "large": corrections.large(words, before),
    }
    return decision, item


def _row_id(page: dict) -> str:
    return f"p{page['page_number']:04d}"


def _selected(page: dict) -> dict | None:
    return next(
        (item for item in page["attempts"] if item["attempt_id"] == page["selected_attempt"]),
        None,
    )


def _completed(page: dict, attempt_id: str) -> dict | None:
    return next(
        (
            item
            for item in page["attempts"]
            if item["attempt_id"] == attempt_id
            and item["outcome"] == "completed"
            and item["raw_artifact"]
        ),
        None,
    )


def _corrected(page: dict) -> dict | None:
    """The page's review decision when it records a person's corrected text."""
    review = page.get("review")
    if review is None:
        return None
    decision = next(item for item in review["decisions"] if item["page_id"] == page["page_id"])
    return decision if "correction" in decision else None


def _output_sha256(page: dict, attempt: dict) -> str:
    """The hash of the text the page publishes for the attempt: its edition or its own."""
    if edition_published(page, attempt["attempt_id"]):
        return page["edition"]["sha256"]
    return attempt["raw_sha256"]


def _empty(attempt: dict) -> bool:
    """A reading with no text at all. A reading of symbols alone, such as `* * *`, is
    warned empty of letters and digits, but a person can still choose it."""
    return empty_reading(attempt) and not (attempt.get("text") or "").strip()


def _name(job: dict, attempt: dict) -> str:
    """A reading named by what made it, as the report and the review view name it."""
    return reader_name({"engines": _engines(job), "source": job["source"]}, attempt)


def _state(page: dict) -> str:
    """Where the page stands, in words, with its code last."""
    code = page["disposition"]
    words = REVIEWED.get(code) if page.get("review") is not None else HOLDS.get(code)
    return f"{words or code} ({code})"


def _summary(root: Path, job: dict, page: dict, attempt: dict | None) -> str:
    """The selected text's first words, after what made it; its reading's ID comes last."""
    if attempt is None:
        return "No selected text"
    text, label = attempt["text"], sentence(_name(job, attempt))
    detail = attempt["attempt_id"]
    edition = page.get("edition")
    corrected = _corrected(page)
    if corrected is not None:
        artifact = corrected["correction"]["artifact"]
        text = (root / artifact).read_text(encoding="utf-8")
        label = f"{sentence(CORRECTION)} of {_name(job, attempt)}"
        detail = f"{detail}, corrected in {artifact}"
    elif edition and edition_published(page, attempt["attempt_id"]):
        text = (root / edition["artifact"]).read_text(encoding="utf-8")
        label += ", with its settled differences applied"
        detail = f"{detail}, edition {edition['artifact']}"
    return f"{label}: {' '.join(text.split()[:12])} ({len(text)} characters; {detail})"


def _binding(job: dict, page: dict) -> dict:
    """The evidence a row was written from; import refuses a row whose evidence changed."""
    selected = _selected(page)
    corrected = _corrected(page)
    if corrected is not None:
        shown = corrected["correction"]["sha256"]
    else:
        shown = None if selected is None else _output_sha256(page, selected)
    return {
        "job_id": job["job_id"],
        "source_sha256": job["source"]["sha256"],
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "selected_attempt": None if selected is None else selected["attempt_id"],
        "output_sha256": shown,
        "review_sha256": _review_sha256(page),
    }


def _review_sha256(page: dict) -> str | None:
    """The page's review decision as the row saw it, so that a sheet written before a
    later review cannot silently replace it."""
    review = page.get("review")
    if review is None:
        return None
    decision = next(item for item in review["decisions"] if item["page_id"] == page["page_id"])
    canonical = json.dumps(decision, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _bound(written: object, now: dict, decided: bool) -> bool:
    """Whether a row's binding still fits its page. The review it saw counts only for a
    row with a decision; a binding written before 0.9 records no review."""
    if not isinstance(written, dict):
        return False
    if not decided or "review_sha256" not in written:
        now = {key: value for key, value in now.items() if key != "review_sha256"}
        written = {key: value for key, value in written.items() if key != "review_sha256"}
    return written == now
