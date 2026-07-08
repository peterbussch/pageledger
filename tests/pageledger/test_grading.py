"""Grading tests: band boundaries, axis combination, threshold config.

Covers:
  - confidence band boundaries at every default threshold
  - warning-count bands and the empty_text hard F
  - schema axis: parse_error / zero rows / all-required-missing F paths
  - coverage and pass-rate bands, coercion-error cap at B
  - schema.quality floors (coverage floor F, low-confidence C cap)
  - final grade = worst of axes; basis labeling
  - threshold overrides merged over defaults; invalid overrides rejected
  - grade_distribution tolerance of ungraded (pre-0.1.3) entries
"""

from __future__ import annotations

import pytest

from pageledger.grading import (
    DEFAULT_THRESHOLDS,
    format_grade,
    grade_distribution,
    grade_is_below,
    grade_page,
    merge_thresholds,
    validate_thresholds,
    worst_grade,
)

THRESHOLDS = merge_thresholds(None)


def _entry(confidence=None, warnings=()):
    return {"confidence": confidence, "warnings": list(warnings)}


def _alignment(
    *,
    parse_error=None,
    row_count=5,
    required_column_coverage=1.0,
    arithmetic_pass_rate=None,
    coercion_error_count=0,
    missing_required=(),
):
    return {
        "columns": {"missing_required": list(missing_required)},
        "metrics": {
            "parse_error": parse_error,
            "row_count": row_count,
            "required_column_coverage": required_column_coverage,
            "arithmetic_pass_rate": arithmetic_pass_rate,
            "coercion_error_count": coercion_error_count,
        },
    }


def _grade(entry, alignment=None, thresholds=THRESHOLDS, **kwargs):
    return grade_page(entry, alignment, thresholds, **kwargs)


# =========================================================================
# Signals axis
# =========================================================================


@pytest.mark.parametrize(
    "confidence, expected",
    [(0.95, "A"), (0.90, "A"), (0.89, "B"), (0.80, "B"), (0.79, "C"), (0.70, "C"), (0.69, "D"), (0.55, "D"), (0.54, "F")],
)
def test_confidence_band_boundaries(confidence, expected):
    result = _grade(_entry(confidence=confidence))
    assert result["grade"] == expected
    assert result["grade_basis"] == "signals_only"
    assert result["grade_detail"]["confidence_band"] == expected


@pytest.mark.parametrize(
    "warnings, expected",
    [((), "A"), (("short_text",), "B"), (("short_text", "fragmented_text"), "C"), (("a", "b", "c"), "D")],
)
def test_warning_count_bands(warnings, expected):
    result = _grade(_entry(warnings=warnings))
    assert result["grade"] == expected
    assert result["grade_detail"]["confidence_band"] is None


def test_empty_text_forces_f():
    result = _grade(_entry(confidence=0.99, warnings=["empty_text"]))
    assert result["grade"] == "F"
    assert "empty_text forces F" in result["grade_detail"]["reasons"]


def test_signals_grade_is_worst_of_confidence_and_warnings():
    result = _grade(_entry(confidence=0.95, warnings=["short_text", "fragmented_text"]))
    assert result["grade"] == "C"  # warnings drag the A confidence down


# =========================================================================
# Schema axis
# =========================================================================


def test_schema_parse_error_is_f():
    result = _grade(_entry(confidence=0.95), _alignment(parse_error="invalid_json"))
    assert result["grade"] == "F"
    assert result["grade_basis"] == "schema_aware"
    assert result["grade_detail"]["schema_grade"] == "F"
    assert result["grade_detail"]["signals_grade"] == "A"


def test_zero_rows_is_f():
    assert _grade(_entry(), _alignment(row_count=0))["grade"] == "F"


def test_all_required_missing_is_f():
    alignment = _alignment(required_column_coverage=0.0, missing_required=["a", "b"])
    result = _grade(_entry(), alignment)
    assert result["grade_detail"]["schema_grade"] == "F"


@pytest.mark.parametrize(
    "coverage, expected",
    [(1.0, "A"), (0.95, "B"), (0.9, "B"), (0.8, "C"), (0.7, "C"), (0.5, "D")],
)
def test_coverage_bands(coverage, expected):
    result = _grade(_entry(), _alignment(required_column_coverage=coverage))
    assert result["grade_detail"]["schema_grade"] == expected


