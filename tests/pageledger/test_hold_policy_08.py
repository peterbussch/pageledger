"""Hold policy 0.8: a junk text layer is outvoted, and a reader that stops early is held."""

import pytest
from test_escalation import _attempt, _page

from pageledger.processing_policy import assess_page
from pageledger.quality import _model_remarks

GENERATIVE = ["ocr", "page_image", "generative"]
DELTA = "suspicious_embedded_text_delta"


def _compared(left, right, ratio):
    return {
        "left_attempt": left,
        "right_attempt": right,
        "agreement_ratio": ratio,
        "spans": [],
        "number_differences": [],
    }


def _junk_layer_page(ocr_reader_ratio=0.85):
    # The 1934 directory cover: LoC's own OCR is the PDF layer; RapidOCR and the model
    # read the page well, and both are twice as long as the junk.
    layer = _attempt("a1", "local_text", "Wl ASHI NGT0N ~~ dlr")
    ocr = _attempt("a2", "local_ocr", "WASHINGTON DIRECTORY 1934", warnings=[DELTA])
    reader = _attempt(
        "a3",
        "image",
        "WASHINGTON DIRECTORY 1934",
        warnings=[DELTA],
        adapter_capabilities=GENERATIVE,
    )
    comparisons = [
        _compared("a1", "a2", 0.2),
        _compared("a1", "a3", 0.25),
        _compared("a2", "a3", ocr_reader_ratio),
    ]
    return _page(layer, ocr, reader, comparisons=comparisons)


def test_two_agreeing_readings_outvote_a_junk_text_layer():
    page = _junk_layer_page()
    before = assess_page(page)
    assert before["selected_attempt"] == "a1"  # 0.6.1: the junk wins
    after = assess_page(page, layer_outvoted=True)
    assert after["selected_attempt"] == "a2"
    assert "coverage_defect" in after["review_reasons"]  # the layer's hold stays on the page


def test_readings_that_disagree_with_each_other_do_not_outvote_the_layer():
    after = assess_page(_junk_layer_page(ocr_reader_ratio=0.3), layer_outvoted=True)
    assert after["selected_attempt"] == "a1"


def test_a_reader_with_half_the_literal_words_is_held():
    ocr = _attempt("a2", "local_ocr", " ".join(f"word{i}" for i in range(100)))
    reader = _attempt(
        "a3",
        "image",
        " ".join(f"word{i}" for i in range(11)),
        adapter_capabilities=GENERATIVE,
    )
    page = _page(_attempt("a1", "local_text", " "), ocr, reader)
    held = assess_page(page, reader_coverage=True)
    assert held["selected_attempt"] == "a2"
    assert held["disposition"] == "coverage_defect"
    full = {**reader, "text": ocr["text"]}
    assert (
        assess_page(_page(page["attempts"][0], ocr, full), reader_coverage=True)["disposition"]
        != "coverage_defect"
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "Smith J 12 Main\n\n**Note:** The page is a low-resolution microfilm image; the "
            "remaining entries are too small to read.",
            ["model_commentary"],
        ),
        (
            "Sobel A 2201\n[Remaining lines of the left page and the entire page 427 are not "
            "reliably legible at this resolution and are omitted.]",
            ["partial_transcription"],
        ),
        ("Smith J 12 Main [illegible] 7550 [?]", []),
        ("Note: figures for 1926 exclude the Far Eastern region.", []),
        ("Table 4.\n\nNote: this transcription leaves out the marginalia.", ["model_commentary"]),
    ],
)
def test_a_model_remarking_on_its_own_reading_is_flagged(text, expected):
    assert _model_remarks(text) == expected


def test_an_inherited_ocr_layer_is_read_again_by_local_ocr():
    layer = _attempt(
        "a1", "local_text", "Readable text from an old OCR", warnings=["ocr_text_layer"]
    )
    stays = assess_page(_page(layer))
    assert stays["next_action"] == "review"
    climbs = assess_page(_page(layer), ocr_layer_climbs=True)
    assert climbs["next_action"] == "local_ocr"
    assert climbs["triggers"] == [{"trigger": "ocr_layer", "stage": "local_text", "attempt": "a1"}]
    assert climbs["disposition"] == "unreviewed_text"
    # Once our own OCR has read the page, the page rests on that reading.
    ocr = _attempt("a2", "local_ocr", "Readable text from an old OCR")
    agreed = assess_page(
        _page(layer, ocr, comparisons=[_compared("a1", "a2", 1.0)]),
        ocr_layer_climbs=True,
        escalate_on=("hold", "disagreement"),
    )
    assert (agreed["next_action"], agreed["selected_attempt"]) == ("review", "a2")
    assert [t["trigger"] for t in agreed["triggers"]] == ["ocr_layer"]


