"""Deterministic job selection and source-bound human review receipts.

Signals identify reasons to inspect a page; they cannot certify its accuracy.
Neither extraction grades nor model confidence participate in this policy.
"""

from __future__ import annotations

import csv
import io
import json
import re
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
    "coverage_defect",
    "low_confidence",
    "handwriting",
    "unreadable",
    "illustration",
    "blank_candidate",
)
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
    "repeated_page_text": "coverage_defect",
    "repetition_loop": "coverage_defect",
    "script_mismatch": "coverage_defect",
    "historical_letters_lost": "coverage_defect",
}
# Jobs written before 0.6 carry no hold_policy and filed an engine's low
# confidence under coverage_defect; verification rebuilds them that way.
_LEGACY_WARNING_HOLDS = {**_WARNING_HOLDS, "low_confidence": "coverage_defect"}
HOLD_POLICY = "0.6"
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def warning_holds(record: dict) -> dict[str, str]:
    """The warning-to-hold mapping that a job or its report was written with."""
    return _WARNING_HOLDS if record.get("hold_policy") == HOLD_POLICY else _LEGACY_WARNING_HOLDS


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


def assess_page(
    page: dict, review: dict | None = None, *, holds_for: dict[str, str] = _WARNING_HOLDS
) -> dict:
    """Select evidence deterministically while keeping all recorded review holds."""
    attempts = page.get("attempts", [])
    reasons = list(dict.fromkeys(page.get("review_reasons") or []))
    completed = [item for item in attempts if item.get("outcome") == "completed"]
    holds_by_id = {item["attempt_id"]: _attempt_holds(item, holds_for) for item in attempts}
    for holds in holds_by_id.values():
        reasons.extend(hold for hold in holds if hold not in reasons)
    numeric_conflict = any(_numeric_conflict(a, b) for a, b in combinations(completed, 2))
    if numeric_conflict and "numeric_column_conflict" not in reasons:
        reasons.append("numeric_column_conflict")
    usable = [
        item
        for item in completed
        if item.get("raw_artifact")
        and item.get("raw_sha256")
        and ("text" not in item or bool(item["text"].strip()))
    ]
    clean = [item for item in usable if not holds_by_id[item["attempt_id"]]]
    selected = (clean or usable or [None])[0]
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
    if (
        clean
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
    return {
        "selected_attempt": selected_id,
        "disposition": disposition,
        "review_reasons": reasons,
        "next_action": next_action,
    }
