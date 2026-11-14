"""Export the selected text of a verified document job."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from ._version import __version__
from .checkpoint import Checkpoint, atomic_bytes, read_record
from .corrections import describe
from .document_report import (
    _link,
    _plain,
    fenced,
    hold_in_words,
    is_pending,
    needs_person,
    reader_name,
    relative_path,
    verify_document_report,
)
from .image_source import page_target
from .processing import verify_job
from .wording import CORRECTION, HOLDS, REVIEWED, warning, warning_code

FORMATS = ("txt", "md", "jsonl", "tei")
# Where a page stands, in the JSONL `state` and the TEI `@ana` categories.
STATES = {
    "needs_person": "The job holds this page for a person; its holds are given in notes.",
    "pending": "The job has not finished reading this page.",
    "unreviewed": "Nothing held this page, and no person has checked it.",
    "reviewed": "A person reviewed this page.",
}
# The reason a review records when the reviewer wrote no note.
_SHEET_REASON = "Review sheet decision: "
TEI = "http://www.tei-c.org/ns/1.0"
_XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
# Characters XML 1.0 cannot carry; OCR engines emit some of them.
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_LINE_BREAK = re.compile("\r\n|\r|\n")
# Line separators JSON may carry raw, but which some line-oriented tools split on.
_JSON_SEPARATORS = {"\u2028": "\\u2028", "\u2029": "\\u2029", "\x85": "\\u0085"}


def export_job(job_dir: Path, out: Path, *, format: str, reviewed_only: bool = False) -> int:
    """Write each page's selected text with its review state and attempt identity.

    A job that does not verify is refused. Links to the source are relative to
    the exported file. Returns the number of pages written.
    """
    if format not in FORMATS:
        raise ValueError(f"Unsupported export format: {format}")
    root = Path(job_dir).expanduser().resolve()
    verification = verify_job(root)
    if verification["status"] != "pass":
        raise ValueError(f"Document job does not verify: {verification['error']}")
    report = verify_document_report(root)
    job = read_record(root / "job.json")
    out = Path(out).expanduser()
    # The folder resolved, so that a symlinked one such as /tmp is written through.
    out = out.parent.resolve() / out.name
    _check_out(out, root, Path(job["source"]["path"]), report["job_id"])
    out.parent.mkdir(parents=True, exist_ok=True)
    source = relative_path(job["source"]["path"], out.parent.resolve())
    pages = _pages(root, Path(job["root"]), report, reviewed_only)
    render = {"txt": _txt, "md": _md, "jsonl": _jsonl, "tei": _tei}[format]
    atomic_bytes(out, render(report, pages, source).encode("utf-8"))
    return len(pages)


def _check_out(out: Path, root: Path, source: Path, job_id: str) -> None:
    """Refuse to write over the job, its source, or a file that is not an earlier export
    of this job."""
    source = source.expanduser().resolve()
    if out == root or out.is_relative_to(root):
        raise ValueError(f"Write the export outside the job's folder, not to {out}")
    if out == source or (source.is_dir() and out.is_relative_to(source)):
        raise ValueError(f"{out} is the job's source; write the export to another file")
    if out.is_symlink() or out.is_dir():
        raise ValueError(f"{out} is a folder or a link; write the export to a file")
    if out.exists() and out.stat().st_size and not _earlier_export(out, job_id):
        raise ValueError(
            f"{out} already exists and is not an export of this job; choose another name "
            "or remove it first"
        )


def _earlier_export(path: Path, job_id: str) -> bool:
    """A file this command wrote for the same job, in any format: one it may replace."""
    with path.open("rb") as stream:
        head = stream.read(65536)
    marks = (f"Job {job_id}, exported with PageLedger", f'"job_id": "{job_id}"')
    return any(mark.encode("utf-8") in head for mark in marks)


def _pages(root: Path, recorded_root: Path, report: dict, reviewed_only: bool) -> list[dict]:
    checkpoints: dict[str, Checkpoint] = {}
    pages = []
    for page in report["pages"]:
        review = page.get("review")
        if reviewed_only and review is None:
            continue
        selected = page["selected_output"]
        # A person who called the page blank, unreadable, a defect or not text did not
        # accept the machine text as the page, so none is exported for it.
        if review is not None and page["disposition"] != "reviewed_text":
            selected = None
        attempt = None
        if selected is not None:
            chosen = next(a for a in page["attempts"] if a["attempt_id"] == selected["attempt_id"])
            run = chosen["run_path"]
            if run not in checkpoints:
                checkpoints[run] = Checkpoint(
                    root / run,
                    existing=True,
                    verify_sources=False,
                    recorded_root=recorded_root / run,
                )
            plan = checkpoints[run].job
            assert plan is not None
            attempt = {
                "attempt_id": chosen["attempt_id"],
                "stage": chosen["stage"],
                "adapter": plan["identity"]["name"],
                "model": checkpoints[run].records[page["page_id"]]["result"]["model"],
                "output_sha256": chosen["raw_sha256"],
            }
        # The edition is the text only when the page publishes it, not when a person
        # chose another reading or the reading's own text.
        edition = page.get("edition") if (selected or {}).get("edition") else None
        chosen_warnings = [] if attempt is None else _warnings(page, attempt["attempt_id"])
        correction = (selected or {}).get("correction")
        pages.append(
            {
                **(
                    {"edition": {"path": edition["artifact"], "sha256": edition["sha256"]}}
                    if edition
                    else {}
                ),
                **(
                    {
                        "correction": {
                            "path": selected["path"],
                            "sha256": selected["sha256"],
                            **correction,
                        }
                    }
                    if correction
                    else {}
                ),
                "job_id": report["job_id"],
                "source_sha256": page["source_sha256"],
                "page_number": page["page_number"],
                "page_id": page["page_id"],
                "disposition": page["disposition"],
                "state": _state(page, report),
                "review_reasons": list(page["review_reasons"]),
                "warnings": chosen_warnings,
                "attempt": attempt,
                "text": None if selected is None else selected["text"],
                "review": None
                if review is None
                else next(d for d in review["decisions"] if d["page_id"] == page["page_id"]),
            }
        )
    return pages


def _state(page: dict, report: dict) -> str:
    if page.get("review") is not None:
        return "reviewed"
    if is_pending(page, report):
        return "pending"
    return "needs_person" if needs_person(page, report) else "unreviewed"


def _warnings(page: dict, attempt_id: str) -> list[str]:
    """The warning codes on the reading a page's text comes from."""
    attempt = next(item for item in page["attempts"] if item["attempt_id"] == attempt_id)
    codes = (warning_code(item) for item in attempt.get("warnings") or [])
    return list(dict.fromkeys(code for code in codes if code))


