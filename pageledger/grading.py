"""Per-page quality grading (A–F) from quality signals and schema alignment.

A grade is a deterministic summary of recorded evidence, not a calibrated
accuracy estimate. Confidence values come from uncalibrated extractors, so
grades are only comparable when the effective extractor identity matches.
Every rendered surface labels the basis — ``A (signals)`` and ``A (schema)``
are never the same string — because a page graded on text signals alone
carries far weaker evidence than one whose records passed schema checks.
"""

from __future__ import annotations

import math
from typing import Any

GRADES = ("A", "B", "C", "D", "F")

_GRADE_ORDER = {grade: index for index, grade in enumerate(GRADES)}

# Bands are "the letter earned at or above this value". Confidence defaults
# port the soviet-corpus quality tracker; coverage/pass-rate bands grade the
# schema axis. All are overridable via run.grading.thresholds.
DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "confidence": {"A": 0.90, "B": 0.80, "C": 0.70, "D": 0.55},
    "required_column_coverage": {"A": 1.0, "B": 0.9, "C": 0.7},
    "arithmetic_pass_rate": {"A": 0.98, "B": 0.90, "C": 0.75},
}

_BASIS_LABELS = {"signals_only": "signals", "schema_aware": "schema"}


def worst_grade(*grades: str | None) -> str:
    present = [grade for grade in grades if grade is not None]
    return max(present, key=lambda grade: _GRADE_ORDER[grade])


def grade_is_below(grade: str, threshold: str) -> bool:
    """True when *grade* is strictly worse than *threshold*."""
    return _GRADE_ORDER[grade] > _GRADE_ORDER[threshold]


def format_grade(grade: str | None, basis: str | None) -> str:
    if grade is None:
        return ""
    label = _BASIS_LABELS.get(basis or "", basis)
    return f"{grade} ({label})" if label else grade


def grade_basis_label(basis: str) -> str:
    """Return the compact public label for a recorded grade basis."""
    return _BASIS_LABELS.get(basis, basis)


