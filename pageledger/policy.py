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
