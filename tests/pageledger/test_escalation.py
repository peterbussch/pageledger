"""Escalation: pages climb on roughness and engine disagreement, not only on warning holds."""

import hashlib

import pytest

from pageledger.processing_config import processing_config
from pageledger.processing_policy import assess_page

SOURCE = "a" * 64


def _config(**processing):
    return {"processing": {"local_ocr": {"adapter": "pdf_ocr"}, **processing}}


def test_escalation_defaults_to_holds_only_and_leaves_the_policy_unchanged():
    # A 0.6 config must compile to the policy its jobs recorded, so resume still accepts them.
    config = processing_config(_config(), pdf=True)
    assert "escalate_on" not in config and "lexicon" not in config
    assert "escalate_on" not in processing_config(_config(escalate_on=["hold"]), pdf=True)


def test_escalation_accepts_rough_and_disagreement_with_a_lexicon():
    config = processing_config(
        _config(
            escalate_on=["disagreement", "rough", "hold"],
            lexicon={"provider": "pymorphy3", "language": "ru", "rough_below": 0.95},
        ),
        pdf=True,
    )
    assert config["escalate_on"] == ["hold", "rough", "disagreement"]
    assert config["lexicon"] == {"provider": "pymorphy3", "language": "ru", "rough_below": 0.95}


@pytest.mark.parametrize(
    "processing,message",
    [
        ({"escalate_on": ["rough", "hold"]}, "rough trigger needs processing.lexicon"),
        ({"escalate_on": ["disagreement"]}, "must include hold"),
        ({"escalate_on": ["hold", "sometimes"]}, "Unknown escalation trigger"),
        ({"escalate_on": ["hold", "hold"]}, "more than once"),
        (
            {"lexicon": {"provider": "pymorphy3", "language": "ru", "rough_below": 0.9}},
            "lexicon is used only by the rough trigger",
        ),
        (
            {
                "escalate_on": ["hold", "rough"],
                "lexicon": {"provider": "pymorphy3", "language": "ru", "rough_below": 1.5},
            },
            "rough_below",
        ),
        (
            {
                "escalate_on": ["hold", "rough"],
                "lexicon": {"provider": "hunspell", "language": "ru", "rough_below": 0.9},
            },
            "provider",
        ),
    ],
)
def test_meaningless_escalation_configs_are_refused(processing, message):
    with pytest.raises(ValueError, match=message):
        processing_config(_config(**processing), pdf=True)


def _attempt(name, stage, text="Readable text", **kwargs):
    return {
        "attempt_id": name,
        "stage": stage,
        "outcome": "completed",
        "raw_artifact": f"raw/{name}.txt",
        "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "text": text,
        "format": "text",
        "warnings": [],
        "classification": {"type": "prose", "reason": ""},
        "alignment": None,
        "usage": {},
        "failure": None,
        **kwargs,
    }


def _page(*attempts, comparisons=()):
    return {
        "page_id": "doc_page_1",
        "page_number": 1,
        "source_sha256": SOURCE,
        "attempts": list(attempts),
        "comparisons": list(comparisons),
    }


def _rough(text):
    """A stand-in lexicon judgement: text containing 'zz' is rough."""
    if "zz" in text:
        return {"known_share": 0.5, "words": 40, "unknown": ["zzword"]}
    return {"known_share": 1.0, "words": 40, "unknown": []}


def test_a_rough_clean_reading_climbs_when_rough_is_on():
    page = _page(_attempt("a1", "local_ocr", "glued zzhalves of lines"))
    held_back = assess_page(page)
    assert held_back["next_action"] == "review"
    climbed = assess_page(page, escalate_on=("hold", "rough"), roughness=_rough, rough_below=0.95)
    assert climbed["next_action"] == "image"
    assert climbed["triggers"] == [
        {
            "trigger": "rough",
            "stage": "local_ocr",
            "attempt": "a1",
            "known_share": 0.5,
            "words": 40,
            "unknown": ["zzword"],
        }
    ]
    assert climbed["disposition"] == "unreviewed_text"


def test_a_smooth_reading_does_not_climb():
    page = _page(_attempt("a1", "local_ocr", "clean prose"))
    result = assess_page(page, escalate_on=("hold", "rough"), roughness=_rough, rough_below=0.95)
    assert result["next_action"] == "review"
    assert result["triggers"] == []


def _disagreeing_page():
    layer = _attempt("a1", "local_text", "the value is 796808")
    ocr = _attempt("a2", "local_ocr", "the value is 196308")
    comparison = {
        "left_attempt": "a1",
        "right_attempt": "a2",
        "agreement_ratio": 0.75,
        "spans": [],
        "number_differences": [{"number": "796808", "side": "left", "context": "is 796808"}],
    }
    return _page(layer, ocr, comparisons=[comparison])


def test_engine_disagreement_climbs_to_the_reader_instead_of_stopping():
    page = _disagreeing_page()
    assert assess_page(page)["next_action"] == "review"
    climbed = assess_page(page, escalate_on=("hold", "disagreement"))
    assert climbed["next_action"] == "image"
    assert climbed["disposition"] == "numeric_disagreement"
    assert climbed["triggers"][0]["trigger"] == "disagreement"
    assert climbed["triggers"][0]["reasons"] == ["numeric_disagreement"]


def test_triggers_never_send_a_page_to_a_second_reader():
    # A stronger model is another witness with the same habits, not a better one.
    reader = _attempt(
        "a3", "image", "glued zzhalves", adapter_capabilities=["ocr", "page_image", "generative"]
    )
    page = _page(_attempt("a1", "local_text", " \n"), _attempt("a2", "local_ocr", " \n"), reader)
    result = assess_page(
        page, escalate_on=("hold", "rough", "disagreement"), roughness=_rough, rough_below=0.95
    )
    assert result["next_action"] == "review"


def test_each_climb_is_recorded_once_with_its_stage_trigger():
    layer = _attempt("a1", "local_text", "zz rough text layer")
    ocr = _attempt("a2", "local_ocr", "zz rough ocr reading")
    reader = _attempt(
        "a3",
        "image",
        "clean model reading",
        adapter_capabilities=["ocr", "page_image", "generative"],
    )
    result = assess_page(
        _page(layer, ocr, reader), escalate_on=("hold", "rough"), roughness=_rough, rough_below=0.95
    )
    assert [(t["stage"], t["attempt"]) for t in result["triggers"]] == [
        ("local_text", "a1"),
        ("local_ocr", "a1"),
    ]
    assert result["selected_attempt"] == "a3"


def test_a_smooth_ocr_reading_replaces_a_rough_text_layer():
    layer = _attempt("a1", "local_text", "zz rough text layer")
    ocr = _attempt("a2", "local_ocr", "clean ocr reading")
    result = assess_page(
        _page(layer, ocr), escalate_on=("hold", "rough"), roughness=_rough, rough_below=0.95
    )
    assert result["selected_attempt"] == "a2"
    assert result["next_action"] == "review"
    assert [t["attempt"] for t in result["triggers"]] == ["a1"]
