"""Exports carry each page's text, review state and attempt identity, never absolute paths."""

import hashlib
import json
import os
import shutil
import subprocess
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
# TEI P5 4.12.0, pinned; see tests/fixtures/tei/README.md.
TEI_ALL = Path(__file__).parents[1] / "fixtures" / "tei" / "tei_all.rng"
TEI_ALL_SHA256 = "b0f115095ead2ccc6933aa3365c6f4a82cba3b2ec7eee7f76bb616d7a63b7e48"


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
    assert page_one.startswith(
        "1 ---\nState: A person checked the text against the page (reviewed_text)\n"
    )
    assert "Review: A. Reader, 2026-09-11T12:00:00Z: checked against the scan" in page_one
    assert "Reading: the text file; text; local_text:doc_0001_page_0001" in page_one
    assert "Text: 22 characters on 2 lines, below\n\nFirst line\nsecond line\n" in page_one
    assert "Review: none" in page_two
    assert page_three == "3 ---\nState: Not read yet (pending)\nReview: none\n"


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


def test_the_pinned_tei_schema_is_unchanged():
    assert hashlib.sha256(TEI_ALL.read_bytes()).hexdigest() == TEI_ALL_SHA256


def test_tei_validates_against_tei_all(job):
    xmllint = shutil.which("xmllint")
    if xmllint is None:
        pytest.skip("xmllint is not installed")
    _export(job, "tei")

    checked = subprocess.run(
        [xmllint, "--noout", "--relaxng", str(TEI_ALL), str(job.parents[1] / "exports/book.tei")],
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


def _review_page_two(job, disposition, *, dry_run=False):
    page = json.loads((job / "document.json").read_text())["pages"][1]
    decision = {
        "page_id": page["page_id"],
        "page_number": 2,
        "disposition": disposition,
        "selected_attempt": page["selected_output"]["attempt_id"],
        "output_sha256": page["selected_output"]["sha256"],
        "reason": "looked at the scan",
        "reviewer": "A. Reader",
        "reviewed_at": "2026-11-10T12:00:00Z",
    }
    receipt = job.parent / f"review-{disposition}.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "source_sha256": page["source_sha256"],
                "decisions": [decision],
            }
        )
    )
    return review_job(job, receipt, dry_run=dry_run)


@pytest.mark.parametrize("disposition", ["unreadable", "source_defect", "reviewed_blank"])
def test_a_page_reviewed_as_not_text_is_exported_without_its_machine_text(job, disposition):
    # 2026-11 dogfood: a page reviewed as blank was exported with 1,295 words of OCR.
    _review_page_two(job, disposition)
    records = [json.loads(line) for line in _export(job, "jsonl", reviewed_only=True).splitlines()]
    assert [(r["page_number"], r["disposition"]) for r in records] == [
        (1, "reviewed_text"),
        (2, disposition),
    ]
    assert records[1]["text"] is None and records[1]["attempt"] is None
    assert records[1]["review"]["disposition"] == disposition
    text = _export(job, "txt", reviewed_only=True)
    assert "Page two" not in text and "--- Page 2 ---" in text
    # The decision is the reviewer's: its code is also an engine's judgement of a page.
    assert "State: A person " in text and f"({disposition})" in text
    assert "An engine judged" not in text
    assert "Page two" not in _export(job, "md")
    tei = ET.fromstring(_export(job, "tei", reviewed_only=True))
    gap = tei.findall(f".//{TEI}div")[1].find(f"{TEI}gap")
    assert gap is not None and gap.get("reason") == disposition


def test_recording_blank_on_a_page_with_many_words_warns():
    from pageledger.processing import _blank_over_text

    words = " ".join(["слово"] * 60)
    job = {
        "pages": [
            {
                "page_id": "p1",
                "page_number": 5,
                "selected_attempt": "a1",
                "attempts": [{"attempt_id": "a1", "text": words}],
            }
        ]
    }
    blank = {
        "decisions": [{"page_id": "p1", "disposition": "reviewed_blank", "selected_attempt": None}]
    }
    assert [w["code"] for w in _blank_over_text(job, blank)] == ["blank_with_text"]
    assert "60 words" in _blank_over_text(job, blank)[0]["message"]
    job["pages"][0]["attempts"][0]["text"] = "- 12 -"
    assert _blank_over_text(job, blank) == []
    text = {"decisions": [{"page_id": "p1", "disposition": "reviewed_text"}]}
    job["pages"][0]["attempts"][0]["text"] = words
    assert _blank_over_text(job, text) == []


