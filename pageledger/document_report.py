"""Byte-faithful document evidence with Markdown derived from document.json."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from . import corrections
from .checkpoint import atomic_bytes
from .image_source import page_target
from .processing_policy import (
    _DISAGREEMENTS,
    _HOLD_ORDER,
    BLANK_REFUTING_POLICIES,
    ENGINE_AGREEMENT_THRESHOLD,
    HOLD_POLICIES,
    LADDER_POLICIES,
    WITNESS_POLICIES,
    _attempt_holds,
    hold_causes,
    inherited_layers,
    selected_clean_comparisons,
    validate_review,
    warning_holds,
)
from .wording import ADVICE, BLANK_WITH_TEXT, warning_code
from .wording import hold as hold_words
from .wording import reviewed as reviewed_words
from .wording import warning as warning_words

_CURRENT_REPORT_FORMAT = "0.9"
# 0.6 names the source by a path relative to the job; 0.5.1 kept the absolute path.
# 0.9 is written for a reader: what happened and what to do first, the record last.
_REPORT_FORMATS = {"0.5.1", "0.6", _CURRENT_REPORT_FORMAT}
_RELATIVE_SOURCE_FORMATS = {"0.6", _CURRENT_REPORT_FORMAT}

_STAGE_LABELS = {
    "local_text": "Local text",
    "local_ocr": "Local OCR",
    "second_ocr": "Second OCR",
    "local_image": "Local model",
    "image": "Image extraction",
    "second_opinion": "Second opinion",
}

# Warnings that hold nothing and would otherwise go unseen in the report.
_NOTED_WARNINGS = ("multi_column_rows", "render_upsampled")


def _stage_accounting(job: dict, pages: list[dict]) -> list[dict]:
    policy = job.get("policy", {})
    configured = [name for name in _STAGE_LABELS if policy.get(name) is not None]
    result = []
    for stage in configured:
        count = sum(attempt["stage"] == stage for page in pages for attempt in page["attempts"])
        reason = None
        if count == 0:
            limits = job["limits"]
            if not job["selected_pages"]:
                reason = "no pages were selected"
            elif limits.get("max_attempt_pages") == 0:
                reason = "the attempt limit is 0"
            elif stage == "local_image" and limits.get("max_local_image_pages") == 0:
                reason = "its page limit is 0"
            elif stage in {"image", "second_opinion"} and limits.get("max_image_pages") == 0:
                reason = "the image page limit is 0"
            elif job["status"] == "paused_budget":
                budget = str(job.get("halt_reason", "")).removeprefix("budget:")
                if budget == "max_attempt_pages":
                    reason = "the attempt limit was reached"
                elif budget == "max_local_image_pages":
                    reason = "the local image page limit was reached"
                elif budget == "max_image_pages":
                    reason = "the image page limit was reached"
                elif budget:
                    reason = f"the {budget} budget was reached"
            elif (
                stage == "local_image"
                and limits.get("max_local_image_pages") is not None
                and job["usage"].get("local_image_calls", 0) >= limits["max_local_image_pages"]
            ):
                reason = "its page limit was reached"
            elif (
                stage in {"image", "second_opinion"}
                and limits.get("max_image_pages") is not None
                and job["usage"].get("image_calls", 0) >= limits["max_image_pages"]
            ):
                reason = "the image page limit was reached"
            elif job["status"] == "halted" and job.get("halt_reason"):
                reason = "the job halted before this stage"
            else:
                reason = "no page met its trigger"
        result.append({"stage": stage, "pages_read": count, "reason": reason})
    return result


_DISPOSITION_LABELS = {
    "unreviewed_text": "Text selected; source review pending",
    "coverage_defect": "Possible missing or incomplete content",
    "low_confidence": "The engine was unsure of some words",
    "numeric_column_conflict": "Numbers need checking",
    "engine_disagreement": "Engines disagree",
    "numeric_disagreement": "Engines read numbers differently",
    "unconfirmed_model_output": "Model output not confirmed by another engine",
    "contested": "Model and literal readings differ; check them against the page",
    "adjudicated_text": "Every difference from the literal reading settled; not read by a person",
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
        chunks.append(
            _link("Source page" + _image_name(report, page), page["source_link"]) + "\n\n"
        )
        selected = page["selected_output"]
        if selected is None:
            chunks.append(f"[No selected text: {page['disposition']}.]\n\n")
        else:
            label = (
                f"Edition of attempt {selected['attempt_id']}"
                if selected.get("edition")
                else f"Correction by a person of attempt {selected['attempt_id']}"
                if selected.get("correction")
                else f"Selected attempt {selected['attempt_id']}"
            )
            chunks.append(_link(label, selected["path"]) + "\n\n")
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
    correction = selected.get("correction")
    if correction is None:
        return _link(label, selected["path"])
    shown = _link(f"Correction by a person of {label}", selected["path"])
    made = f"; made by {_escape(correction['reviewer'])} at {_escape(correction['reviewed_at'])}"
    revised = ""
    if correction["from_path"].startswith("corrections/"):
        earlier = corrections.author(
            page["review_history"], page["page_id"], correction["from_sha256"]
        )
        who = f"the correction by {earlier['reviewer']} at {earlier['reviewed_at']}"
        revised = f", revising {_link(who, correction['from_path'])}"
    return f"{shown} ({corrections.describe(correction['words'])}{made}{revised})"


def _agreement(page: dict, holds_for: dict[str, str], layer_witness: bool = False) -> str:
    selected = page.get("selected_attempt")
    if selected is None:
        return "not compared"
    comparisons = selected_clean_comparisons(page, selected, holds_for, layer_witness=layer_witness)
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


def _recorded_concerns(page: dict, holds_for: dict[str, str], layer_witness: bool = False) -> str:
    reasons = page.get("review_reasons") or []
    if not reasons:
        return "None recorded"
    concerns = []
    for reason in reasons:
        evidence = []
        if reason in _DISAGREEMENTS and page["selected_attempt"] is not None:
            comparisons = selected_clean_comparisons(
                page, page["selected_attempt"], holds_for, layer_witness=layer_witness
            )
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
        if reason == "contested":
            contested = page["contested"]
            evidence.append(
                f"{contested['open']} open of {sum(contested['kinds'].values())} "
                f"in {_link('spans', contested['artifact'])}"
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
    if not any("triggers" in page for page in report["pages"]) and not report.get(
        "stage_accounting"
    ):
        return []
    rows = []
    for stage, label in _STAGE_LABELS.items():
        attempts = [a for page in report["pages"] for a in page["attempts"] if a["stage"] == stage]
        accounting = next(
            (item for item in report.get("stage_accounting", []) if item["stage"] == stage),
            None,
        )
        if not attempts and accounting is None:
            continue
        if not attempts:
            assert accounting is not None
            rows.append(f"| {label} | 0 ({_escape(accounting['reason'])}) | not reported | 0 |")
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


def _contest_accounting(report: dict) -> list[str]:
    contested = [page["contested"] for page in report["pages"] if page.get("contested")]
    if not contested:
        return []
    kinds: Counter = Counter()
    for item in contested:
        kinds.update(item["kinds"])
    listed = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items())) or "none"
    by_rule = sum(item["by_rule"] for item in contested)
    by_review = sum(item["by_review"] for item in contested)
    open_spans = sum(item["open"] for item in contested)
    held = sum(item["open"] > 0 for item in contested)
    superseded = sum(len(item["superseded"]) for item in contested)
    return [
        f"Contested spans on {len(contested)} pages: {listed}. Settled by rule: {by_rule}; "
        f"by adjudication: {by_review}; open: {open_spans}, holding {held} pages. "
        f"Adjudicated pages: {report['counts']['adjudicated_pages']}. "
        f"Holds set aside by contest: {superseded}.",
        "",
    ]


# The job records the command's earlier name; the report names the one people type.
_WRITER_NAMES = {"review-job": "review --apply"}


def _relocation_lines(report: dict) -> list[str]:
    """Changes recorded on a copy of the job, away from the directory it was written in."""
    return [
        line
        for item in report.get("relocations", [])
        for line in (
            f"Changed elsewhere: {_WRITER_NAMES.get(item['command'], item['command'])} "
            f"on {item['at']} in "
            f"{_escape(item['root'])}, not where this job was written.",
            "",
        )
    ]


def _snapshot_lines(source: dict) -> list[str]:
    snapshot = source.get("iiif")
    if not snapshot:
        return []
    status = (
        "complete"
        if snapshot["status"] == "complete"
        else "incomplete: canvases that were never fetched are not in this job"
    )
    return [
        f"IIIF snapshot of {_escape(snapshot['requested_url'])}, {status}.",
        f"Fetch record SHA-256: `{snapshot['sha256']}`",
    ]


def _image_name(report: dict, page: dict) -> str:
    """For page images, the file a page was read from, after its number."""
    source = report["source"]
    if source.get("kind") != "page_images" or not source.get("images"):
        return ""
    return f" ({source['images'][page['page_number'] - 1]['name']})"


# -- Report format 0.9: written for a reader -----------------------------------

# Stands for the job's folder in commands the report suggests; the terminal shows the path.
JOB_DIR = "JOB_DIR"
_COSTLESS = "nothing, since no page was read"
# Holds the policy places on a reading, rather than the reading's own record.
_PLACED_BY_POLICY = {"layer_outvoted", "reader_short", "letters_lost"}


def is_pending(page: dict, report: dict) -> bool:
    """Never read, or waiting for a stage of a job that has not finished."""
    if page["disposition"] == "pending":
        return True
    configured = {item["stage"] for item in report.get("stage_accounting", [])}
    return (
        report["status"] != "completed"
        and page.get("review") is None
        and page.get("next_action") in configured
    )


def needs_person(page: dict, report: dict) -> bool:
    """Read, not reviewed, and held or without text.

    The one definition of a page that needs a person: the report, `inspect`, the lines
    `process` prints and the review view all count with it.
    """
    return not is_pending(page, report) and _needs_person(page)


def _page_numbers(pages: list[dict]) -> str:
    runs: list[list[int]] = []
    for number in sorted(page["page_number"] for page in pages):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def _pages(pages: list[dict]) -> str:
    count = len(pages)
    return f"{count} page{'' if count == 1 else 's'} ({_page_numbers(pages)})"


def _grouped(pages: list[dict], keys: Callable[[dict], list[str]]) -> list[tuple[str, list[dict]]]:
    """Pages under each key, the key with most pages first, then in hold order."""
    groups: dict[str, list[dict]] = {}
    for page in pages:
        for key in keys(page):
            groups.setdefault(key, []).append(page)
    order = {code: index for index, code in enumerate(_HOLD_ORDER)}
    return sorted(groups.items(), key=lambda item: (-len(item[1]), order.get(item[0], 99), item[0]))


def _held_for(page: dict) -> list[str]:
    return list(dict.fromkeys(page.get("review_reasons") or [page["disposition"]]))


def _group_keys(page: dict) -> list[str]:
    """A page's hold reasons, with a blank candidate that has text grouped on its own."""
    return [
        f"{reason}:text" if reason == "blank_candidate" and _has_text(page) else reason
        for reason in _held_for(page)
    ]


