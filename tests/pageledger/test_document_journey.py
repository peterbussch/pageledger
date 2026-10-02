"""Regression coverage for the maintained newcomer document-job journey."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pageledger.adapters import TextAdapter
from pageledger.processing import process, review_job

ROOT = Path(__file__).resolve().parents[2]


def test_document_first_run_journey_records_recovery_and_bounded_review(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "journey"
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    environment.pop("PAGELEDGER_TUTORIAL_RECOVERY_HELPER", None)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "run_first_run.py"),
            "--document",
            str(ROOT / "docs" / "document-first-run.md"),
            "--work-dir",
            str(work_dir),
            "--python",
            sys.executable,
            "--source-root",
            str(ROOT),
            "--expected-version",
            "0.6.1",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"

    complete = json.loads((work_dir / "jobs" / "complete" / "document.json").read_text())
    assert complete["status"] == "completed"
    assert complete["counts"]["selected_pages"] == 3
    assert complete["usage"]["cost_known"] is False
    assert complete["pages"][0]["review"] is not None

    recovery = work_dir / "jobs" / "interrupted"
    recovery_document = json.loads((recovery / "document.json").read_text())
    assert recovery_document["status"] == "completed"
    assert all(len(page["attempts"]) == 1 for page in recovery_document["pages"])
    calls = [
        json.loads(line) for line in (work_dir / "calls-interrupted.jsonl").read_text().splitlines()
    ]
    assert [call["page_number"] for call in calls] == [1, 2, 3]
    assert "DOCUMENT_JOURNEY_RECOVERY_OK" in result.stdout
    assert "DOCUMENT_JOURNEY_STALE_RECEIPT_REJECTED" in result.stdout
    assert "DOCUMENT_JOURNEY_REVIEW_OK" in result.stdout


def test_stale_review_record_is_rejected_without_extraction_or_artifact_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("one complete page\ftwo complete pages\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(
        "schema_version: '0.1'\n"
        "taxonomy:\n"
        "  page_types:\n"
        "    prose:\n"
        "      default_action: transcribe_text\n"
        "run:\n"
        "  adapter: text\n"
        "processing:\n"
        "  local_text:\n"
        "    adapter: text\n"
        "  local_ocr: null\n",
        encoding="utf-8",
    )
    job_dir = tmp_path / "job"
    assert process(source=source, config_path=config, out_dir=job_dir)["status"] == "completed"
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))["payload"]
    page = job["pages"][0]
    stale = {
        "schema_version": "0.1",
        "source_sha256": "0" * 64,
        "decisions": [
            {
                "page_id": page["page_id"],
                "page_number": page["page_number"],
                "disposition": "reviewed_text",
                "selected_attempt": None,
                "output_sha256": None,
                "reason": "stale review record",
                "reviewer": "synthetic test reviewer",
                "reviewed_at": "2026-09-12T12:00:00Z",
            }
        ],
    }
    review_path = tmp_path / "stale-review.json"
    review_path.write_text(json.dumps(stale), encoding="utf-8")
    before = {
        path.relative_to(job_dir): path.read_bytes()
        for path in job_dir.rglob("*")
        if path.is_file()
    }
    calls: list[int] = []

    def fail_if_called(self, source, *, page_id, page_number, action, prompt=None):
        calls.append(page_number)
        raise AssertionError("review-job must not call an extraction adapter")

    monkeypatch.setattr(TextAdapter, "extract", fail_if_called)
    with pytest.raises(ValueError, match="Invalid document review identity"):
        review_job(job_dir, review_path)

    assert calls == []
    after = {
        path.relative_to(job_dir): path.read_bytes()
        for path in job_dir.rglob("*")
        if path.is_file()
    }
    assert after == before