def test_an_inherited_layer_is_set_aside_once_our_own_ocr_has_read_the_page():
    # The 1922 pamphlet: IA's layer misreads 170 and the words under the underlining; every
    # better reading then disagreed with it and the page climbed to the paid reader.
    layer = _attempt(
        "a1", "local_text", "170 ра очих даже Да-. ЛЕКИХ", warnings=["ocr_text_layer", "sparse"]
    )
    ocr = _attempt("a2", "local_ocr", "170 рабочих даже далеких")
    page = _page(layer, ocr, comparisons=[{**_compared("a1", "a2", 0.3), "number_differences": []}])
    result = assess_page(page, ocr_layer_climbs=True, escalate_on=("hold", "disagreement"))
    assert result["selected_attempt"] == "a2"
    assert result["next_action"] == "review"
    assert "engine_disagreement" not in result["review_reasons"]
    assert "coverage_defect" not in result["review_reasons"]  # the layer's own hold
    assert result["disposition"] == "unreviewed_text"


LADDER = ("local_text", "local_ocr", "second_ocr", "local_image", "image", "second_opinion")


def _layer_and_indra(ratio):
    # The 1914 pre-reform book (pageledger-08b): IA's layer agrees with Indra on 15% of
    # words, P18 sets it aside, and Indra's reading (CER 0.18) was accepted unread by anyone else.
    layer = _attempt("a1", "local_text", "въ Карпатской Руси", warnings=["ocr_text_layer"])
    ocr = _attempt("a2", "local_ocr", "в Карпатской Руси")
    return _page(layer, ocr, comparisons=[_compared("a1", "a2", ratio)])


def test_an_ocr_reading_no_other_engine_confirms_climbs_to_the_second_ocr():
    result = assess_page(
        _layer_and_indra(0.15), ocr_layer_climbs=True, corroborate=True, stages=LADDER
    )
    assert result["selected_attempt"] == "a2"
    assert result["next_action"] == "second_ocr"
    assert result["triggers"][-1] == {
        "trigger": "uncorroborated",
        "stage": "local_ocr",
        "attempt": "a2",
    }
    # 0.8 as first written: accepted on Indra's word alone.
    alone = assess_page(_layer_and_indra(0.15), ocr_layer_climbs=True, stages=LADDER)
    assert (alone["next_action"], alone["disposition"]) == ("review", "unreviewed_text")


def test_an_inherited_layer_that_agrees_confirms_the_ocr_but_cannot_challenge_it():
    confirmed = assess_page(
        _layer_and_indra(0.9), ocr_layer_climbs=True, corroborate=True, stages=LADDER
    )
    assert confirmed["next_action"] == "review"
    assert confirmed["disposition"] == "unreviewed_text"
    assert "uncorroborated" not in [t["trigger"] for t in confirmed["triggers"]]


def test_a_held_layer_does_not_confirm_anything():
    page = _layer_and_indra(0.9)
    page["attempts"][0]["warnings"].append("sparse")
    result = assess_page(page, ocr_layer_climbs=True, corroborate=True, stages=LADDER)
    assert result["next_action"] == "second_ocr"


def test_a_scan_without_a_layer_is_read_twice():
    # The CIA transcript and FBI pages have no layer: Indra alone was accepted.
    page = _page(_attempt("a1", "local_text", " "), _attempt("a2", "local_ocr", "SECRET"))
    result = assess_page(page, corroborate=True, text_refutes_blank=True, stages=LADDER)
    assert (result["next_action"], result["disposition"]) == ("second_ocr", "unreviewed_text")
    assert [t["trigger"] for t in result["triggers"]] == ["uncorroborated"]