def _decision_of(page: dict) -> dict:
    return next(item for item in page["review"]["decisions"] if item["page_id"] == page["page_id"])


def _status_line(report: dict, job: str) -> str:
    status = report["status"]
    action = _escape(report["next_action"]).replace(JOB_DIR, job)
    if status == "completed":
        return "Status: finished. Every stage this job is configured to run has run."
    if status == "processing":
        return (
            "Status: not finished. The job stopped before all its stages had run, because it "
            "was interrupted or is still running."
        )
    if status == "paused_budget":
        return f"Status: paused at a limit. {action}"
    return f"Status: halted. {action}"


def summary_lines(report: dict, job: str = JOB_DIR) -> list[str]:
    """What happened and what to do now: the report's opening, which `process` also prints."""
    pages, counts = report["pages"], report["counts"]
    pending = [page for page in pages if is_pending(page, report)]
    held = [page for page in pages if needs_person(page, report)]
    reviewed = [page for page in pages if page.get("review") is not None]
    settled = {id(page) for page in [*pending, *held, *reviewed]}
    clean = [page for page in pages if id(page) not in settled]
    name = Path(report["source"]["path"]).name
    total = counts["source_pages"]
    selected = counts["selected_pages"]
    of = f"{selected} of its {total} pages" if total is not None else f"{selected} pages"
    lines = [
        _status_line(report, job),
        "",
        f"Source: {_escape(name)}; {of} selected, {counts['processed_pages']} read.",
        "",
    ]
    if held:
        groups = _grouped(held, _group_keys)
        several = sum(len(items) for _, items in groups) > len(held)
        lines.append(
            f"- Need a person: {_pages(held)}"
            + (", for these reasons (a page can have more than one):" if several else ":")
        )
        lines += [
            f"  - {_hold_label(items[0], code.split(':')[0])}: {_pages(items)}."
            for code, items in groups
        ]
    for code, items in _grouped(clean, lambda page: [page["disposition"]]):
        lines.append(f"- {hold_words(code)}: {_pages(items)}.")
    for code, items in _grouped(reviewed, lambda page: [_decision_of(page)["disposition"]]):
        lines.append(f"- {reviewed_words(code)}: {_pages(items)}.")
    for stage, items in _grouped(pending, lambda page: [str(page.get("next_action"))]):
        waiting = (
            "Not read yet"
            if stage not in _STAGE_LABELS
            else f"Not finished: waiting for the {_stage_label(stage)} stage"
        )
        lines.append(f"- {waiting}: {_pages(items)}.")
    if not pages:
        lines.append("- No page was read.")
    step = _next_step(report, job, held)
    if step:
        lines += ["", step]
        if job == JOB_DIR and JOB_DIR in step:
            lines.append(f"{JOB_DIR} stands for the folder that holds this report.")
    return lines


