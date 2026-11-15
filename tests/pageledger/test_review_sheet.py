"""Review sheets: a CSV a person fills in, refused whenever it no longer fits the job."""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest
import yaml
from jsonschema import validate
from test_processing import TEXT, StageAdapter, setup  # noqa: F401

from pageledger import runner
from pageledger.checkpoint import read_record
from pageledger.cli import main
from pageledger.processing import create_review_sheet, process, review_job, verify_job
from pageledger.review_sheet import FIELDS, binding_path

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def job(tmp_path, monkeypatch):
    """Two pages; page 2 was held after local text and read again by OCR."""
    source = tmp_path / "scans" / "source.txt"
    source.parent.mkdir()
    source.write_text(TEXT + "\f" + TEXT)
    config = tmp_path / "config.yml"
    stages = {stage: {"adapter": stage} for stage in ("local_text", "local_ocr")}
    config.write_text(yaml.safe_dump({"schema_version": "0.1", "processing": stages}))

    def adapter(name, *args, **kwargs):
        stage = StageAdapter()
        object.__setattr__(stage, "stage", name)
        object.__setattr__(stage, "defective", {2})
        return stage

    monkeypatch.setattr(runner, "load_adapter", adapter)
    monkeypatch.delenv("PAGELEDGER_REVIEWER", raising=False)
    root = tmp_path / "job"
    process(source=source, config_path=config, out_dir=root)
    return root


def _sheet(job, path):
    create_review_sheet(job, path)
    return path


def _edit(sheet, change):
    """Change rows and save them the way a spreadsheet does: CRLF, minimal quoting."""
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    change(rows)
    with sheet.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)


def _pages(job):
    return read_record(job / "job.json")["pages"]


def test_a_sheet_with_0_8_s_cells_still_applies(job, tmp_path):
    sheet = _sheet(job, tmp_path / "review.csv")

    def as_0_8_wrote_it(rows):
        for row in rows:
            row.update(disposition="coverage_defect", review_reasons="coverage_defect")
            row.update(attempt_summary="local_text:doc_0001_page_0001: words (9 characters)")
            row.update(decision="accept")

    _edit(sheet, as_0_8_wrote_it)
    result = review_job(job, sheet, reviewer="A. Reader")
    assert result["decisions"] == {"reviewed_text": 2}


def test_a_filled_in_sheet_records_decisions_and_leaves_blank_rows_unreviewed(job, tmp_path):
    sheet = _sheet(job, tmp_path / "review.csv")
    assert sheet.read_bytes().startswith(b"\xef\xbb\xbf")
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["source_page"] for row in rows] == [
        "scans/source.txt#page=1",
        "scans/source.txt#page=2",
    ]
    # The cells a person reads lead with words; the code, or the reading's ID, comes last.
    assert rows[1]["review_reasons"] == (
        "Text may be missing or damaged in the reading (coverage_defect)"
    )
    assert rows[1]["disposition"] == rows[1]["review_reasons"]
    assert rows[0]["attempt_summary"].startswith("The engine local_text: ")
    assert rows[0]["attempt_summary"].endswith("; local_text:doc_0001_page_0001)")

    _edit(sheet, lambda rows: rows[0].update(decision="accept", note="Checked against the scan"))
    result = review_job(job, sheet, reviewer="A. Reader")

    assert result["decisions"] == {"reviewed_text": 1}
    first, second = _pages(job)
    assert first["disposition"] == "reviewed_text"
    assert first["review"]["decisions"][0]["reviewer"] == "A. Reader"
    assert first["review"]["decisions"][0]["reason"] == "Checked against the scan"
    assert second["review"] is None
    assert verify_job(job)["status"] == "pass"


def test_a_renamed_spreadsheet_sheet_finds_its_unique_binding(job, tmp_path):
    original = _sheet(job, tmp_path / "review.csv")
    renamed = tmp_path / "review-done.csv"
    shutil.copyfile(original, renamed)
    _edit(renamed, lambda rows: rows[0].update(decision="accept"))

    result = review_job(job, renamed, reviewer="A. Reader")

    assert result["decisions"] == {"reviewed_text": 1}
    assert _pages(job)[0]["disposition"] == "reviewed_text"


def test_applying_a_renamed_sheet_twice_says_the_job_changed_not_that_a_file_is_missing(
    job, tmp_path
):
    original = _sheet(job, tmp_path / "review.csv")
    renamed = tmp_path / "review-done.csv"
    shutil.copyfile(original, renamed)
    _edit(renamed, lambda rows: rows[0].update(decision="accept"))
    review_job(job, renamed, reviewer="A. Reader")

    with pytest.raises(ValueError) as refused:
        review_job(job, renamed, reviewer="A. Reader")

    message = str(refused.value)
    assert "no longer matches the job: page 1 was read again, reviewed or corrected" in message
    assert "applying a sheet a second time" in message and "Write a new review sheet" in message
    assert "missing or ambiguous" not in message


