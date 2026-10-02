"""Byte-faithful document evidence with Markdown derived from document.json."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from .checkpoint import atomic_bytes
from .processing_policy import (
    _DISAGREEMENTS,
    ENGINE_AGREEMENT_THRESHOLD,
    _attempt_holds,
    selected_clean_comparisons,
    validate_review,
    warning_holds,
)

_CURRENT_REPORT_FORMAT = "0.6"
# 0.6 names the source by a path relative to the job; 0.5.1 kept the absolute path.
_REPORT_FORMATS = {"0.5.1", _CURRENT_REPORT_FORMAT}

_STAGE_LABELS = {
    "local_text": "Local text",
    "local_ocr": "Local OCR",
    "image": "Image extraction",
    "second_opinion": "Second opinion",
}

_DISPOSITION_LABELS = {
    "unreviewed_text": "Text selected; source review pending",
    "coverage_defect": "Possible missing or incomplete content",
    "low_confidence": "The engine was unsure of some words",
    "numeric_column_conflict": "Numbers need checking",
    "engine_disagreement": "Engines disagree",
    "numeric_disagreement": "Engines read numbers differently",
    "unconfirmed_model_output": "Model output not confirmed by another engine",
    "blank_candidate": "Candidate blank",
    "provider_failure": "Extraction failed",
    "outcome_unknown": "Extraction outcome unknown",
    "pending": "Not processed",
    "reviewed_text": "Reviewed text",
    "reviewed_blank": "Reviewed blank",
    "illustration": "Illustration",
    "handwriting": "Handwriting",
    "unreadable": "Unreadable source",
    "source_defect": "Source problem confirmed",
}


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


def relative_path(path: str, start: Path) -> str:
    """`path` relative to `start`, or only its name when it is on another Windows drive."""
    try:
        return Path(os.path.relpath(path, start)).as_posix()
    except ValueError:
        return Path(path).name


def _link(label: str, target: str) -> str:
    return f"[{_escape(label)}](<{quote(target, safe='/:#?=&%')}>)"


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def render_transcript(report: dict) -> str:
    """Serialize the transcript without trimming or normalizing extraction text."""
    chunks = [
        f"# Transcript: {_escape(report['job_id'])}\n\n",
        f"Source SHA-256: `{report['source']['sha256']}`\n\n",
    ]
    for page in report["pages"]:
        chunks.append(f"## Page {page['page_number']}\n\n")
        chunks.append(_link("Source page", page["source_link"]) + "\n\n")
        selected = page["selected_output"]
        if selected is None:
            chunks.append(f"[No selected text: {page['disposition']}.]\n\n")
        else:
            chunks.append(
                _link(f"Selected attempt {selected['attempt_id']}", selected["path"]) + "\n\n"
            )
            chunks.append(selected["text"])
            chunks.append("\n\n")
    return "".join(chunks)


def _stage_label(stage: str) -> str:
    return _STAGE_LABELS.get(stage, stage)


def _disposition_label(disposition: str) -> str:
    label = _DISPOSITION_LABELS.get(disposition, disposition)
    # Keep the durable code alongside the readable label for audit searches and
    # to avoid implying that a presentation label changed the stored decision.
    return f"{label} ({disposition})"


def _review_status(page: dict) -> str:
    review = page.get("review")
    if review is None:
        if page["disposition"] == "pending":
            return "Not processed"
        if page["disposition"] == "provider_failure":
            return "Review required: extraction failed"
        if page["disposition"] == "outcome_unknown":
            return "Review required: extraction outcome unknown"
        return "Review required"
    decision = next(item for item in review["decisions"] if item["page_id"] == page["page_id"])
    label = _disposition_label(decision["disposition"])
    source_only = decision["selected_attempt"] is None
    prefix = "Source-only review" if source_only else "Reviewed"
    return (
        f"{prefix}: {label}; reviewed by {_escape(decision['reviewer'])} at "
        f"{_escape(decision['reviewed_at'])}: {_escape(decision['reason'])}"
    )


def _current_output(page: dict) -> str:
    selected = page.get("selected_output")
    if selected is None:
        if page.get("review") is not None:
            decision = next(
                item for item in page["review"]["decisions"] if item["page_id"] == page["page_id"]
            )
            if decision["selected_attempt"] is None:
                return "No selected output (source-only review)"
        return "No selected output"
    attempt = next(
        (item for item in page["attempts"] if item["attempt_id"] == selected["attempt_id"]), None
    )
    if attempt is None:
        raise ValueError("selected output attempt is missing from document report")
    label = f"{_stage_label(attempt['stage'])} attempt {selected['attempt_id']}"
    return _link(label, selected["path"])


def _agreement(page: dict, holds_for: dict[str, str]) -> str:
    selected = page.get("selected_attempt")
    if selected is None:
        return "not compared"
    comparisons = selected_clean_comparisons(page, selected, holds_for)
    if not comparisons:
        return "not compared"
    ratio = min(item["agreement_ratio"] for item in comparisons)
    return f"{'agree' if ratio >= ENGINE_AGREEMENT_THRESHOLD else 'disagree'} ({ratio:.0%})"


def _human_review(page: dict) -> str:
    review = page.get("review")
    if review is None:
        return "not reviewed"
    decision = next(item for item in review["decisions"] if item["page_id"] == page["page_id"])
    detail = f"{_escape(decision['reviewer'])} at {_escape(decision['reviewed_at'])}"
    if decision["selected_attempt"] is None:
        detail = f"Source-only review: {detail}: {_escape(decision['reason'])}"
    return f"{detail}; {_disposition_label(decision['disposition'])}"


def _needs_person(page: dict) -> bool:
    return page.get("review") is None and bool(
        page.get("review_reasons") or page.get("selected_output") is None
    )


def _selected_text(page: dict) -> str:
    return _current_output(page) if page.get("selected_output") is not None else "none"


def _explicit_attempt_hold(attempt: dict, reason: str, holds_for: dict[str, str]) -> str | None:
    """Return explicit evidence tying a hold to one attempt, if present."""
    for warning in attempt.get("warnings") or []:
        code = warning.get("type", warning.get("code")) if isinstance(warning, dict) else warning
        if isinstance(code, str) and holds_for.get(code) == reason:
            return str(code)
    classification = (attempt.get("classification") or {}).get("type")
    classification_reason = (attempt.get("classification") or {}).get("reason")
    if isinstance(classification, str) and (
        classification != "unknown" or classification_reason != "empty_pdf_text_ambiguous"
    ):
        if holds_for.get(classification) == reason:
            return str(classification)
    if reason not in _attempt_holds(attempt, holds_for):
        return None
    # The shared policy helper has already established an alignment hold; its
    # detailed structure remains in the linked attempt evidence.
    if reason in {"coverage_defect", "numeric_column_conflict"}:
        return "alignment"
    return None


def _recorded_concerns(page: dict, holds_for: dict[str, str]) -> str:
    reasons = page.get("review_reasons") or []
    if not reasons:
        return "None recorded"
    concerns = []
    for reason in reasons:
        evidence = []
        if reason in _DISAGREEMENTS and page["selected_attempt"] is not None:
            comparisons = selected_clean_comparisons(page, page["selected_attempt"], holds_for)
            for comparison in comparisons:
                applies = (
                    comparison["agreement_ratio"] < ENGINE_AGREEMENT_THRESHOLD
                    if reason == "engine_disagreement"
                    else bool(comparison["number_differences"])
                )
                if applies:
                    evidence.append(
                        f"comparison of {_escape(comparison['left_attempt'])} and "
                        f"{_escape(comparison['right_attempt'])}"
                    )
        for attempt in page["attempts"]:
            code = _explicit_attempt_hold(attempt, reason, holds_for)
            if code is None:
                continue
            stage = _stage_label(attempt["stage"])
            path = attempt.get("raw_artifact")
            link = _link(attempt["attempt_id"], path) if path else _escape(attempt["attempt_id"])
            if reason == "blank_candidate" and code == "empty_text":
                evidence.append(f"empty text returned by {stage} attempt {link}")
            else:
                evidence.append(f"recorded on {stage} attempt {link}")
        if evidence:
            concerns.append(f"{_disposition_label(reason)}; {', '.join(evidence)}")
        else:
            concerns.append(f"{_disposition_label(reason)}; retained review concern")
    return "; ".join(concerns)


def _escalation_accounting(report: dict) -> list[str]:
    """Work done at each stage and why pages climbed; only for jobs that escalate on triggers."""
    if not any("triggers" in page for page in report["pages"]):
        return []
    rows = []
    for stage, label in _STAGE_LABELS.items():
        attempts = [a for page in report["pages"] for a in page["attempts"] if a["stage"] == stage]
        if not attempts:
            continue
        tokens = [a["usage"].get("tokens") for a in attempts]
        seconds = sum(a["usage"].get("compute_seconds") or 0 for a in attempts)
        known = sum(t for t in tokens if isinstance(t, int))
        if all(isinstance(t, int) for t in tokens):
            shown = str(known)
        elif any(isinstance(t, int) for t in tokens):
            shown = f"{known}+ (some unknown)"
        else:
            shown = "not reported"  # local engines report no tokens
        rows.append(f"| {label} | {len(attempts)} | {shown} | {round(seconds, 1)} |")
    fired = Counter(t["trigger"] for page in report["pages"] for t in page.get("triggers", []))
    climbed = ", ".join(f"{name} {count}" for name, count in sorted(fired.items())) or "none"
    return [
        "Work by stage:",
        "",
        "| Stage | Pages read | Tokens | Seconds |",
        "| --- | --- | --- | --- |",
        *rows,
        "",
        f"Climbs other than holds: {climbed}.",
        "",
    ]


def render_document_report(report: dict) -> str:
    """Render a report, retaining the 0.5.0 format when its marker is absent."""
    current = "report_format" in report
    if current and report["report_format"] not in _REPORT_FORMATS:
        raise ValueError(f"Unsupported document report format: {report['report_format']}")
    source, counts = report["source"], report["counts"]
    annotations, retention = source["annotations"], report["source_retention"]
    annotation_count = "unknown" if annotations["count"] is None else str(annotations["count"])
    source_count = "unknown" if counts["source_pages"] is None else str(counts["source_pages"])
    usage = report["usage"]
    format_06 = report.get("report_format") == "0.6"
    cost = "unknown" if not usage["cost_known"] else f"${usage['cost_usd']}"
    tokens = str(usage["tokens"]) if usage["tokens"] is not None else "unknown"
    if usage.get("tokens_known") is False:
        tokens = f"unknown (known subtotal: {tokens})"
    lines = [
        f"# Document report: {_escape(report['job_id'])}",
        "",
        f"Status: {report['status']}",
        "",
        f"Source: {_link(source['path'], quote(source['path'], safe='/'))}",
        f"Source SHA-256: `{source['sha256']}`",
        "",
        f"Pages processed: {counts['processed_pages']}; selected for this job: {counts['selected_pages']}; full source: {source_count}.",
        f"Selected outputs: {counts['selected_outputs']}; unresolved pages: {counts['unresolved_pages']}.",
        "",
        f"Annotations: {annotations['status']} ({annotation_count}). This inventory does not establish body extraction or citation completeness.",
        "",
        f"Attempt pages: {usage['attempt_pages']}; image calls: {usage['image_calls']}; tokens: {tokens}; cost: {cost}.",
        "",
        *_escalation_accounting(report),
        *(["Current page results", ""] if current else []),
        *(
            [
                f"Pages needing a person: {sum(_needs_person(page) for page in report['pages'])}",
                "",
                "| Page | Selected text | Engine agreement | Human review | Recorded concerns |",
                "| --- | --- | --- | --- | --- |",
            ]
            if format_06
            else [
                (
                    "| Page | Current output | Review status | Recorded concerns |"
                    if current
                    else "| Page | Disposition | Selected output | Review evidence |"
                ),
                "| --- | --- | --- | --- |",
            ]
        ),
    ]
    pages, holds = report["pages"], warning_holds(report)
    if format_06:
        pages = sorted(pages, key=lambda page: (not _needs_person(page), page["page_number"]))
    for page in pages:
        if format_06:
            row = (
                f"| {_link(str(page['page_number']), page['source_link'])} | "
                f"{_selected_text(page)} | {_agreement(page, holds)} | "
                f"{_human_review(page)} | {_recorded_concerns(page, holds)} |"
            )
        elif current:
            row = (
                f"| {_link(str(page['page_number']), page['source_link'])} | "
                f"{_current_output(page)} | {_review_status(page)} | "
                f"{_recorded_concerns(page, warning_holds(report))} |"
            )
        else:
            selected = page["selected_output"]
            selected_link = _link(selected["attempt_id"], selected["path"]) if selected else "none"
            reasons = ", ".join(page["review_reasons"]) or "none recorded"
            if page["review"] is not None:
                decision = next(
                    item
                    for item in page["review"]["decisions"]
                    if item["page_id"] == page["page_id"]
                )
                reasons += f"; reviewed by {decision['reviewer']} at {decision['reviewed_at']}: {decision['reason']}"
            row = (
                f"| {_link(str(page['page_number']), page['source_link'])} | {page['disposition']} | "
                f"{selected_link} | {_escape(reasons)} |"
            )
        lines.append(row)
    if format_06:
        lines.extend(["", "Engine agreement is evidence, not proof: engines can share a mistake."])
    lines.extend(["", "Attempt evidence:", ""])
    for page in report["pages"]:
        for attempt in page["attempts"]:
            path = attempt["raw_artifact"]
            evidence = (
                _link(attempt["attempt_id"], path) if path else _escape(attempt["attempt_id"])
            )
            failure = (attempt.get("failure") or {}).get("code")
            detail = f"; failure {_escape(failure)}" if failure else ""
            stage = _stage_label(attempt["stage"]) if current else attempt["stage"]
            lines.append(
                f"- Page {page['page_number']}: {evidence}; stage {stage}; "
                f"outcome {attempt['outcome']}{detail}."
            )
    lines.extend(
        [
            "",
            f"Capture: {retention['capture']}",
            f"Preservation: {retention['preservation']}",
            f"Removal eligibility: {retention['removal_eligibility']}",
            f"Source removed: {'yes' if retention['removed'] else 'no'}",
            "",
        ]
    )
    for name, target in report["links"].items():
        if target is not None:
            lines.append(f"{name.title()}: {_link(name, target)}")
    lines.extend(
        [
            "",
            f"Transcript: {_link('transcript.md', report['transcript']['path'])}",
            f"Transcript SHA-256: `{report['transcript']['sha256']}`",
            "",
            f"Next action: {_escape(report['next_action'])}",
            "",
        ]
    )
    return "\n".join(lines)


def build_document_report(
    job: dict, root: Path, *, report_format: str | None = _CURRENT_REPORT_FORMAT
) -> dict:
    """Read and verify evidence, deriving the complete report without writing files."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unsafe report root")
    fields = (
        "schema_version",
        "job_id",
        "created_at",
        "source",
        "selected_pages",
        "status",
        "usage",
        "limits",
        "links",
        "source_retention",
        "next_action",
    )
    report = {field: copy.deepcopy(job[field]) for field in fields}
    for optional in ("hold_policy", "limits_history"):
        if optional in job:
            report[optional] = copy.deepcopy(job[optional])
    if report_format is not None:
        if report_format not in _REPORT_FORMATS:
            raise ValueError(f"Unsupported document report format: {report_format}")
        report["report_format"] = report_format
    if report_format == "0.6":
        report["source"]["path"] = relative_path(job["source"]["path"], root)
    quoted_source = quote(report["source"]["path"], safe="/")
    pages = []
    for original in job["pages"]:
        page = copy.deepcopy(original)
        if page["source_sha256"] != job["source"]["sha256"]:
            raise ValueError("Report page source identity mismatch")
        if page.get("review") is not None:
            validate_review(page["review"], page)
            decision = next(
                item for item in page["review"]["decisions"] if item["page_id"] == page["page_id"]
            )
            if (
                decision["selected_attempt"] != page["selected_attempt"]
                or decision["disposition"] != page["disposition"]
            ):
                raise ValueError("Page selection does not match its review receipt")
        elif page["disposition"] in {"reviewed_text", "reviewed_blank"}:
            raise ValueError("Reviewed page requires a bound review receipt")
        contents = {}
        for attempt in page["attempts"]:
            attempt.pop("text", None)
            if attempt["raw_artifact"] is not None:
                contents[attempt["attempt_id"]] = _artifact_bytes(
                    root, attempt["raw_artifact"], attempt["raw_sha256"]
                )
        selected = [
            item for item in page["attempts"] if item["attempt_id"] == page["selected_attempt"]
        ]
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
            page["selected_output"] = {
                "attempt_id": chosen["attempt_id"],
                "path": chosen["raw_artifact"],
                "sha256": chosen["raw_sha256"],
                "format": chosen["format"],
                "text": text,
            }
        page["source_link"] = f"{quoted_source}#page={page['page_number']}"
        pages.append(page)
    report["pages"] = pages
    report["counts"] = {
        "source_pages": job["source"]["page_count"],
        "selected_pages": len(job["selected_pages"]),
        "processed_pages": sum(bool(page["attempts"]) for page in pages),
        "selected_outputs": sum(page["selected_output"] is not None for page in pages),
        "unresolved_pages": sum(
            page["disposition"] not in {"reviewed_text", "reviewed_blank"} for page in pages
        ),
    }
    transcript = render_transcript(report).encode("utf-8")
    report["transcript"] = {
        "path": "transcript.md",
        "sha256": hashlib.sha256(transcript).hexdigest(),
    }
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
    document = (json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode(
        "utf-8"
    )
    markdown = render_document_report(report).encode("utf-8")
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
        if (
            transcript != render_transcript(report).encode("utf-8")
            or hashlib.sha256(transcript).hexdigest() != report["transcript"]["sha256"]
            or (root / "report.md").read_bytes() != render_document_report(report).encode("utf-8")
        ):
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
    except (OSError, KeyError, TypeError, UnicodeError, StopIteration) as exc:
        raise ValueError("Invalid document report") from exc