def _next_step(report: dict, job: str, held: list[dict]) -> str | None:
    status = report["status"]
    stem = Path(report["source"]["path"]).stem
    review = f"`pageledger review {job} --out {shlex.quote(stem + '-review')}`"
    those = "the page that needs" if len(held) == 1 else f"the {len(held)} pages that need"
    if status == "processing":
        return f"Next: `pageledger resume {job}` finishes the job."
    if held:
        later = ", for the pages read so far" if status != "completed" else ""
        return f"Next{later}: {review} shows {those} a person beside the page images."
    if status != "completed":
        return None
    if any(
        _decision_of(page)["disposition"] == "source_defect"
        for page in report["pages"]
        if page.get("review")
    ):
        return f"Next: {_escape(report['next_action'])}"
    export = f"`pageledger export {job} --format md --out {shlex.quote(stem + '.md')}`"
    return f"Next: {export} writes the selected text out."


def _layer(attempt: dict) -> bool:
    return any(warning_code(item) == "ocr_text_layer" for item in attempt.get("warnings") or [])


def _reading(page: dict, attempt_id: str, *, link: bool = True) -> str:
    attempt = next((item for item in page["attempts"] if item["attempt_id"] == attempt_id), None)
    if attempt is None:
        return f"reading {_escape(attempt_id)}"
    name = _stage_label(attempt["stage"])
    if _layer(attempt):
        name += " (the PDF's own text, an earlier OCR)"
    path = attempt.get("raw_artifact")
    shown = _link(attempt_id, path) if path and link else f"`{_escape(attempt_id)}`"
    return f"the {name} reading {shown}"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _causes(page: dict, report: dict) -> list[dict]:
    """Holds on the page's readings, leaving out a layer this job's policy set aside."""
    witness = report.get("hold_policy") in WITNESS_POLICIES
    aside = set() if witness else inherited_layers(page["attempts"])
    return [item for item in page.get("hold_causes", []) if item["attempt_id"] not in aside]


