"""Validation and evaluation for PageLedger page policies."""

from __future__ import annotations

import math
from typing import Any

from .grading import GRADES, grade_is_below


def validate_policy_rules(value: Any, path: str) -> list[dict[str, Any]]:
    """Validate one rerun_if or quarantine_if rule list."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list of single-key mappings")

    rules: list[dict[str, Any]] = []
    for index, rule in enumerate(value):
        rule_path = f"{path}[{index}]"
        if not isinstance(rule, dict) or len(rule) != 1:
            raise ValueError(f"{rule_path} must be a single-key mapping")
        predicate, operand = next(iter(rule.items()))
        predicate_path = f"{rule_path}.{predicate}"
        if predicate not in {
            "grade_below",
            "missing_required_columns",
            "arithmetic_failure_rate_above",
        }:
            raise ValueError(f"{predicate_path} is not a known policy predicate")
        if predicate == "grade_below":
            if not isinstance(operand, str) or operand not in GRADES:
                raise ValueError(
                    f"{predicate_path} must be one of: {', '.join(GRADES)}"
                )
        elif predicate == "missing_required_columns":
            if operand is not True:
                raise ValueError(f"{predicate_path} must be true")
        elif (
            isinstance(operand, bool)
            or not isinstance(operand, (int, float))
            or not math.isfinite(operand)
            or not 0 <= operand <= 1
        ):
            raise ValueError(f"{predicate_path} must be a finite number between 0 and 1")
        rules.append(rule)
    return rules


def evaluate_policies(
    rules: list[dict[str, Any]],
    *,
    grade: str,
    alignment: dict[str, Any] | None,
) -> list[str]:
    """Return the predicate names whose recorded evidence matches a page."""
    matches: list[str] = []
    for rule in rules:
        predicate, operand = next(iter(rule.items()))
        if predicate == "grade_below":
            matched = grade_is_below(grade, operand)
        elif predicate == "missing_required_columns":
            matched = bool(
                alignment
                and alignment.get("columns", {}).get("missing_required")
            )
        else:
            pass_rate = (
                alignment.get("metrics", {}).get("arithmetic_pass_rate")
                if alignment
                else None
            )
            matched = (
                isinstance(pass_rate, (int, float))
                and not isinstance(pass_rate, bool)
                and math.isfinite(pass_rate)
                and pass_rate < 1 - operand
            )
        if matched:
            matches.append(predicate)
    return matches


def rebuild_policy_queues(
    *,
    config: Any,
    quality_entries: list[dict[str, Any]],
    alignments: dict[str, dict[str, Any]],
    routes: dict[str, dict[str, Any]],
    review_queue: list[dict[str, Any]] | None = None,
    quarantine_queue: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rebuild evidence-derived queues while preserving operational entries."""
    grades = {entry["page_id"]: entry for entry in quality_entries}
    review = []
    for item in review_queue or []:
        reason = str(item.get("reason", ""))
        if reason in {"quality_warning", "grade_below_threshold"}:
            continue
        if reason.startswith("rerun_if:"):
            continue
        review.append(_refresh_grade(item, grades))
    quarantine = [
        _refresh_grade(item, grades)
        for item in quarantine_queue or []
        if not str(item.get("reason", "")).startswith("quarantine_if:")
    ]

    for entry in quality_entries:
        route = routes.get(entry["page_id"], {})
        queue_entry = {
            "page_id": entry["page_id"],
            "page_number": entry["page_number"],
            "type": route.get("type", config.default_review_type),
            "confidence": route.get("confidence"),
            "grade": entry["grade"],
            "grade_basis": entry["grade_basis"],
        }
        if entry.get("warnings"):
            review.append({
                **queue_entry,
                "action": "review",
                "reason": "quality_warning",
            })
        if config.review_below_grade is not None and grade_is_below(
            entry["grade"], config.review_below_grade
        ):
            review.append({
                **queue_entry,
                "action": "review",
                "reason": "grade_below_threshold",
            })
        alignment = alignments.get(entry["page_id"])
        for predicate in evaluate_policies(
            config.rerun_rules,
            grade=entry["grade"],
            alignment=alignment,
        ):
            review.append({
                **queue_entry,
                "action": "review",
                "reason": f"rerun_if:{predicate}",
            })
        for predicate in evaluate_policies(
            config.quarantine_rules,
            grade=entry["grade"],
            alignment=alignment,
        ):
            quarantine.append({
                **queue_entry,
                "action": "quarantine",
                "reason": f"quarantine_if:{predicate}",
            })
    return review, quarantine


def _refresh_grade(
    item: dict[str, Any], grades: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    refreshed = dict(item)
    graded = grades.get(str(item.get("page_id")))
    if graded is not None and "grade" in item:
        refreshed["grade"] = graded["grade"]
        refreshed["grade_basis"] = graded["grade_basis"]
    return refreshed
