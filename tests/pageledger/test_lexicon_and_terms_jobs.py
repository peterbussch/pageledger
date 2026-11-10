"""Document jobs with a word list lexicon, and adjudication packets narrowed to named terms."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from test_processing import TEXT, _contesting, launch, setup  # noqa: F401

from pageledger.checkpoint import read_record
from pageledger.processing import adjudicate_job, adjudication_packets, verify_job

SCHEMAS = Path(__file__).parents[2] / "schemas"


def _with_wordlist(setup, path):  # noqa: F811
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["escalate_on"] = ["hold", "rough"]
    data["processing"]["lexicon"] = {"provider": "wordlist", "path": str(path), "rough_below": 0.9}
    setup[1].write_text(yaml.safe_dump(data))


def test_a_job_records_its_word_list_and_refuses_a_changed_one(setup, tmp_path):  # noqa: F811
    setup[3].update(defective=set())
    words = tmp_path / "words.txt"
    words.write_text("\n".join(sorted(set(TEXT.lower().replace(".", "").split()))))
    _with_wordlist(setup, words)
    assert launch(setup, pages="1")["status"] == "completed"
    job = read_record(setup[2] / "job.json")
    assert job["lexicon"]["provider"] == "wordlist"
    assert job["lexicon"]["path"] == str(words.resolve())
    page = job["pages"][0]
    # Every word is known, so the clean text layer is not rough and stays selected.
    assert page["triggers"] == [] and page["selected_attempt"].startswith("local_text")
    jsonschema.validate(
        json.loads((setup[2] / "job.json").read_text()),
        json.loads((SCHEMAS / "job.schema.json").read_text()),
    )
    assert verify_job(setup[2])["status"] == "pass"

    words.write_text(words.read_text() + "\nzebra")
    result = verify_job(setup[2])
    assert result["status"] == "fail" and "restore the recorded word list" in result["error"]
    words.unlink()
    result = verify_job(setup[2])
    assert result["status"] == "fail" and "cannot read the word list" in result["error"]


def test_an_unknown_word_list_page_is_rough_and_climbs(setup, tmp_path):  # noqa: F811
    setup[3].update(defective=set())
    words = tmp_path / "words.txt"
    words.write_text("the\nfor\nthis\n")
    _with_wordlist(setup, words)
    assert launch(setup, pages="1")["status"] == "completed"
    page = read_record(setup[2] / "job.json")["pages"][0]
    assert page["triggers"][0]["trigger"] == "rough"
    assert page["triggers"][0]["known_share"] < 0.9
    assert verify_job(setup[2])["status"] == "pass"


def test_a_missing_word_list_fails_before_the_job_exists(setup, tmp_path):  # noqa: F811
    _with_wordlist(setup, tmp_path / "absent.txt")
    with pytest.raises(ValueError, match="cannot read the word list"):
        launch(setup, pages="1")
    assert not setup[2].exists()


def test_latin_print_settles_english_misreadings_with_a_word_list(setup, tmp_path):  # noqa: F811
    reader = "The Falls Church office of the Evening Star is open.\n" * 3
    literal = reader.replace("Church", "Chureh").replace("Star", "Siar")
    words = tmp_path / "words.txt"
    words.write_text("the\nfalls\nchurch\noffice\nof\nevening\nstar\nis\nopen\n")
    data = yaml.safe_load(setup[1].read_text())
    data["processing"].update(
        local_ocr={"adapter": "rapidocr"},
        escalate_on=["hold", "always"],
        contest={"rules": "latin-print-0.1"},
        lexicon={"provider": "wordlist", "path": str(words)},
    )
    setup[1].write_text(yaml.safe_dump(data))
    # No text layer, so RapidOCR's reading is the literal witness.
    setup[3].update(defective=set(), scanned=True, texts={"rapidocr": literal, "image": reader})
    assert launch(setup, pages="1")["status"] == "completed"
    page = read_record(setup[2] / "job.json")["pages"][0]
    assert page["contested"]["by_rule"] == 6 and page["contested"]["open"] == 0
    assert page["disposition"] == "adjudicated_text"
    record = json.loads((setup[2] / page["contested"]["artifact"]).read_text(encoding="utf-8"))
    assert {span["settled_by"] for span in record["spans"]} == {"rule:latin-print-0.1/G"}
    jsonschema.validate(record, json.loads((SCHEMAS / "contested.schema.json").read_text()))
    assert verify_job(setup[2])["status"] == "pass"

    words.write_text(words.read_text() + "chureh\n")
    assert verify_job(setup[2])["status"] == "fail"


READER = (
    "Soviet Embassy 1119 16th nw.\n"
    "Filler line of prose for spacing only.\n"
    "Filler line of prose for spacing only.\n"
    "Filler line of prose for spacing only.\n"
    "Smith John 12 Main st.\n"
)
LITERAL = READER.replace("1119", "1125").replace("Smith", "Smyth")


def _two_spans(setup, monkeypatch):  # noqa: F811
    _contesting(setup, monkeypatch, literal=LITERAL)
    setup[3]["texts"]["image"] = READER
    assert launch(setup, pages="1")["status"] == "completed"
    page = read_record(setup[2] / "job.json")["pages"][0]
    assert page["contested"]["open"] == 2


def test_packets_can_be_narrowed_to_spans_near_named_terms(setup, monkeypatch, tmp_path):  # noqa: F811
    from pageledger.cli import main

    _two_spans(setup, monkeypatch)
    out = tmp_path / "packets"
    assert main(["adjudicate", str(setup[2]), "--packets", str(out), "--term", "soviet"]) == 0
    packet = json.loads((out / "doc_0001_page_0001.json").read_text(encoding="utf-8"))
    assert [span["candidates"] for span in packet["spans"]] == [["1119", "1125"]]
    assert packet["terms"] == ["soviet"]
    schema = json.loads((SCHEMAS / "adjudication-packet.schema.json").read_text())
    jsonschema.validate(packet, schema)

    answer = {
        "page_id": packet["page_id"],
        "contested_sha256": packet["contested_sha256"],
        "decisions": [
            {"span_id": packet["spans"][0]["span_id"], "text": "1119", "confidence": "high"}
        ],
    }
    (out / "doc_0001_page_0001.decisions.json").write_text(json.dumps(answer))
    adjudicate_job(setup[2], out, reviewer="agent:test")
    page = read_record(setup[2] / "job.json")["pages"][0]
    # The span no term touched is still open and still holds the page.
    assert page["disposition"] == "contested" and page["contested"]["open"] == 1
    assert verify_job(setup[2])["status"] == "pass"


def test_the_filter_counts_what_it_leaves_out(setup, monkeypatch, tmp_path):  # noqa: F811
    _two_spans(setup, monkeypatch)
    result = adjudication_packets(setup[2], tmp_path / "a", ["Tass", "U S S R"])
    assert (result["packets"], result["spans"], result["open_spans"], result["left_out"]) == (
        0,
        0,
        2,
        2,
    )
    assert not (tmp_path / "a" / "doc_0001_page_0001.json").exists()
    result = adjudication_packets(setup[2], tmp_path / "b")
    assert (result["packets"], result["spans"], result["open_spans"]) == (1, 2, 2)
    assert "left_out" not in result


def test_term_and_decisions_do_not_go_together(setup, tmp_path, capsys):  # noqa: F811
    from pageledger.cli import main

    assert main(["adjudicate", str(setup[2]), "--decisions", str(tmp_path), "--term", "x"]) != 0
    assert "--term goes with --packets" in capsys.readouterr().err


def test_terms_match_whole_words_in_any_case_across_line_breaks():
    from pageledger.adjudication import _touches, term_patterns

    text = "Embassy of the U S S R\n1119-16th nw NAtional-7550\nTassel Co 12 F nw"
    span = {"start": text.index("1119"), "end": text.index("1119") + 4, "witness": "1125"}
    assert _touches(text, span, term_patterns(["u s s r"]))
    assert _touches(text, span, term_patterns(["Embassy\tof"]))
    assert not _touches(text, span, term_patterns(["Tass"]))
    assert not _touches(text, span, term_patterns(["Soviet"]))
    assert _touches(text, {**span, "witness": "Soviet"}, term_patterns(["Soviet"]))
    with pytest.raises(ValueError, match="cannot be empty"):
        term_patterns([" "])
