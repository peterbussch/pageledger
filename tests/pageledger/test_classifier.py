"""Unit coverage for the structural classifier and hook protocol."""

from __future__ import annotations

from pathlib import Path

import pytest

from pageledger.classifier import (
    DEFAULT_CLASSIFY_THRESHOLDS,
    ClassificationResult,
    classifier_conformance_check,
    classify_signals,
    load_classifier_hook,
    merge_classify_thresholds,
    structural_signals,
)


def _decision(
    text: str,
    *,
    result_format: str = "text",
    detail: dict | None = None,
    pdf_embedded_text_probe: bool = False,
    thresholds: dict | None = None,
) -> ClassificationResult:
    signals = structural_signals(
        text,
        result_format=result_format,
        confidence_detail=detail,
    )
    return classify_signals(
        signals,
        thresholds or dict(DEFAULT_CLASSIFY_THRESHOLDS),
        pdf_embedded_text_probe=pdf_embedded_text_probe,
    )


def test_structural_signals_include_line_and_quality_evidence() -> None:
    signals = structural_signals(
        "Name  2024\nAlice | 17\n",
        result_format="text",
        confidence_detail={"below_60_ratio": 0.25},
    )

    assert signals["nonempty_line_count"] == 2
    assert signals["pipe_line_ratio"] == 0.5
    assert signals["column_line_ratio"] == 0.5
    assert signals["digit_ratio"] > 0
    assert signals["alpha_token_count"] == signals["word_count"]
    assert signals["below_60_ratio"] == 0.25


def test_empty_probe_capability_controls_blank_vs_unknown() -> None:
    assert _decision("") == ClassificationResult("blank", 0.95, "blank_text")
    assert _decision("   \n\t") == ClassificationResult("blank", 0.95, "blank_text")
    assert _decision("", pdf_embedded_text_probe=True) == ClassificationResult(
        "unknown", None, "empty_pdf_text_ambiguous"
    )
    assert _decision("   \n", pdf_embedded_text_probe=True) == ClassificationResult(
        "unknown", None, "empty_pdf_text_ambiguous"
    )


def test_structured_payload_precedes_sparse() -> None:
    assert _decision('{"a": 1}', result_format="json") == ClassificationResult(
        "table_likely", 0.85, "structured_payload:json"
    )


def test_pipe_density_branch() -> None:
    result = _decision("name | value\nalpha | 10\nbeta | 20")
    assert result == ClassificationResult("table_likely", 0.75, "pipe_line_density")


def test_column_and_digit_density_branch() -> None:
    result = _decision("AA  123456\nBB  234567\nCC  345678")
    assert result == ClassificationResult("table_likely", 0.6, "column_digit_density")


def test_sparse_column_evidence_still_detects_digit_dense_ocr_tables() -> None:
    lines = ["1234567890" for _ in range(49)] + ["row  123456"]
    result = _decision("\n".join(lines))
    assert result == ClassificationResult("table_likely", 0.6, "column_digit_density")


def test_fragmented_branch_precedes_sparse() -> None:
    result = _decision(" ".join(["aa"] * 20))
    assert result == ClassificationResult("unknown", None, "fragmented_text")


def test_sparse_and_prose_branches() -> None:
    assert _decision("A short page with a few words.").type == "sparse"
    assert _decision(" ".join(["ordinary"] * 26)) == ClassificationResult(
        "prose", 0.7, "prose_text"
    )


def test_low_word_confidence_reduces_fixed_confidence() -> None:
    result = _decision(
        " ".join(["ordinary"] * 26),
        detail={"below_60_ratio": 0.25},
    )
    assert result == ClassificationResult(
        "prose", 0.5, "prose_text+low_word_confidence"
    )

    with pytest.raises(ValueError, match="below_60_ratio must be between 0 and 1"):
        structural_signals(
            "text", result_format="text", confidence_detail={"below_60_ratio": 1.5}
        )


def test_threshold_overrides_are_merged_and_validated() -> None:
    thresholds = merge_classify_thresholds({"sparse_max_words": 2})
    assert _decision("three ordinary words", thresholds=thresholds).type == "prose"
    assert thresholds["table_min_lines"] == DEFAULT_CLASSIFY_THRESHOLDS["table_min_lines"]

    with pytest.raises(ValueError, match="Unknown classify.thresholds"):
        merge_classify_thresholds({"typo": 1})
    with pytest.raises(ValueError, match="must be an integer"):
        merge_classify_thresholds({"table_min_lines": 2.5})
    with pytest.raises(ValueError, match="between 0 and 1"):
        merge_classify_thresholds({"table_digit_ratio": 1.1})


def test_hook_load_conformance_and_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = tmp_path / "hook_module.py"
    module.write_text(
        "class Hook:\n"
        "    name = 'domain'\n"
        "    version = '1.0'\n"
        "    page_types = ('letter',)\n"
        "    def __init__(self, label='letter'):\n"
        "        self.label = label\n"
        "    def classify_page(self, **kwargs):\n"
        "        return None\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    hook = load_classifier_hook("hook_module:Hook", {"label": "letter"})
    assert hook.name == "domain"
    assert hook.label == "letter"
    assert classifier_conformance_check(hook) == []
    assert classifier_conformance_check(object())
