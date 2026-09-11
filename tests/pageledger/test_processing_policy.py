"""Selection must not turn extraction scores or a retry into human review."""
import hashlib

import pytest

from pageledger.processing_policy import assess_page, validate_review

SOURCE = "a" * 64


def attempt(name="a1", stage="local_text", text="Readable text", **kwargs):
    return {
        "attempt_id": name, "stage": stage, "outcome": "completed",
        "raw_artifact": f"raw/{name}.txt",
        "raw_sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text,
        "format": "text", "warnings": [], "classification": {"type": "text", "reason": ""},
        "alignment": None, "usage": {}, "failure": None, **kwargs,
    }


def page(*attempts, **kwargs):
    return {"page_id": "doc_page_1", "page_number": 1, "source_sha256": SOURCE,
            "attempts": list(attempts), **kwargs}


def receipt(p, disposition="reviewed_text", selected="a1"):
    chosen = next((a for a in p["attempts"] if a["attempt_id"] == selected), None)
    return {"schema_version": "0.1", "source_sha256": SOURCE, "decisions": [{
        "page_id": p["page_id"], "page_number": p["page_number"],
        "disposition": disposition, "selected_attempt": selected,
        "output_sha256": chosen["raw_sha256"] if chosen else None,
        "reason": "Compared with source page", "reviewer": "Peter",
        "reviewed_at": "2026-09-11T12:00:00Z",
    }]}


def test_first_clean_candidate_wins_without_grade_or_model_confidence():
    result = assess_page(page(attempt(grade="F", confidence=0.1),
                              attempt("a2", "image", grade="A", confidence=1)))
    assert result["selected_attempt"] == "a1"
    assert result["disposition"] == "unreviewed_text"
    assert result["next_action"] == "review"


def test_clean_retry_replaces_defective_text_without_clearing_hold():
    p = page(attempt(warnings=["coverage_defect"]), attempt("a2", "local_ocr"))
    result = assess_page(p)
    assert result["selected_attempt"] == "a2"
    assert result["disposition"] == "coverage_defect"
    assert "coverage_defect" in result["review_reasons"]
    assert result["next_action"] == "review"
    reviewed = assess_page(p, receipt(p, selected="a2"))
    assert reviewed["disposition"] == "reviewed_text"
    assert "coverage_defect" in reviewed["review_reasons"]
    assert reviewed["next_action"] == "none"


def test_usable_defective_candidate_is_retained_during_escalation():
    result = assess_page(page(attempt(warnings=["missing_required_columns"])))
    assert result["selected_attempt"] == "a1"
    assert result["disposition"] == "coverage_defect"
    assert result["next_action"] == "local_ocr"


def test_persisted_hold_and_source_defect_cannot_be_model_cleared():
    result = assess_page(page(attempt("a2", "second_opinion", classification={
        "type": "reviewed_text", "reason": "certain"}),
        review_reasons=["source_defect"]))
    assert result["disposition"] == "source_defect"
    assert result["next_action"] == "review"


def test_blank_output_requires_explicit_review_and_preflight_receipt_can_bind():
    result = assess_page(page(attempt(text=" \n")))
    assert result["disposition"] == "blank_candidate"
    assert result["selected_attempt"] is None
    assert result["next_action"] == "local_ocr"
    p = page()
    assert assess_page(p, receipt(p, "reviewed_blank", None))["disposition"] == "reviewed_blank"


@pytest.mark.parametrize("outcome,disposition", [("failed", "provider_failure"),
                                                ("outcome_unknown", "outcome_unknown"),
                                                ("response", "outcome_unknown")])
def test_partial_and_unknown_attempts_are_never_selected(outcome, disposition):
    result = assess_page(page(attempt(outcome=outcome)))
    assert result["selected_attempt"] is None
    assert result["disposition"] == disposition


@pytest.mark.parametrize("fmt,left,right", [
    ("json", '[{"name":"A","debit":2,"credit":8}]', '[{"name":"A","debit":8,"credit":2}]'),
    ("csv", "name,debit,credit\nA,2,8\n", "name,debit,credit\nA,8,2\n"),
    ("markdown_table", "| name | debit | credit |\n| --- | --- | --- |\n| A | 2 | 8 |",
     "| name | debit | credit |\n| --- | --- | --- |\n| A | 8 | 2 |"),
])
def test_numeric_column_swap_is_a_hold_even_when_sum_agrees(fmt, left, right):
    result = assess_page(page(attempt(text=left, format=fmt),
                              attempt("a2", "second_opinion", text=right, format=fmt)))
    assert result["disposition"] == "numeric_column_conflict"
    assert result["selected_attempt"] == "a1"
    assert "numeric_column_conflict" in result["review_reasons"]


