"""Review sheets: a CSV a person fills in, refused whenever it no longer fits the job."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import validate
from test_processing import TEXT, StageAdapter

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


def test_a_filled_in_sheet_records_decisions_and_leaves_blank_rows_unreviewed(job, tmp_path):
    sheet = _sheet(job, tmp_path / "review.csv")
    assert sheet.read_bytes().startswith(b"\xef\xbb\xbf")
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["source_page"] for row in rows] == [
        "scans/source.txt#page=1",
        "scans/source.txt#page=2",
    ]
    assert rows[1]["review_reasons"] == "coverage_defect"

    _edit(sheet, lambda rows: rows[0].update(decision="accept", note="Checked against the scan"))
    result = review_job(job, sheet, reviewer="A. Reader")

    assert result["decisions"] == {"reviewed_text": 1}
    first, second = _pages(job)
    assert first["disposition"] == "reviewed_text"
    assert first["review"]["decisions"][0]["reviewer"] == "A. Reader"
    assert first["review"]["decisions"][0]["reason"] == "Checked against the scan"
    assert second["review"] is None
    assert verify_job(job)["status"] == "pass"


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