def _engine(attempt: dict) -> str:
    return f"{attempt['adapter']} ({attempt['model']})" if attempt["model"] else attempt["adapter"]


def _shown(report: dict, page: dict) -> dict:
    """The report's own record of an exported page."""
    return next(p for p in report["pages"] if p["page_id"] == page["page_id"])


def _reader(report: dict, page: dict) -> str:
    """What made the page's reading, in the report's words."""
    shown = _shown(report, page)
    attempt_id = page["attempt"]["attempt_id"]
    return reader_name(report, next(a for a in shown["attempts"] if a["attempt_id"] == attempt_id))


def _reading(report: dict, page: dict) -> str:
    """The reading by what made it, then its engine and ID: the report credits a PDF's
    earlier OCR to that OCR, not to the library that read the layer."""
    attempt = page["attempt"]
    return f"{_reader(report, page)}; {_engine(attempt)}; {attempt['attempt_id']}"


def _state_words(report: dict, page: dict) -> str:
    code = page["disposition"]
    return f"{hold_in_words(_shown(report, page), code)} ({code})"


def _facts(report: dict, page: dict) -> list[tuple[str, str]]:
    """The review state and the reading's identity shown above a page's text."""
    if "correction" in page:
        return _corrected_facts(report, page)
    review, attempt = page["review"], page["attempt"]
    facts = [
        ("State", _state_words(report, page)),
        (
            "Review",
            "none"
            if review is None
            else f"{review['reviewer']}, {review['reviewed_at']}: {review['reason']}",
        ),
    ]
    if attempt is not None:
        facts.append(("Reading", _reading(report, page)))
        facts.append(("Reading SHA-256", attempt["output_sha256"]))
    if "edition" in page:
        facts.append(
            ("Edition", f"{page['edition']['path']}, the reading with its settled differences")
        )
        facts.append(("Edition SHA-256", page["edition"]["sha256"]))
    return facts


