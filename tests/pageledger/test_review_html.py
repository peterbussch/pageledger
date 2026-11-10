"""The review view: a reading copy of a job whose decisions leave as a review sheet."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from test_processing import TEXT, StageAdapter, _contesting, launch, setup  # noqa: F401

from pageledger import review_html, runner
from pageledger.checkpoint import read_record
from pageledger.cli import main
from pageledger.processing import create_review_sheet, review_job, verify_job
from pageledger.review_html import (
    CSV_JS,
    decisions_csv,
    diff_html,
    render_page_image,
    text_html,
    write_review_html,
)

HOSTILE = (
    'Before </script><script>alert("x")</script> & \'quoted\' "words" <b>bold</b>\r\n'
    "  indented\ttabbed  line\n"
    "سطر عربي mixed with Latin\n"
) * 3


@pytest.fixture
def job(tmp_path, monkeypatch):
    """Two pages; page 2 is held after local text, and OCR reads it as hostile text."""
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
        object.__setattr__(stage, "texts", {"local_ocr": HOSTILE})
        return stage

    monkeypatch.setattr(runner, "load_adapter", adapter)
    monkeypatch.delenv("PAGELEDGER_REVIEWER", raising=False)
    root = tmp_path / "job"
    from pageledger.processing import process

    process(source=source, config_path=config, out_dir=root)
    return root


def _embedded(document: str) -> dict:
    found = re.search(
        r'<script type="application/json" id="review-data">(.*?)</script>', document, re.S
    )
    assert found
    return json.loads(found.group(1))


def _files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".resume.lock"
    }


def test_the_view_shows_each_page_and_changes_nothing_in_the_job(job, tmp_path):
    before = _files(job)
    result = write_review_html(job, tmp_path / "view", images="none")

    assert _files(job) == before
    assert result["pages"] == 2 and result["images"] == 0
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    assert sorted(path.name for path in (tmp_path / "view").iterdir()) == [
        "index.html",
        "review.csv",
        "review.csv.binding.json",
    ]
    assert 'id="p0001"' in document and 'id="p0002"' in document
    # The held page comes first in the list.
    assert document.index('href="#p0002"') < document.index('href="#p0001"')
    assert "Possible missing or incomplete content (coverage_defect)" in document
    assert "warnings.md#document-job-dispositions" in document
    assert "Nothing you choose here is recorded in the job." in document
    assert "pageledger review-job" in document and "--reviewer" in document
    assert "No page image in this view." in document
    # Nothing is fetched; the only web addresses are links to the documentation.
    assert not re.search(r"src=\"[a-z]+:|<link|@import|url\(", document)
    links = re.findall(r'href="(https?:[^"]*)"', document)
    assert links and all(link.startswith(review_html.DOCS) for link in links)
    assert "default-src &#x27;none&#x27;" in document


def test_job_text_is_escaped_and_shown_exactly(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")

    assert 'alert("x")' not in document
    assert "<b>bold</b>" not in document
    # Only the view's own two scripts close a script element.
    assert document.count("</script>") == 2
    shown = text_html(HOSTILE)
    assert '<span class="ctl" title="U+000D">␍</span>' in shown
    assert f'<pre class="text" dir="auto">{shown}</pre>' in document
    assert html.unescape(re.sub(r"<span class=\"ctl\"[^>]*>␍</span>", "\r", shown)) == HOSTILE


def test_embedded_rows_and_binding_are_what_review_sheet_writes(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    data = _embedded((tmp_path / "view" / "index.html").read_text(encoding="utf-8"))
    # A sheet at the same depth, so its links to the source are the same.
    sheet = tmp_path / "sheet" / "review.csv"
    create_review_sheet(job, sheet)
    with sheet.open(encoding="utf-8-sig", newline="") as stream:
        assert data["rows"] == list(csv.DictReader(stream))
    assert data["binding"] == sheet.with_name("review.csv.binding.json").read_text()
    assert (tmp_path / "view" / "review.csv").read_bytes() == sheet.read_bytes()
    assert decisions_csv(data["rows"], {}) == sheet.read_bytes()


def test_decisions_downloaded_from_the_page_are_accepted_by_review_job(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    data = _embedded((tmp_path / "view" / "index.html").read_text(encoding="utf-8"))
    page2 = read_record(job / "job.json")["pages"][1]
    ocr = next(a["attempt_id"] for a in page2["attempts"] if a["stage"] == "local_ocr")
    decisions = {
        "p0001": {"decision": "accept", "note": 'Checked, "twice", against\nthe scan'},
        "p0002": {"decision": f"use:{ocr}", "note": ""},
    }
    sheet = tmp_path / "view" / "review.csv"
    sheet.write_bytes(decisions_csv(data["rows"], decisions))

    result = review_job(job, sheet, reviewer="A. Reader")

    assert result["decisions"] == {"reviewed_text": 2}
    pages = read_record(job / "job.json")["pages"]
    assert pages[0]["review"]["decisions"][0]["reason"] == 'Checked, "twice", against\nthe scan'
    assert pages[1]["selected_attempt"] == ocr
    assert verify_job(job)["status"] == "pass"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_pages_javascript_builds_the_same_sheet(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    data = _embedded((tmp_path / "view" / "index.html").read_text(encoding="utf-8"))
    decisions = {"p0001": {"decision": "blank", "note": 'a "note", with commas\nand lines'}}
    program = (
        CSV_JS
        + "const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        + "process.stdout.write(Buffer.from(buildCsv(input.fields, input.rows, input.decisions)));"
    )
    built = subprocess.run(
        ["node", "-e", program],
        input=json.dumps({**data, "decisions": decisions}).encode(),
        capture_output=True,
        check=True,
    ).stdout
    assert built == decisions_csv(data["rows"], decisions)
    sheet = tmp_path / "view" / "review.csv"
    sheet.write_bytes(built)
    assert review_job(job, sheet, reviewer="A. Reader")["decisions"] == {"reviewed_blank": 1}


def test_the_decision_choices_are_review_jobs_own(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    page = document[document.index('id="p0002"') :]
    values = re.findall(r'<option value="([^"]*)"', page[: page.index("</select>")])
    record = read_record(job / "job.json")["pages"][1]
    others = [
        f"use:{a['attempt_id']}"
        for a in record["attempts"]
        if a["attempt_id"] != record["selected_attempt"] and a["outcome"] == "completed"
    ]
    assert others
    assert values == [
        "",
        "accept",
        *others,
        "blank",
        "illustration",
        "handwriting",
        "unreadable",
        "source_defect",
    ]


def test_other_readings_show_their_differences_from_the_selected_text(job, tmp_path):
    write_review_html(job, tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    page = document[document.index('id="p0002"') :]
    assert "<details><summary>" in page
    assert "<del>" in page and "<ins>" in page


def test_a_word_diff_keeps_every_character_of_the_selected_text():
    selected = "one  two\tthree\nfour <five>"
    other = "zero one too three four <five> six"
    shown = diff_html(selected, other)
    assert "<del>two</del><ins>too</ins>" in shown
    assert shown.startswith("<ins>zero</ins>")
    assert shown.endswith("<ins>six</ins>")
    kept = re.sub(r"<ins>.*?</ins>", "", shown)
    assert html.unescape(re.sub(r"</?del>", "", kept)) == selected


def test_page_images_follow_the_option(job, tmp_path, monkeypatch):
    rendered = []

    def render(source, page_number, target):
        rendered.append(page_number)
        target.write_bytes(b"jpeg")
        return True

    monkeypatch.setattr(review_html, "render_page_image", render)
    result = write_review_html(job, tmp_path / "held", images="held")
    assert rendered == [2] and result["images"] == 1
    document = (tmp_path / "held" / "index.html").read_text(encoding="utf-8")
    assert '<img src="images/p0002.jpg"' in document
    assert '<img src="images/p0001.jpg"' not in document
    assert (tmp_path / "held" / "images" / "p0002.jpg").read_bytes() == b"jpeg"

    rendered.clear()
    write_review_html(job, tmp_path / "all", images="all")
    assert rendered == [1, 2]


def test_a_text_source_has_no_page_images(job, tmp_path):
    result = write_review_html(job, tmp_path / "view")
    assert result["images"] == 0
    assert not (tmp_path / "view" / "images").exists()


def test_rendering_a_pdf_page_needs_poppler(tmp_path, monkeypatch):
    assert render_page_image(tmp_path / "page.txt", 1, tmp_path / "x.jpg") is False
    monkeypatch.setattr(review_html.shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="--images none"):
        render_page_image(tmp_path / "page.pdf", 1, tmp_path / "x.jpg")


def test_the_view_refuses_an_existing_folder_and_a_job_that_does_not_verify(job, tmp_path):
    (tmp_path / "taken").mkdir()
    with pytest.raises(ValueError, match="already exists"):
        write_review_html(job, tmp_path / "taken", images="none")
    report = job / "report.md"
    report.write_text(report.read_text() + "edited\n")
    with pytest.raises(ValueError, match="does not verify"):
        write_review_html(job, tmp_path / "view", images="none")
    assert not (tmp_path / "view").exists()


def test_the_command_writes_the_view(job, tmp_path, capsys):
    assert main(["review-html", str(job), "--out", str(tmp_path / "view"), "--images", "none"]) == 0
    assert "Review view:" in capsys.readouterr().out
    assert main(["review-html", str(job), "--out", str(tmp_path / "view"), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_open_contested_spans_are_marked_in_the_text(setup, monkeypatch, tmp_path):  # noqa: F811
    _contesting(setup, monkeypatch)
    assert launch(setup, pages="1")["status"] == "completed"
    page = read_record(setup[2] / "job.json")["pages"][0]
    assert page["disposition"] == "contested"

    write_review_html(setup[2], tmp_path / "view", images="none")
    document = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")

    assert document.count('<mark class="open"') == page["contested"]["open"] == 8
    assert 'data-kind="word"' in document
    assert "Open contested spans (8)" in document
    assert "marked in the text" in document
    assert '<mark class="open" id="p0001-sp_' in document
    assert 'href="#p0001-sp_' in document
    marked = document[document.index("<h3>Selected text</h3>") :]
    assert marked.index('<mark class="open"') < marked.index("</pre>")
