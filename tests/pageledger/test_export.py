"""Exports carry each page's text, review state and attempt identity, never absolute paths."""

import json
import os
import shutil
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml
from jsonschema import validate

from pageledger.checkpoint import read_record
from pageledger.cli import main
from pageledger.document_report import (
    build_document_report,
    relative_path,
    render_document_report,
    render_transcript,
)
from pageledger.export import FORMATS, export_job
from pageledger.processing import process, review_job

ROOT = Path(__file__).resolve().parents[2]
TEI = "{http://www.tei-c.org/ns/1.0}"
TEI_ALL = "https://tei-c.org/release/xml/tei/custom/schema/relaxng/tei_all.rng"


@pytest.fixture
def job(tmp_path):
    """Three pages: page 1 reviewed, page 2 unreviewed, page 3 never processed."""
    source = tmp_path / "scans" / "book.txt"
    source.parent.mkdir()
    source.write_text("First line\nsecond line\fPage two \x07text\fPage three")
    config = tmp_path / "config.yml"
    processing = {"local_text": {"adapter": "text"}, "limits": {"max_attempt_pages": 2}}
    config.write_text(yaml.safe_dump({"schema_version": "0.1", "processing": processing}))
    root = tmp_path / "jobs" / "book"
    process(source=source, config_path=config, out_dir=root)
    page = json.loads((root / "document.json").read_text())["pages"][0]
    decision = {
        "page_id": page["page_id"],
        "page_number": 1,
        "disposition": "reviewed_text",
        "selected_attempt": page["selected_output"]["attempt_id"],
        "output_sha256": page["selected_output"]["sha256"],
        "reason": "checked against the scan",
        "reviewer": "A. Reader",
        "reviewed_at": "2026-09-11T12:00:00Z",
    }
    receipt = tmp_path / "review.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "source_sha256": page["source_sha256"],
                "decisions": [decision],
            }
        )
    )
    review_job(root, receipt)
    return root


def _export(job, fmt, **options):
    out = job.parents[1] / "exports" / f"book.{fmt}"
    out.parent.mkdir(exist_ok=True)
    export_job(job, out, format=fmt, **options)
    return out.read_text(encoding="utf-8")


def test_text_export_marks_each_page_with_its_review_and_attempt(job):
    text = _export(job, "txt")

    page_one, page_two, page_three = text.split("--- Page ")[1:]
    assert page_one.startswith("1 ---\nDisposition: Reviewed text (reviewed_text)\n")
    assert "Review: A. Reader, 2026-09-11T12:00:00Z: checked against the scan" in page_one
    assert "Attempt: local_text:doc_0001_page_0001, text" in page_one
    assert "\n\nFirst line\nsecond line\n" in page_one
    assert "Review: none" in page_two
    assert page_three == "3 ---\nDisposition: Not processed (pending)\nReview: none\n"


def test_markdown_links_each_page_relative_to_the_export(job):
    markdown = _export(job, "md")

    assert markdown.startswith("# book.txt\n")
    assert "- [Source page](<../scans/book.txt#page=2>)" in markdown


def test_jsonl_records_follow_the_export_schema(job):
    schema = json.loads((ROOT / "schemas" / "export-page.schema.json").read_text())
    records = [json.loads(line) for line in _export(job, "jsonl").splitlines()]

    for record in records:
        validate(record, schema)
    assert [r["page_number"] for r in records] == [1, 2, 3]
    assert records[0]["review"]["reviewer"] == "A. Reader"
    assert records[0]["attempt"]["adapter"] == "text"
    assert records[2]["attempt"] is None and records[2]["text"] is None


def test_tei_marks_page_breaks_review_state_and_missing_text(job):
    tei = ET.fromstring(_export(job, "tei").encode())

    breaks = tei.findall(f".//{TEI}pb")
    assert [(pb.get("n"), pb.get("facs")) for pb in breaks] == [
        (str(n), f"../scans/book.txt#page={n}") for n in (1, 2, 3)
    ]
    pages = tei.findall(f".//{TEI}div[@type='page']")
    assert [div.get("subtype") for div in pages] == ["reviewed", "unreviewed", "unreviewed"]
    first = pages[0].find(f"{TEI}ab")
    assert first.text == "First line" and first.find(f"{TEI}lb").tail == "second line"
    assert pages[1].find(f"{TEI}ab").text == "Page two text"
    assert pages[2].find(f"{TEI}gap").get("reason") == "pending"


def test_tei_validates_against_tei_all(job, tmp_path):
    xmllint = shutil.which("xmllint")
    if xmllint is None:
        pytest.skip("xmllint is not installed")
    schema = tmp_path / "tei_all.rng"
    try:
        with urllib.request.urlopen(TEI_ALL, timeout=30) as response:
            schema.write_bytes(response.read())
    except OSError as exc:
        pytest.skip(f"TEI All schema unavailable: {exc}")
    _export(job, "tei")

    checked = subprocess.run(
        [xmllint, "--noout", "--relaxng", str(schema), str(job.parents[1] / "exports/book.tei")],
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize("report_format", ["0.6", "0.5.1"])
def test_exports_never_contain_absolute_paths(job, report_format):
    if report_format == "0.5.1":
        report = build_document_report(read_record(job / "job.json"), job, report_format="0.5.1")
        (job / "document.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        (job / "transcript.md").write_text(render_transcript(report))
        (job / "report.md").write_text(render_document_report(report))

    for fmt in FORMATS:
        assert str(job.parents[1]) not in _export(job, fmt)


def test_reports_name_the_source_by_a_path_relative_to_the_job(job):
    for name in ("document.json", "report.md", "transcript.md"):
        content = (job / name).read_text()
        assert str(job.parents[1]) not in content
        assert "../../scans/book.txt" in content


def test_relative_path_across_windows_drives_keeps_only_the_name(monkeypatch):
    def other_drive(path, start):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", other_drive)
    assert relative_path("/scans/book.pdf", Path("/jobs/book")) == "book.pdf"


def test_reviewed_only_exports_only_reviewed_pages(job):
    records = _export(job, "jsonl", reviewed_only=True).splitlines()

    assert [json.loads(line)["page_number"] for line in records] == [1]


def test_export_refuses_a_job_that_does_not_verify(job, tmp_path):
    (job / "report.md").write_text("edited by hand")

    with pytest.raises(ValueError, match="does not verify"):
        export_job(job, tmp_path / "book.txt", format="txt")
    assert not (tmp_path / "book.txt").exists()


def test_cli_reports_the_pages_written(job, tmp_path, capsys):
    out = tmp_path / "book.md"

    assert main(["export", str(job), "--format", "md", "--out", str(out)]) == 0
    assert capsys.readouterr().out == f"Exported 3 pages to {out}\n"
