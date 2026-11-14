"""One writer at a time: a job busy with another command refuses to be changed."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from pageledger.checkpoint import writer_lock
from pageledger.cli import main
from pageledger.review_sheet import FIELDS


@pytest.fixture
def job(tmp_path: Path) -> Path:
    source = tmp_path / "letters.txt"
    source.write_text("first page\fsecond page\n", encoding="utf-8")
    root = tmp_path / "job"
    assert main(["process", str(source), "--out", str(root)]) == 0
    return root


def _filled_sheet(job: Path, sheet: Path) -> Path:
    assert main(["review", str(job), "--sheet", str(sheet)]) == 0
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["decision"] = "accept"
    with sheet.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return sheet


def _state(job: Path) -> dict[str, bytes]:
    return {
        path.relative_to(job).as_posix(): path.read_bytes()
        for path in sorted(job.rglob("*"))
        if path.is_file() and path.name != ".resume.lock"
    }


def test_a_job_with_an_active_writer_refuses_every_other_writer(
    job: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sheet = _filled_sheet(job, tmp_path / "review.csv")
    decisions = tmp_path / "adjudicated.json"
    decisions.write_text(json.dumps({"schema_version": "0.1", "pages": []}))
    before = _state(job)
    capsys.readouterr()

    with writer_lock(job):
        for argv in (
            ["resume", str(job)],
            ["review", str(job), "--apply", str(sheet), "--reviewer", "A. Reader"],
            ["review", str(job), "--sheet", str(tmp_path / "second.csv")],
            ["review", str(job)],
            ["adjudicate", str(job), "--decisions", str(decisions), "--reviewer", "A. Reader"],
        ):
            assert main(argv) == 1, argv
            assert "already has an active writer" in capsys.readouterr().err, argv

    assert _state(job) == before
    assert not (tmp_path / "second.csv").exists()
    # Once the writer is gone, the same review applies.
    assert main(["review", str(job), "--apply", str(sheet), "--reviewer", "A. Reader"]) == 0
