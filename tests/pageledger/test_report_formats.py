"""Jobs written in an earlier report format keep verifying, byte for byte, after the wording moves on."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pageledger.document_report import render_document_report, render_transcript
from pageledger.processing import verify_job

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "report-0.9"
RENDERED = ("letters-lost", "escalated", "paused", "text-corrected", "job")


@pytest.mark.parametrize("name", RENDERED)
def test_a_format_0_9_report_renders_as_it_was_written(name):
    folder = GOLDEN / name
    report = json.loads((folder / "document.json").read_bytes())
    assert report["report_format"] == "0.9"
    assert render_document_report(report).encode("utf-8") == (folder / "report.md").read_bytes()
    assert render_transcript(report).encode("utf-8") == (folder / "transcript.md").read_bytes()


def test_a_format_0_9_job_still_verifies(tmp_path):
    job = tmp_path / "jobs" / "book"
    shutil.copytree(GOLDEN / "job", job)
    shutil.copyfile(GOLDEN / "book.txt", tmp_path / "book.txt")
    result = verify_job(job)
    assert result["status"] == "pass", result