def test_renamed_sheet_reports_missing_or_ambiguous_binding(job, tmp_path):
    original = _sheet(job, tmp_path / "review.csv")
    renamed = tmp_path / "review-done.csv"
    shutil.copyfile(original, renamed)
    shutil.copyfile(binding_path(original), tmp_path / "copy.binding.json")
    with pytest.raises(ValueError, match="review-done.csv.binding.json"):
        review_job(job, renamed, reviewer="A. Reader")

    (tmp_path / "copy.binding.json").unlink()
    binding_path(original).unlink()
    with pytest.raises(ValueError, match="review-done.csv.binding.json"):
        review_job(job, renamed, reviewer="A. Reader")


def test_the_binding_follows_its_schema(job, tmp_path):
    sheet = _sheet(job, tmp_path / "review.csv")
    schema = json.loads((ROOT / "schemas" / "review-sheet-binding.schema.json").read_text())
    binding = json.loads(binding_path(sheet).read_text())

    validate(binding, schema)
    assert [b["page_id"] for b in binding["bindings"].values()] == [
        p["page_id"] for p in _pages(job)
    ]


def test_use_accepts_another_completed_attempt(job, tmp_path):
    ocr = next(a["attempt_id"] for a in _pages(job)[1]["attempts"] if a["stage"] == "local_ocr")
    sheet = _sheet(job, tmp_path / "review.csv")
    _edit(sheet, lambda rows: rows[1].update(decision=f"use:{ocr}"))

    review_job(job, sheet, reviewer="A. Reader")

    assert _pages(job)[1]["selected_attempt"] == ocr
    assert _pages(job)[1]["disposition"] == "reviewed_text"


def test_dry_run_checks_decisions_without_recording_them(job, tmp_path):
    sheet = _sheet(job, tmp_path / "review.csv")
    _edit(sheet, lambda rows: rows[0].update(decision="blank"))

    result = review_job(job, sheet, reviewer="A. Reader", dry_run=True)

    assert result["status"] == "checked"
    assert result["decisions"] == {"reviewed_blank": 1}
    assert _pages(job)[0]["review"] is None


def test_deleted_rows_and_notes_that_start_with_a_dash_are_accepted(job, tmp_path):
    sheet = _sheet(job, tmp_path / "review.csv")

    def change(rows):
        rows.pop()
        rows[0].update(decision="accept", note="-1 word illegible")

    _edit(sheet, change)
    review_job(job, sheet, reviewer="A. Reader")

    assert _pages(job)[0]["review"]["decisions"][0]["reason"] == "-1 word illegible"
    assert _pages(job)[1]["review"] is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda rows: rows[1].update(row_id="p9999"), "Unknown review row: p9999"),
        (lambda rows: rows[1].update(row_id="p0001"), "Duplicate review row: p0001"),
        (lambda rows: rows[1].update(decision="accept", note="=SUM(A1:A2)"), "formula in note"),
        (lambda rows: rows[1].update(decision="accept", note="\t=1+1"), "formula in note"),
        (lambda rows: rows[1].update(decision="approve"), "unknown decision 'approve'"),
        (lambda rows: rows[1].update(decision="use:local_ocr:nothing"), "no completed attempt"),
    ],
)
def test_an_unfit_sheet_is_refused_before_anything_is_recorded(job, tmp_path, change, message):
    sheet = _sheet(job, tmp_path / "review.csv")

    def accept_first_then(rows):
        rows[0]["decision"] = "accept"
        change(rows)

    _edit(sheet, accept_first_then)

    with pytest.raises(ValueError, match=message):
        review_job(job, sheet, reviewer="A. Reader")
    assert all(page["review"] is None for page in _pages(job))


def _damage_text(old, new):
    def damage(sheet):
        sheet.write_text(sheet.read_text(encoding="utf-8-sig").replace(old, new, 1))

    return damage


def _damage_binding(change):
    def damage(sheet):
        binding = json.loads(binding_path(sheet).read_text())
        binding_path(sheet).write_text(json.dumps(change(binding)))

    return damage


def _drop_page(binding):
    binding["bindings"].pop("p0002")
    return binding