def _corrected_facts(report: dict, page: dict) -> list[tuple[str, str]]:
    """A corrected page's text is the person's: say so first, and name the reading it
    corrected, with that reading's identity, after it."""
    review, attempt, correction = page["review"], page["attempt"], page["correction"]
    note = review["reason"]
    # A later review that accepted the corrected text carries the correction forward.
    made = (review["reviewer"], review["reviewed_at"]) == (
        correction["reviewer"],
        correction["reviewed_at"],
    )
    decided = ("corrected the text" if made else "accepted the text as corrected") + (
        "" if note.startswith(_SHEET_REASON) else f"; their note: {note}"
    )
    return [
        ("State", _state_words(report, page)),
        ("Review", f"{review['reviewer']}, {review['reviewed_at']}: {decided}"),
        (
            "Text from",
            f"{CORRECTION} by {correction['reviewer']} of {_reader(report, page)} "
            f"({describe(correction['words'])})",
        ),
        (
            "Correction",
            f"{correction['path']}, made on {correction['reviewed_at']} from "
            f"{correction['from_path']}",
        ),
        ("Correction SHA-256", correction["sha256"]),
        # What the correction started from: the reading, its edition or an earlier correction.
        ("Corrected from SHA-256", correction["from_sha256"]),
        ("Reading", _reading(report, page)),
        ("Reading SHA-256", attempt["output_sha256"]),
    ]


def _heading(report: dict) -> tuple[str, str]:
    return (
        Path(report["source"]["path"]).name,
        f"Job {report['job_id']}, exported with PageLedger {__version__}",
    )


def _txt(report: dict, pages: list[dict], source: str) -> str:
    title, job = _heading(report)
    lines = [title, f"SHA-256 {report['source']['sha256']}", job, ""]
    for page in pages:
        lines.append(f"--- Page {page['page_number']} ---")
        lines += [f"{label}: {' '.join(value.split())}" for label, value in _facts(report, page)]
        if page["text"] is not None:
            # The page's text could hold a line such as "--- Page 2 ---": its length
            # says exactly where it ends.
            text = page["text"]
            count = len(_LINE_BREAK.split(text))  # a reading keeps CRLF and lone CR
            lines.append(
                f"Text: {len(text)} characters on {count} line{'' if count == 1 else 's'}, below"
            )
            lines += ["", text]
        lines.append("")
    return "\n".join(lines)


def _md(report: dict, pages: list[dict], source: str) -> str:
    title, job = _heading(report)
    lines = [f"# {_plain(title)}", "", f"SHA-256 `{report['source']['sha256']}`. {job}.", ""]
    for page in pages:
        path, fragment = page_target(report["source"], page["page_number"])
        # Quoted whole, so that a colon in a file name cannot make a scheme such as javascript:.
        target = quote(source) + quote(path) + fragment
        lines += [f"## Page {page['page_number']}", "", f"- {_link('Source page', target)}"]
        lines += [f"- {label}: {_plain(value)}" for label, value in _facts(report, page)]
        if page["text"] is not None:
            lines += ["", fenced(page["text"]).removesuffix("\n")]
        lines.append("")
    return "\n".join(lines)


def _jsonl(report: dict, pages: list[dict], source: str) -> str:
    lines = []
    for page in pages:
        line = json.dumps(page, ensure_ascii=False)
        for character, escaped in _JSON_SEPARATORS.items():
            line = line.replace(character, escaped)
        lines.append(line + "\n")
    return "".join(lines)


