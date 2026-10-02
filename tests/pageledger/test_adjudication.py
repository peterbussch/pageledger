"""Settling spans from receipts, and the edition text the settlements make."""

from pageledger.adjudication import edition, settle


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


def test_edits_apply_at_their_offsets_and_an_insertion_lands_before_a_replacement():
    base = "один два три"
    record = _record(
        {"span_id": "ins", "start": 5, "end": 5, "base": ""},
        {"span_id": "rep", "start": 5, "end": 8, "base": "два"},
        {"span_id": "same", "start": 9, "end": 12, "base": "три"},
    )
    decided = {
        "ins": {"text": "и "},
        "rep": {"text": "дваа"},
        "same": {"text": "три"},
    }
    assert edition(base, record, decided) == "один и дваа три"
    assert edition(base, record, {"same": {"text": "три"}}) is None
