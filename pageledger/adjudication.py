"""Settling contested spans against the page image, and the edition text that results.

Rules settle what the two readings alone decide. Every other span waits for an
adjudicator, an agent or a person looking at the page image, whose decisions
come back as receipts bound to the exact spans they answer. When no span on a
page is open, the edition is the reader's text with each decision applied at its
offsets. The attempts themselves are never edited.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .checkpoint import atomic_bytes, file_digest

CONFIDENCE = ("high", "medium", "low")
RECEIPT_VERSION = "0.2"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
INSTRUCTIONS = """\
# Settle contested spans against the page image

Each `<page_id>.json` packet lists places where two engines read a page
differently. The page image is `<page_id>.jpg`.

For every span, `candidates` are the two readings, in no particular order, and
`before` and `after` are the text around the place, to help you find it. Your
answer replaces exactly what lies between `before` and `after`. Look at the page
image and write what is printed there:

- one of the candidates, or other text when neither is right;
- the printer's misprints exactly as printed, never corrected;
- an empty string where the page prints nothing there.

Do not choose the more fluent or more likely word: choose the printed one.

Answer in `<page_id>.decisions.json`, copying `page_id` and `contested_sha256`
from the packet:

    {"page_id": "...", "contested_sha256": "...",
     "decisions": [{"span_id": "sp_...", "text": "...", "confidence": "high", "note": "..."}]}

Use `high` only when the image shows the text plainly. A span answered with
`medium` or `low` stays open for a person.
"""


def encode(record: dict) -> bytes:
    """The bytes of a contested-spans artifact; its hash binds receipts to it."""
    return (json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode()


def settle(record: dict, sha256: str, receipts: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """Spans still open, and each span's deciding answer from receipts bound to these spans.

    A later receipt overrides an earlier one. Receipts bound to other spans, for
    example before a new reading changed the base, no longer apply and are kept
    only as history.
    """
    rule_open = {span["span_id"] for span in record["spans"] if span["status"] == "open"}
    decided: dict[str, dict] = {}
    for receipt in receipts:
        if receipt["contested_sha256"] != sha256:
            continue
        for decision in receipt["decisions"]:
            if decision["span_id"] in rule_open:
                decided[decision["span_id"]] = {**decision, "reviewer": receipt["reviewer"]}
    still_open = [
        span
        for span in record["spans"]
        if span["status"] == "open" and decided.get(span["span_id"], {}).get("confidence") != "high"
    ]
    return still_open, {k: v for k, v in decided.items() if v["confidence"] == "high"}


def edition(base: str, record: dict, decided: dict[str, dict]) -> str | None:
    """The base with every decided change applied, or None when nothing changes.

    An answer replaces exactly the characters of its span. Inserted words get a
    space where they would touch a word, and a removed word takes one space with it.
    """
    edits = sorted(
        (span["start"], span["end"], decided[span["span_id"]]["text"])
        for span in record["spans"]
        if span["span_id"] in decided and decided[span["span_id"]]["text"] != span["base"]
    )
    for (_, end, _), (start, _, _) in zip(edits, edits[1:], strict=False):
        if start < end:
            raise ValueError("Settled spans overlap; the contested spans are invalid")
    # From the end backwards, so earlier offsets still hold.
    text = base
    for start, end, new in reversed(edits):
        if start == end and new:
            if start and not text[start - 1].isspace() and not new[0].isspace():
                new = " " + new
            if start < len(text) and text[start].isalnum() and not new[-1].isspace():
                new = new + " "
        elif not new:
            if text[end : end + 1] == " ":
                end += 1
            elif start and text[start - 1] == " ":
                start -= 1
        text = text[:start] + new + text[end:]
    return text if edits else None


def validate_receipt(receipt: Any, page: dict) -> None:
    """Reject an adjudication receipt that does not bind this page and well-formed answers."""
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {
            "schema_version",
            "source_sha256",
            "page_id",
            "page_number",
            "contested_sha256",
            "image_sha256",
            "decisions",
            "reviewer",
            "reviewed_at",
        }
        or receipt["schema_version"] != RECEIPT_VERSION
        or receipt["source_sha256"] != page["source_sha256"]
        or receipt["page_id"] != page["page_id"]
        or receipt["page_number"] != page["page_number"]
        or not isinstance(receipt["contested_sha256"], str)
        or not _HASH.fullmatch(receipt["contested_sha256"])
        or not (receipt["image_sha256"] is None or _HASH.fullmatch(str(receipt["image_sha256"])))
        or not isinstance(receipt["reviewer"], str)
        or not receipt["reviewer"].strip()
    ):
        raise ValueError(f"Adjudication receipt for {page['page_id']} is invalid")
    try:
        if datetime.fromisoformat(receipt["reviewed_at"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except (AttributeError, ValueError) as exc:
        raise ValueError("Adjudication timestamp must include a timezone") from exc
    _check_decisions(receipt["decisions"], page["page_id"])


def _check_decisions(decisions: Any, page_id: str) -> None:
    if not isinstance(decisions, list) or not decisions:
        raise ValueError(f"Adjudication for {page_id} has no decisions")
    seen = set()
    for decision in decisions:
        if (
            not isinstance(decision, dict)
            or set(decision) != {"span_id", "text", "confidence", "note"}
            or not isinstance(decision["span_id"], str)
            or decision["span_id"] in seen
            or not isinstance(decision["text"], str)
            or decision["confidence"] not in CONFIDENCE
            or not isinstance(decision["note"], str)
        ):
            raise ValueError(f"Adjudication for {page_id} has an invalid or repeated decision")
        seen.add(decision["span_id"])


def receipt_for(
    page: dict, answer: Any, *, record: dict, sha256: str, reviewer: str, reviewed_at: str
) -> dict:
    """Bind one page's answers to the spans they settle."""
    if not isinstance(answer, dict) or set(answer) != {"page_id", "contested_sha256", "decisions"}:
        raise ValueError(
            f"Decisions for {page['page_id']} need page_id, contested_sha256, decisions"
        )
    if answer["page_id"] != page["page_id"] or answer["contested_sha256"] != sha256:
        raise ValueError(f"Decisions for {page['page_id']} answer other spans; write packets again")
    _check_decisions(answer["decisions"], page["page_id"])
    # Only spans still open: answers already recorded are not recorded again, under
    # whoever runs the command this time.
    still_open, _ = settle(record, sha256, page.get("adjudications", []))
    open_ids = {span["span_id"] for span in still_open}
    unknown = [d["span_id"] for d in answer["decisions"] if d["span_id"] not in open_ids]
    if unknown:
        raise ValueError(f"Decisions for {page['page_id']} name no open span: {unknown[0]}")
    base = next(a for a in page["attempts"] if a["attempt_id"] == record["base"]["attempt"])
    return {
        "schema_version": RECEIPT_VERSION,
        "source_sha256": page["source_sha256"],
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "contested_sha256": sha256,
        "image_sha256": (base.get("input_evidence") or {}).get("sha256"),
        "decisions": answer["decisions"],
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
    }