def _held_readings(page: dict, report: dict, *, derived: bool = True) -> set[str]:
    """Readings with a hold of their own. Without ``derived``, only the holds a reading's
    own record files, which is what jobs before hold policy 0.9 compared against."""
    if "hold_causes" not in page and report.get("report_format") != _CURRENT_REPORT_FORMAT:
        # Reports in older formats record no causes: their warnings still say.
        holds = warning_holds(report)
        return {item["attempt_id"] for item in page["attempts"] if _attempt_holds(item, holds)}
    return {
        item["attempt_id"]
        for item in page.get("hold_causes", [])
        if derived or item["cause"] not in _PLACED_BY_POLICY
    }


def _counted(page: dict, report: dict) -> list[dict]:
    """The comparisons of the selected reading that the job weighed, as ``assess_page`` does."""
    selected = page.get("selected_attempt")
    if selected is None or page.get("contested") or report.get("hold_policy") not in HOLD_POLICIES:
        return []
    witness = report.get("hold_policy") in WITNESS_POLICIES
    layers = inherited_layers(page["attempts"])
    held = _held_readings(page, report, derived=witness)
    unusable = {
        item["attempt_id"]
        for item in page["attempts"]
        if item.get("outcome") != "completed" or not item.get("raw_artifact")
    }
    counted = []
    for item in page.get("comparisons", []):
        pair = (item["left_attempt"], item["right_attempt"])
        if selected in pair:
            other = pair[1] if pair[0] == selected else pair[0]
            if other not in held | unusable and (witness or other not in layers):
                counted.append(item)
    if witness and any(
        item["agreement_ratio"] >= ENGINE_AGREEMENT_THRESHOLD
        for item in counted
        if not {item["left_attempt"], item["right_attempt"]} & layers
    ):
        # Two of the job's own engines agree: the layer that disagrees with them is outvoted.
        counted = [
            item for item in counted if not {item["left_attempt"], item["right_attempt"]} & layers
        ]
    return counted


def _hold_evidence(page: dict, reason: str, report: dict) -> list[str]:
    evidence = []
    if reason in _DISAGREEMENTS:
        for item in _counted(page, report):
            pair = (
                f"{_sentence(_reading(page, item['left_attempt']))} and "
                f"{_reading(page, item['right_attempt'])}"
            )
            differences = len(item.get("number_differences") or [])
            if (
                reason == "engine_disagreement"
                and item["agreement_ratio"] < ENGINE_AGREEMENT_THRESHOLD
            ):
                evidence.append(
                    f"{pair} share {item['agreement_ratio']:.0%} of their words; the job "
                    f"requires {ENGINE_AGREEMENT_THRESHOLD:.0%}."
                )
            if reason == "numeric_disagreement" and differences:
                evidence.append(
                    f"{pair} read {differences} number{'' if differences == 1 else 's'} differently."
                )
    if reason == "contested":
        contested = page["contested"]
        evidence.append(
            f"{contested['open']} of {sum(contested['kinds'].values())} differences are open; "
            f"they are listed in {_link('the contested spans', contested['artifact'])}."
        )
    for item in _causes(page, report):
        if item["hold"] != reason:
            continue
        who = _reading(page, item["attempt_id"])
        cause = item["cause"]
        if cause == "letters_lost":
            evidence.append(
                f"{_sentence(who)} spells {item['yat_words']} words with ѣ where another reading "
                f"of the page spells {item['most_yat_words']}: it lost the pre-reform letters."
            )
        elif cause == "layer_outvoted":
            evidence.append(
                f"Two later readings agree with each other better than with {who}, so the "
                "text layer is the suspect."
            )
        elif cause == "reader_short":
            evidence.append(
                f"{_sentence(who)} has under half the words of the OCR reading: it left text out."
            )
        elif cause == "alignment":
            evidence.append(f"The table check of {who} found it.")
        elif cause in {"empty_reading", "empty_text"} and reason == "blank_candidate":
            evidence.append(_blank_evidence(page, item["attempt_id"], report))
        else:
            evidence.append(f"`{_escape(cause)}` on {who}: {warning_words(cause)}")
    last = page["attempts"][-1] if page["attempts"] else None
    if last is not None and reason in {"provider_failure", "outcome_unknown"}:
        who = _sentence(_reading(page, last["attempt_id"]))
        failure = (last.get("failure") or {}).get("code")
        evidence.append(
            f"{who} failed ({_escape(failure)})." if failure else f"{who} has no saved result."
        )
    if reason == "unconfirmed_model_output" and not evidence:
        evidence.append("No reading by another engine, with no hold of its own, agrees with it.")
    if not evidence and reason not in {"provider_failure", "outcome_unknown"}:
        evidence.append("The job recorded this hold; no single reading carries it.")
    return evidence