def _unreadable(sheet):
    """A cell longer than the csv module will read, as a damaged file can hold."""
    sheet.write_text(sheet.read_text(encoding="utf-8-sig") + '"' + "x" * 200_000 + '"\n')


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (_damage_text("decision", "verdict"), "Review sheet columns must be"),
        (_damage_text("\np0002,", "\np0002,2,extra,"), "Row 3 of the review sheet does not"),
        (_damage_text("\np0002,", ",x\np0002,"), "Row 2 of the review sheet does not"),
        (_damage_text("\np0002,", "\np0002\n"), "Row 3 of the review sheet does not"),
        (_unreadable, "Cannot read the review sheet"),
        (lambda sheet: binding_path(sheet).write_text("{"), "Cannot read review sheet binding"),
        (_damage_binding(lambda b: {**b, "schema_version": "9"}), "binding is invalid"),
        (_damage_binding(lambda b: [b]), "binding is invalid"),
        (_damage_binding(_drop_page), "written for a different set of pages"),
    ],
)
def test_a_damaged_sheet_or_binding_is_refused_before_anything_is_recorded(
    job, tmp_path, damage, message
):
    sheet = _sheet(job, tmp_path / "review.csv")
    _edit(sheet, lambda rows: rows[0].update(decision="accept"))
    before = (job / "job.json").read_bytes()
    damage(sheet)

    with pytest.raises(ValueError, match=message):
        review_job(job, sheet, reviewer="A. Reader")
    assert (job / "job.json").read_bytes() == before


@pytest.mark.parametrize("formula", ["=HYPERLINK(1)", "+1", "@SUM(A1)", "\t=1"])
def test_a_formula_in_the_decision_column_is_refused(job, tmp_path, formula):
    sheet = _sheet(job, tmp_path / "review.csv")
    _edit(sheet, lambda rows: rows[0].update(decision=formula))

    with pytest.raises(ValueError, match="Review row p0001 has a formula in decision"):
        review_job(job, sheet, reviewer="A. Reader")
    assert all(page["review"] is None for page in _pages(job))


@pytest.mark.parametrize(
    "data",
    [
        {"schema_version": "0.1", "bindings": {}},
        {"schema_version": "0.1", "bindings": [], "rows": []},
        {"schema_version": "0.2", "bindings": {}, "rows": []},
    ],
)
def test_a_decisions_file_of_another_shape_is_refused(job, tmp_path, data):
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="A decisions file holds schema_version 0.1"):
        review_job(job, path, reviewer="A. Reader")
    assert all(page["review"] is None for page in _pages(job))


def test_a_sheet_written_before_the_evidence_changed_is_refused(job, tmp_path):
    ocr = next(a["attempt_id"] for a in _pages(job)[1]["attempts"] if a["stage"] == "local_ocr")
    earlier = _sheet(job, tmp_path / "earlier.csv")
    later = _sheet(job, tmp_path / "later.csv")
    _edit(later, lambda rows: rows[1].update(decision=f"use:{ocr}"))
    review_job(job, later, reviewer="A. Reader")
    _edit(earlier, lambda rows: rows[0].update(decision="accept"))

    with pytest.raises(ValueError, match="p0002 no longer matches"):
        review_job(job, earlier, reviewer="A. Reader")
    assert _pages(job)[0]["review"] is None


def test_decisions_need_a_named_reviewer(job, tmp_path, monkeypatch):
    sheet = _sheet(job, tmp_path / "review.csv")
    _edit(sheet, lambda rows: rows[0].update(decision="accept"))

    with pytest.raises(ValueError, match="Name the reviewer"):
        review_job(job, sheet)
    monkeypatch.setenv("PAGELEDGER_REVIEWER", "B. Reader")
    review_job(job, sheet)
    assert _pages(job)[0]["review"]["decisions"][0]["reviewer"] == "B. Reader"


def test_a_json_receipt_names_its_own_reviewers(job, tmp_path):
    receipt = tmp_path / "review.json"
    receipt.write_text("{}")

    with pytest.raises(ValueError, match="--reviewer is for review sheets"):
        review_job(job, receipt, reviewer="A. Reader")


