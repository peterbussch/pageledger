"""Bounded word-level comparison of extraction attempts."""

from pageledger.comparison import MAX_TEXT_CHARS, compare_texts


def test_identical_and_whitespace_only_texts_agree():
    assert compare_texts("A\u0301  word\nagain", "Á word again")["agreement_ratio"] == 1


def test_changed_number_is_reported_in_context():
    result = compare_texts("Total | 1 200 rubles", "Total | 1 250 rubles")
    assert result["number_differences"]
    assert {item["number"] for item in result["number_differences"]} == {"1 200", "1 250"}
    assert all("Total" in item["context"] for item in result["number_differences"])


def test_adjacent_small_numbers_are_not_grouped():
    result = compare_texts("page 12 13", "page 12 14")
    assert {item["number"] for item in result["number_differences"]} == {"13", "14"}


def test_missing_paragraph_has_disagreement_span():
    result = compare_texts(
        "one two three. missing paragraph here. four five", "one two three. four five"
    )
    assert result["agreement_ratio"] < 1
    assert result["spans"]


def test_text_length_is_capped_and_reported():
    result = compare_texts("a " * MAX_TEXT_CHARS, "a " * MAX_TEXT_CHARS)
    assert result["truncated"] is True
    assert result["agreement_ratio"] == 1
