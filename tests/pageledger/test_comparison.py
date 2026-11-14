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


def test_marked_words_keep_their_combining_marks():
    """Hindi readings that differ in vowel signs are not taken for agreement (hold policy 0.9)."""
    left, right = "राम की किताब यहाँ है", "राम कि कताब यहां ह"
    assert compare_texts(left, right)["agreement_ratio"] == 0.8
    assert compare_texts(left, right, marks=True)["agreement_ratio"] == 0.2
    assert compare_texts("ми́р и мир", "ми́р и мир", marks=True)["agreement_ratio"] == 1.0
    assert compare_texts("1 000 и 12,5", "1 000 и 12,5", marks=True)["agreement_ratio"] == 1.0


def test_only_hold_policy_09_jobs_compare_marked_words():
    from pageledger.processing_policy import HOLD_POLICIES, MARKED_WORD_POLICIES

    assert MARKED_WORD_POLICIES == {"0.9"}
    assert HOLD_POLICIES - MARKED_WORD_POLICIES == {"0.6", "0.6.1", "0.8"}