def test_two_agreeing_ocr_engines_settle_the_page_and_a_clean_typeset_layer_is_not_reread():
    two = _page(
        _attempt("a1", "local_text", " "),
        _attempt("a2", "local_ocr", "the same words"),
        _attempt("a3", "second_ocr", "the same words"),
        comparisons=[_compared("a2", "a3", 0.95)],
    )
    agreed = assess_page(two, corroborate=True, text_refutes_blank=True, stages=LADDER)
    assert (agreed["next_action"], agreed["disposition"]) == ("review", "unreviewed_text")
    typeset = assess_page(
        _page(_attempt("a1", "local_text", "Born-digital text")), corroborate=True, stages=LADDER
    )
    assert typeset["next_action"] == "review"


def test_without_a_second_ocr_stage_an_unconfirmed_reading_is_not_held_for_it():
    stages = ("local_text", "local_ocr", "image", "second_opinion")
    result = assess_page(
        _layer_and_indra(0.15), ocr_layer_climbs=True, corroborate=True, stages=stages
    )
    assert result["disposition"] == "unreviewed_text"
    assert result["next_action"] == "review"


def test_the_trigger_history_names_each_climb_once():
    page = _layer_and_indra(0.15)
    page["attempts"].append(_attempt("a3", "second_ocr", "в Карпатской Руси"))
    page["comparisons"] += [_compared("a1", "a3", 0.15), _compared("a2", "a3", 0.9)]
    result = assess_page(page, ocr_layer_climbs=True, corroborate=True, stages=LADDER)
    assert [(t["trigger"], t["stage"]) for t in result["triggers"]] == [
        ("ocr_layer", "local_text"),
        ("uncorroborated", "local_ocr"),
    ]
    assert (result["selected_attempt"], result["disposition"]) == ("a2", "unreviewed_text")


def _second_ocr_held(stages):
    # junk-text-layer p6 (pageledger-08b): RapidOCR's own reading is held as low confidence,
    # so it neither confirms nor challenges Indra, and the page stopped there.
    page = _layer_and_indra(0.10)
    page["attempts"].append(_attempt("a3", "second_ocr", "в Кар", warnings=["low_confidence"]))
    page["comparisons"] += [_compared("a1", "a3", 0.07), _compared("a2", "a3", 0.11)]
    return assess_page(page, ocr_layer_climbs=True, corroborate=True, stages=stages)


def test_a_reading_the_second_ocr_could_not_confirm_climbs_to_the_reader():
    result = _second_ocr_held(LADDER)
    assert result["next_action"] == "local_image"
    assert result["triggers"][-1] == {
        "trigger": "uncorroborated",
        "stage": "second_ocr",
        "attempt": "a2",
    }


def test_without_a_reader_an_unconfirmed_reading_is_held_with_the_second_ocr_s_reason():
    result = _second_ocr_held(("local_text", "local_ocr", "second_ocr"))
    assert (result["next_action"], result["disposition"]) == ("review", "low_confidence")


def test_a_clean_disagreement_is_left_to_the_disagreement_trigger():
    page = _layer_and_indra(0.10)
    page["attempts"].append(_attempt("a3", "second_ocr", "в Кар"))
    page["comparisons"] += [_compared("a1", "a3", 0.07), _compared("a2", "a3", 0.3)]
    held = assess_page(page, ocr_layer_climbs=True, corroborate=True, stages=LADDER)
    assert (held["next_action"], held["disposition"]) == ("review", "engine_disagreement")
    climbs = assess_page(
        page,
        ocr_layer_climbs=True,
        corroborate=True,
        stages=LADDER,
        escalate_on=("hold", "disagreement"),
    )
    assert [t["trigger"] for t in climbs["triggers"]][-1] == "disagreement"
    assert climbs["next_action"] == "local_image"


def test_a_held_local_reading_sends_an_unconfirmed_page_on_to_the_paid_reader():
    # junk-text-layer p4: dots.ocr's reading was held too, and the page stopped on Indra's.
    page = _layer_and_indra(0.23)
    page["attempts"] += [
        _attempt("a3", "second_ocr", "в Кар", warnings=["suspicious_symbol_density"]),
        _attempt(
            "a4",
            "local_image",
            "въ Кар",
            warnings=["suspicious_symbol_density"],
            adapter_capabilities=GENERATIVE,
        ),
    ]
    page["comparisons"] += [_compared("a2", "a3", 0.32), _compared("a2", "a4", 0.35)]
    result = assess_page(page, ocr_layer_climbs=True, corroborate=True, stages=LADDER)
    assert result["next_action"] == "image"
    assert [(t["trigger"], t["stage"]) for t in result["triggers"]][-1] == (
        "uncorroborated",
        "local_image",
    )