def _blank_evidence(page: dict, attempt_id: str, report: dict) -> str:
    attempt = next(item for item in page["attempts"] if item["attempt_id"] == attempt_id)
    line = f"{_sentence(_reading(page, attempt_id))} came back empty"
    if attempt["stage"] == "local_text":
        line += " (a scanned PDF page has no text of its own)"
    line += "."
    selected = page.get("selected_output") or {}
    words = len((selected.get("text") or "").split())
    if words and selected.get("attempt_id") != attempt_id:
        if report.get("hold_policy") in BLANK_REFUTING_POLICIES:
            line += (
                f" The selected reading has {words} words, but no reading by an engine that "
                "is not generative, with no hold of its own, confirms that the page has text."
            )
        else:
            line += (
                f" The selected reading has {words} words; under this job's hold policy an "
                "empty reading holds the page until a person reviews it."
            )
    return line


def _comparisons(page: dict, report: dict) -> list[str]:
    """Every comparison the job recorded on the page, with what it made of it."""
    counted = [id(item) for item in _counted(page, report)]
    witness = report.get("hold_policy") in WITNESS_POLICIES
    held = _held_readings(page, report, derived=witness)
    layers = inherited_layers(page["attempts"])
    reasons = page.get("review_reasons") or []
    selected = page.get("selected_attempt")
    lines = []
    for item in page.get("comparisons", []):
        ratio = item["agreement_ratio"]
        differences = len(item.get("number_differences") or [])
        figure = f"{ratio:.0%} of words agree" + (
            f", {differences} number{'' if differences == 1 else 's'} differ" if differences else ""
        )
        pair = (item["left_attempt"], item["right_attempt"])
        other = pair[1] if pair[0] == selected else pair[0]
        if id(item) in counted:
            verdict = (
                "counted as agreement"
                if ratio >= ENGINE_AGREEMENT_THRESHOLD
                else "counted as disagreement"
                if "engine_disagreement" in reasons
                else "below 60%, but the page is not held for it"
            )
        elif selected not in pair:
            verdict = "neither is the selected reading"
        elif page.get("contested"):
            verdict = "not counted: the contested spans compare these readings word by word"
        elif report.get("hold_policy") not in HOLD_POLICIES:
            verdict = "not counted: this job was written before comparisons could hold a page"
        elif other in held:
            verdict = f"not counted, because {_reading(page, other, link=False)} is held itself"
        elif other in layers and not witness:
            verdict = (
                "not counted: this job's hold policy sets an earlier OCR in the PDF aside "
                "once the job's own OCR has read the page"
            )
        elif other in layers or selected in layers:
            verdict = "not counted: two of the job's own engines agree, which outweighs the layer"
        else:
            verdict = "not counted"
        lines.append(
            f"{_sentence(_reading(page, pair[0], link=False))} and "
            f"{_reading(page, pair[1], link=False)}: {figure}; {verdict}."
        )
    return lines


def _short_comparison(page: dict, report: dict) -> str:
    counted = _counted(page, report)
    if not counted:
        return "none counted" if page.get("comparisons") else "none recorded"
    ratio = min(item["agreement_ratio"] for item in counted)
    below = ratio < ENGINE_AGREEMENT_THRESHOLD
    return f"{ratio:.0%} of words agree" + (" (below the job's 60%)" if below else "")


def _selected_words(page: dict) -> str:
    selected = page.get("selected_output")
    if selected is None:
        return "none"
    if selected.get("correction") is not None:
        return _current_output(page)
    if not any(item["attempt_id"] == selected["attempt_id"] for item in page["attempts"]):
        raise ValueError("selected output attempt is missing from document report")
    reading = _reading(page, selected["attempt_id"])
    if selected.get("edition"):
        return f"{reading}, with its settled differences applied ({_link('edition', selected['path'])})"
    return reading


def _held_words(page: dict) -> str:
    return "; ".join(_hold_label(page, reason) for reason in _held_for(page))


def _has_text(page: dict) -> bool:
    return bool(((page.get("selected_output") or {}).get("text") or "").split())


def _hold_label(page: dict, reason: str) -> str:
    """A hold in plain words; a blank candidate whose selected reading has text says so."""
    if reason == "blank_candidate" and _has_text(page):
        return f"{BLANK_WITH_TEXT} (`blank_candidate`)"
    return hold_words(reason)


def _review_words(page: dict) -> str:
    if page.get("review") is None:
        return "not checked by a person"
    decision = _decision_of(page)
    return (
        f"{reviewed_words(decision['disposition'])}, by {_escape(decision['reviewer'])} at "
        f"{_escape(decision['reviewed_at'])}: {_escape(decision['reason'])}"
    )


def _held_page(page: dict, report: dict) -> list[str]:
    lines = [
        f"### Page {_link(str(page['page_number']) + _image_name(report, page), page['source_link'])}",
        "",
    ]
    for reason in _held_for(page):
        lines.append(f"- {_hold_label(page, reason)}.")
        lines += [f"  - {line}" for line in _hold_evidence(page, reason, report)]
    lines.append(f"- Selected text: {_selected_words(page)}.")
    comparisons = _comparisons(page, report)
    if comparisons:
        lines.append("- Comparisons the job recorded (they do not clear the holds above):")
        lines += [f"  - {line}" for line in comparisons]
    else:
        lines.append("- Comparisons: none recorded.")
    lines.append("")
    return lines


