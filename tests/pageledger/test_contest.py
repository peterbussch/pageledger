"""Contested spans, on the cases the October 2026 dogfood met in print."""

import pytest

from pageledger.contest import contest

WORDS = {
    "написана",
    "академия",
    "наук",
    "независимости",
    "производительность",
    "аутсайдерских",
    "предложение",
    "поныне",
    "полные",
    "сходства",
    "работа",
    "была",
    "в",
    "и",
    "потому",
    "всё",
    "формула",
    "верна",
}


def known(word):
    return word.lower() in WORDS


def spans(base, witness, engine="rapidocr", lexicon=known):
    found = contest(base, witness, rules="ru-print-0.1", engine=engine, known=lexicon)
    for span in found:
        assert base[span["start"] : span["end"]] == span["base"]
    return found


def only(base, witness, **kwargs):
    found = spans(base, witness, **kwargs)
    assert len(found) == 1, found
    return found[0]


def test_identical_readings_have_no_spans():
    assert spans("Работа была написана.", "Работа была написана.") == []


def test_a_block_read_in_another_order_is_not_a_difference():
    left, right = "Работа была написана.", "Академия наук поныне."
    assert spans(f"{left}\n{right}", f"{right}\n{left}") == []


def test_words_hyphenated_across_lines_match_and_keep_raw_offsets():
    assert spans("Черты сход-\nства.", "Черты сходства.") == []
    span = only("Черты сход-\nства.", "Черты сходетва.", lexicon=None)
    assert span["base"] == "сход-\nства"


@pytest.mark.parametrize(
    "witness,reader", [("АКАДЕМИЯ HAYK", "АКАДЕМИЯ НАУК"), ("ТОПОРОB", "ТОПОРОВ")]
)
def test_look_alike_letters_settle_to_the_reader(witness, reader):
    span = only(reader, witness)
    assert span["kind"] == "homoglyph"
    assert span["status"] == "settled" and span["settled_by"] == "rule:ru-print-0.1/H"
    assert span["text"] == span["base"]


def test_a_listed_glyph_confusion_settles_to_the_reader():
    span = only("Работа была написана.", "Работа была нанисана.")
    assert (span["kind"], span["status"], span["settled_by"]) == (
        "glyph",
        "settled",
        "rule:ru-print-0.1/G",
    )


@pytest.mark.parametrize(
    "printed,reader",
    [
        ("назависимости", "независимости"),
        ("призводительность", "производительность"),
        ("аудсайдерских", "аутсайдерских"),
    ],
)
def test_the_misprint_guard_keeps_printed_misprints_open(printed, reader):
    span = only(f"Работа {reader} была.", f"Работа {printed} была.")
    assert span["kind"] == "misprint_guard" and span["status"] == "open"


def test_glyph_pairs_never_cross_case():
    # The print has a capital the reader lowered; the rule must not hide it.
    span = only("Работа предложение.", "Работа Иредложение.")
    assert span["kind"] == "misprint_guard" and span["status"] == "open"


def test_glyph_pairs_belong_to_the_engine_that_makes_them():
    span = only("Работа была написана.", "Работа была нанисана.", engine="pdf_text")
    assert span["kind"] == "misprint_guard"


def test_without_a_lexicon_only_look_alikes_are_settled():
    assert only("Работа написана.", "Работа нанисана.", lexicon=None)["kind"] == "word"
    assert only("АКАДЕМИЯ НАУК", "АКАДЕМИЯ HAYK", lexicon=None)["kind"] == "homoglyph"


def test_two_real_words_stay_open():
    span = only("Работа поныне.", "Работа полные.")
    assert (span["kind"], span["base"], span["witness"]) == ("word", "поныне", "полные")


def test_numbers_are_never_settled_by_rule():
    span = only("Москва, 1962.", "Москва, 1963.")
    assert span["kind"] == "number" and span["status"] == "open"


def test_a_formula_is_one_region_and_absorbs_what_ocr_made_of_it():
    span = only("Формула $x^2 + y$ верна.", "Формула х2 4- у верна.")
    assert (span["kind"], span["base"], span["status"]) == ("region", "$x^2 + y$", "open")
    assert span["witness"] == "х2 4 у"


def test_the_readers_own_doubt_is_a_region():
    assert only("Работа была [illegible].", "Работа была.")["kind"] == "region"
    span = only("Работа была написана [?].", "Работа была написана.")
    assert span["kind"] == "region" and span["base"] == "написана [?]"


def test_a_line_the_reader_dropped_is_one_span():
    span = only("Работа была написана.", "Работа была и потому всё написана.")
    assert (span["base"], span["witness"], span["start"]) == ("", "и потому всё", 12)


def test_words_only_the_reader_has_are_one_span():
    span = only("Работа и потому всё была.", "Работа была.")
    assert (span["kind"], span["base"], span["witness"]) == ("word", "и потому всё", "")


def test_span_ids_are_stable_and_distinct():
    base, witness = "Москва, 1962, 1962.", "Москва, 1963, 1963."
    first, second = spans(base, witness), spans(base, witness)
    assert [s["span_id"] for s in first] == [s["span_id"] for s in second]
    assert len({s["span_id"] for s in first}) == 2


def test_a_word_hyphenated_at_a_line_end_matches_its_two_halves_anywhere():
    # Column order carried the second half of the hyphenated word elsewhere.
    assert spans("Работа в запад-\nной науке.", "Работа в запад науке. ной") == []


def test_a_reader_that_splits_or_joins_a_word_is_contested():
    # The print has госсредств; the reader wrote two words.
    assert spans("Работа гос средств.", "Работа госсредств.", lexicon=None)
    assert spans("Работа западной науке.", "Работа запад ной науке.", lexicon=None)
