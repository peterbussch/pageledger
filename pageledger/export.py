"""Export the selected text of a verified document job."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from ._version import __version__
from .checkpoint import Checkpoint, atomic_bytes, read_record
from .document_report import (
    _disposition_label,
    _escape,
    _link,
    relative_path,
    verify_document_report,
)
from .processing import verify_job

FORMATS = ("txt", "md", "jsonl", "tei")
TEI = "http://www.tei-c.org/ns/1.0"
# Characters XML 1.0 cannot carry; OCR engines emit some of them.
_NOT_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


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
    out = Path(out).expanduser()
    source = relative_path(read_record(root / "job.json")["source"]["path"], out.parent.resolve())
    pages = _pages(root, report, reviewed_only)
    render = {"txt": _txt, "md": _md, "jsonl": _jsonl, "tei": _tei}[format]
    atomic_bytes(out, render(report, pages, source).encode("utf-8"))
    return len(pages)


def _pages(root: Path, report: dict, reviewed_only: bool) -> list[dict]:
    checkpoints: dict[str, Checkpoint] = {}
    pages = []
    for page in report["pages"]:
        review = page.get("review")
        if reviewed_only and review is None:
            continue
        selected = page["selected_output"]
        attempt = None
        if selected is not None:
            chosen = next(a for a in page["attempts"] if a["attempt_id"] == selected["attempt_id"])
            run = chosen["run_path"]
            if run not in checkpoints:
                checkpoints[run] = Checkpoint(root / run, existing=True, verify_sources=False)
            plan = checkpoints[run].job
            assert plan is not None
            attempt = {
                "attempt_id": chosen["attempt_id"],
                "stage": chosen["stage"],
                "adapter": plan["identity"]["name"],
                "model": checkpoints[run].records[page["page_id"]]["result"]["model"],
                "output_sha256": selected["sha256"],
            }
        pages.append(
            {
                "job_id": report["job_id"],
                "source_sha256": page["source_sha256"],
                "page_number": page["page_number"],
                "page_id": page["page_id"],
                "disposition": page["disposition"],
                "attempt": attempt,
                "text": None if selected is None else selected["text"],
                "review": None
                if review is None
                else next(d for d in review["decisions"] if d["page_id"] == page["page_id"]),
            }
        )
    return pages


def _engine(attempt: dict) -> str:
    return f"{attempt['adapter']} ({attempt['model']})" if attempt["model"] else attempt["adapter"]


def _facts(page: dict) -> list[tuple[str, str]]:
    """The review state and attempt identity shown above a page's text."""
    review, attempt = page["review"], page["attempt"]
    facts = [
        ("Disposition", _disposition_label(page["disposition"])),
        (
            "Review",
            "none"
            if review is None
            else f"{review['reviewer']}, {review['reviewed_at']}: {review['reason']}",
        ),
    ]
    if attempt is not None:
        facts.append(("Attempt", f"{attempt['attempt_id']}, {_engine(attempt)}"))
        facts.append(("Output SHA-256", attempt["output_sha256"]))
    return facts


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
        lines += [f"{label}: {' '.join(value.split())}" for label, value in _facts(page)]
        if page["text"] is not None:
            lines += ["", page["text"]]
        lines.append("")
    return "\n".join(lines)


def _md(report: dict, pages: list[dict], source: str) -> str:
    title, job = _heading(report)
    lines = [f"# {_escape(title)}", "", f"SHA-256 `{report['source']['sha256']}`. {job}.", ""]
    for page in pages:
        target = f"{source}#page={page['page_number']}"
        lines += [f"## Page {page['page_number']}", "", f"- {_link('Source page', target)}"]
        lines += [f"- {label}: {_escape(value)}" for label, value in _facts(page)]
        if page["text"] is not None:
            lines += ["", page["text"]]
        lines.append("")
    return "\n".join(lines)


def _jsonl(report: dict, pages: list[dict], source: str) -> str:
    return "".join(json.dumps(page, ensure_ascii=False) + "\n" for page in pages)


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
    for engine in sorted({_engine(page["attempt"]) for page in pages if page["attempt"]}):
        ET.SubElement(encoding, "p").text = f"Engine: {engine}"
    reviewed = sum(page["review"] is not None for page in pages)
    ET.SubElement(encoding, "p").text = (
        f"{reviewed} of {len(pages)} pages were reviewed by a person; each page division "
        "says which. Control characters, which XML cannot carry, are omitted."
    )
    body = ET.SubElement(ET.SubElement(tei, "text"), "body")
    for page in pages:
        number = page["page_number"]
        ET.SubElement(body, "pb", n=str(number), facs=f"{quote(source)}#page={number}")
        status = "reviewed" if page["review"] is not None else "unreviewed"
        div = ET.SubElement(body, "div", type="page", subtype=status)
        if page["text"] is None:
            ET.SubElement(div, "gap", reason=page["disposition"])
            continue
        lines = _NOT_XML.sub("", page["text"]).splitlines()
        ab = ET.SubElement(div, "ab")
        ab.text = lines[0] if lines else ""
        for line in lines[1:]:
            ET.SubElement(ab, "lb").tail = line
    _indent(tei)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tei, encoding="unicode") + "\n"


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