def _notes(report: dict) -> list[str]:
    """Warnings that hold nothing, once each, with the pages they are on."""
    holds = warning_holds(report)
    found: dict[str, list[dict]] = {}
    for page in report["pages"]:
        codes = {
            code
            for attempt in page["attempts"]
            for code in map(warning_code, attempt.get("warnings") or [])
            if code and code not in holds and code not in _HOLD_ORDER
        }
        for code in codes:
            found.setdefault(code, []).append(page)
    lines = []
    for code, pages in sorted(found.items(), key=lambda item: (-len(item[1]), item[0])):
        line = f"- `{_escape(code)}` on {_pages(pages)}: {warning_words(code)}"
        if code in ADVICE:
            line += f" What to do: {ADVICE[code]}"
        lines.append(line)
    return lines


def _usage_lines(report: dict) -> list[str]:
    usage = report["usage"]
    read = usage["attempt_pages"]
    if read == 0:
        tokens, cost = "none", _COSTLESS
    else:
        tokens = "not reported" if usage["tokens"] is None else str(usage["tokens"])
        if usage.get("tokens_known") is False:
            tokens = f"unknown: not every engine reported them (reported: {usage['tokens']})"
        if usage["cost_known"]:
            cost = f"${usage['cost_usd']}, as the engines reported it"
        else:
            cost = "unknown: not every engine reported a cost"
            if usage.get("known_cost_usd"):
                cost += f" (reported: ${usage['known_cost_usd']})"
    calls = f"{usage['image_calls']}"
    if "image_requests" in usage:
        calls += f" ({usage['image_requests']} image requests)"
    lines = [
        f"- Readings made: {read} (one per page and stage).",
        f"- Calls to an image model: {calls}.",
    ]
    if "local_image_calls" in usage:
        lines.append(f"- Readings by a local image model: {usage['local_image_calls']}.")
    lines += [f"- Tokens: {tokens}.", f"- Cost: {cost}."]
    return lines


def _attempt_line(page: dict, attempt: dict) -> str:
    path = attempt["raw_artifact"]
    evidence = _link(attempt["attempt_id"], path) if path else _escape(attempt["attempt_id"])
    detail = ""
    failure = (attempt.get("failure") or {}).get("code")
    if failure:
        detail += f"; failure {_escape(failure)}"
    codes = [code for code in map(warning_code, attempt.get("warnings") or []) if code]
    if codes:
        detail += "; warnings " + ", ".join(f"`{_escape(code)}`" for code in codes)
    tiling = attempt.get("tiling")
    if tiling:
        read = tiling["sent"] - len(tiling["failed"])
        detail += (
            f"; tiles: {tiling['tiles']} planned, {tiling['sent']} sent and paid for, {read} read"
        )
        for label, key in (("failed", "failed"), ("cut through text", "edge_cut")):
            if tiling[key]:
                detail += f", {label}: {', '.join(tiling[key])}"
    return (
        f"- Page {page['page_number']}: {evidence}; stage {_stage_label(attempt['stage'])}; "
        f"outcome {attempt['outcome']}{detail}."
    )


def _record_lines(report: dict) -> list[str]:
    source, counts = report["source"], report["counts"]
    annotations, retention = source["annotations"], report["source_retention"]
    annotation_count = "unknown" if annotations["count"] is None else str(annotations["count"])
    source_count = "unknown" if counts["source_pages"] is None else str(counts["source_pages"])
    lines = [
        "## For the record",
        "",
        "Everything below is in document.json, of which this report is a rendering.",
        "",
        f"- Job: `{_escape(report['job_id'])}`, created {report['created_at']}; hold policy "
        f"{report.get('hold_policy', 'none recorded')}; report format {report['report_format']}.",
        f"- Source: {_link(source['path'], quote(source['path'], safe='/'))}",
        f"- Source SHA-256: `{source['sha256']}`",
        *(f"- {line}" for line in _snapshot_lines(source)),
        f"- Pages: {counts['selected_pages']} selected of {source_count} in the source; "
        f"{counts['processed_pages']} read; {counts['selected_outputs']} with selected text; "
        f"{counts['unresolved_pages']} not yet reviewed as text or blank (`unresolved_pages`).",
        *(
            [f"- Pages whose text a person corrected: {counts['corrected_pages']}."]
            if "corrected_pages" in counts
            else []
        ),
        f"- PDF annotations (notes and links attached to pages): {annotations['status']} "
        f"({annotation_count}). Counting them says nothing about whether the body text or "
        "its citations were extracted completely.",
    ]
    limits = [f"{name} {value}" for name, value in report["limits"].items() if value is not None]
    lines.append(f"- Limits: {', '.join(limits) if limits else 'none set'}.")
    for item in report.get("limits_history", []):
        lines.append(
            f"- Limit raised on {item['at']}: {item['limit']} from {item['previous']} "
            f"to {item['value']}."
        )
    lines += [f"- {line}" for line in _relocation_lines(report) if line]
    lines.append("")
    stages = _escalation_accounting(report)
    if stages:
        fired = Counter(t["trigger"] for page in report["pages"] for t in page.get("triggers", []))
        climbed = ", ".join(f"`{name}` {count}" for name, count in sorted(fired.items()))
        lines += [
            "### Work by stage",
            "",
            *stages[2:-3],
            "",
            "Readings sent on to the next stage for a reason other than a hold, by trigger "
            f"(a page can be sent on more than once): {climbed or 'none'}.",
            "",
        ]
    contest = _contest_accounting(report)
    if contest:
        lines += ["### Contested readings", "", *contest]
    lines += ["### Every reading", ""]
    for page in report["pages"]:
        lines += [_attempt_line(page, attempt) for attempt in page["attempts"]]
    if not any(page["attempts"] for page in report["pages"]):
        lines.append("None.")
    lines += [
        "",
        "### Source custody",
        "",
        "PageLedger records what it knows of the source file; it does not preserve or remove it.",
        "",
        f"- Captured (the file was present when the job ran): {retention['capture']}",
        f"- Preservation elsewhere verified: {retention['preservation']}",
        f"- Assessed for removal: {retention['removal_eligibility']}",
        f"- Removed: {'yes' if retention['removed'] else 'no'}",
        "",
        "### Files",
        "",
    ]
    for name, target in report["links"].items():
        if target is not None:
            lines.append(f"- {name.title()}: {_link(name, target)}")
    lines += [
        f"- Transcript: {_link('transcript.md', report['transcript']['path'])}, SHA-256 "
        f"`{report['transcript']['sha256']}`",
        f"- Next action recorded by the job: {_escape(report['next_action'])}",
        "",
    ]
    return lines


