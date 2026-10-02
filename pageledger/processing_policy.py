"""Deterministic job selection and source-bound human review receipts.

Signals identify reasons to inspect a page; they cannot certify its accuracy.
Neither extraction grades nor model confidence participate in this policy.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Any

from .processing_config import STAGES

REVIEW_DISPOSITIONS = frozenset(
    {
        "reviewed_text",
        "reviewed_blank",
        "illustration",
        "handwriting",
        "unreadable",
        "source_defect",
    }
)
_HOLD_ORDER = (
    "source_defect",
    "numeric_column_conflict",
    "numeric_disagreement",
    "engine_disagreement",
    "unconfirmed_model_output",
    "coverage_defect",
    "low_confidence",
    "handwriting",
    "unreadable",
    "illustration",
    "blank_candidate",
)
_DISAGREEMENTS = ("engine_disagreement", "numeric_disagreement")
_COMPARISON_REASONS = (*_DISAGREEMENTS, "unconfirmed_model_output")
_WARNING_HOLDS = {
    "coverage_defect": "coverage_defect",
    "clipped_text": "coverage_defect",
    "truncated_text": "coverage_defect",
    "output_truncated": "coverage_defect",
    "missing_required_columns": "coverage_defect",
    "missing_content": "coverage_defect",
    "suspicious_embedded_text_delta": "coverage_defect",
    "fragmented_text": "coverage_defect",
    "numeric_column_conflict": "numeric_column_conflict",
    "column_conflict": "numeric_column_conflict",
    "source_defect": "source_defect",
    "empty_text": "blank_candidate",
    "blank": "blank_candidate",
    "blank_candidate": "blank_candidate",
    "illustration": "illustration",
    "handwriting": "handwriting",
    "unreadable": "unreadable",
    "sparse": "coverage_defect",
    "fragmented": "coverage_defect",
    "joined": "coverage_defect",
    "unknown": "coverage_defect",
    "joined_text": "coverage_defect",
    "replacement_characters": "coverage_defect",
    "control_characters": "coverage_defect",
    "suspicious_symbol_density": "coverage_defect",
    "low_confidence": "low_confidence",
    "instruction_echo": "coverage_defect",
    "digits_only_text": "coverage_defect",
    "mixed_script_tokens": "coverage_defect",
    "private_use_characters": "coverage_defect",
    "foreign_script_characters": "coverage_defect",
    "repeated_page_text": "coverage_defect",
    "repetition_loop": "coverage_defect",
    "script_mismatch": "coverage_defect",
    "historical_letters_lost": "coverage_defect",
}
# Jobs written before 0.6 carry no hold_policy and filed an engine's low
# confidence under coverage_defect; verification rebuilds them that way.
_LEGACY_WARNING_HOLDS = {**_WARNING_HOLDS, "low_confidence": "coverage_defect"}
# 0.6.1: clean text from a later engine settles a blank candidate. Jobs written
# under "0.6" kept the hold and are rebuilt that way.
HOLD_POLICY = "0.6.1"
HOLD_POLICIES = frozenset({"0.6", HOLD_POLICY})
# Word agreement below which two engines disagree. On the 24 calibration pages it
# flags 9 for RapidOCR/Apple Vision, 14 for Surya/RapidOCR, 22 for Tesseract/RapidOCR.
ENGINE_AGREEMENT_THRESHOLD = 0.60
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def warning_holds(record: dict) -> dict[str, str]:
    """The warning-to-hold mapping that a job or its report was written with."""
    return _WARNING_HOLDS if record.get("hold_policy") in HOLD_POLICIES else _LEGACY_WARNING_HOLDS


def validate_review(review: dict, page: dict) -> None:
    """Reject receipts that do not bind an exact source, page, and completed output."""
    if (
        not isinstance(review, dict)
        or review.get("schema_version") != "0.1"
        or not isinstance(review.get("source_sha256"), str)
        or not _HASH.fullmatch(review["source_sha256"])
        or review["source_sha256"] != page.get("source_sha256")
        or not isinstance(review.get("decisions"), list)
    ):
        raise ValueError("Review source binding is invalid")
    matches = [
        item
        for item in review["decisions"]
        if isinstance(item, dict) and item.get("page_id") == page.get("page_id")
    ]
    if len(matches) != 1:
        raise ValueError("Review must contain exactly one decision for this page")
    decision = matches[0]
    required = {
        "page_id",
        "page_number",
        "disposition",
        "selected_attempt",
        "output_sha256",
        "reason",
        "reviewer",
        "reviewed_at",
    }
    if (
        set(decision) != required
        or decision["page_number"] != page.get("page_number")
        or type(decision["page_number"]) is not int
        or not isinstance(decision["disposition"], str)
        or decision["disposition"] not in REVIEW_DISPOSITIONS
        or any(
            not isinstance(decision[key], str) or not decision[key].strip()
            for key in ("reason", "reviewer", "reviewed_at")
        )
    ):
        raise ValueError("Review decision or page binding is invalid")
    try:
        reviewed_at = datetime.fromisoformat(decision["reviewed_at"].replace("Z", "+00:00"))
        if reviewed_at.tzinfo is None:
            raise ValueError("review timestamp must include timezone")
    except ValueError as exc:
        raise ValueError("Review timestamp is invalid") from exc
    selected = decision["selected_attempt"]
    if selected is None:
        if decision["output_sha256"] is not None or decision["disposition"] == "reviewed_text":
            raise ValueError("Reviewed text requires an exact completed output")
        return
    matches = [item for item in page.get("attempts", []) if item.get("attempt_id") == selected]
    if (
        len(matches) != 1
        or matches[0].get("outcome") != "completed"
        or not matches[0].get("raw_artifact")
        or not isinstance(decision["output_sha256"], str)
        or not _HASH.fullmatch(decision["output_sha256"])
        or matches[0].get("raw_sha256") != decision["output_sha256"]
    ):
        raise ValueError("Review output binding is invalid")


def _attempt_holds(attempt: dict, holds_for: dict[str, str] = _WARNING_HOLDS) -> list[str]:
    holds = []
    warnings = attempt.get("warnings") or []
    for warning in warnings:
        code = warning.get("type", warning.get("code")) if isinstance(warning, dict) else warning
        if isinstance(code, str) and code in holds_for:
            holds.append(holds_for[code])
    classification = (attempt.get("classification") or {}).get("type")
    if classification in holds_for:
        reason = (attempt.get("classification") or {}).get("reason", "")
        if reason != "empty_pdf_text_ambiguous":
            holds.append(holds_for[classification])
    alignment = attempt.get("alignment") or {}
    metrics = alignment.get("metrics") or {}
    if (
        (alignment.get("columns") or {}).get("missing_required")
        or alignment.get("structure_issues")
        or metrics.get("parse_error")
        or metrics.get("structure_issue_count", 0) > 0
        or (
            metrics.get("required_column_coverage") is not None
            and metrics["required_column_coverage"] < 1
        )
    ):
        holds.append("coverage_defect")
    if (
        alignment.get("coercion_errors")
        or metrics.get("coercion_error_count", 0) > 0
        or (metrics.get("arithmetic_pass_rate") is not None and metrics["arithmetic_pass_rate"] < 1)
        or any(
            check.get("passed") is False
            or check.get("rows_failed", 0) > 0
            or check.get("rows_unchecked", 0) > 0
            for check in alignment.get("checks", [])
        )
    ):
        holds.append("numeric_column_conflict")
    if attempt.get("outcome") == "completed" and "text" in attempt and not attempt["text"].strip():
        holds.append("blank_candidate")
    return list(dict.fromkeys(holds))


def _records(attempt: dict) -> list[dict[str, Any]] | None:
    """Parse only explicitly structured candidates with named columns."""
    aligned = (attempt.get("alignment") or {}).get("records")
    if isinstance(aligned, list) and aligned and all(isinstance(row, dict) for row in aligned):
        return aligned
    fmt, text = attempt.get("format"), attempt.get("text")
    if not isinstance(text, str):
        return None
    try:
        if fmt == "json":
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("records"), list):
                data = data["records"]
            if isinstance(data, list) and data and all(isinstance(row, dict) for row in data):
                return data
            if isinstance(data, dict) and "headers" in data and "rows" in data:
                headers, rows = data["headers"], data["rows"]
            else:
                return None
        elif fmt == "csv":
            parsed = list(csv.reader(io.StringIO(text)))
            if not parsed:
                return None
            headers, rows = parsed[0], parsed[1:]
        elif fmt in {"markdown_table", "markdown"}:
            parsed = [
                line.strip().strip("|").split("|") for line in text.splitlines() if "|" in line
            ]
            if len(parsed) < 3 or not all(re.fullmatch(r"\s*:?-+:?\s*", c) for c in parsed[1]):
                return None
            headers, rows = [header.strip() for header in parsed[0]], parsed[2:]
            rows = [[cell.strip() for cell in row] for row in rows]
        else:
            return None
        if (
            not headers
            or any(not isinstance(header, str) or not header.strip() for header in headers)
            or len(set(headers)) != len(headers)
            or not all(isinstance(row, list) and len(row) == len(headers) for row in rows)
        ):
            return None
        return [dict(zip(headers, row, strict=True)) for row in rows]
    except (ValueError, TypeError):
        return None


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _numeric_conflict(left: dict, right: dict) -> bool:
    a, b = _records(left), _records(right)
    if not a or not b:
        return False
    columns = set(a[0])
    if not columns or any(set(row) != columns for row in [*a, *b]):
        return False
    numeric = sorted(
        key for key in columns if all(_number(row[key]) is not None for row in [*a, *b])
    )
    if not numeric:
        return False
    labels = sorted(columns - set(numeric))
    if labels:

        def keyed(rows):
            keys = [tuple(str(row[key]) for key in labels) for row in rows]
            return dict(zip(keys, rows, strict=True)) if len(set(keys)) == len(keys) else None

        keyed_a, keyed_b = keyed(a), keyed(b)
        if keyed_a is None or keyed_b is None or set(keyed_a) != set(keyed_b):
            return False
        pairs = [(row, keyed_b[key]) for key, row in keyed_a.items()]
    else:
        if len(a) != len(b):
            return False
        pairs = list(zip(a, b, strict=True))
    return any(
        _number(row_a[key]) != _number(row_b[key]) for row_a, row_b in pairs for key in numeric
    )


def _usable(attempts: list[dict]) -> list[dict]:
    """Completed attempts whose retained output is not empty."""
    return [
        item
        for item in attempts
        if item.get("outcome") == "completed"
        and item.get("raw_artifact")
        and item.get("raw_sha256")
        and ("text" not in item or bool(item["text"].strip()))
    ]


def selected_clean_comparisons(
    page: dict, selected_attempt: str, holds_for: dict[str, str]
) -> list[dict]:
    """Comparisons of the selected attempt with an attempt that has no hold of its own.

    A held attempt is why the page moved on to another engine, so its
    disagreement with that engine is expected and is not evidence.
    """
    clean = {
        item["attempt_id"]
        for item in _usable(page["attempts"])
        if not _attempt_holds(item, holds_for)
    }
    return [
        item
        for item in page.get("comparisons", [])
        if (item["left_attempt"] == selected_attempt and item["right_attempt"] in clean)
        or (item["right_attempt"] == selected_attempt and item["left_attempt"] in clean)
    ]


def _blank_refuted(attempts: list[dict], clean: list[dict], holds_for: dict[str, str]) -> bool:
    """A blank hold came only from empty readings, and a non-generative engine read clean text."""
    for item in attempts:
        if "blank_candidate" not in _attempt_holds(item, holds_for):
            continue
        codes = {
            warning.get("type", warning.get("code")) if isinstance(warning, dict) else warning
            for warning in item.get("warnings") or []
        }
        judged_blank = codes & {"blank", "blank_candidate"} or (
            (item.get("classification") or {}).get("type") in {"blank", "blank_candidate"}
            and (item.get("text") or "").strip()
        )
        if judged_blank or (item.get("text") or "").strip():
            return False
    return any("generative" not in item.get("adapter_capabilities", ()) for item in clean)


def assess_page(
    page: dict,
    review: dict | None = None,
    *,
    holds_for: dict[str, str] = _WARNING_HOLDS,
    text_refutes_blank: bool = False,
    escalate_on: tuple[str, ...] | list[str] = ("hold",),
    roughness: Callable[[str], dict | None] | None = None,
    rough_below: float | None = None,
) -> dict:
    """Select evidence deterministically while keeping all recorded review holds.

    With ``text_refutes_blank`` (hold policy 0.6.1) one hold can be settled by
    later evidence: a blank candidate raised only because an engine returned no
    text is cleared when another, non-generative engine reads clean text from the
    page. An engine's own judgement that the page is blank is never cleared.

    ``escalate_on`` adds triggers to 0.6's rule that a held page climbs: ``rough``
    (too few known words in the selected reading, judged by ``roughness``) and
    ``disagreement`` (clean readings disagree). Triggers lift a page at most to the
    image stage: a second reader is another witness, not a better one.
    """
    attempts = page.get("attempts", [])
    reasons = list(dict.fromkeys(page.get("review_reasons") or []))
    completed = [item for item in attempts if item.get("outcome") == "completed"]
    holds_by_id = {item["attempt_id"]: _attempt_holds(item, holds_for) for item in attempts}
    for holds in holds_by_id.values():
        reasons.extend(hold for hold in holds if hold not in reasons)
    numeric_conflict = any(_numeric_conflict(a, b) for a, b in combinations(completed, 2))
    if numeric_conflict and "numeric_column_conflict" not in reasons:
        reasons.append("numeric_column_conflict")
    usable = _usable(completed)
    clean = [item for item in usable if not holds_by_id[item["attempt_id"]]]
    if (
        text_refutes_blank
        and "blank_candidate" in reasons
        and _blank_refuted(attempts, clean, holds_for)
    ):
        reasons.remove("blank_candidate")
    selected = _select(clean, usable, escalate_on, roughness, rough_below)
    selected_comparisons = (
        selected_clean_comparisons(page, selected["attempt_id"], holds_for)
        if selected and holds_for is _WARNING_HOLDS
        else []
    )
    if any(item["agreement_ratio"] < ENGINE_AGREEMENT_THRESHOLD for item in selected_comparisons):
        reasons.append("engine_disagreement")
    if any(item["number_differences"] for item in selected_comparisons):
        reasons.append("numeric_disagreement")
    if (
        holds_for is _WARNING_HOLDS
        and selected
        and "generative" in selected.get("adapter_capabilities", ())
    ):
        confirmed = any(
            item["agreement_ratio"] >= ENGINE_AGREEMENT_THRESHOLD for item in selected_comparisons
        )
        if not confirmed:
            reasons.append("unconfirmed_model_output")
    disposition = next((hold for hold in _HOLD_ORDER if hold in reasons), None)
    if disposition is None:
        if selected:
            disposition = "unreviewed_text"
        elif any(item.get("outcome") in {"outcome_unknown", "response"} for item in attempts):
            disposition = "outcome_unknown"
        elif attempts:
            disposition = "provider_failure"
        else:
            disposition = "pending"
    if attempts and attempts[-1].get("outcome") in {"failed", "outcome_unknown", "response"}:
        disposition = (
            "provider_failure" if attempts[-1]["outcome"] == "failed" else "outcome_unknown"
        )
        if disposition not in reasons:
            reasons.append(disposition)
    stage_index = max(
        (STAGES.index(item["stage"]) for item in attempts if item.get("stage") in STAGES),
        default=-1,
    )
    triggers = _triggers(selected, reasons, stage_index, escalate_on, roughness, rough_below)
    held = not clean and not any(reason in reasons for reason in _COMPARISON_REASONS)
    if (
        not (held or triggers)
        or numeric_conflict
        or disposition in {"source_defect", "outcome_unknown", "provider_failure", "illustration"}
        or stage_index == len(STAGES) - 1
    ):
        next_action = "review"
    else:
        next_action = STAGES[stage_index + 1]
    selected_id = selected["attempt_id"] if selected else None
    receipt = review if review is not None else page.get("review")
    if receipt is not None:
        validate_review(receipt, page)
        decision = next(item for item in receipt["decisions"] if item["page_id"] == page["page_id"])
        disposition, selected_id, next_action = (
            decision["disposition"],
            decision["selected_attempt"],
            "none",
        )
    result = {
        "selected_attempt": selected_id,
        "disposition": disposition,
        "review_reasons": reasons,
        "next_action": next_action,
    }
    if tuple(escalate_on) != ("hold",):
        # Why the page climbed at each earlier stage, then why it climbs now.
        result["triggers"] = [
            *_trigger_history(
                page, holds_for, text_refutes_blank, escalate_on, roughness, rough_below
            ),
            *(triggers if next_action not in {"review", "none"} else []),
        ]
    return result


def _trigger_history(
    page: dict,
    holds_for: dict[str, str],
    text_refutes_blank: bool,
    escalate_on: tuple[str, ...] | list[str],
    roughness: Callable[[str], dict | None] | None,
    rough_below: float | None,
) -> list[dict]:
    """Re-assess the page as it stood after each earlier stage, from retained attempts only."""
    reached = sorted(
        {STAGES.index(a["stage"]) for a in page.get("attempts", []) if a.get("stage") in STAGES}
    )
    history: list[dict] = []
    for index in reached[:-1]:
        kept = [a for a in page["attempts"] if a.get("stage") in STAGES[: index + 1]]
        ids = {a["attempt_id"] for a in kept}
        earlier = {
            **page,
            "attempts": kept,
            "comparisons": [
                c
                for c in page.get("comparisons", [])
                if c["left_attempt"] in ids and c["right_attempt"] in ids
            ],
            "review": None,
        }
        before = assess_page(
            earlier,
            holds_for=holds_for,
            text_refutes_blank=text_refutes_blank,
            escalate_on=escalate_on,
            roughness=roughness,
            rough_below=rough_below,
        )
        # Its list repeats the history found so far; only the tail is this stage's.
        history.extend(before["triggers"][len(history) :])
    return history


def _triggers(
    selected: dict | None,
    reasons: list[str],
    stage_index: int,
    escalate_on: tuple[str, ...] | list[str],
    roughness: Callable[[str], dict | None] | None,
    rough_below: float | None,
) -> list[dict]:
    """Why a page whose reading is not held still climbs, with the evidence that fired."""
    following = STAGES[stage_index + 1] if stage_index + 1 < len(STAGES) else None
    if selected is None or following not in {"local_ocr", "image"}:
        return []
    fired = []
    stage = STAGES[stage_index]
    measure = _rough_measure(selected, escalate_on, roughness, rough_below)
    if measure is not None:
        fired.append(
            {"trigger": "rough", "stage": stage, "attempt": selected["attempt_id"], **measure}
        )
    found = [reason for reason in _DISAGREEMENTS if reason in reasons]
    if "disagreement" in escalate_on and found:
        fired.append(
            {
                "trigger": "disagreement",
                "stage": stage,
                "attempt": selected["attempt_id"],
                "reasons": found,
            }
        )
    return fired


def _rough_measure(
    attempt: dict,
    escalate_on: tuple[str, ...] | list[str],
    roughness: Callable[[str], dict | None] | None,
    rough_below: float | None,
) -> dict | None:
    """The lexicon's measure of a reading when it falls below the threshold, else None."""
    if "rough" not in escalate_on or roughness is None or rough_below is None:
        return None
    measure = roughness(attempt.get("text") or "")
    return measure if measure is not None and measure["known_share"] < rough_below else None


def _select(
    clean: list[dict],
    usable: list[dict],
    escalate_on: tuple[str, ...] | list[str],
    roughness: Callable[[str], dict | None] | None,
    rough_below: float | None,
) -> dict | None:
    """The first clean reading; with the rough trigger, the first clean one that is not rough.

    When every clean reading is rough, the least rough wins (the earliest on a tie), so
    a page that climbed for roughness moves to the better reading it climbed for.
    """
    if not clean:
        return next(iter(usable), None)
    rough = [_rough_measure(item, escalate_on, roughness, rough_below) for item in clean]
    smooth = [item for item, measure in zip(clean, rough, strict=True) if measure is None]
    if smooth:
        return smooth[0]
    shares = [measure["known_share"] for measure in rough if measure is not None]
    return clean[shares.index(max(shares))]