def test_cli_writes_a_sheet_and_reports_the_decisions(job, tmp_path, capsys):
    sheet = tmp_path / "review.csv"
    assert main(["review-sheet", str(job), "--out", str(sheet)]) == 0
    assert f"Review sheet: {sheet} (2 pages)" in capsys.readouterr().out
    _edit(sheet, lambda rows: rows[0].update(decision="accept"))

    arguments = ["review-job", str(job), "--review", str(sheet), "--reviewer", "A. Reader"]
    assert main([*arguments, "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith("Status: checked\nDecisions: reviewed_text 1\n")


def _published(root: Path) -> dict:
    return json.loads((root / "document.json").read_text())["pages"][0]["selected_output"]


def test_after_a_person_chose_a_reading_s_own_text_its_edition_stays_unpublished(
    setup,  # noqa: F811
    monkeypatch,
    tmp_path,
):
    from test_processing import _adjudicated, _review

    from pageledger.export import export_job

    page = _adjudicated(setup, monkeypatch, tmp_path)
    base, witness = page["edition"]["base_attempt"], page["contested"]["witness_attempt"]
    _review(setup, tmp_path, f"use:{witness}")
    out = tmp_path / "witness.jsonl"
    export_job(setup[2], out, format="jsonl")
    assert "edition" not in json.loads(out.read_text().splitlines()[0])
    _review(setup, tmp_path, f"use:{base}")
    raw = _published(setup[2])
    assert not raw.get("edition")
    sheet = tmp_path / "fresh.csv"
    create_review_sheet(setup[2], sheet)
    summary = next(csv.DictReader(sheet.read_text(encoding="utf-8-sig").splitlines()))
    assert "settled differences" not in summary["attempt_summary"]
    # Accepting the text shown keeps that text, not the edition.
    _review(setup, tmp_path, "accept")
    assert _published(setup[2])["sha256"] == raw["sha256"]
    # A correction typed from it starts from it.
    sheet = tmp_path / "after.csv"
    create_review_sheet(setup[2], sheet)
    bindings = json.loads(binding_path(sheet).read_text())["bindings"]
    decisions = tmp_path / "corrected.json"
    typed = raw["text"].replace("Работа", "Робота", 1)
    row = {"row_id": "p0001", "page_number": "1", "decision": "correct", "text": typed}
    decisions.write_text(json.dumps({"schema_version": "0.1", "bindings": bindings, "rows": [row]}))
    review_job(setup[2], decisions, reviewer="person:test")
    correction = _published(setup[2])["correction"]
    assert correction["from_sha256"] == raw["sha256"]
    assert correction["words"]["changed"] == 1
    assert verify_job(setup[2])["status"] == "pass"


def test_a_sheet_is_never_written_over_nor_into_the_job(job, tmp_path):
    root = job
    sheet = tmp_path / "out" / "review.csv"
    create_review_sheet(root, sheet)
    filled = sheet.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        create_review_sheet(root, sheet)
    assert sheet.read_bytes() == filled
    with pytest.raises(ValueError, match="outside the job's folder"):
        create_review_sheet(root, root / "report.md")
    assert verify_job(root)["status"] == "pass"


def test_a_second_sheet_or_a_person_s_file_keeps_its_own_guide(job, tmp_path):
    root = job
    folder = tmp_path / "shared"
    folder.mkdir()
    (folder / "review-decisions.txt").write_text("my own notes")
    create_review_sheet(root, folder / "a.csv")
    assert (folder / "review-decisions.txt").read_text() == "my own notes"
    assert (folder / "a-decisions.txt").read_text().startswith("How to fill in a.csv\n")
    create_review_sheet(root, tmp_path / "b" / "b.csv")
    create_review_sheet(root, tmp_path / "b" / "c.csv")
    assert (tmp_path / "b" / "review-decisions.txt").read_text().startswith("How to fill in b.csv")
    assert (tmp_path / "b" / "c-decisions.txt").read_text().startswith("How to fill in c.csv")


def _fill(sheet: Path, decisions: dict[str, str], extra: str = "") -> None:
    rows = list(csv.DictReader(sheet.read_text(encoding="utf-8-sig").splitlines()))
    for row in rows:
        row["decision"] = decisions.get(row["row_id"], "")
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FIELDS), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
        handle.write(extra)


def test_an_older_sheet_cannot_replace_a_later_review(job, tmp_path):
    root = job
    older, later = tmp_path / "older.csv", tmp_path / "later.csv"
    create_review_sheet(root, older)
    create_review_sheet(root, later)
    _fill(later, {"p0001": "blank"})
    review_job(root, later, reviewer="B. Later")
    _fill(older, {"p0001": "accept"})
    with pytest.raises(ValueError, match="no longer matches"):
        review_job(root, older, reviewer="A. Older")
    # A row the older sheet leaves empty does not stand in its way.
    _fill(older, {"p0002": "accept"})
    review_job(root, older, reviewer="A. Older")
    pages = read_record(root / "job.json")["pages"]
    assert [page["disposition"] for page in pages] == ["reviewed_blank", "reviewed_text"]


def test_empty_spreadsheet_rows_are_skipped_and_other_separators_named(job, tmp_path):
    root = job
    sheet = tmp_path / "s.csv"
    create_review_sheet(root, sheet)
    _fill(sheet, {"p0001": "accept"}, extra=",,,,,,,\n,,,,,,,\n")
    assert review_job(root, sheet, reviewer="A. Reader", dry_run=True)["status"] == "checked"
    sheet.write_text(sheet.read_text(encoding="utf-8-sig").replace(",", ";"), encoding="utf-8")
    with pytest.raises(ValueError, match="separates them with semicolons"):
        review_job(root, sheet, reviewer="A. Reader")
    sheet.write_bytes("row_id,page_number\nЖ".encode("cp1251"))
    with pytest.raises(ValueError, match="CSV UTF-8"):
        review_job(root, sheet, reviewer="A. Reader")
