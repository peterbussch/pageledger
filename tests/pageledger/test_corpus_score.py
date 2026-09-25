"""The scorer measures agreement with a reference line by line."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "corpus"))

import score  # noqa: E402


def _engine(**pages):
    return {
        page: {"text": text, "seconds": None, "cost_usd": None, "failure": None}
        for page, text in pages.items()
    }


def _score(reference, engine_text):
    return score.score({"DOC_p0001": reference}, _engine(DOC_p0001=engine_text), {})


def test_each_reference_line_is_scored_against_the_closest_engine_text():
    report = _score(
        "Городъ Харьковъ\nНаселеніе 1 850 душъ",
        "Население 1850 душ\nГородъ Харьковъ",
    )

    first, second = report["pages"]["DOC_p0001"]["rows"]
    assert first["character_edits"] == 0
    assert (second["character_edits"], second["characters"]) == (3, 20)
    assert second["normalized_character_edits"] == 1
    overall = report["overall"]
    assert overall["cer"] == round(3 / 35, 4)
    assert overall["exact_lines"] == 0.5 and overall["missing_lines"] == 0
    assert overall["numbers_found"] == 1
    assert overall["historical_kept"] == 0.5 and overall["historical_added"] == 0


def test_code_points_and_graphemes_are_counted_separately():
    row = _score("ѣa\u0338", "еa\u0338!")["pages"]["DOC_p0001"]["rows"][0]

    assert (row["character_edits"], row["characters"]) == (1, 3)
    assert (row["grapheme_edits"], row["graphemes"]) == (1, 2)
    assert row["normalized_character_edits"] == row["normalized_grapheme_edits"] == 0


def test_a_reference_line_is_found_inside_a_long_engine_line():
    filler = " ".join(["слово"] * 300)
    report = _score("Итого по губерніи 12 345", f"{filler} Итого по губерніи 12 345 {filler}")

    assert report["overall"]["cer"] == 0


def test_reader_markup_and_lines_too_short_to_match_are_left_out():
    report = _score("12\n—\nсло[?]во [illegible] домъ", "слово домъ")

    rows = report["pages"]["DOC_p0001"]["rows"]
    assert len(rows) == 1 and rows[0]["character_edits"] == 0


def test_html_tables_are_read_as_rows_and_cells():
    table = (
        "<table><tr><td>Уѣздъ</td><td>1 234</td></tr>"
        "<tr><td>Городъ &amp; посадъ</td><td>56</td></tr></table>"
    )
    report = _score("Уѣздъ | 1 234\nГородъ & посадъ | 56", table)

    assert report["overall"]["cer"] == 0 and report["overall"]["numbers_found"] == 1


@pytest.mark.parametrize(("engine_text", "found"), [("Уезд 1234 56", 1), ("Уезд 1235 56", 0.5)])
def test_numbers_are_compared_cell_by_cell(engine_text, found):
    assert _score("Уѣздъ | 1 234 | 56", engine_text)["overall"]["numbers_found"] == found


def test_pages_without_text_and_loops_are_counted():
    report = score.score(
        {"A_p0001": "Первая строка страницы", "B_p0001": "Вторая строка страницы"},
        _engine(A_p0001=None, B_p0001="Вторая строка страницы\n" + "MALE\n" * 30),
        {},
    )

    assert report["pages"]["A_p0001"]["missing_lines"] == 1
    assert report["pages"]["B_p0001"]["loop"] is True
    assert report["overall"]["pages_without_text"] == 1 and report["overall"]["loops"] == 1


def test_a_corpus_run_supplies_text_time_cost_and_failures(tmp_path):
    run = tmp_path / "run"
    (run / "DOC" / "raw").mkdir(parents=True)
    (run / "DOC" / "raw" / "text.txt").write_text("text layer", encoding="utf-8")
    (run / "DOC" / "raw" / "ocr.txt").write_text("ocr reading", encoding="utf-8")
    text = {"stage": "local_text", "outcome": "completed", "failure": None}
    ocr = {"stage": "local_ocr", "outcome": "completed", "failure": None}
    rows = [
        {
            "item_id": "DOC",
            "page_number": 3,
            "selected_output": "DOC/raw/text.txt",
            "attempts": [
                {**text, "seconds": 1.5, "usage": {"cost_usd": None}, "output": "DOC/raw/text.txt"},
                {**ocr, "seconds": 2.0, "usage": {"cost_usd": 0.002}, "output": "DOC/raw/ocr.txt"},
            ],
        },
        {
            "item_id": "DOC",
            "page_number": 4,
            "selected_output": None,
            "attempts": [
                {**ocr, "outcome": "failed", "failure": "MODEL_TIMEOUT", "seconds": 300.0,
                 "usage": {}, "output": None},
            ],
        },
    ]  # fmt: skip
    (run / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    selected = score.read_engine(run)
    by_stage = score.read_engine(run, "local_ocr")

    assert selected["DOC_p0003"] == {
        "text": "text layer", "seconds": 3.5, "cost_usd": 0.002, "failure": None
    }  # fmt: skip
    assert by_stage["DOC_p0003"]["text"] == "ocr reading"
    assert by_stage["DOC_p0004"] == {
        "text": None, "seconds": 300.0, "cost_usd": None, "failure": "MODEL_TIMEOUT"
    }  # fmt: skip
    assert "DOC_p0003" in score.read_engine(run, "local_text")
    assert score.read_engine(run, "image") == {}


def test_command_line_scores_one_split_by_stratum(tmp_path, capsys):
    references, engine = tmp_path / "references", tmp_path / "engine"
    for directory in (references, engine):
        directory.mkdir()
        for page in ("A_p0001", "B_p0001"):
            (directory / f"{page}.txt").write_text("Одна и та же строка", encoding="utf-8")
    pages = tmp_path / "pages.json"
    pages.write_text(json.dumps([{"page_id": "A_p0001", "stratum": "prose"}]))
    splits = tmp_path / "splits.json"
    splits.write_text(json.dumps({"A": "holdout", "B": "dev"}))

    arguments = [str(references), str(engine), "--pages", str(pages), "--json"]
    assert score.main([*arguments, "--split", "holdout", "--splits", str(splits)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert list(report["pages"]) == ["A_p0001"]
    assert report["strata"]["prose"]["cer"] == 0
    assert report["basis"] == "against reference"


def test_split_needs_the_splits_file(tmp_path):
    with pytest.raises(SystemExit) as caught:
        score.main([str(tmp_path), str(tmp_path), "--split", "dev"])
    assert caught.value.code == 2