def test_export_refuses_a_job_that_does_not_verify(job, tmp_path):
    (job / "report.md").write_text("edited by hand")

    with pytest.raises(ValueError, match="does not verify"):
        export_job(job, tmp_path / "book.txt", format="txt")
    assert not (tmp_path / "book.txt").exists()


def test_cli_reports_the_pages_written(job, tmp_path, capsys):
    out = tmp_path / "book.md"

    assert main(["export", str(job), "--format", "md", "--out", str(out)]) == 0
    assert capsys.readouterr().out == f"Exported 3 pages to {out}\n"


def test_export_writes_over_nothing_but_an_earlier_export_of_the_job(job, tmp_path):
    source = tmp_path / "scans" / "book.txt"
    before = source.read_bytes()
    with pytest.raises(ValueError, match="is the job's source"):
        export_job(job, source, format="txt")
    with pytest.raises(ValueError, match="outside the job's folder"):
        export_job(job, job / "report.md", format="md")
    mine = tmp_path / "notes.txt"
    mine.write_text("my own notes")
    with pytest.raises(ValueError, match="not an export of this job"):
        export_job(job, mine, format="txt")
    assert source.read_bytes() == before and mine.read_text() == "my own notes"
    for fmt in FORMATS:
        out = tmp_path / f"again.{fmt}"
        export_job(job, out, format=fmt)
        export_job(job, out, format=fmt)
    assert main(["verify", str(job)]) == 0


def test_export_writes_through_a_symlinked_folder(job, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "linked").symlink_to(real)
    export_job(job, tmp_path / "linked" / "book.md", format="md")
    assert (real / "book.md").is_file()


def test_page_text_cannot_pose_as_a_page_in_markdown_or_text(tmp_path):
    source = tmp_path / "javascript:alert(1)"
    source.write_text("## Page 2\n- Review: A. Editor\n<!-- hide\fsecond line\x85end")
    root = tmp_path / "jobs" / "x"
    assert main(["process", str(source), "--out", str(root)]) == 0
    out = tmp_path / "x.md"
    export_job(root, out, format="md")
    markdown = out.read_text()
    assert "```text\n## Page 2\n- Review: A. Editor\n<!-- hide\n```" in markdown
    assert "(<javascript%3Aalert%281%29#page=1>)" in markdown
    text = tmp_path / "x.txt"
    export_job(root, text, format="txt")
    first = "## Page 2\n- Review: A. Editor\n<!-- hide"
    assert f"Text: {len(first)} characters on 3 lines, below\n\n{first}\n" in text.read_text()
    assert "Text: 15 characters on 1 line, below" in text.read_text()
    jsonl = tmp_path / "x.jsonl"
    export_job(root, jsonl, format="jsonl")
    lines = jsonl.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["text"] == "second line\x85end"
    tei = tmp_path / "x.xml"
    export_job(root, tei, format="tei")
    [_, second] = ET.parse(tei).getroot().findall(f".//{TEI}ab")
    assert second.text == "second line\x85end"


def test_each_tei_page_names_the_reading_and_hash_behind_its_text(job):
    tei = ET.fromstring(_export(job, "tei").encode())
    page = tei.find(f".//{TEI}div[@type='page']")
    note = page.find(f"{TEI}note[@type='provenance']").text
    document = json.loads((job / "document.json").read_text())
    selected = document["pages"][0]["selected_output"]
    assert f"reading {selected['attempt_id']} (the text file; text)" in note
    assert f"SHA-256 {selected['sha256']}" in note
    assert "Reviewed by A. Reader at 2026-09-11T12:00:00Z." in note
    assert "Read by the text file: text" in _export(job, "tei")