def _render_for_reader(report: dict) -> str:
    name = Path(report["source"]["path"]).name
    pages = sorted(report["pages"], key=lambda page: page["page_number"])
    held = [page for page in pages if needs_person(page, report)]
    pending = [page for page in pages if is_pending(page, report)]
    shown = {id(page) for page in [*held, *pending]}
    others = [page for page in pages if id(page) not in shown]
    lines = [f"# {_escape(name)}: document job report", "", *summary_lines(report), ""]
    if held:
        lines += [
            "## Pages that need a person",
            "",
            "Each page's holds are listed with the evidence for them. Agreement between "
            "engines is evidence, not proof: engines can share a mistake.",
            "",
        ]
        for page in held:
            lines += _held_page(page, report)
    if pending:
        lines += ["## Pages not finished", ""]
        for page in pending:
            stages = ", ".join(_stage_label(a["stage"]) for a in page["attempts"])
            read = f"read so far by {stages}" if stages else "not read by any stage yet"
            waiting = (
                f"waiting for the {_stage_label(page['next_action'])} stage"
                if page.get("next_action") in _STAGE_LABELS
                else "not read yet"
            )
            line = (
                f"- Page {_link(str(page['page_number']) + _image_name(report, page), page['source_link'])}: "
                f"{waiting}; {read}."
            )
            if page.get("selected_output") is not None:
                line += f" Text so far: {_selected_words(page)}."
            if page.get("review_reasons"):
                line += f" Held so far for: {_held_words(page)}."
            lines.append(line)
        lines += [
            "",
            "What is shown for these pages is provisional: the stages still to run may "
            "select another reading, and add or settle holds.",
            "",
        ]
    notes = _notes(report)
    if notes:
        lines += [
            "## Worth knowing",
            "",
            "These warnings hold no page, but they bear on how far to trust the text.",
            "",
            *notes,
            "",
        ]
    if others:
        lines += [
            "## Other pages",
            "",
            "Pages that nothing holds, and pages a person has reviewed, with any holds the "
            "review settled. Agreement between engines is evidence, not proof: engines can "
            "share a mistake.",
            "",
            "| Page | Selected text | Engine comparison | Held for | Review |",
            "| --- | --- | --- | --- | --- |",
        ]
        for page in others:
            lines.append(
                f"| {_link(str(page['page_number']) + _image_name(report, page), page['source_link'])} | "
                f"{_selected_words(page)} | {_short_comparison(page, report)} | "
                f"{_held_words(page) if page.get('review_reasons') else 'nothing'} | "
                f"{_review_words(page)} |"
            )
        lines.append("")
    lines += ["## Usage and cost", "", *_usage_lines(report), "", *_record_lines(report)]
    return "\n".join(lines)