_AROUND = 80


def write_packets(job: dict, root: Path, directory: Path) -> dict:
    """One packet per page with open spans and the image the reader saw.

    Packets are blind: a span shows both readings in sorted order and the words
    around it, never which engine read what. An adjudicator told which reading is
    the model's tends to keep it; the model that made a substitution will vouch
    for it.
    """
    if directory.exists() and any(directory.iterdir()):
        raise ValueError(
            f"{directory} is not empty; write packets to a new directory so old answers "
            "are not recorded again"
        )
    directory.mkdir(parents=True, exist_ok=True)
    atomic_bytes(directory / "INSTRUCTIONS.md", INSTRUCTIONS.encode())
    written = 0
    for page in job["pages"]:
        contested = page.get("contested")
        if not contested or not contested["open"] or page.get("review") is not None:
            continue
        record = json.loads((root / contested["artifact"]).read_bytes())
        base = next(a for a in page["attempts"] if a["attempt_id"] == record["base"]["attempt"])
        text = base["text"]
        still_open, _ = settle(record, contested["sha256"], page.get("adjudications", []))
        image = None
        evidence = base.get("input_evidence")
        if evidence:
            name = f"{page['page_id']}.jpg"
            source = root / base["run_path"] / evidence["artifact"]
            if file_digest(source) != evidence["sha256"]:
                raise ValueError(f"Image evidence for {page['page_id']} changed")
            shutil.copyfile(source, directory / name)
            image = {"path": name, "sha256": evidence["sha256"]}
        packet = {
            "schema_version": "0.1",
            "job_id": job["job_id"],
            "source_sha256": page["source_sha256"],
            "page_id": page["page_id"],
            "page_number": page["page_number"],
            "contested_sha256": contested["sha256"],
            "image": image,
            "spans": [
                {
                    "span_id": span["span_id"],
                    "candidates": sorted({span["base"], span["witness"]}),
                    "before": text[max(0, span["start"] - _AROUND) : span["start"]],
                    "after": text[span["end"] : span["end"] + _AROUND],
                }
                for span in still_open
            ],
        }
        atomic_bytes(directory / f"{page['page_id']}.json", encode(packet))
        written += 1
    return {"packets": written, "out": str(directory)}
