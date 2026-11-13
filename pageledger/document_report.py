"""Byte-faithful document evidence with Markdown derived from document.json."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
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
from .wording import (
    ADVICE,
    ANOTHER_READING,
    BLANK_SCAN,
    BLANK_SHORT,
    BLANK_WITH_TEXT,
    CAUSES,
    CORRECTION,
    COULD_CONFIRM,
    DOCS,
    HOLDS,
    NOT_CONFIRMED,
    ONLY_OTHER,
    REVIEWED,
    UNCONFIRMED_ADVICE,
    reading_name,
    sentence,
    warning_code,
)
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

AGREEMENT_CAVEAT = "Engine agreement is evidence, not proof: engines can share a mistake."

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


def _plain(text: str) -> str:
    """Text from outside PageLedger (a file name, a reviewer's note, a model's name) in
    the reader's format: also no raw HTML, so that a `<!--` in it hides nothing."""
    return _escape(text).replace("<", "\\<")


def fenced(text: str) -> str:
    """Page text as a Markdown code block, so that nothing in it is read as Markdown or
    HTML. The fence is longer than any run of backticks in the text, and exactly one
    line break is added before the closing fence: the text is everything between the
    two fence lines but that last line break."""
    longest = max((len(run) for run in re.findall("`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}\n"


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
        if report.get("report_format") == _CURRENT_REPORT_FORMAT:
            chunks.append(_transcript_page(report, page))
        elif selected is None:
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


def _transcript_page(report: dict, page: dict) -> str:
    selected = page["selected_output"]
    if selected is None:
        state = page["disposition"]
        return f"[No selected text: {HOLDS.get(state) or REVIEWED.get(state) or state}.]\n\n"
    name = reader_name(report, _attempt(page, selected["attempt_id"]))
    if selected.get("correction"):
        name = f"{CORRECTION} of {name}"
    elif selected.get("edition"):
        name += ", with its settled differences applied"
    return (
        _link(f"Selected reading: {name}", selected["path"]) + f"\n\n{fenced(selected['text'])}\n"
    )


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

# Stands for the job's folder in a next action the job records (a paused job's).
JOB_DIR = "JOB_DIR"
# The report is read inside the job's folder, which it cannot name after a move.
_HERE = "."
_COSTLESS = "nothing, since no page was read"
# Holds the policy places on a reading, rather than the reading's own record.
_PLACED_BY_POLICY = {"layer_outvoted", "reader_short", "letters_lost"}
# Warnings that say a reading has no letters or digits.
_EMPTY = {"empty_text", "no_text_layer"}


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
    return sorted(
        groups.items(),
        key=lambda item: (-len(item[1]), order.get(item[0].split("\x1f")[0], 99), item[0]),
    )


def _held_for(page: dict) -> list[str]:
    return list(dict.fromkeys(page.get("review_reasons") or [page["disposition"]]))


def _layer(attempt: dict) -> bool:
    return any(warning_code(item) == "ocr_text_layer" for item in attempt.get("warnings") or [])


def reader_name(report: dict, attempt: dict) -> str:
    """A reading named by what made it, from the engines the report records."""
    return reading_name(
        attempt["stage"],
        (report.get("engines") or {}).get(attempt["stage"]),
        layer=_layer(attempt),
        images=report["source"].get("kind") == "page_images",
    )


def empty_reading(attempt: dict) -> bool:
    """A reading with no letters or digits, as its warnings record."""
    return any(warning_code(item) in _EMPTY for item in attempt.get("warnings") or [])


def _has_text(page: dict) -> bool:
    return bool(((page.get("selected_output") or {}).get("text") or "").split())


def _scan(page: dict) -> bool:
    """The page's own text is empty: a scan without a text layer, or a page image."""
    return any(item["stage"] == "local_text" and empty_reading(item) for item in page["attempts"])


def shown_holds(page: dict) -> list[str]:
    """The holds to show a person. A page with text that another reason holds is not
    also called blank; the record keeps every reason the job recorded."""
    reasons = _held_for(page)
    if "blank_candidate" in reasons and len(reasons) > 1 and _has_text(page):
        reasons.remove("blank_candidate")
    return reasons


def hold_in_words(page: dict, reason: str) -> str:
    """A hold or page state in plain words, without its code."""
    if reason == "blank_candidate" and _has_text(page):
        if _scan(page):
            return BLANK_SCAN
        if any(empty_reading(attempt) for attempt in page["attempts"]):
            return BLANK_WITH_TEXT
        return BLANK_SHORT
    return HOLDS.get(reason) or REVIEWED.get(reason) or reason


def _flaw(report: dict, page: dict, item: dict, clause: str | None = None) -> str | None:
    """One recorded cause of a hold as a clause: the reading, and what is wrong with it."""
    clause = clause or CAUSES.get(item["cause"])
    attempt = next((a for a in page["attempts"] if a["attempt_id"] == item["attempt_id"]), None)
    if clause is None or attempt is None:
        return None
    name = reader_name(report, attempt)
    selected = page.get("selected_attempt")
    if selected is None or selected == item["attempt_id"]:
        return f"{name} {clause}"
    words = {"selected": reader_name(report, _attempt(page, selected)), "name": name}
    if _unconfirmed(page, item):
        completed = [a for a in page["attempts"] if a.get("outcome") == "completed"]
        other = ONLY_OTHER if len(completed) == 2 else COULD_CONFIRM
        return NOT_CONFIRMED.format(flaw=clause, other=other, **words)
    return ANOTHER_READING.format(flaw=clause, **words)


# Stands for the flaw while the sentence around it is built.
_FLAW = "\x00"


def _unconfirmed(page: dict, item: dict) -> bool:
    """A hold an inherited OCR layer's own warning placed: it holds the page only while
    nothing confirms the selected reading."""
    completed = [a for a in page["attempts"] if a.get("outcome") == "completed"]
    return (
        item["attempt_id"] != page.get("selected_attempt")
        and item["attempt_id"] in inherited_layers(completed)
        and item["cause"] not in _PLACED_BY_POLICY
        and item["hold"] != "blank_candidate"
    )


def held_for(report: dict, page: dict) -> list[tuple[str, str]]:
    """Each hold shown on the page with what caused it, most telling first, as (hold, clause).

    A hold is said by its recorded causes: which reading, and what is wrong with it.
    A hold with no recorded cause (older formats, comparisons, contest), and a blank
    hold, which no single reading explains, keep the hold's own words.
    """
    causes = _causes(page, report)
    selected = page.get("selected_attempt")
    order = {code: index for index, code in enumerate(_HOLD_ORDER)}
    framed: list[tuple[str, str, str, str]] = []
    for reason in sorted(shown_holds(page), key=lambda code: order.get(code, len(order))):
        items = [] if reason == "blank_candidate" else [c for c in causes if c["hold"] == reason]
        items.sort(key=lambda item: item["attempt_id"] != selected)
        flaws: list[tuple[str, str, str]] = []
        said: set[int] = set()
        for item in items:
            # The sentence around the flaw, so that several flaws of one reading share it.
            frame = _flaw(report, page, item, _FLAW) if item["cause"] in CAUSES else None
            if frame is None or id(item) in said:
                continue
            before, after = frame.split(_FLAW)
            if item["attempt_id"] == selected:
                # The same flaw on other readings is said with the selected one's.
                same = [o for o in items if o is not item and o["cause"] == item["cause"]]
                said.update(id(o) for o in same)
                names = [reader_name(report, _attempt(page, o["attempt_id"])) for o in same]
                names = list(dict.fromkeys(names))
                if names:
                    after += f"; the same is true of {_joined(names)}"
            flaws.append((before, CAUSES[item["cause"]], after))
        if flaws:
            framed += [(reason, *flaw) for flaw in flaws]
        else:
            framed.append((reason, "", _lower(hold_in_words(page, reason)), ""))
    # One sentence for each reading and hold order kept: its flaws are joined, whatever
    # holds they raised.
    merged: dict[tuple[str, str], tuple[str, list[str]]] = {}
    for reason, before, clause, after in framed:
        key = (before, after) if before else (reason, clause)
        clauses = merged.setdefault(key, (reason, []))[1]
        if clause not in clauses:
            clauses.append(clause)
    return [
        (reason, (key[0] + _joined(clauses) + key[1]) if key[0] != reason else clauses[0])
        for key, (reason, clauses) in merged.items()
    ]


def _joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


# What a person reads first gives at most this many causes of a page's holds.
_FIRST_CAUSES = 2


def held_in_words(report: dict, page: dict, *, every: bool = False, more: str = "") -> str:
    """What holds the page, in sentences; at most two causes unless ``every``. ``more``
    says where the rest are, after a count of them."""
    shown = [text for _hold, text in held_for(report, page)]
    left = 0 if every else max(0, len(shown) - _FIRST_CAUSES)
    text = " ".join(sentence(item) + "." for item in shown[: len(shown) - left])
    if left:
        text += f" {left} more {'cause' if left == 1 else 'causes'} {more}."
    return text


def _group_keys(report: dict, page: dict) -> list[str]:
    """The causes a page is listed under where a person reads first: its first two."""
    return [f"{hold}\x1f{text}" for hold, text in held_for(report, page)[:_FIRST_CAUSES]]


def _group_words(key: str) -> str:
    return key.split("\x1f", 1)[-1]


def _waiting_for(report: dict, stage: object) -> str:
    if stage not in _STAGE_LABELS:
        return "Not read yet"
    engine = (report.get("engines") or {}).get(stage)
    images = report["source"].get("kind") == "page_images"
    return f"Not finished: waiting for {reading_name(str(stage), engine, images=images)}"


def _has_images(report: dict) -> bool:
    source = report["source"]
    return source.get("kind") == "page_images" or source["path"].lower().endswith(".pdf")


def _read_and_held(report: dict) -> tuple[str, str]:
    """How many pages were read and how many need a person, as the terminal says it."""
    held = _sorted_pages(report)[0]
    read = report["counts"]["processed_pages"]
    pages = f"{read} page{'' if read == 1 else 's'} read"
    if not held:
        return pages, "none needs a person"
    return pages, "1 needs a person" if len(held) == 1 else f"{len(held)} need a person"


def _status_line(report: dict, job: str) -> str:
    status = report["status"]
    action = _escape(report["next_action"]).replace(JOB_DIR, job)
    pages, need = _read_and_held(report)
    if status == "completed":
        return f"Finished: {pages}; {need}."
    if status == "processing":
        return (
            f"Not finished: {pages} so far; {need}. The job was interrupted, or is still running."
        )
    if status == "paused_budget":
        return f"Paused at a limit. {action}"
    return f"Halted. {action}"


def _sorted_pages(report: dict) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """The pages that need a person, are not finished, were reviewed, and the rest."""
    pages = sorted(report["pages"], key=lambda page: page["page_number"])
    pending = [page for page in pages if is_pending(page, report)]
    held = [page for page in pages if needs_person(page, report)]
    reviewed = [page for page in pages if page.get("review") is not None]
    settled = {id(page) for page in [*pending, *held, *reviewed]}
    return held, pending, reviewed, [page for page in pages if id(page) not in settled]


def summary_lines(report: dict) -> list[str]:
    """The report's opening: what happened, where the pages stand, and what to do now."""
    counts = report["counts"]
    held, pending, reviewed, clean = _sorted_pages(report)
    name = Path(report["source"]["path"]).name
    total = counts["source_pages"]
    selected = counts["selected_pages"]
    of = f"{selected} of its {total} pages" if total is not None else f"{selected} pages"
    source = f"[{_plain(name)}][source]"
    lines = [
        _status_line(report, _HERE),
        "",
        f"Source: {source}; {of} selected, {counts['processed_pages']} read.",
        "",
    ]
    if held:
        groups = _grouped(held, lambda page: _group_keys(report, page))
        several = sum(len(items) for _, items in groups) > len(held)
        lines.append(
            f"- Need a person: {_pages(held)}"
            + (", for these reasons (a page can have more than one):" if several else ":")
        )
        lines += [
            f"  - {_plain(sentence(_group_words(key)))}: {_pages(items)}." for key, items in groups
        ]
    for code, items in _grouped(clean, lambda page: [page["disposition"]]):
        lines.append(f"- {hold_in_words(items[0], code)}: {_pages(items)}.")
    for code, items in _grouped(reviewed, lambda page: [_decision(page)["disposition"]]):
        lines.append(f"- {REVIEWED.get(code, code)}: {_pages(items)}.")
    for stage, items in _grouped(pending, lambda page: [str(page.get("next_action"))]):
        lines.append(f"- {_waiting_for(report, stage)}: {_pages(items)}.")
    if not report["pages"]:
        lines.append("- No page was read.")
    step = _next_step(report, held)
    if step:
        lines += ["", step]
    return lines


def _next_step(report: dict, held: list[dict]) -> str | None:
    status = report["status"]
    here = "in this job's folder, "
    if status == "processing":
        return f"Next: {here}`pageledger resume .` finishes the job."
    if held:
        if len(held) == 1:
            those = "the page that needs a person" + (
                " beside its page image" if _has_images(report) else ""
            )
        else:
            those = f"the {len(held)} pages that need a person" + (
                ", each beside its page image," if _has_images(report) else ""
            )
        later = ", for the pages read so far" if status != "completed" else ""
        return (
            f"Next{later}: {here}`pageledger review .` writes the review view, where you check "
            f"{those} and record what you decide."
        )
    if status != "completed":
        return None
    if any(
        _decision(page)["disposition"] == "source_defect"
        for page in report["pages"]
        if page.get("review")
    ):
        return f"Next: {_escape(report['next_action'])}"
    return (
        f"Next: {here}`pageledger export . --out ../NAME.md`, with NAME the folder's name, "
        "writes the selected text out beside the job."
    )


def export_command(job: Path | str) -> str:
    """The one export a job's next step suggests: its text as Markdown, named after its folder."""
    folder = Path(job).expanduser()
    name = folder.name if folder.name not in {"", ".", ".."} else folder.resolve().name
    return f"pageledger export {shlex.quote(str(job))} --out {shlex.quote(name + '.md')}"


# -- What `process` prints when a job ends --------------------------------------


def closing_lines(report: dict, job: Path | str) -> list[str]:
    """What `process` prints when the job ends: where the job is, what was read, what needs
    a person and why, and the one next command, in words and without codes."""
    folder = shlex.quote(str(job))
    held = _sorted_pages(report)[0]
    pages, need = _read_and_held(report)
    status = report["status"]
    action = report["next_action"].replace(JOB_DIR, folder)
    if status == "completed":
        lines = [f"Finished: {pages}; {need}."]
    elif status == "processing":
        lines = [f"Not finished: {pages} so far; {need}."]
    elif status == "paused_budget":
        lines = [f"Paused at a limit: {pages}; {need} so far.", action]
    else:
        lines = [f"Halted: {action}", f"{pages.capitalize()}; {need}."]
    lines.append(f"The job is in {folder}.")
    if held:
        groups = _grouped(held, lambda page: _group_keys(report, page))
        lines.append("Why it needs a person:" if len(held) == 1 else "Why they need a person:")
        for key, items in groups[:3]:
            count = f"{len(items)} page{'' if len(items) == 1 else 's'}"
            lines.append(f"  {count}: {_group_words(key)}.")
        if len(groups) > 3:
            lines.append("  Other causes are in the job's report.md.")
    if status == "processing":
        lines.append(f"Next: pageledger resume {folder} finishes the job.")
    elif held:
        one = len(held) == 1
        those = "that page" if one else "those pages"
        beside = (" beside its page image" if one else " beside their page images") * _has_images(
            report
        )
        lines.append(f"Next: pageledger review {folder} shows {those}{beside} for checking.")
    if report["counts"]["selected_outputs"] and status != "processing":
        lines.append(f"Text out: {export_command(job)}")
    return lines


def _lower(text: str) -> str:
    return text[:1].lower() + text[1:]


# -- The page table and the record -----------------------------------------------


def _attempt(page: dict, attempt_id: str) -> dict:
    attempt = next((item for item in page["attempts"] if item["attempt_id"] == attempt_id), None)
    if attempt is None:
        raise ValueError("selected output attempt is missing from document report")
    return attempt


def _selected_cell(report: dict, page: dict) -> str:
    selected = page.get("selected_output")
    if selected is None:
        return "none"
    name = reader_name(report, _attempt(page, selected["attempt_id"]))
    correction = selected.get("correction")
    if correction is not None:
        made = corrections.describe(correction["words"])
        return f"{_link(sentence(CORRECTION), selected['path'])} of {_plain(name)}: {made}"
    if selected.get("edition"):
        return f"{_link(sentence(name), selected['path'])}, with its settled differences applied"
    return _link(sentence(name), selected["path"])


def held_words(report: dict, page: dict, *, every: bool = False, more: str) -> str:
    """What holds a page, as the page table says it and the review view repeats it."""
    if is_pending(page, report):
        cell = sentence(_waiting_for(report, page.get("next_action"))) + "."
        if page.get("review_reasons"):
            cell += " Held so far: " + held_in_words(report, page, every=every, more=more)
        return cell
    if not page.get("review_reasons") and not needs_person(page, report):
        return "nothing"
    return held_in_words(report, page, every=every, more=more)


def _held_cell(report: dict, page: dict) -> str:
    return _plain(held_words(report, page, more="in the record below"))


def _review_cell(page: dict) -> str:
    if page.get("review") is None:
        return "not checked"
    decision = _decision(page)
    return (
        f"{REVIEWED.get(decision['disposition'], decision['disposition'])}: "
        f"{_plain(decision['reviewer'])}, {_escape(decision['reviewed_at'])}, "
        f"“{_plain(decision['reason'])}”"
    )


def _page_table(report: dict) -> list[str]:
    held, pending, reviewed, clean = _sorted_pages(report)
    rest = sorted([*reviewed, *clean], key=lambda page: page["page_number"])
    view = (
        "beside its image, with every reading of it"
        if _has_images(report)
        else "with every reading of it"
    )
    lines = [
        "## Pages",
        "",
        "Pages that need a person come first. The review view (`pageledger review .` in this "
        f"job's folder) shows each page {view}, and is where decisions and corrections are made.",
        "",
        "| Page | Held for | Selected text | Review |",
        "| --- | --- | --- | --- |",
    ]
    for page in [*held, *pending, *rest]:
        lines.append(
            f"| {_escape(str(page['page_number']) + _image_name(report, page))} | "
            f"{_held_cell(report, page)} | {_selected_cell(report, page)} | "
            f"{_review_cell(page)} |"
        )
    lines.append("")
    if any(
        item["agreement_ratio"] >= ENGINE_AGREEMENT_THRESHOLD
        for page in report["pages"]
        for item in _counted(page, report)
    ):
        lines += [AGREEMENT_CAVEAT, ""]
    if pending:
        lines += [
            "For pages not finished, the text and holds are those so far: the stages still to "
            "run may select another reading, and add or settle holds.",
            "",
        ]
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
    # A render already past the scan's own pixels gains nothing from more: its advice is
    # render_upsampled's, not render_dpi_capped's.
    upsampled = {id(page) for page in found.get("render_upsampled", [])}
    lines = []
    for code, pages in sorted(found.items(), key=lambda item: (-len(item[1]), item[0])):
        line = f"- On {_pages(pages)} (`{_escape(code)}`): {warning_words(code)}"
        if code == "render_dpi_capped" and upsampled:
            rest = [page for page in pages if id(page) not in upsampled]
            if rest:
                line += f" What to do, on {_pages(rest)}: {ADVICE[code]}"
        elif code in ADVICE:
            line += f" What to do: {ADVICE[code]}"
        lines.append(line)
    return lines


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
            f", {differences} number{' differs' if differences == 1 else 's differ'}"
            if differences
            else ""
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
            verdict = f"not counted, because `{_escape(other)}` is held itself"
        elif other in layers and not witness:
            verdict = (
                "not counted: this job's hold policy sets an earlier OCR in the PDF aside "
                "once the job's own OCR has read the page"
            )
        elif other in layers or selected in layers:
            verdict = "not counted: two of the job's own engines agree, which outweighs the layer"
        else:
            verdict = "not counted"
        lines.append(f"`{_escape(pair[0])}` and `{_escape(pair[1])}`: {figure}; {verdict}")
    return lines


def _short_comparison(page: dict, report: dict) -> str:
    counted = _counted(page, report)
    if not counted:
        return "none counted" if page.get("comparisons") else "none recorded"
    ratio = min(item["agreement_ratio"] for item in counted)
    below = ratio < ENGINE_AGREEMENT_THRESHOLD
    return f"{ratio:.0%} of words agree" + (" (below the job's 60%)" if below else "")


def _reading_record(page: dict, attempt: dict) -> str:
    path = attempt["raw_artifact"]
    shown = _link(attempt["attempt_id"], path) if path else f"`{_escape(attempt['attempt_id'])}`"
    detail = [attempt["outcome"]]
    failure = (attempt.get("failure") or {}).get("code")
    if failure:
        detail.append(f"failure `{_escape(failure)}`")
    detail += [
        f"`{_escape(code)}`" for code in map(warning_code, attempt.get("warnings") or []) if code
    ]
    tiling = attempt.get("tiling")
    if tiling:
        read = tiling["sent"] - len(tiling["failed"])
        tiles = f"tiles: {tiling['tiles']} planned, {tiling['sent']} sent and paid for, {read} read"
        for label, key in (("failed", "failed"), ("cut through text", "edge_cut")):
            if tiling[key]:
                tiles += f", {label}: {' '.join(tiling[key])}"
        detail.append(tiles)
    if attempt["attempt_id"] == page.get("selected_attempt"):
        detail.append("selected")
    return f"{shown} {', '.join(detail)}"


def _cause_record(report: dict, page: dict, item: dict) -> str:
    line = f"`{item['hold']}` from `{item['cause']}` on `{_escape(item['attempt_id'])}`"
    attempt = next((a for a in page["attempts"] if a["attempt_id"] == item["attempt_id"]), None)
    if attempt is not None and item["cause"] in CAUSES:
        line += f": {_escape(reader_name(report, attempt))} {CAUSES[item['cause']]}"
    if item["cause"] == "letters_lost":
        line += (
            f" ({item['yat_words']} words spelled with ѣ, against {item['most_yat_words']} in "
            "another reading)"
        )
    return line


def _page_record(report: dict, page: dict) -> list[str]:
    """One page's record: its state, then one line for each hold's cause, reading,
    selected text and comparison, so that no line runs on."""
    number = _escape(str(page["page_number"]) + _image_name(report, page))
    if report["source"].get("kind") == "page_images":
        # Each page is a file of its own, which the source's one link does not reach.
        number = _link(str(page["page_number"]) + _image_name(report, page), page["source_link"])
    line = f"- Page {number}: "
    line += f"`{page['disposition']}`"
    if page.get("review_reasons"):
        line += "; held for " + ", ".join(f"`{reason}`" for reason in page["review_reasons"])
    if page.get("review") is not None:
        decision = _decision(page)
        line += f"; reviewed as `{decision['disposition']}`"
        if decision["selected_attempt"] is None:
            line += " from the source alone"
    items = [f"Hold: {_cause_record(report, page, item)}." for item in _causes(page, report)]
    for attempt in page["attempts"]:
        name = _escape(sentence(reader_name(report, attempt)))
        items.append(f"{name}: {_reading_record(page, attempt)}.")
    if not page["attempts"]:
        items.append("Readings: none.")
    selected = page.get("selected_output") or {}
    correction = selected.get("correction")
    if correction is not None:
        text = (
            f"Selected text: a {_link('correction', selected['path'])} of "
            f"`{_escape(selected['attempt_id'])}` by {_escape(correction['reviewer'])} at "
            f"{_escape(correction['reviewed_at'])}"
        )
        if correction["from_path"].startswith("corrections/"):
            earlier = corrections.author(
                page["review_history"], page["page_id"], correction["from_sha256"]
            )
            who = f"the correction by {earlier['reviewer']} at {earlier['reviewed_at']}"
            text += f", revising {_link(who, correction['from_path'])}"
        items.append(text + ".")
    elif selected.get("edition"):
        items.append(
            f"Selected text: the {_link('edition', selected['path'])} of "
            f"`{_escape(selected['attempt_id'])}`, with its settled differences applied."
        )
    if page.get("contested"):
        contested = page["contested"]
        open_spans = f"{contested['open']} of {sum(contested['kinds'].values())} open"
        items.append(f"Contested spans: {_link(open_spans, contested['artifact'])}.")
    items += [f"Compared {text}." for text in _comparisons(page, report)]
    if is_pending(page, report) and page.get("next_action") in _STAGE_LABELS:
        items.append(f"Waiting for the `{page['next_action']}` stage.")
    return [line + ".", *(f"  - {item}" for item in items)]


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


def _engines_line(report: dict) -> list[str]:
    engines = report.get("engines")
    if not engines:
        return []
    images = report["source"].get("kind") == "page_images"
    read = [
        f"the `{stage}` stage, {_plain(reading_name(stage, engine, images=images))} "
        f"(`{_escape(engine['adapter'])}`)"
        for stage, engine in engines.items()
    ]
    return [f"- Read by: {'; '.join(read)}."]


def _record_lines(report: dict) -> list[str]:
    source, counts = report["source"], report["counts"]
    annotations, retention = source["annotations"], report["source_retention"]
    annotation_count = "unknown" if annotations["count"] is None else str(annotations["count"])
    source_count = "unknown" if counts["source_pages"] is None else str(counts["source_pages"])
    lines = [
        "## For the record",
        "",
        "Everything below is in document.json, of which this report is a rendering. The codes "
        f"in backticks are explained in [PageLedger's warnings]({DOCS}warnings.md).",
        "",
        f"- Job: `{_escape(report['job_id'])}`, created {report['created_at']}; hold policy "
        f"{report.get('hold_policy', 'none recorded')}; report format {report['report_format']}.",
        f"- Source: [{_plain(source['path'])}][source]",
        f"- Source SHA-256: `{source['sha256']}`",
        *(f"- {line}" for line in _snapshot_lines(source)),
        *_engines_line(report),
        f"- Pages: {counts['selected_pages']} selected of {source_count} in the source; "
        f"{counts['processed_pages']} read; {counts['selected_outputs']} with selected text; "
        f"{counts['unresolved_pages']} not yet reviewed as text or blank (`unresolved_pages`).",
        *(
            [f"- Pages whose text a person corrected: {counts['corrected_pages']}."]
            if "corrected_pages" in counts
            else []
        ),
        "",
        "### Each page",
        "",
        "Each page's number in the source, its state and every hold the job recorded; then "
        "what caused each hold, on which reading; each reading, by its ID (a link to its "
        "text), with its outcome, failure and warnings; and the comparisons the job recorded "
        "and whether it counted them.",
        "",
    ]
    pages = sorted(report["pages"], key=lambda page: page["page_number"])
    lines += [line for page in pages for line in _page_record(report, page)] or ["None."]
    lines += ["", "### How the job ran", ""]
    stages = _escalation_accounting(report)
    if stages:
        fired = Counter(t["trigger"] for page in report["pages"] for t in page.get("triggers", []))
        climbed = ", ".join(f"`{name}` {count}" for name, count in sorted(fired.items()))
        # The record names stages by their codes, as the readings' IDs do.
        names = {f"| {label} |": f"| `{stage}` |" for stage, label in _STAGE_LABELS.items()}
        rows = [
            next((row.replace(old, new) for old, new in names.items() if row.startswith(old)), row)
            for row in stages[2:-3]
        ]
        lines += [
            *rows,
            "",
            "Readings sent on to the next stage for a reason other than a hold, by trigger "
            f"(a page can be sent on more than once): {climbed or 'none'}.",
            "",
        ]
    lines += _contest_accounting(report)
    lines += _usage_lines(report)
    limits = [f"{name} {value}" for name, value in report["limits"].items() if value is not None]
    lines.append(f"- Limits: {', '.join(limits) if limits else 'none set'}.")
    for item in report.get("limits_history", []):
        lines.append(
            f"- Limit raised on {item['at']}: {item['limit']} from {item['previous']} "
            f"to {item['value']}."
        )
    lines.append(
        f"- PDF annotations (notes and links attached to pages): {annotations['status']} "
        f"({annotation_count}). Counting them says nothing about whether the body text or "
        "its citations were extracted completely."
    )
    lines += [f"- {line}" for line in _relocation_lines(report) if line]
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
        *(
            [f"  - {JOB_DIR} stands for this job's folder."]
            if JOB_DIR in report["next_action"]
            else []
        ),
        "",
        # Where [source] links go: the source's path, written out once.
        f"[source]: <{quote(quote(source['path'], safe='/'), safe='/:#?=&%')}>",
        "",
    ]
    return lines


def _render_for_reader(report: dict) -> str:
    name = Path(report["source"]["path"]).name
    lines = [f"# {_plain(name)}: document job report", "", *summary_lines(report), ""]
    notes = _notes(report)
    unconfirmed = [
        page
        for page in _sorted_pages(report)[0]
        if any(
            _unconfirmed(page, item) and item["hold"] in shown_holds(page)
            for item in _causes(page, report)
        )
    ]
    if notes or unconfirmed:
        lines += ["## Worth knowing", ""]
        if unconfirmed:
            lines += [
                f"- On {_pages(unconfirmed)}, nothing confirms the selected text: an earlier "
                "OCR, which could have, has a flaw of its own.",
                *(f"  - {advice}" for advice in UNCONFIRMED_ADVICE),
                "",
            ]
        if notes:
            lines += [
                "These warnings hold no page, but they bear on how far to trust the text.",
                "",
                *notes,
                "",
            ]
    if report["pages"]:
        lines += _page_table(report)
    lines += _record_lines(report)
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
        lines.extend(["", AGREEMENT_CAVEAT])
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


def _engines(job: dict) -> dict:
    """Each configured stage's engine: its adapter, and what names the reading (the
    Tesseract language, the model a vision stage asked for). Nothing else is copied."""
    engines = {}
    for stage in _STAGE_LABELS:
        profile = (job.get("policy") or {}).get(stage)
        if not profile:
            continue
        options = profile.get("adapter_options") or {}
        engine = {"adapter": profile["adapter"]}
        if profile["adapter"] == "pdf_ocr":
            engine["lang"] = options.get("lang", "eng")
        if profile["adapter"] == "vision" and isinstance(options.get("model"), str):
            engine["model"] = options["model"]
        engines[stage] = engine
    return engines


def _decision(page: dict) -> dict:
    return next(item for item in page["review"]["decisions"] if item["page_id"] == page["page_id"])


def edition_published(page: dict, attempt_id: str) -> bool:
    """Whether the page's text is its edition: the attempt's reading with its settled
    spans applied, unless a person reviewed the attempt's own text instead."""
    edition = page.get("edition")
    if edition is None or edition["base_attempt"] != attempt_id:
        return False
    if page.get("review") is None:
        return True
    return _decision(page)["output_sha256"] in (None, edition["sha256"])


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
    if report_format == _CURRENT_REPORT_FORMAT:
        report["engines"] = _engines(job)
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
            if edition_published(page, chosen["attempt_id"]):
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