def merge_thresholds(overrides: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    merged = {axis: dict(bands) for axis, bands in DEFAULT_THRESHOLDS.items()}
    for axis, bands in (overrides or {}).items():
        merged[axis].update(bands)
    return merged


def grade_page(
    quality_entry: dict[str, Any],
    alignment: dict[str, Any] | None,
    thresholds: dict[str, dict[str, float]],
    *,
    quality_floors: Any = None,
) -> dict[str, Any]:
    """Compute grade fields for one quality.jsonl entry.

    Returns ``{"grade", "grade_basis", "grade_detail"}``. The final grade is
    the worst of the signal and schema axes — evidence of a problem on
    either axis is never averaged away.
    """
    reasons: list[str] = []

    warnings = quality_entry.get("warnings") or []
    confidence = quality_entry.get("confidence")

    confidence_band: str | None = None
    if confidence is not None:
        confidence_band = _band(confidence, thresholds["confidence"], floor="F")
        if confidence_band != "A":
            reasons.append(f"confidence {confidence:.2f} in {confidence_band} band")

    warning_count = len(warnings)
    if warning_count == 0:
        warning_band = "A"
    elif warning_count == 1:
        warning_band = "B"
    elif warning_count == 2:
        warning_band = "C"
    else:
        warning_band = "D"
    if warning_count:
        reasons.append(
            f"{warning_count} quality warning{'s' if warning_count != 1 else ''}: "
            + ", ".join(warnings)
        )

    if "empty_text" in warnings:
        signals_grade = "F"
        reasons.append("empty_text forces F")
    else:
        signals_grade = worst_grade(confidence_band, warning_band)

    schema_grade: str | None = None
    coverage: float | None = None
    pass_rate: float | None = None
    if alignment is not None:
        metrics = alignment["metrics"]
        coverage = metrics["required_column_coverage"]
        pass_rate = metrics["arithmetic_pass_rate"]
        schema_grade, schema_reasons = _schema_grade(metrics, alignment, thresholds, quality_floors)
        reasons.extend(schema_reasons)

    if schema_grade is None:
        basis = "signals_only"
        grade = signals_grade
    else:
        basis = "schema_aware"
        grade = worst_grade(signals_grade, schema_grade)

    low_confidence_floor = getattr(quality_floors, "low_confidence_threshold", None)
    if (
        low_confidence_floor is not None
        and confidence is not None
        and confidence < low_confidence_floor
        and not grade_is_below(grade, "C")
    ):
        grade = "C"
        reasons.append(
            f"confidence {confidence:.2f} below schema.quality.low_confidence_threshold "
            f"{low_confidence_floor:.2f} caps grade at C"
        )

    return {
        "grade": grade,
        "grade_basis": basis,
        "grade_detail": {
            "signals_grade": signals_grade,
            "schema_grade": schema_grade,
            "confidence_band": confidence_band,
            "warning_count": warning_count,
            "required_column_coverage": coverage,
            "arithmetic_pass_rate": pass_rate,
            "reasons": reasons,
        },
    }


def grade_distribution(entries: list[dict[str, Any]]) -> dict[str, int]:
    """Count graded pages per letter. Ungraded entries (pre-0.1.3 runs) are skipped."""
    distribution = {grade: 0 for grade in GRADES}
    for entry in entries:
        grade = entry.get("grade")
        if grade in distribution:
            distribution[grade] += 1
    return distribution


def grade_distributions_by_basis(
    entries: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """Count grades without combining unlike evidence bases.

    Legacy graded entries without ``grade_basis`` are isolated under
    ``unknown`` rather than being presented as signals- or schema-backed.
    """
    buckets: dict[str, dict[str, int]] = {}
    for entry in entries:
        grade = entry.get("grade")
        if grade not in GRADES:
            continue
        raw_basis = entry.get("grade_basis")
        basis = raw_basis if isinstance(raw_basis, str) and raw_basis else "unknown"
        distribution = buckets.setdefault(basis, {letter: 0 for letter in GRADES})
        distribution[grade] += 1

    ordered: dict[str, dict[str, int]] = {}
    for basis in ("signals_only", "schema_aware", "unknown"):
        if basis in buckets:
            ordered[basis] = buckets.pop(basis)
    for basis in sorted(buckets):
        ordered[basis] = buckets[basis]
    return ordered


def _schema_grade(
    metrics: dict[str, Any],
    alignment: dict[str, Any],
    thresholds: dict[str, dict[str, float]],
    quality_floors: Any,
) -> tuple[str, list[str]]:
    reasons: list[str] = []

    if metrics["parse_error"] is not None:
        return "F", [f"schema parse_error: {metrics['parse_error']}"]
    if metrics["row_count"] == 0:
        return "F", ["no records extracted"]

    coverage = metrics["required_column_coverage"]
    if alignment["columns"]["missing_required"] and coverage == 0.0:
        return "F", ["all required columns missing"]

    coverage_floor = getattr(quality_floors, "minimum_required_column_coverage", None)
    if coverage_floor is not None and coverage < coverage_floor:
        return "F", [
            f"required_column_coverage {coverage:.2f} below "
            f"schema.quality.minimum_required_column_coverage {coverage_floor:.2f}"
        ]

    coverage_band = _band(coverage, thresholds["required_column_coverage"], floor="D")
    if coverage_band != "A":
        reasons.append(f"required_column_coverage {coverage:.2f} in {coverage_band} band")

    pass_rate = metrics["arithmetic_pass_rate"]
    pass_rate_band: str | None = None
    if pass_rate is not None:
        pass_rate_band = _band(pass_rate, thresholds["arithmetic_pass_rate"], floor="D")
        if pass_rate_band != "A":
            reasons.append(f"arithmetic_pass_rate {pass_rate:.2f} in {pass_rate_band} band")

    grade = worst_grade(coverage_band, pass_rate_band)

    if metrics["coercion_error_count"] > 0 and not grade_is_below(grade, "B"):
        grade = "B"
        reasons.append(
            f"{metrics['coercion_error_count']} coercion error"
            f"{'s' if metrics['coercion_error_count'] != 1 else ''} cap schema grade at B"
        )

    structure_issue_count = metrics.get("structure_issue_count", 0)
    if structure_issue_count > 0 and not grade_is_below(grade, "B"):
        grade = "B"
        reasons.append(
            f"{structure_issue_count} structural issue"
            f"{'s' if structure_issue_count != 1 else ''} cap schema grade at B"
        )

    return grade, reasons


def _band(value: float, bands: dict[str, float], *, floor: str) -> str:
    for grade in GRADES[:-1]:
        threshold = bands.get(grade)
        if threshold is not None and value >= threshold:
            return grade
    return floor


def validate_thresholds(overrides: Any) -> None:
    """Validate run.grading.thresholds overrides; raise ValueError with key paths."""
    if overrides is None:
        return
    if not isinstance(overrides, dict):
        raise ValueError("run.grading.thresholds must be a mapping")
    for axis, bands in overrides.items():
        if axis not in DEFAULT_THRESHOLDS:
            raise ValueError(
                f"run.grading.thresholds.{axis} is not a known axis; expected one of: "
                + ", ".join(sorted(DEFAULT_THRESHOLDS))
            )
        if not isinstance(bands, dict):
            raise ValueError(f"run.grading.thresholds.{axis} must be a mapping")
        merged = dict(DEFAULT_THRESHOLDS[axis])
        for letter, value in bands.items():
            if letter not in _GRADE_ORDER or letter == "F":
                raise ValueError(f"run.grading.thresholds.{axis} keys must be grade letters A-D")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"run.grading.thresholds.{axis}.{letter} must be a number between 0 and 1"
                )
            if not math.isfinite(value) or value < 0 or value > 1:
                raise ValueError(
                    f"run.grading.thresholds.{axis}.{letter} must be a number between 0 and 1"
                )
            merged[letter] = float(value)
        ordered = [merged[letter] for letter in ("A", "B", "C", "D") if letter in merged]
        if any(earlier < later for earlier, later in zip(ordered, ordered[1:], strict=False)):
            raise ValueError(f"run.grading.thresholds.{axis} must be non-increasing from A to D")
