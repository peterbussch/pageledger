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

Each `<page_id>.json` packet lists the places where a vision model's reading of a
page (the base) differs from a literal OCR reading (the witness). The image the
model read is `<page_id>.jpg`.

For every span, look at the page image and write what is printed at that place:

- the base reading, the witness reading, or other text when neither is right;
- the printer's misprints exactly as printed, never corrected;
- an empty string where the page prints nothing there.

`base` is the exact text between the span's offsets in the base reading; an
empty `base` means the witness has words there that the base lacks.

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
    decided: dict[str, dict] = {}
    for receipt in receipts:
        if receipt["contested_sha256"] != sha256:
            continue
        for decision in receipt["decisions"]:
            decided[decision["span_id"]] = {**decision, "reviewer": receipt["reviewer"]}
    still_open = [
        span
        for span in record["spans"]
        if span["status"] == "open" and decided.get(span["span_id"], {}).get("confidence") != "high"
    ]
    return still_open, {k: v for k, v in decided.items() if v["confidence"] == "high"}


def edition(base: str, record: dict, decided: dict[str, dict]) -> str | None:
    """The base with every decided change applied, or None when nothing changes."""
    edits = sorted(
        (
            (span["start"], span["end"], decided[span["span_id"]]["text"])
            for span in record["spans"]
            if span["span_id"] in decided and decided[span["span_id"]]["text"] != span["base"]
        ),
        reverse=True,
    )
    # From the end backwards, so earlier offsets still hold. At one offset a
    # replacement goes first, then an insertion lands before its new text.
    text = base
    for start, end, new in edits:
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
            "base_sha256",
            "image_sha256",
            "decisions",
            "reviewer",
            "reviewed_at",
        }
        or receipt["schema_version"] != RECEIPT_VERSION
        or receipt["source_sha256"] != page["source_sha256"]
        or receipt["page_id"] != page["page_id"]
        or receipt["page_number"] != page["page_number"]
        or not all(
            isinstance(receipt[key], str) and _HASH.fullmatch(receipt[key])
            for key in ("contested_sha256", "base_sha256")
        )
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
    open_ids = {span["span_id"] for span in record["spans"] if span["status"] == "open"}
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
        "base_sha256": record["base"]["text_sha256"],
        "image_sha256": (base.get("input_evidence") or {}).get("sha256"),
        "decisions": answer["decisions"],
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
    }


def write_packets(job: dict, root: Path, directory: Path) -> dict:
    """One packet per page with open spans: readings, open spans and the image the reader saw."""
    directory.mkdir(parents=True, exist_ok=True)
    atomic_bytes(directory / "INSTRUCTIONS.md", INSTRUCTIONS.encode())
    written = 0
    for page in job["pages"]:
        contested = page.get("contested")
        if not contested or not contested["open"]:
            continue
        record = json.loads((root / contested["artifact"]).read_bytes())
        attempts = {a["attempt_id"]: a for a in page["attempts"]}
        base = attempts[record["base"]["attempt"]]
        witness = attempts[record["witness"]["attempt"]]
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
            "base": {"attempt": base["attempt_id"], "text": base["text"]},
            "witness": {"attempt": witness["attempt_id"], "text": witness["text"]},
            "spans": [
                {key: span[key] for key in ("span_id", "kind", "start", "end", "base", "witness")}
                for span in still_open
            ],
        }
        atomic_bytes(directory / f"{page['page_id']}.json", encode(packet))
        written += 1
    return {"packets": written, "out": str(directory)}