def _tei(report: dict, pages: list[dict], source: str) -> str:
    title, job = _heading(report)
    tei = ET.Element("TEI", xmlns=TEI)
    header = ET.SubElement(tei, "teiHeader")
    file_desc = ET.SubElement(header, "fileDesc")
    ET.SubElement(ET.SubElement(file_desc, "titleStmt"), "title").text = title
    ET.SubElement(ET.SubElement(file_desc, "publicationStmt"), "p").text = f"{job}."
    source_p = ET.SubElement(ET.SubElement(file_desc, "sourceDesc"), "p")
    ref = ET.SubElement(source_p, "ref", target=quote(source))
    ref.text = title
    ref.tail = f", SHA-256 {report['source']['sha256']}"
    encoding = ET.SubElement(header, "encodingDesc")
    engines = {
        f"{_reader(report, page)}: {_engine(page['attempt'])}" for page in pages if page["attempt"]
    }
    for engine in sorted(engines):
        ET.SubElement(encoding, "p").text = _NOT_XML.sub("", f"Read by {engine}")
    reviewed = sum(page["review"] is not None for page in pages)
    ET.SubElement(encoding, "p").text = (
        f"{reviewed} of {len(pages)} pages were reviewed by a person; each page division "
        "says which. Control characters, which XML cannot carry, are omitted."
    )
    taxonomy = ET.SubElement(ET.SubElement(encoding, "classDecl"), "taxonomy")
    taxonomy.set(_XML_ID, "pageledger-state")
    for state, meaning in STATES.items():
        category = ET.SubElement(taxonomy, "category")
        category.set(_XML_ID, f"pageledger-{state.replace('_', '-')}")
        ET.SubElement(category, "catDesc").text = meaning
    corrected = [page for page in pages if "correction" in page]
    if corrected:
        revisions = ET.SubElement(header, "revisionDesc")
        for page in corrected:
            correction = page["correction"]
            change = ET.SubElement(revisions, "change", when=correction["reviewed_at"])
            change.set(_XML_ID, f"correction-{page['page_number']}")
            change.text = _NOT_XML.sub(
                "",
                f"Page {page['page_number']}: text corrected by {correction['reviewer']} from "
                f"{correction['from_path']} ({describe(correction['words'])}): "
                f"{correction['reason']}",
            )
    body = ET.SubElement(ET.SubElement(tei, "text"), "body")
    for page in pages:
        number = page["page_number"]
        path, fragment = page_target(report["source"], number)
        facs = quote(source + path) + fragment
        ET.SubElement(body, "pb", n=str(number), facs=facs)
        status = "reviewed" if page["review"] is not None else "unreviewed"
        div = ET.SubElement(body, "div", type="page", subtype=status)
        div.set("ana", f"#pageledger-{page['state'].replace('_', '-')}")
        if "correction" in page:
            div.set("change", f"#correction-{number}")
        if page["state"] == "needs_person":
            for reason in page["review_reasons"] or [page["disposition"]]:
                note = ET.SubElement(div, "note", type="hold", n=reason)
                note.text = f"{HOLDS.get(reason) or REVIEWED.get(reason) or reason}."
        for code in page["warnings"]:
            ET.SubElement(div, "note", type="warning", n=code).text = warning(code)
        if page["text"] is None:
            ET.SubElement(div, "gap", reason=page["disposition"])
            continue
        provenance = ET.SubElement(div, "note", type="provenance")
        provenance.text = _NOT_XML.sub("", _provenance(report, page))
        lines = _LINE_BREAK.split(_NOT_XML.sub("", page["text"]))
        if len(lines) > 1 and lines[-1] == "":
            lines.pop()
        ab = ET.SubElement(div, "ab")
        ab.text = lines[0] if lines else ""
        for line in lines[1:]:
            ET.SubElement(ab, "lb").tail = line
    _indent(tei)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tei, encoding="unicode") + "\n"


def _provenance(report: dict, page: dict) -> str:
    """Where a page's text comes from, so that a TEI edition can be checked against the job."""
    if "correction" in page:
        what, sha256 = f"{CORRECTION} of reading", page["correction"]["sha256"]
    elif "edition" in page:
        what, sha256 = "the edition of reading", page["edition"]["sha256"]
    else:
        what, sha256 = "reading", page["attempt"]["output_sha256"]
    text = (
        f"Text from {what} {page['attempt']['attempt_id']} ({_reader(report, page)}; "
        f"{_engine(page['attempt'])}), SHA-256 {sha256} before characters XML cannot "
        "carry were left out."
    )
    review = page["review"]
    if review is not None:
        text += f" Reviewed by {review['reviewer']} at {review['reviewed_at']}."
    return text


def _indent(element: ET.Element, depth: int = 0) -> None:
    """Indent elements that hold only elements, so no text is changed."""
    children = list(element)
    if not children or element.text is not None or any(c.tail is not None for c in children):
        return
    for child in children:
        child.tail = "\n" + "  " * (depth + 1)
        _indent(child, depth + 1)
    element.text = "\n" + "  " * (depth + 1)
    children[-1].tail = "\n" + "  " * depth
