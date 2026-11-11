"""A job verifies wherever it is; it resumes only where it was written."""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest
from test_processing import (  # noqa: F401  (setup is a fixture)
    _answer,
    _contesting,
    _limit_attempts,
    _validate_schemas,
    launch,
    setup,
)

from pageledger.checkpoint import read_record, write_record
from pageledger.cli import main
from pageledger.export import export_job
from pageledger.processing import (
    adjudicate_job,
    create_review_sheet,
    process,
    resume_job,
    review_job,
    verify_job,
)
from pageledger.review_html import write_review_html


def _copy(root: Path, tmp_path: Path, name: str = "deposit") -> Path:
    target = tmp_path / name / "job"
    shutil.copytree(root, target)
    return target.resolve()


def _codes(result: dict) -> list[str]:
    return [warning["code"] for warning in result["warnings"]]


def _accept_first_page(root: Path, tmp_path: Path) -> dict:
    sheet = tmp_path / "review.csv"
    create_review_sheet(root, sheet)
    rows = list(csv.DictReader(sheet.read_text(encoding="utf-8-sig").splitlines()))
    rows[0]["decision"] = "accept"
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    return review_job(root, sheet, reviewer="person:colleague")


def test_a_copied_job_with_ocr_and_image_stages_verifies_where_it_is(setup, tmp_path, capsys):  # noqa: F811
    assert launch(setup, pages="2-3")["status"] == "completed"
    original = setup[2].resolve()
    stages = {stage["stage"] for stage in read_record(original / "job.json")["stages"]}
    assert stages == {"local_text", "local_ocr", "image"}
    assert verify_job(original)["warnings"] == []

    copy = _copy(original, tmp_path)
    result = verify_job(copy)
    assert result["status"] == "pass", result
    assert result["warnings"] == [
        {
            "code": "job_relocated",
            "message": f"Job was written in {original} and is now in {copy}",
            "recorded_root": str(original),
            "root": str(copy),
        }
    ]

    # The copy stands alone: the original directory can go.
    shutil.rmtree(original)
    assert verify_job(copy)["status"] == "pass"
    assert main(["verify-job", str(copy)]) == 0
    out = capsys.readouterr().out
    assert "Status: pass" in out
    assert f"Warning: [job_relocated] Job was written in {original}" in out

    # Read-only commands need no extra step.
    assert main(["inspect-job", str(copy)]) == 0
    assert "source.txt: document job report" in capsys.readouterr().out
    assert export_job(copy, tmp_path / "out.jsonl", format="jsonl") == 2
    assert create_review_sheet(copy, tmp_path / "sheet.csv")["rows"] == 2
    view = write_review_html(copy, tmp_path / "view", images="none")
    assert view["pages"] == 2
    assert str(copy) in (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    assert read_record(copy / "job.json")["root"] == str(original)


def test_tampering_with_a_moved_job_still_fails(setup, tmp_path):  # noqa: F811
    assert launch(setup, pages="2-3")["status"] == "completed"
    job = read_record(setup[2] / "job.json")
    image = next(stage for stage in job["stages"] if stage["stage"] == "image")
    evidence = next((setup[2] / image["run_path"] / "evidence").iterdir())

    def tampered(change) -> dict:
        copy = _copy(setup[2], tmp_path, f"t{len(list(tmp_path.glob('t*')))}")
        change(copy)
        return verify_job(copy)

    raw = job["pages"][0]["attempts"][0]["raw_artifact"]
    for change, error in (
        (
            lambda copy: (copy / raw).write_text("changed"),
            "Recovery output changed or completed output is missing",
        ),
        (
            lambda copy: (copy / "report.md").write_text("changed"),
            "Derived document report does not match document.json",
        ),
        (lambda copy: shutil.rmtree(copy / image["run_path"]), "Completed child run is missing"),
        (
            lambda copy: (copy / image["run_path"] / "evidence" / evidence.name).write_bytes(b"x"),
            "Image evidence invalid: artifact bytes or SHA-256 changed",
        ),
    ):
        assert tampered(change)["error"] == error


def test_a_stage_run_from_another_tree_is_refused(setup, tmp_path):  # noqa: F811
    """Each stage checkpoint is still bound to the directory the job recorded."""
    assert launch(setup, pages="2")["status"] == "completed"
    other = tmp_path / "other"
    process(source=setup[0], config_path=setup[1], out_dir=other, pages="2")
    copy = _copy(setup[2], tmp_path)
    shutil.rmtree(copy / "attempts" / "local_text")
    shutil.copytree(other / "attempts" / "local_text", copy / "attempts" / "local_text")
    result = verify_job(copy)
    assert result["status"] == "fail"
    assert result["error"] == "Invalid recovery job identity or plan"


def test_a_colleague_reviews_a_copy_and_the_record_says_where(setup, tmp_path):  # noqa: F811
    launch(setup)
    original = setup[2].resolve()
    sealed = (original / "job.json").read_bytes()
    copy = _copy(original, tmp_path)

    assert _accept_first_page(copy, tmp_path)["status"] == "completed"
    assert (original / "job.json").read_bytes() == sealed
    job = read_record(copy / "job.json")
    assert job["root"] == str(original)
    [relocation] = job["relocations"]
    assert relocation["root"] == str(copy) and relocation["command"] == "review-job"
    assert job["pages"][0]["disposition"] == "reviewed_text"
    _validate_schemas(copy)
    report = (copy / "report.md").read_text(encoding="utf-8")
    assert f"Changed elsewhere: review-job on {relocation['at']} in {copy}" in report

    result = verify_job(copy)
    assert result["status"] == "pass", result
    assert _codes(result) == ["job_relocated", "job_changed_elsewhere"]

    # Moved on again, it still verifies and still says where it was reviewed.
    again = _copy(copy, tmp_path, "archive")
    assert _codes(verify_job(again)) == ["job_relocated", "job_changed_elsewhere"]

    # Hiding the relocation, or changing the receipt, does not verify.
    for edit, error in (
        (
            lambda value: value.pop("relocations"),
            "Document report differs from verified job evidence",
        ),
        (
            lambda value: value["pages"][0]["review"]["decisions"][0].update(reason="changed"),
            "Active review differs from retained review history",
        ),
    ):
        job = read_record(again / "job.json")
        edit(job)
        write_record(again / "job.json", job)
        assert verify_job(again)["error"] == error
        shutil.rmtree(again)
        again = _copy(copy, tmp_path, "archive")


def test_review_in_place_records_no_relocation(setup, tmp_path):  # noqa: F811
    launch(setup)
    _accept_first_page(setup[2], tmp_path)
    job = read_record(setup[2] / "job.json")
    assert "relocations" not in job
    assert "Changed elsewhere" not in (setup[2] / "report.md").read_text(encoding="utf-8")
    assert verify_job(setup[2])["warnings"] == []


def test_adjudicating_a_copy_is_recorded(setup, monkeypatch, tmp_path):  # noqa: F811
    _contesting(setup, monkeypatch)
    assert launch(setup, pages="1")["status"] == "completed"
    copy = _copy(setup[2], tmp_path)
    _answer(copy, tmp_path / "packets", "полные")
    adjudicate_job(copy, tmp_path / "packets", reviewer="agent:test")
    job = read_record(copy / "job.json")
    assert [item["command"] for item in job["relocations"]] == ["adjudicate"]
    assert job["pages"][0]["disposition"] == "adjudicated_text"
    assert "relocations" not in read_record(setup[2] / "job.json")
    result = verify_job(copy)
    assert result["status"] == "pass", result
    assert _codes(result) == ["job_relocated", "job_changed_elsewhere"]


def test_a_deposited_copy_is_reviewed_without_its_source(setup, tmp_path, capsys):  # noqa: F811
    launch(setup)
    copy = _copy(setup[2], tmp_path)
    source = setup[0].resolve()
    source.rename(tmp_path / "gone.txt")
    missing = {
        "code": "source_missing",
        "message": f"Source file is missing: {source}",
        "source": str(source),
    }

    result = _accept_first_page(copy, tmp_path)

    assert result["status"] == "completed" and result["warnings"] == [missing]
    assert read_record(copy / "job.json")["pages"][0]["disposition"] == "reviewed_text"
    assert _codes(verify_job(copy)) == ["job_relocated", "job_changed_elsewhere", "source_missing"]
    # Other bytes at the recorded path are another document, and are refused.
    source.write_text("another document")
    with pytest.raises(ValueError, match="Document job source changed or is missing"):
        _accept_first_page(copy, tmp_path)


def test_a_deposited_copy_is_adjudicated_without_its_source(setup, monkeypatch, tmp_path, capsys):  # noqa: F811
    _contesting(setup, monkeypatch)
    assert launch(setup, pages="1")["status"] == "completed"
    copy = _copy(setup[2], tmp_path)
    setup[0].unlink()
    _answer(copy, tmp_path / "packets", "полные")

    code = main(
        ["adjudicate", str(copy), "--decisions", str(tmp_path / "packets"), "--reviewer", "a:b"]
    )

    assert code == 0
    assert "Warning: [source_missing] Source file is missing" in capsys.readouterr().out
    assert read_record(copy / "job.json")["pages"][0]["disposition"] == "adjudicated_text"
    assert verify_job(copy)["status"] == "pass"


def test_a_paused_job_resumes_only_where_it_was_written(setup, tmp_path):  # noqa: F811
    assert _limit_attempts(setup, 2)["status"] == "paused_budget"
    original = setup[2].resolve()
    moved = tmp_path / "elsewhere" / "job"
    moved.parent.mkdir()
    shutil.move(original, moved)
    sealed = (moved / "job.json").read_bytes()
    calls = list(setup[3]["calls"])

    with pytest.raises(ValueError, match=f"move it back to {original} to resume it"):
        resume_job(moved, raise_limits={"max_attempt_pages": 3})
    assert setup[3]["calls"] == calls
    assert (moved / "job.json").read_bytes() == sealed
    assert verify_job(moved)["status"] == "pass"

    shutil.move(moved, original)
    assert resume_job(original, raise_limits={"max_attempt_pages": 3})["status"] == "completed"
    assert len(setup[3]["calls"]) == len(calls) + 1
    assert verify_job(original)["warnings"] == []


def test_verify_reports_a_missing_source_but_refuses_a_changed_one(setup, tmp_path):  # noqa: F811
    launch(setup)
    source = setup[0]
    source.rename(tmp_path / "gone.txt")
    result = verify_job(setup[2])
    assert result["status"] == "pass"
    assert result["warnings"] == [
        {
            "code": "source_missing",
            "message": f"Source file is missing: {source.resolve()}",
            "source": str(source.resolve()),
        }
    ]
    # Other bytes at the recorded path are a different document, not a missing one.
    source.write_text("another document")
    result = verify_job(setup[2])
    assert result["status"] == "fail"
    assert result["error"] == "Document job source changed or is missing"


def test_a_job_with_an_empty_partials_directory_lost_verifies(setup):  # noqa: F811
    launch(setup)
    partials = setup[2] / "partials"
    assert partials.is_dir() and not any(partials.iterdir())
    partials.rmdir()
    assert verify_job(setup[2])["status"] == "pass"
    assert json.loads((setup[2] / "document.json").read_text())["status"] == "completed"
