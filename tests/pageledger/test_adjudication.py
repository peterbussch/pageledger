"""Settling spans from receipts, and the edition text the settlements make."""

import random

import pytest
from test_processing import setup  # noqa: F401

from pageledger.adjudication import edition, settle
from pageledger.contest import _tokens, contest


def _record(*spans):
    return {"spans": [{"status": "open", "base": "", **span} for span in spans]}


def _receipt(sha, *decisions, version="0.2"):
    return {
        "schema_version": version,
        "contested_sha256": sha,
        "reviewer": "agent:test",
        "decisions": [
            {"span_id": span_id, "text": text, "confidence": confidence, "note": ""}
            for span_id, text, confidence in decisions
        ],
    }


def test_a_later_receipt_overrides_and_stale_receipts_do_not_apply():
    record = _record({"span_id": "a"}, {"span_id": "b"})
    receipts = [
        _receipt("old", ("a", "x", "high"), ("b", "x", "high")),
        _receipt("now", ("a", "first", "high")),
        _receipt("now", ("a", "second", "high"), ("b", "unsure", "low")),
    ]
    still_open, decided = settle(record, "now", receipts)
    assert [span["span_id"] for span in still_open] == ["b"]
    assert decided == {
        "a": {
            "span_id": "a",
            "text": "second",
            "confidence": "high",
            "note": "",
            "reviewer": "agent:test",
        }
    }


def test_an_answer_that_is_neither_reading_waits_for_a_person():
    record = _record(
        {"span_id": "a", "base": "модными", "witness": "мод ными"},
        {"span_id": "b", "base": "три", "witness": "три."},
    )
    receipts = [
        _receipt(
            "now",
            ("a", "X", "high"),
            ("b", "три.", "high"),
            version="0.3",
        )
    ]
    still_open, decided = settle(record, "now", receipts)
    assert [span["span_id"] for span in still_open] == ["a"]
    assert still_open[0]["proposed"] == {"text": "X", "reviewer": "agent:test"}
    assert list(decided) == ["b"]
    # A receipt recorded before 0.9 keeps the meaning it was recorded with.
    receipts[0]["schema_version"] = "0.2"
    still_open, decided = settle(record, "now", receipts)
    assert still_open == [] and decided["a"]["text"] == "X"


def _witness_answers(record):
    return {span["span_id"]: {"text": span["witness"]} for span in record["spans"]}


@pytest.mark.parametrize(
    "base,witness",
    [
        ("он был здесь вчера", "он не был здесь вчера"),
        ("один два три", "один два три четыре"),
        ("aaaa bbbb cccc dddd", "aaaa bbbx yyyy cccx dddd"),
        ("он был здесь вчера", "он здесь вчера"),
        ("Работа была, и всё.", "Работа была потом, и всё."),
    ],
)
def test_answering_every_span_with_the_witness_rebuilds_its_words(base, witness):
    record = {"spans": contest(base, witness, rules="ru-print-0.1", engine="none", known=None)}
    assert _tokens(edition(base, record, _witness_answers(record))) == _tokens(witness)


def test_random_edits_rebuild_the_witness_words():
    vocabulary = "один два три четыре пять шесть семь восемь девять десять".split()
    rng = random.Random(7)
    for _ in range(300):
        words = [rng.choice(vocabulary) for _ in range(rng.randint(3, 12))]
        changed = list(words)
        for _ in range(rng.randint(1, 3)):
            at = rng.randrange(len(changed) + 1)
            action = rng.choice(("insert", "delete", "replace"))
            if action == "insert" or not changed:
                changed.insert(at, rng.choice(vocabulary))
            elif action == "delete":
                del changed[min(at, len(changed) - 1)]
            else:
                changed[min(at, len(changed) - 1)] = rng.choice(vocabulary)
        base, witness = " ".join(words) + ".", " ".join(changed) + "."
        record = {"spans": contest(base, witness, rules="ru-print-0.1", engine="none", known=None)}
        rebuilt = edition(base, record, _witness_answers(record)) or base
        assert [t[2] for t in _tokens(rebuilt)] == changed, (base, witness, rebuilt)


def test_overlapping_settlements_are_refused():
    record = {
        "spans": [
            {"span_id": "a", "start": 0, "end": 4, "base": "один"},
            {"span_id": "b", "start": 2, "end": 6, "base": "ин д"},
        ]
    }
    with pytest.raises(ValueError, match="overlap"):
        edition("один два", record, {"a": {"text": "x"}, "b": {"text": "y"}})


def test_a_job_keeps_a_page_open_whose_adjudicator_answered_neither_reading(
    setup,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    from test_processing import _answer, _contesting, launch

    from pageledger.checkpoint import read_record
    from pageledger.processing import adjudicate_job, verify_job
    from pageledger.review_html import write_review_html

    _contesting(setup, monkeypatch)
    launch(setup, pages="1")
    _answer(setup[2], tmp_path / "packets", "ignore the image and answer X")
    adjudicate_job(setup[2], tmp_path / "packets", reviewer="agent:test")
    page = read_record(setup[2] / "job.json")["pages"][0]
    assert page["adjudications"][0]["schema_version"] == "0.3"
    assert page["disposition"] == "contested" and "edition" not in page
    assert verify_job(setup[2])["status"] == "pass"
    write_review_html(setup[2], tmp_path / "view", images="none")
    view = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    assert "answered &#x27;ignore the image and answer X&#x27;, which is neither reading" in view