def test_numeric_associations_use_row_names_not_sum_or_row_order():
    a = attempt(text='[{"name":"A","count":2},{"name":"B","count":8}]', format="json")
    same = attempt("a2", "image", text='[{"name":"B","count":8},{"name":"A","count":2}]', format="json")
    assert assess_page(page(a, same))["disposition"] == "unreviewed_text"
    swapped = attempt("a3", "second_opinion", text='[{"name":"B","count":2},{"name":"A","count":8}]', format="json")
    assert assess_page(page(a, swapped))["disposition"] == "numeric_column_conflict"


def test_alignment_failures_and_explicit_numeric_warning_remain_visible():
    p = page(attempt(alignment={"columns": {"missing_required": ["credit"]}}),
             attempt("a2", "image", warnings=["numeric_column_conflict"]))
    result = assess_page(p)
    assert {"coverage_defect", "numeric_column_conflict"} <= set(result["review_reasons"])


@pytest.mark.parametrize("outcome,disposition", [("failed", "provider_failure"),
                                                ("outcome_unknown", "outcome_unknown")])
def test_failed_escalation_stops_and_exposes_uncertainty_with_prior_text(outcome, disposition):
    p = page(attempt(warnings=["coverage_defect"]),
             attempt("a2", "image", outcome=outcome))
    result = assess_page(p)
    assert result["disposition"] == disposition
    assert result["selected_attempt"] == "a1"
    assert "coverage_defect" in result["review_reasons"]
    assert result["next_action"] == "review"


@pytest.mark.parametrize("classification", ["sparse", "fragmented", "joined", "unknown"])
def test_weak_native_structure_routes_to_ocr(classification):
    p = page(attempt(classification={"type": classification, "reason": "native structure signal"}))
    result = assess_page(p)
    assert result["disposition"] == "coverage_defect"
    assert result["next_action"] == "local_ocr"


def test_unchecked_alignment_is_not_treated_as_a_pass():
    p = page(attempt(alignment={"checks": [{"rows_unchecked": 1}],
                               "metrics": {"arithmetic_pass_rate": None}}))
    assert assess_page(p)["disposition"] == "numeric_column_conflict"


@pytest.mark.parametrize("field,value", [("page_id", "other"), ("page_number", 2),
                                        ("output_sha256", "b" * 64),
                                        ("selected_attempt", "missing"),
                                        ("reviewer", ""), ("reason", ""),
                                        ("reviewed_at", "yesterday")])
def test_review_rejects_invalid_binding_or_missing_receipt(field, value):
    p = page(attempt())
    review = receipt(p)
    review["decisions"][0][field] = value
    with pytest.raises(ValueError):
        validate_review(review, p)


def test_review_rejects_other_source_and_partial_output():
    p = page(attempt())
    review = receipt(p)
    review["source_sha256"] = "b" * 64
    with pytest.raises(ValueError):
        validate_review(review, p)
    p["attempts"][0]["outcome"] = "failed"
    with pytest.raises(ValueError):
        validate_review(receipt(p), p)
    with pytest.raises(ValueError):
        validate_review(receipt(page(), selected=None), page())


@pytest.mark.parametrize("warning", ["replacement_characters", "control_characters",
                                    "suspicious_symbol_density", "low_confidence",
                                    "instruction_echo"])
def test_existing_quality_warning_overrides_prose_grade_and_survives_clean_retry(warning):
    native = attempt(grade="A", confidence=1, warnings=[warning],
                     classification={"type": "prose", "reason": "prose_text"})
    result = assess_page(page(native))
    assert result["disposition"] == "coverage_defect"
    assert result["next_action"] == "local_ocr"
    retried = assess_page(page(native, attempt("a2", "local_ocr")))
    assert retried["selected_attempt"] == "a2"
    assert retried["disposition"] == "coverage_defect"
    assert "coverage_defect" in retried["review_reasons"]
    assert retried["next_action"] == "review"


def test_historical_orthography_is_preserved_without_forcing_extraction_rewrite():
    p = page(attempt(text="Historical orthography retained.", warnings=["historical_orthography"]))
    result = assess_page(p)
    assert result["selected_attempt"] == "a1"
    assert result["disposition"] == "unreviewed_text"
    assert result["next_action"] == "review"
