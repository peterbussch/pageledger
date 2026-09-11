"""Byte-faithful document evidence with Markdown derived from document.json."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from .checkpoint import atomic_bytes
from .processing_policy import validate_review


def _artifact_bytes(root: Path, relative: str, expected_hash: str) -> bytes:
    if not isinstance(relative, str) or not relative:
        raise ValueError("Missing report artifact path")
    path = Path(relative)
    if path.is_absolute() or any(part in {".", ".."} for part in relative.split("/")):
        raise ValueError("Unsafe report artifact path")
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("Unsafe report artifact symlink")
    try:
        content = candidate.read_bytes()
    except OSError as exc:
        raise ValueError(f"Report artifact is unavailable: {relative}") from exc
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError(f"Report artifact hash mismatch: {relative}")
    return content


def _link(label: str, target: str) -> str:
    return f"[{_escape(label)}](<{quote(target, safe='/:#?=&%')}>)"


def _escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("\n", " ").replace("\r", " ")


def render_transcript(report: dict) -> str:
    """Serialize the transcript without trimming or normalizing extraction text."""
    chunks = [f"# Transcript: {_escape(report['job_id'])}\n\n",
              f"Source SHA-256: `{report['source']['sha256']}`\n\n"]
    for page in report["pages"]:
        chunks.append(f"## Page {page['page_number']}\n\n")
        chunks.append(_link("Source page", page["source_link"]) + "\n\n")
        selected = page["selected_output"]
        if selected is None:
            chunks.append(f"[No selected text: {page['disposition']}.]\n\n")
        else:
            chunks.append(_link(f"Selected attempt {selected['attempt_id']}", selected["path"]) + "\n\n")
            chunks.append(selected["text"])
            chunks.append("\n\n")
    return "".join(chunks)


def render_document_report(report: dict) -> str:
    """Render the human summary using document.json alone."""
    source, counts = report["source"], report["counts"]
    annotations, retention = source["annotations"], report["source_retention"]
    annotation_count = "unknown" if annotations["count"] is None else str(annotations["count"])
    source_count = "unknown" if counts["source_pages"] is None else str(counts["source_pages"])
    usage = report["usage"]
    cost = "unknown" if not usage["cost_known"] else f"${usage['cost_usd']}"
    tokens = str(usage["tokens"]) if usage["tokens"] is not None else "unknown"
    if usage.get("tokens_known") is False:
        tokens = f"unknown (known subtotal: {tokens})"
    lines = [f"# Document report: {_escape(report['job_id'])}", "",
             f"Status: {report['status']}", "",
             f"Source: {_link(source['path'], source['path'])}",
             f"Source SHA-256: `{source['sha256']}`", "",
             f"Pages processed: {counts['processed_pages']}; selected for this job: {counts['selected_pages']}; full source: {source_count}.",
             f"Selected outputs: {counts['selected_outputs']}; unresolved pages: {counts['unresolved_pages']}.", "",
             f"Annotations: {annotations['status']} ({annotation_count}). This inventory does not establish body extraction or citation completeness.", "",
             f"Attempt pages: {usage['attempt_pages']}; image calls: {usage['image_calls']}; tokens: {tokens}; cost: {cost}.", "",
             "| Page | Disposition | Selected output | Review evidence |",
             "| --- | --- | --- | --- |"]
    for page in report["pages"]:
        selected = page["selected_output"]
        selected_link = _link(selected["attempt_id"], selected["path"]) if selected else "none"
        reasons = ", ".join(page["review_reasons"]) or "none recorded"
        if page["review"] is not None:
            decision = next(item for item in page["review"]["decisions"] if item["page_id"] == page["page_id"])
            reasons += f"; reviewed by {decision['reviewer']} at {decision['reviewed_at']}: {decision['reason']}"
        lines.append(f"| {_link(str(page['page_number']), page['source_link'])} | {page['disposition']} | {selected_link} | {_escape(reasons)} |")
    lines.extend(["", "Attempt evidence:", ""])
    for page in report["pages"]:
        for attempt in page["attempts"]:
            path = attempt["raw_artifact"]
            evidence = _link(attempt["attempt_id"], path) if path else _escape(attempt["attempt_id"])
            failure = (attempt.get("failure") or {}).get("code")
            detail = f"; failure {_escape(failure)}" if failure else ""
            lines.append(f"- Page {page['page_number']}: {evidence}; stage {attempt['stage']}; outcome {attempt['outcome']}{detail}.")
    lines.extend(["", f"Capture: {retention['capture']}",
                  f"Preservation: {retention['preservation']}",
                  f"Removal eligibility: {retention['removal_eligibility']}",
                  f"Source removed: {'yes' if retention['removed'] else 'no'}", ""])
    for name, target in report["links"].items():
        if target is not None:
            lines.append(f"{name.title()}: {_link(name, target)}")
    lines.extend(["", f"Transcript: {_link('transcript.md', report['transcript']['path'])}",
                  f"Transcript SHA-256: `{report['transcript']['sha256']}`", "",
                  f"Next action: {_escape(report['next_action'])}", ""])
    return "\n".join(lines)


def build_document_report(job: dict, root: Path) -> dict:
    """Read and verify evidence, deriving the complete report without writing files."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unsafe report root")
    fields = ("schema_version", "job_id", "created_at", "source", "selected_pages", "status",
              "usage", "limits", "links", "source_retention", "next_action")
    report = {field: copy.deepcopy(job[field]) for field in fields}
    pages = []
    for original in job["pages"]:
        page = copy.deepcopy(original)
        if page["source_sha256"] != job["source"]["sha256"]:
            raise ValueError("Report page source identity mismatch")
        if page.get("review") is not None:
            validate_review(page["review"], page)
            decision = next(item for item in page["review"]["decisions"] if item["page_id"] == page["page_id"])
            if (decision["selected_attempt"] != page["selected_attempt"]
                    or decision["disposition"] != page["disposition"]):
                raise ValueError("Page selection does not match its review receipt")
        elif page["disposition"] in {"reviewed_text", "reviewed_blank"}:
            raise ValueError("Reviewed page requires a bound review receipt")
        contents = {}
        for attempt in page["attempts"]:
            attempt.pop("text", None)
            if attempt["raw_artifact"] is not None:
                contents[attempt["attempt_id"]] = _artifact_bytes(root, attempt["raw_artifact"], attempt["raw_sha256"])
        selected = [item for item in page["attempts"] if item["attempt_id"] == page["selected_attempt"]]
        page["selected_output"] = None
        if page["selected_attempt"] is not None:
            if len(selected) != 1 or selected[0]["outcome"] != "completed":
                raise ValueError("Selected report output must be one completed attempt")
            chosen = selected[0]
            if chosen["attempt_id"] not in contents:
                raise ValueError("Selected report output has no raw artifact")
            try:
                text = contents[chosen["attempt_id"]].decode("utf-8")
            except UnicodeError as exc:
                raise ValueError("Selected output is not UTF-8") from exc
            page["selected_output"] = {"attempt_id": chosen["attempt_id"],
                                       "path": chosen["raw_artifact"], "sha256": chosen["raw_sha256"],
                                       "format": chosen["format"], "text": text}
        page["source_link"] = f"{job['source']['path']}#page={page['page_number']}"
        pages.append(page)
    report["pages"] = pages
    report["counts"] = {"source_pages": job["source"]["page_count"],
                        "selected_pages": len(job["selected_pages"]),
                        "processed_pages": sum(bool(page["attempts"]) for page in pages),
                        "selected_outputs": sum(page["selected_output"] is not None for page in pages),
                        "unresolved_pages": sum(page["disposition"] not in {"reviewed_text", "reviewed_blank"}
                                                for page in pages)}
    transcript = render_transcript(report).encode("utf-8")
    report["transcript"] = {"path": "transcript.md", "sha256": hashlib.sha256(transcript).hexdigest()}
    return report


