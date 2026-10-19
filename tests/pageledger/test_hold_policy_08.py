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