def render_document_report(report: dict) -> str:
    """Render a report, retaining the 0.5.0 format when its marker is absent."""
    current = "report_format" in report
    if current and report["report_format"] not in _REPORT_FORMATS:
        raise ValueError(f"Unsupported document report format: {report['report_format']}")
    if current and report["report_format"] == _CURRENT_REPORT_FORMAT:
        return _render_for_reader(report)
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
        *_snapshot_lines(source),
        "",
        f"Pages processed: {counts['processed_pages']}; selected for this job: {counts['selected_pages']}; full source: {source_count}.",
        f"Selected outputs: {counts['selected_outputs']}; unresolved pages: {counts['unresolved_pages']}.",
        *(
            [f"Pages whose text a person corrected: {counts['corrected_pages']}."]
            if "corrected_pages" in counts
            else []
        ),
        "",
        f"Annotations: {annotations['status']} ({annotation_count}). This inventory does not establish body extraction or citation completeness.",
        "",
        f"Attempt pages: {usage['attempt_pages']}; image calls: {usage['image_calls']}"
        + (f" ({usage['image_requests']} image requests)" if "image_requests" in usage else "")
        + f"; tokens: {tokens}; cost: {cost}.",
        "",
        *_relocation_lines(report),
        *_escalation_accounting(report),
        *_contest_accounting(report),
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
    # Under hold policy 0.9 an inherited OCR layer is a witness, and its comparison is shown.
    witness = report.get("hold_policy") in WITNESS_POLICIES
    if format_06:
        pages = sorted(pages, key=lambda page: (not _needs_person(page), page["page_number"]))
    for page in pages:
        if format_06:
            row = (
                f"| {_link(str(page['page_number']) + _image_name(report, page), page['source_link'])} | "
                f"{_selected_text(page)} | {_agreement(page, holds, witness)} | "
                f"{_human_review(page)} | {_recorded_concerns(page, holds, witness)} |"
            )
        elif current:
            row = (
                f"| {_link(str(page['page_number']) + _image_name(report, page), page['source_link'])} | "
                f"{_current_output(page)} | {_review_status(page)} | "
                f"{_recorded_concerns(page, warning_holds(report), witness)} |"
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
                f"| {_link(str(page['page_number']) + _image_name(report, page), page['source_link'])} | {page['disposition']} | "
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
            noted = [w for w in attempt.get("warnings", []) if w in _NOTED_WARNINGS]
            if noted:
                detail += f"; quality warning {', '.join(noted)}"
            tiling = attempt.get("tiling")
            if tiling:
                read = tiling["sent"] - len(tiling["failed"])
                detail += (
                    f"; tiles: {tiling['tiles']} planned, {tiling['sent']} sent and paid for, "
                    f"{read} read"
                )
                for label, key in (("failed", "failed"), ("cut through text", "edge_cut")):
                    if tiling[key]:
                        detail += f", {label}: {', '.join(tiling[key])}"
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


def _apply_correction(root: Path, page: dict) -> None:
    """A person's corrected text becomes the selected text; the reading it started from is named."""
    if page.get("review") is None:
        return
    decision = _decision(page)
    correction = decision.get("correction")
    if correction is None:
        return
    selected = page["selected_output"]
    history = page.get("review_history", [])
    start = selected["path"]
    if correction["from_sha256"] != decision["output_sha256"]:
        start = corrections.author(history, page["page_id"], correction["from_sha256"])[
            "correction"
        ]["artifact"]
    made = corrections.author(history, page["page_id"], correction["sha256"])
    content = _artifact_bytes(root, correction["artifact"], correction["sha256"])
    selected.pop("edition", None)
    selected.update(
        path=correction["artifact"],
        sha256=correction["sha256"],
        text=content.decode("utf-8"),
        correction={
            "from_path": start,
            "from_sha256": correction["from_sha256"],
            "words": dict(correction["words"]),
            **{key: made[key] for key in ("reviewer", "reviewed_at", "reason")},
        },
    )


def _decision(page: dict) -> dict:
    return next(item for item in page["review"]["decisions"] if item["page_id"] == page["page_id"])


def build_document_report(
    job: dict,
    root: Path,
    *,
    report_format: str | None = _CURRENT_REPORT_FORMAT,
    include_stage_accounting: bool | None = None,
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
    for optional in ("hold_policy", "limits_history", "relocations"):
        if optional in job:
            report[optional] = copy.deepcopy(job[optional])
    if include_stage_accounting is None:
        include_stage_accounting = report_format is not None
    if include_stage_accounting:
        report["stage_accounting"] = _stage_accounting(job, job["pages"])
    if report_format is not None:
        if report_format not in _REPORT_FORMATS:
            raise ValueError(f"Unsupported document report format: {report_format}")
        report["report_format"] = report_format
    if report_format in _RELATIVE_SOURCE_FORMATS:
        # Relative to where the job was written, so the report reads the same after a move.
        report["source"]["path"] = relative_path(job["source"]["path"], Path(job.get("root", root)))
    quoted_source = quote(report["source"]["path"], safe="/")
    holds_for = warning_holds(job)
    pages = []
    for original in job["pages"]:
        page = copy.deepcopy(original)
        if report_format == _CURRENT_REPORT_FORMAT:
            # Holds that no warning explains (a reading that lost the pre-reform letters
            # another kept, say), so that the report can name them.
            causes = hold_causes(
                original,
                holds_for,
                ladder_rules=job.get("hold_policy") in LADDER_POLICIES,
                witnesses=job.get("hold_policy") in WITNESS_POLICIES,
            )
            if causes:
                page["hold_causes"] = causes
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
            edition = page.get("edition")
            reviewed = None
            if page.get("review") is not None:
                reviewed = next(
                    item["output_sha256"]
                    for item in page["review"]["decisions"]
                    if item["page_id"] == page["page_id"]
                )
            # The selected attempt with its settled spans applied, unless a person
            # reviewed the attempt's own text instead. The attempt is unchanged.
            if (
                edition is not None
                and edition["base_attempt"] == chosen["attempt_id"]
                and reviewed in (None, edition["sha256"])
            ):
                content = _artifact_bytes(root, edition["artifact"], edition["sha256"])
                page["selected_output"].update(
                    path=edition["artifact"],
                    sha256=edition["sha256"],
                    text=content.decode("utf-8"),
                    edition=True,
                )
            _apply_correction(root, page)
        path, fragment = page_target(job["source"], page["page_number"])
        page["source_link"] = quoted_source + quote(path) + fragment
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
    if any("contested" in page for page in pages):
        report["counts"]["adjudicated_pages"] = sum(
            page["disposition"] == "adjudicated_text" for page in pages
        )
    corrected = sum("correction" in (page["selected_output"] or {}) for page in pages)
    if corrected:
        report["counts"]["corrected_pages"] = corrected
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