def write_document_report(job: dict, root: Path) -> dict:
    """Verify evidence, then atomically publish JSON and its derived renderings.

    The caller validates the immutable job/source checkpoint. Every raw reference
    is checked here; incomplete attempt evidence is retained but cannot be selected.
    Each file is atomic. A crash between files is detected by re-rendering JSON.
    """
    root = Path(root)
    report = build_document_report(job, root)
    transcript = render_transcript(report).encode("utf-8")
    document = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    restored = json.loads(document)
    markdown = render_document_report(restored).encode("utf-8")
    if render_transcript(restored).encode("utf-8") != transcript:
        raise ValueError("Transcript did not survive document serialization")
    atomic_bytes(root / "transcript.md", transcript)
    atomic_bytes(root / "report.md", markdown)
    atomic_bytes(root / "document.json", document)
    verify_document_report(root)
    return report


def verify_document_report(root: Path) -> dict:
    """Check renderings and every linked raw hash against authoritative JSON."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unsafe report root")
    for name in ("document.json", "transcript.md", "report.md"):
        if (root / name).is_symlink():
            raise ValueError("Unsafe derived report path")
    try:
        report = json.loads((root / "document.json").read_bytes())
        transcript = (root / "transcript.md").read_bytes()
        if (transcript != render_transcript(report).encode("utf-8")
                or hashlib.sha256(transcript).hexdigest() != report["transcript"]["sha256"]
                or (root / "report.md").read_bytes() != render_document_report(report).encode("utf-8")):
            raise ValueError("Derived document report does not match document.json")
        for page in report["pages"]:
            for attempt in page["attempts"]:
                if attempt["raw_artifact"] is not None:
                    _artifact_bytes(root, attempt["raw_artifact"], attempt["raw_sha256"])
            selected = page["selected_output"]
            if selected is not None:
                raw = _artifact_bytes(root, selected["path"], selected["sha256"])
                if raw != selected["text"].encode("utf-8"):
                    raise ValueError("Selected text differs from raw artifact bytes")
        return report
    except (OSError, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError("Invalid document report") from exc