@pytest.mark.parametrize(
    "pass_rate, expected",
    [(1.0, "A"), (0.98, "A"), (0.95, "B"), (0.90, "B"), (0.80, "C"), (0.75, "C"), (0.5, "D")],
)
def test_pass_rate_bands(pass_rate, expected):
    result = _grade(_entry(), _alignment(arithmetic_pass_rate=pass_rate))
    assert result["grade_detail"]["schema_grade"] == expected


def test_null_pass_rate_axis_ignored():
    result = _grade(_entry(), _alignment(arithmetic_pass_rate=None))
    assert result["grade_detail"]["schema_grade"] == "A"
    assert result["grade_detail"]["arithmetic_pass_rate"] is None


def test_coercion_errors_cap_schema_grade_at_b():
    result = _grade(_entry(), _alignment(coercion_error_count=3))
    assert result["grade_detail"]["schema_grade"] == "B"
    # but a worse grade is not improved by the cap
    worse = _grade(_entry(), _alignment(arithmetic_pass_rate=0.5, coercion_error_count=3))
    assert worse["grade_detail"]["schema_grade"] == "D"


def test_final_grade_is_worst_of_axes():
    result = _grade(_entry(confidence=0.6), _alignment())
    assert result["grade_detail"]["signals_grade"] == "D"
    assert result["grade_detail"]["schema_grade"] == "A"
    assert result["grade"] == "D"
    assert result["grade_basis"] == "schema_aware"


# =========================================================================
# schema.quality floors
# =========================================================================


class _Floors:
    def __init__(self, coverage=None, low_confidence=None):
        self.minimum_required_column_coverage = coverage
        self.low_confidence_threshold = low_confidence


def test_coverage_floor_forces_f():
    alignment = _alignment(required_column_coverage=0.9)
    result = _grade(_entry(), alignment, quality_floors=_Floors(coverage=1.0))
    assert result["grade_detail"]["schema_grade"] == "F"


def test_low_confidence_threshold_caps_at_c():
    result = _grade(
        _entry(confidence=0.82), _alignment(), quality_floors=_Floors(low_confidence=0.85)
    )
    assert result["grade"] == "C"
    # an already-worse grade is not lifted to C
    worse = _grade(
        _entry(confidence=0.30), _alignment(), quality_floors=_Floors(low_confidence=0.85)
    )
    assert worse["grade"] == "F"


# =========================================================================
# Thresholds and helpers
# =========================================================================


def test_threshold_overrides_merge_over_defaults():
    merged = merge_thresholds({"confidence": {"A": 0.95}})
    assert merged["confidence"]["A"] == 0.95
    assert merged["confidence"]["B"] == DEFAULT_THRESHOLDS["confidence"]["B"]
    result = grade_page(_entry(confidence=0.92), None, merged)
    assert result["grade"] == "B"


@pytest.mark.parametrize(
    "overrides, message",
    [
        ("nope", "must be a mapping"),
        ({"unknown_axis": {}}, "not a known axis"),
        ({"confidence": {"F": 0.1}}, "grade letters A-D"),
        ({"confidence": {"A": 2}}, "between 0 and 1"),
        ({"confidence": {"A": 0.5}}, "non-increasing"),
    ],
)
def test_invalid_threshold_overrides_rejected(overrides, message):
    with pytest.raises(ValueError, match=message):
        validate_thresholds(overrides)


def test_config_exposes_grading_knobs(tmp_path):
    from pageledger.config import load_config

    config = tmp_path / "config.yml"
    config.write_text(
        "schema_version: '0.1'\n"
        "run:\n"
        "  grading:\n"
        "    review_below_grade: c\n"
        "    thresholds:\n"
        "      confidence: {A: 0.95}\n",
        encoding="utf-8",
    )
    loaded = load_config(config)
    assert loaded.review_below_grade == "C"
    assert loaded.grading_thresholds["confidence"]["A"] == 0.95

    config.write_text(
        "schema_version: '0.1'\nrun:\n  grading:\n    review_below_grade: E\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="review_below_grade"):
        load_config(config)


def test_grade_helpers():
    assert worst_grade("A", "C", None) == "C"
    assert grade_is_below("D", "C") is True
    assert grade_is_below("C", "C") is False
    assert format_grade("A", "signals_only") == "A (signals)"
    assert format_grade("B", "schema_aware") == "B (schema)"
    assert format_grade(None, None) == ""


def test_grade_distribution_skips_ungraded_entries():
    entries = [{"grade": "A"}, {"grade": "F"}, {"grade": "A"}, {}]
    assert grade_distribution(entries) == {"A": 2, "B": 0, "C": 0, "D": 0, "F": 1}
