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
    assert span["witness"] == "х2 4- у"


def test_the_readers_own_doubt_is_a_region():
    assert only("Работа была [illegible].", "Работа была.")["kind"] == "region"
    span = only("Работа была написана [?].", "Работа была написана.")
    assert span["kind"] == "region" and span["base"] == "написана [?]"


def test_a_line_the_reader_dropped_is_one_span():
    span = only("Работа была написана.", "Работа была и потому всё написана.")
    # Anchored after the word the witness read just before it.
    assert (span["base"], span["witness"], span["start"]) == ("", "и потому всё", 11)


def test_words_only_the_reader_has_are_one_span():
    span = only("Работа и потому всё была.", "Работа была.")
    assert (span["kind"], span["base"], span["witness"]) == ("word", "и потому всё", "")


def test_span_ids_are_stable_and_distinct():
    base, witness = "Москва, 1962, 1962.", "Москва, 1963, 1963."
    first, second = spans(base, witness), spans(base, witness)
    assert [s["span_id"] for s in first] == [s["span_id"] for s in second]
    assert len({s["span_id"] for s in first}) == 2


def test_a_word_hyphenated_at_a_line_end_matches_its_two_halves():
    # Column order carried the second half of the hyphenated word elsewhere.
    assert spans("Работа в запад-\nной науке.", "Работа в запад- науке. ной") == []
    assert spans("Работа в запад-\nной науке.", "Работа в запад ной науке.") == []


def test_a_reader_that_splits_or_joins_a_word_is_contested():
    # The print has госсредств; the reader wrote two words.
    assert spans("Работа гос средств.", "Работа госсредств.", lexicon=None)
    assert spans("Работа западной науке.", "Работа запад ной науке.", lexicon=None)


def test_a_moved_word_is_a_difference():
    # The model moved the negation; no single word moves without being checked.
    assert spans("Это верно, и не иначе.", "Это не верно, и иначе.", lexicon=None)
    assert spans("Он не только пришёл", "Он только не пришёл", lexicon=None)


def test_a_moved_run_of_words_is_reading_order():
    assert (
        spans(
            "Первая строка здесь.\nВторая строка там.", "Вторая строка там.\nПервая строка здесь."
        )
        == []
    )


def test_a_substitution_is_found_where_the_model_made_it():
    # Column order moved the last sentence first, and the page repeats the word.
    base = "Работа полные сил. Работа была. Мир полные чаши."
    witness = "Мир полные чаши. Работа поныне сил. Работа была."
    span = only(base, witness, lexicon=None)
    assert (span["start"], span["base"], span["witness"]) == (7, "полные", "поныне")


def test_halves_of_a_hyphenated_word_are_not_taken_from_elsewhere():
    base = "он пошёл домой. Мы шли по-\nтом лесом. Книга лежит на столе."
    witness = "он пошёл по домой. Мы шли потем лесом. Книга лежит в том столе."
    found = spans(base, witness, lexicon=None)
    assert {s["witness"] for s in found} >= {"по", "потем"}


def test_a_compound_split_differently_is_the_same_words():
    assert spans("Работа северо-западный край.", "Работа северо-\nзападный край.") == []


def test_a_word_running_into_a_region_belongs_to_the_region():
    found = spans("Это сло-\nво[?] здесь.", "Это слово здесь.", lexicon=None)
    assert [s["kind"] for s in found] == ["region"]


# Receipts bind to the spans they answer, so the spans a rule set produces must not
# change under it. If this fails, the contest procedure changed: give it a new rule
# set name (ru-print-0.2) rather than updating these hashes.
GOLDEN = [
    ("Работа была написана поныне в Москве.", "Работа была нанисана полные в Москве."),
    ("АКАДЕМИЯ НАУК. Формула $x^2$ верна [?].", "АКАДЕМИЯ HAYK. Формула х2 верна."),
    ("Он был здесь вчера, сход-\nства нет.", "Он не был здесь, сходства нет 1962."),
    ("Мир полные чаши. Работа полные сил.", "Работа поныне сил. Мир полные чаши."),
]


def test_the_contest_procedure_is_pinned_to_its_rule_set():
    import hashlib
    import json

    from pageledger.contest import RULE_SETS

    produced = [
        contest(b, w, rules="ru-print-0.1", engine="rapidocr", known=known) for b, w in GOLDEN
    ]
    digest = hashlib.sha256(
        json.dumps(produced, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    rules = hashlib.sha256(
        json.dumps(RULE_SETS["ru-print-0.1"], sort_keys=True).encode()
    ).hexdigest()
    assert (digest, rules) == (
        "bab17339b831c7eeb93142864e38c074d9b0c6043e8435d71c7e7c97895f6927",
        "2517cff7f6aa6b857f5c8fbfe68c8cfc54c8dea76c7b1550f1f87fd19e183899",
    )
