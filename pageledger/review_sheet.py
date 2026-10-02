"""Spreadsheet review: one CSV row per page, bound to the evidence it was written from."""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

from .checkpoint import atomic_bytes
from .document_report import relative_path
from .runner import _utc_now

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
# `use:ATTEMPT` accepts another completed attempt's text instead.
DECISIONS = {
    "accept": "reviewed_text",
    "blank": "reviewed_blank",
    "illustration": "illustration",
    "handwriting": "handwriting",
    "unreadable": "unreadable",
    "source_defect": "source_defect",
}
# A cell starting with one of these was a spreadsheet formula.
_FORMULA_PREFIXES = ("=", "+", "@")


def binding_path(sheet: Path) -> Path:
    return sheet.with_name(sheet.name + ".binding.json")


def write_review_sheet(job: dict, sheet: Path) -> dict:
    """Write the CSV and the sidecar that binds each row to its page's evidence."""
    sheet = sheet.expanduser().resolve()
    sheet.parent.mkdir(parents=True, exist_ok=True)
    source = relative_path(job["source"]["path"], sheet.parent)
    rows, bindings = [], {}
    for page in job["pages"]:
        row_id = _row_id(page)
        rows.append(
            {
                "row_id": row_id,
                "page_number": str(page["page_number"]),
                "source_page": f"{source}#page={page['page_number']}",
                "disposition": page["disposition"],
                "attempt_summary": _summary(job, page, _selected(page)),
                "review_reasons": "; ".join(page["review_reasons"]),
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
    return {"out": str(sheet), "binding": str(binding_path(sheet)), "rows": len(rows)}


def read_review_sheet(job: dict, sheet: Path, reviewer: str | None) -> dict:
    """Check every row against the job, then return its decisions as a review receipt.

    A row with a blank decision, or a deleted row, leaves its page unreviewed.
    """
    sheet = sheet.expanduser().resolve()
    try:
        reader = csv.DictReader(io.StringIO(sheet.read_text(encoding="utf-8-sig"), newline=""))
        rows = list(reader)
        binding = json.loads(binding_path(sheet).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, csv.Error, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read the review sheet or its binding: {exc}") from exc
    if reader.fieldnames != list(FIELDS):
        raise ValueError(f"Review sheet columns must be: {', '.join(FIELDS)}")
    pages = {_row_id(page): page for page in job["pages"]}
    if (
        not isinstance(binding, dict)
        or binding.get("schema_version") != "0.1"
        or not isinstance(binding.get("bindings"), dict)
    ):
        raise ValueError(f"Review sheet binding is invalid: {binding_path(sheet)}")
    bindings = binding["bindings"]
    if set(bindings) != set(pages):
        raise ValueError("Review sheet was written for a different set of pages")
    reviewer = reviewer or os.environ.get("PAGELEDGER_REVIEWER")
    reviewed_at = _utc_now()
    decisions, seen = [], set()
    for number, row in enumerate(rows, start=2):
        if None in row or None in row.values():
            raise ValueError(f"Row {number} of the review sheet does not have {len(FIELDS)} cells")
        row_id = row["row_id"]
        if row_id not in pages:
            raise ValueError(f"Unknown review row: {row_id}")
        if row_id in seen:
            raise ValueError(f"Duplicate review row: {row_id}")
        seen.add(row_id)
        page = pages[row_id]
        if bindings[row_id] != _binding(job, page) or row["page_number"] != str(
            page["page_number"]
        ):
            raise ValueError(
                f"Review row {row_id} no longer matches the job's evidence; "
                "write a new review sheet"
            )
        for column in ("decision", "note"):
            if row[column].startswith(_FORMULA_PREFIXES):
                raise ValueError(f"Review row {row_id} has a formula in {column}")
        token = row["decision"].strip()
        if token:
            decision = _decision(page, row_id, token)
            reason = row["note"].strip() or f"Review sheet decision: {token}"
            decisions.append(
                {**decision, "reason": reason, "reviewer": reviewer, "reviewed_at": reviewed_at}
            )
    if decisions and not reviewer:
        raise ValueError(
            "Name the reviewer with --reviewer NAME or the PAGELEDGER_REVIEWER variable"
        )
    return {
        "schema_version": "0.1",
        "source_sha256": job["source"]["sha256"],
        "decisions": decisions,
    }


def _decision(page: dict, row_id: str, token: str) -> dict:
    if token.startswith("use:"):
        attempt_id = token.removeprefix("use:")
        attempt = next(
            (
                item
                for item in page["attempts"]
                if item["attempt_id"] == attempt_id
                and item["outcome"] == "completed"
                and item["raw_artifact"]
            ),
            None,
        )
        if attempt is None:
            raise ValueError(f"Review row {row_id}: no completed attempt {attempt_id}")
        disposition = "reviewed_text"
    elif token in DECISIONS:
        attempt, disposition = _selected(page), DECISIONS[token]
        if attempt is not None and token == "accept":
            # Accepting a page with an edition accepts the edition, the published text.
            return {
                "page_id": page["page_id"],
                "page_number": page["page_number"],
                "disposition": disposition,
                "selected_attempt": attempt["attempt_id"],
                "output_sha256": _output_sha256(page, attempt),
            }
    else:
        choices = ", ".join([*DECISIONS, "use:ATTEMPT"])
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


def _row_id(page: dict) -> str:
    return f"p{page['page_number']:04d}"


def _selected(page: dict) -> dict | None:
    return next(
        (item for item in page["attempts"] if item["attempt_id"] == page["selected_attempt"]),
        None,
    )


def _output_sha256(page: dict, attempt: dict) -> str:
    edition = page.get("edition")
    if edition and edition["base_attempt"] == attempt["attempt_id"]:
        return edition["sha256"]
    return attempt["raw_sha256"]


def _summary(job: dict, page: dict, attempt: dict | None) -> str:
    if attempt is None:
        return "No selected text"
    text, label = attempt["text"], attempt["attempt_id"]
    edition = page.get("edition")
    if edition and edition["base_attempt"] == attempt["attempt_id"]:
        text = (Path(job["root"]) / edition["artifact"]).read_text(encoding="utf-8")
        label = f"{label}, edition {edition['artifact']}"
    return f"{label}: {' '.join(text.split()[:12])} ({len(text)} characters)"


def _binding(job: dict, page: dict) -> dict:
    """The evidence a row was written from; import refuses a row whose evidence changed."""
    selected = _selected(page)
    return {
        "job_id": job["job_id"],
        "source_sha256": job["source"]["sha256"],
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "selected_attempt": None if selected is None else selected["attempt_id"],
        "output_sha256": None if selected is None else _output_sha256(page, selected),
    }
