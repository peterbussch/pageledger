"""Settling spans from receipts, and the edition text the settlements make."""

import random

import pytest

from pageledger.adjudication import edition, settle
from pageledger.contest import _tokens, contest


def _record(*spans):
    return {"spans": [{"status": "open", "base": "", **span} for span in spans]}


def _receipt(sha, *decisions):
    return {
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
