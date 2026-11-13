"""Report format 0.9: report.md for a reader, and the one count of pages that need a person."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml
from jsonschema import validate
from test_document_report import _compared, job_fixture
from test_export import TEI_ALL
from test_hold_policy_09 import KEPT, LAYER, LOST
from test_processing import launch, setup  # noqa: F401
from test_stability import _documented_warning_codes

from pageledger import wording
from pageledger.cli import main
from pageledger.document_report import (
    build_document_report,
    closing_lines,
    needs_person,
    render_document_report,
)
from pageledger.export import export_job
from pageledger.processing import resume_job, verify_job
from pageledger.processing_policy import _HOLD_ORDER, _WARNING_HOLDS, REVIEW_DISPOSITIONS
from pageledger.review_html import write_review_html

ROOT = Path(__file__).resolve().parents[2]
TEI = "{http://www.tei-c.org/ns/1.0}"


# An attempt ID as the job writes it: what a person should not have to read first.
ATTEMPT_ID = re.compile(r"[a-z_]+(?:-\d{4})?:doc_\d{4}_page_\d{4}")


def _report(setup) -> str:  # noqa: F811
    """A job's report.md, checked against its document.json for what must agree."""
    report = (setup[2] / "report.md").read_text(encoding="utf-8")
    document = _document(setup)
    first, record = report.split("\n## For the record", 1)
    # The parts a person reads first name readings by what made them, in words.
    assert not ATTEMPT_ID.search(first)
    assert "Local text" not in first and "Local OCR" not in first
    reasons = {reason for page in document["pages"] for reason in page["review_reasons"]}
    # A comparison is said to count as a disagreement only on a page held for one.
    if "engine_disagreement" not in reasons:
        assert "counted as disagreement" not in report
    # The record keeps every reason, reading, outcome, failure and warning.
    for page in document["pages"]:
        entry = _entry(report, page["page_number"])
        for reason in page["review_reasons"]:
            assert f"`{reason}`" in entry, (page["page_number"], reason)
        for attempt in page["attempts"]:
            assert attempt["attempt_id"] in entry
            assert attempt["outcome"] in entry
            for code in attempt["warnings"]:
                assert f"`{code}`" in entry
            if attempt["failure"]:
                assert f"`{attempt['failure']['code']}`" in entry
        if page["selected_output"] is not None:
            assert f"{page['selected_output']['attempt_id']}" in entry
            assert "selected" in entry
    return report


def _entry(report: str, number: int) -> str:
    """The record's entry for one page: its line and the short lines under it."""
    record = report.split("\n## For the record", 1)[1]
    entry = re.search(rf"\n- Page {number}\b[^\n]*(?:\n  - [^\n]*)*", record)
    assert entry, f"page {number} is not in the record"
    # No line a person reads in the record runs on.
    assert all(len(line) < 300 for line in entry.group().splitlines()), entry.group()
    return entry.group()


def _row(report: str, number: int) -> str:
    """The page table's row for one page."""
    line = re.search(rf"\n\| {number}\b[^\n]*", report.split("\n## For the record", 1)[0])
    assert line, f"page {number} is not in the page table"
    return line.group()


def _document(setup) -> dict:  # noqa: F811
    return json.loads((setup[2] / "document.json").read_text(encoding="utf-8"))


def _opening(report: str) -> list[str]:
    return report.split("\n\n## ", 1)[0].splitlines()


# ---------------------------------------------------------------------------
# The opening: what happened and what to do now
# ---------------------------------------------------------------------------


def test_a_clean_job_says_nothing_waits_and_points_to_export(setup):  # noqa: F811
    setup[3].update(defective=set())
    assert launch(setup, pages="1-2")["status"] == "completed"
    opening = _opening(_report(setup))
    assert opening[:5] == [
        "# source.txt: document job report",
        "",
        "Finished: 2 pages read; none needs a person.",
        "",
        "Source: [source.txt][source]; 2 of its 3 pages selected, 2 read.",
    ]
    assert "Need a person" not in _report(setup)
    assert (
        "- Text selected and nothing held the page; no person has checked it: 2 pages (1-2)."
        in opening
    )
    # A command to run in the folder that holds the report; the report cannot know
    # that folder's name after a move, so the file's name is left to the reader.
    assert opening[-1] == (
        "Next: in this job's folder, `pageledger export . --out ../NAME.md`, with NAME the "
        "folder's name, writes the selected text out beside the job."
    )
    assert _report(setup).endswith("\n[source]: <../source.txt>\n")
    assert "JOB_DIR" not in _report(setup)
    assert "| 1 | nothing | [The engine local_text](<attempts/local_text/" in _report(setup)
    assert verify_job(setup[2])["status"] == "pass"


def test_an_all_held_job_groups_its_pages_by_reason(setup):  # noqa: F811
    setup[3].update(defective={1, 2})
    assert launch(setup, pages="1-2")["status"] == "completed"
    report = _report(setup)
    assert "- Need a person: 2 pages (1-2):" in report
    assert (
        "  - The engine local_text may be missing text; the same is true of the engine local_ocr and the engine image: 2 pages (1-2)."
        in report
    )
    # A text source has no page images, and the next step does not promise them.
    assert (
        "Next: in this job's folder, `pageledger review .` writes the review view, where you "
        "check the 2 pages that need a person and record what you decide." in report
    )
    assert _row(report, 1).startswith(
        "\n| 1 | The engine local_text may be missing text; the same is true of the engine local_ocr and the engine image. |"
    )
    assert "retained review concern" not in report
    assert verify_job(setup[2])["status"] == "pass"


def test_a_halted_job_leads_with_its_reason_and_spends_nothing(tmp_path):
    job = job_fixture(tmp_path)
    job.update(
        status="halted",
        pages=[],
        selected_pages=[],
        next_action="book.pdf uses an encryption handler that pypdf cannot read. Use an "
        "unencrypted copy. Then start a new job.",
    )
    job["source"].update(page_count=None, annotations={"status": "unknown", "count": None})
    job["usage"].update(attempt_pages=0, cost_usd=0, cost_known=True, tokens=0)
    rendered = render_document_report(build_document_report(job, tmp_path))

    assert rendered.splitlines()[2] == (
        "Halted. book.pdf uses an encryption handler that pypdf cannot read. "
        "Use an unencrypted copy. Then start a new job."
    )
    assert "- No page was read." in rendered
    assert "- Cost: nothing, since no page was read." in rendered
    assert "$0" not in rendered


def test_unknown_cost_stays_unknown(tmp_path):
    job = job_fixture(tmp_path)
    job["usage"].update(tokens=12, tokens_known=False, known_cost_usd=0.5)
    rendered = render_document_report(build_document_report(job, tmp_path))
    assert "- Tokens: unknown: not every engine reported them (reported: 12)." in rendered
    assert "- Cost: unknown: not every engine reported a cost (reported: $0.5)." in rendered


def test_a_paused_job_names_the_limit_and_the_pages_left(setup):  # noqa: F811
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["limits"]["max_image_pages"] = 1
    setup[1].write_text(yaml.safe_dump(data))
    assert launch(setup)["status"] == "paused_budget"
    report = _report(setup)
    assert report.splitlines()[2] == (
        "Paused at a limit. Reached max_image_pages (1). Review the retained results, or "
        "continue with: pageledger resume . --raise-limit max_image_pages=N"
    )
    assert "- Not finished: waiting for the engine image: 1 page (3)." in report
    assert "| 3 | Not finished: waiting for the engine image. Held so far:" in report
    # A page still waiting for a stage is not counted as needing a person.
    document = _document(setup)
    assert [needs_person(page, document) for page in document["pages"]] == [False, True, False]
    assert "Next, for the pages read so far: in this job's folder, `pageledger review .`" in report
    assert verify_job(setup[2])["status"] == "pass"


def test_an_interrupted_job_is_unfinished_not_corrupt(setup, capsys):  # noqa: F811
    setup[3]["failure"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        launch(setup)
    report = _report(setup)
    assert report.splitlines()[2] == (
        "Not finished: 3 pages read so far; none needs a person. The job was interrupted, or "
        "is still running."
    )
    assert "- Not finished: waiting for the engine image: 2 pages (2-3)." in report
    assert "Need a person" not in report
    assert "Next: in this job's folder, `pageledger resume .` finishes the job." in report

    result = verify_job(setup[2])
    assert result["status"] == "fail" and result["reason"] == "unfinished"
    assert "did not finish" in result["error"] and "pageledger resume" in result["error"]
    assert main(["verify", str(setup[2])]) == 1
    out = capsys.readouterr()
    assert out.out.startswith("Status: fail (unfinished)\n")
    assert "Run `pageledger resume" in out.err

    # A request that may have been sent is not repeated; the job halts, finished, and verifies.
    assert resume_job(setup[2])["status"] == "halted"
    assert verify_job(setup[2])["status"] == "pass"
    assert _report(setup).splitlines()[2].startswith("Halted.")


def test_process_ends_with_where_the_job_is_and_one_next_command(setup, capsys):  # noqa: F811
    source, config, out, shared = setup
    shared.update(defective={2, 3})
    assert main(["process", str(source), "--config", str(config), "--out", str(out)]) == 0
    printed = capsys.readouterr().out.splitlines()
    closing = [line for line in printed if not line.startswith("Config warning")]
    assert closing == [
        "Finished: 3 pages read; 2 need a person.",
        f"The job is in {out}.",
        "Why they need a person:",
        "  2 pages: the engine local_text may be missing text; the same is true of the engine "
        "local_ocr and the engine image.",
        f"Next: pageledger review {out} shows those pages for checking.",
        f"Text out: pageledger export {out} --out job.md",
    ]
    assert not any(
        "`" in line or "inspect" in line or "coverage_defect" in line for line in closing
    )


def test_closing_lines_follow_the_job_status(tmp_path):
    job = job_fixture(tmp_path)
    job["pages"] = job["pages"][:1]
    job["selected_pages"] = [1]
    report = build_document_report(job, tmp_path)
    assert closing_lines(report, Path("jobs/my book")) == [
        "Finished: 1 page read; none needs a person.",
        "The job is in 'jobs/my book'.",
        "Text out: pageledger export 'jobs/my book' --out 'my book.md'",
    ]
    job.update(status="processing")
    report = build_document_report(job, tmp_path)
    assert closing_lines(report, "jobs/a")[-1] == "Next: pageledger resume jobs/a finishes the job."
    job.update(
        status="paused_budget",
        next_action="Reached max_image_pages (1). Review the retained results, or continue "
        "with: pageledger resume JOB_DIR --raise-limit max_image_pages=N",
    )
    report = build_document_report(job, tmp_path)
    lines = closing_lines(report, "jobs/a")
    assert lines[:2] == [
        "Paused at a limit: 1 page read; none needs a person so far.",
        "Reached max_image_pages (1). Review the retained results, or continue with: "
        "pageledger resume jobs/a --raise-limit max_image_pages=N",
    ]


# ---------------------------------------------------------------------------
# Holds and comparisons, said truthfully
# ---------------------------------------------------------------------------


def test_the_kiev_page_names_the_lost_letters_and_what_was_compared(setup):  # noqa: F811
    setup[3].update(
        defective=set(),
        texts={"local_text": KEPT, "local_ocr": LOST},
        warn={"local_text": LAYER},
    )
    assert launch(setup, pages="1")["status"] == "completed"
    report = _report(setup)
    entry = _entry(report, 1)
    assert (
        "  - Hold: `coverage_defect` from `letters_lost` on `local_ocr-0001:doc_0001_page_0001`: "
        "the engine local_ocr lost the old-spelling letters (such as ѣ) that a different reading "
        "kept (0 words spelled with ѣ, against 16 in another reading)." in entry
    )
    assert (
        "  - Compared `local_text:doc_0001_page_0001` and `local_ocr-0001:doc_0001_page_0001`: "
        "50% of words agree; not counted, because `local_ocr-0001:doc_0001_page_0001` is held "
        "itself." in entry
    )
    assert "- On 1 page (1) (`historical_orthography`):" in report
    assert "the community `orus` model" in report and "made more errors overall" in report
    assert "retained review concern" not in report and "not compared" not in report
    document = _document(setup)
    [cause] = document["pages"][0]["hold_causes"]
    assert cause == {
        "attempt_id": "local_ocr-0001:doc_0001_page_0001",
        "hold": "coverage_defect",
        "cause": "letters_lost",
        "yat_words": 0,
        "most_yat_words": 16,
    }
    validate(document, json.loads((ROOT / "schemas/document.schema.json").read_text()))
    assert verify_job(setup[2])["status"] == "pass"


def test_agreement_on_a_held_page_is_not_presented_as_reassurance(tmp_path):
    job = job_fixture(tmp_path)
    page = job["pages"][0]
    _compared(page, 0.91)
    page["attempts"][0]["warnings"] = ["low_confidence"]
    page.update(disposition="low_confidence", review_reasons=["low_confidence"])
    job["hold_policy"] = "0.9"
    rendered = render_document_report(build_document_report(job, tmp_path))
    assert "| 1 | The page's own text was unsure of many words. |" in _row(rendered, 1)
    assert "`a1` and `clean`: 91% of words agree; counted as agreement." in _entry(rendered, 1)
    assert "agree (" not in rendered


def _scan_page(tmp_path, reasons):
    import hashlib

    job = job_fixture(tmp_path, text="")
    page = job["pages"][0]
    page["attempts"][0]["warnings"] = ["empty_text"]
    ocr = tmp_path / "attempts" / "local_ocr" / "raw" / "p1.md"
    ocr.parent.mkdir(parents=True)
    ocr.write_text("Twelve words of OCR text read from the scanned page here today.\n")
    page["attempts"].append(
        {
            **page["attempts"][0],
            "attempt_id": "a2",
            "stage": "local_ocr",
            "raw_artifact": str(ocr.relative_to(tmp_path)),
            "raw_sha256": hashlib.sha256(ocr.read_bytes()).hexdigest(),
            "warnings": ["low_confidence"] if "low_confidence" in reasons else [],
        }
    )
    page.update(selected_attempt="a2", disposition=reasons[0], review_reasons=reasons)
    job["hold_policy"] = "0.9"
    job["policy"] = {
        "local_text": {"adapter": "pdf_text"},
        "local_ocr": {"adapter": "pdf_ocr", "adapter_options": {"lang": "rus"}},
    }
    return job


def test_a_scan_with_ocr_text_held_for_another_reason_is_not_called_blank(tmp_path):
    job = _scan_page(tmp_path, ["low_confidence", "blank_candidate"])
    rendered = render_document_report(build_document_report(job, tmp_path))
    first = rendered.split("\n## For the record", 1)[0]
    assert "blank" not in first.lower()
    assert "| 1 | Tesseract OCR (rus) was unsure of many words. | [Tesseract OCR (rus)](<" in first
    # The record still lists every reason the job recorded.
    assert "held for `low_confidence`, `blank_candidate`" in _entry(rendered, 1)


def test_a_scan_held_only_as_blank_says_it_is_a_scan(tmp_path):
    job = _scan_page(tmp_path, ["blank_candidate"])
    rendered = render_document_report(build_document_report(job, tmp_path))
    assert f"  - {wording.BLANK_SCAN}: 1 page (1)." in rendered
    assert f"| 1 | {wording.BLANK_SCAN}. |" in rendered


def test_readings_are_named_by_what_made_them():
    name = wording.reading_name
    assert name("local_text", {"adapter": "pdf_text"}) == "the PDF's own text"
    assert name("local_text", {"adapter": "pdf_text"}, layer=True) == (
        "the PDF's own text (an earlier OCR)"
    )
    assert name("local_text", {"adapter": "pdf_text"}, images=True) == "the page image's own text"
    assert name("local_text", {"adapter": "text"}) == "the text file"
    assert name("local_ocr", {"adapter": "pdf_ocr", "lang": "rus"}) == "Tesseract OCR (rus)"
    assert name("second_ocr", {"adapter": "rapidocr"}) == "the second OCR engine (RapidOCR)"
    assert name("local_ocr", {"adapter": "indra"}) == "Indra OCR"
    assert name("second_ocr", {"adapter": "alto"}) == (
        "the second OCR engine (the page's ALTO file)"
    )
    assert name("image", {"adapter": "vision", "model": "qwen2.5-vl"}) == (
        "the vision model qwen2.5-vl"
    )
    assert name("local_image", {"adapter": "vision", "model": "m"}) == "the local vision model m"
    assert name("second_opinion", {"adapter": "vision", "model": "m"}) == (
        "the second opinion (the vision model m)"
    )
    assert name("local_ocr", {"adapter": "mine.ocr:Engine"}) == "the engine mine.ocr:Engine"
    # A report that records no engines still names the stage in words.
    assert name("local_ocr") == "the OCR engine"
    assert name("local_text") == "the page's own text"


def test_the_report_records_only_what_names_an_engine(tmp_path):
    job = job_fixture(tmp_path)
    job["policy"] = {
        "local_text": {"adapter": "pdf_text", "adapter_options": {}},
        "local_ocr": {"adapter": "pdf_ocr", "adapter_options": {"dpi": 400}},
        "image": {
            "adapter": "vision",
            "adapter_options": {"model": "m", "base_url": "http://x", "env_key": "KEY"},
        },
        "second_opinion": None,
    }
    report = build_document_report(job, tmp_path)
    assert report["engines"] == {
        "local_text": {"adapter": "pdf_text"},
        "local_ocr": {"adapter": "pdf_ocr", "lang": "eng"},
        "image": {"adapter": "vision", "model": "m"},
    }
    validate(
        json.loads(json.dumps(report)),
        json.loads((ROOT / "schemas/document.schema.json").read_text()),
    )
    assert "engines" not in build_document_report(job, tmp_path, report_format="0.6")


# ---------------------------------------------------------------------------
# One count of pages that need a person
# ---------------------------------------------------------------------------


def test_the_report_process_inspect_and_the_view_count_alike(setup, tmp_path, capsys):  # noqa: F811
    source, config, out, shared = setup
    shared.update(defective={2})
    assert main(["process", str(source), "--config", str(config), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    document = _document(setup)
    needing = [page["page_number"] for page in document["pages"] if needs_person(page, document)]
    assert needing == [2]
    assert "Finished: 3 pages read; 1 needs a person." in printed.splitlines()
    line = "- Need a person: 1 page (2):"
    assert line in _report(setup).splitlines()
    assert main(["inspect", str(out)]) == 0
    assert line in capsys.readouterr().out.splitlines()
    write_review_html(out, tmp_path / "view", images="none")
    view = (tmp_path / "view" / "index.html").read_text(encoding="utf-8")
    assert "; 1 need a person;" in view


# ---------------------------------------------------------------------------
# Exports carry the hold
# ---------------------------------------------------------------------------


def test_tei_and_jsonl_carry_the_hold_and_the_warnings(setup, tmp_path):  # noqa: F811
    setup[3].update(defective={2})
    launch(setup, pages="1-2")
    export_job(setup[2], tmp_path / "book.jsonl", format="jsonl")
    records = [json.loads(line) for line in (tmp_path / "book.jsonl").read_text().splitlines()]
    assert [r["state"] for r in records] == ["unreviewed", "needs_person"]
    assert records[1]["review_reasons"] == ["coverage_defect"]
    assert records[1]["warnings"] == ["coverage_defect"]
    schema = json.loads((ROOT / "schemas" / "export-page.schema.json").read_text())
    for record in records:
        validate(record, schema)

    export_job(setup[2], tmp_path / "book.tei", format="tei")
    tei = ET.fromstring((tmp_path / "book.tei").read_bytes())
    pages = tei.findall(f".//{TEI}div[@type='page']")
    assert [div.get("subtype") for div in pages] == ["unreviewed", "unreviewed"]
    assert [div.get("ana") for div in pages] == [
        "#pageledger-unreviewed",
        "#pageledger-needs-person",
    ]
    [hold] = pages[1].findall(f"{TEI}note[@type='hold']")
    assert hold.get("n") == "coverage_defect"
    assert hold.text == "Text may be missing or damaged in the reading."
    assert not pages[0].findall(f"{TEI}note")
    categories = tei.findall(f".//{TEI}classDecl/{TEI}taxonomy/{TEI}category")
    assert len(categories) == 4
    xmllint = shutil.which("xmllint")
    if xmllint:
        checked = subprocess.run(
            [xmllint, "--noout", "--relaxng", str(TEI_ALL), str(tmp_path / "book.tei")],
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 0, checked.stderr


# ---------------------------------------------------------------------------
# The wording table
# ---------------------------------------------------------------------------

# Dispositions assess_page gives a page that is not held.
_STATES = {"unreviewed_text", "adjudicated_text", "provider_failure", "outcome_unknown", "pending"}


def test_every_code_has_plain_words():
    documented = set(_documented_warning_codes())
    frozen = json.loads((ROOT / "tests/fixtures/stability/taxonomies.json").read_text())["sets"]
    assert set(frozen["warning_codes"]) <= set(wording.WARNINGS)
    assert set(wording.WARNINGS) == documented
    assert set(_HOLD_ORDER) | _STATES <= set(wording.HOLDS)
    assert set(frozen["hold_reasons"]) <= set(wording.HOLDS)
    assert set(REVIEW_DISPOSITIONS) == set(wording.REVIEWED)
    holds_nothing = documented - set(_WARNING_HOLDS) - set(_HOLD_ORDER)
    assert set(wording.ADVICE) == holds_nothing


def _table(section: str) -> dict[str, list[str]]:
    rows = {}
    for line in section.splitlines():
        if line.startswith("| `"):
            code, *cells = (cell.strip() for cell in line.strip().strip("|").split("|"))
            rows[code.strip("`")] = cells
    return rows


def test_the_docs_repeat_the_wording_table_exactly():
    text = (ROOT / "docs" / "warnings.md").read_text(encoding="utf-8")
    section = text.split("\n## In plain words\n", 1)[1]
    holds, rest = section.split("### Review decisions", 1)
    decisions, warnings = rest.split("### Warnings", 1)
    warnings, causes = warnings.split("### What is wrong with a reading", 1)
    assert {k: v[0] for k, v in _table(holds).items()} == wording.HOLDS
    assert " ".join(wording.BLANK_WITH_TEXT.split()) in " ".join(holds.split())
    assert " ".join(wording.BLANK_SCAN.split()) in " ".join(holds.split())
    assert {k: v[0] for k, v in _table(decisions).items()} == wording.REVIEWED
    table = _table(warnings)
    assert {k: v[0] for k, v in table.items()} == wording.WARNINGS
    assert {k: v[1] for k, v in table.items() if v[1]} == wording.ADVICE
    assert wording.UNKNOWN_WARNING in warnings
    assert {k: v[0] for k, v in _table(causes).items()} == wording.CAUSES


# A finite verb, so that a clause reads as a sentence after a reading's name.
_VERBS = {"added", "could", "disagrees", "found", "had", "has", "holds", "is", "judged"}
_VERBS |= {"lacks", "lost", "may", "repeats", "says", "uses", "was", "wrote", "does"}


def test_every_cause_of_a_hold_reads_after_a_reading_s_name():
    causes = {*_WARNING_HOLDS, "empty_reading", "alignment"}
    causes |= {"letters_lost", "layer_outvoted", "reader_short"}
    assert causes <= set(wording.CAUSES)
    name = wording.reading_name("local_ocr", {"adapter": "pdf_ocr", "lang": "rus"})
    for code, clause in wording.CAUSES.items():
        assert clause[:1].islower() and not clause.endswith("."), code
        assert clause.split()[0] in _VERBS, code
        assert f"{name} {clause}".startswith("Tesseract OCR (rus) ")


# ---------------------------------------------------------------------------
# Nothing the 0.6 format recorded is dropped
# ---------------------------------------------------------------------------


def test_the_new_format_keeps_every_link_hash_and_fact_of_the_old(tmp_path):
    job = job_fixture(tmp_path)
    page = job["pages"][0]
    _compared(page, 0.42)
    page["review_reasons"] = ["engine_disagreement"]
    page["attempts"][0]["warnings"] = ["multi_column_rows"]
    partial = tmp_path / "attempts" / "partial.md"
    partial.write_bytes(b"Partial response")
    import hashlib

    page["attempts"].append(
        {
            **page["attempts"][0],
            "attempt_id": "a2",
            "stage": "image",
            "outcome": "failed",
            "raw_artifact": "attempts/partial.md",
            "raw_sha256": hashlib.sha256(partial.read_bytes()).hexdigest(),
            "failure": {"code": "MODEL_OUTPUT_TRUNCATED"},
            "tiling": {"tiles": 4, "sent": 3, "failed": ["t02"], "edge_cut": ["t03"]},
        }
    )
    job["policy"] = {"local_text": {"adapter": "text"}, "second_opinion": {"adapter": "vision"}}
    job["links"]["article"] = "https://example.org/article"
    job["relocations"] = [{"command": "review-job", "at": "2026-11-10T00:00:00Z", "root": "/x"}]
    job["limits"] = {"max_image_pages": 2}
    old = render_document_report(build_document_report(job, tmp_path, report_format="0.6"))
    new = render_document_report(build_document_report(job, tmp_path))

    source = build_document_report(job, tmp_path)["pages"][0]["source_link"].split("#")[0]
    for target in re.findall(r"\(<([^>]*)>\)", old):
        if target.startswith(source):
            # The source is linked once, by reference; each page's number is its page in it.
            assert f"\n[source]: <{source}>\n" in new and "\n- Page 1: " in new
        else:
            assert f"(<{target}>)" in new, target
    transcript = build_document_report(job, tmp_path, report_format="0.6")["transcript"]
    for digest in re.findall(r"`([0-9a-f]{64})`", old):
        # The transcript names its readings in words from format 0.9, so its hash differs.
        assert digest in new or digest == transcript["sha256"]
    for fact in (
        "MODEL_OUTPUT_TRUNCATED",
        "multi_column_rows",
        "| 0 (no page met its trigger)",
        "none (0)" if False else "present (2)",
        "Changed elsewhere: review --apply on 2026-11-10T00:00:00Z in /x",
        "unverified",
        "not_assessed",
        "Review source pages before relying on extracted text.",
        "42%",
    ):
        assert fact in old and fact in new, fact
    assert "outcome failed" in old
    assert (
        "[a2](<attempts/partial.md>) failed, failure `MODEL_OUTPUT_TRUNCATED`, "
        "`multi_column_rows`, tiles: 4 planned, 3 sent and paid for, 2 read, failed: t02, "
        "cut through text: t03" in new
    )


# ---------------------------------------------------------------------------
# What the job weighed, and the review view of older reports
# ---------------------------------------------------------------------------


def test_a_layer_outvoted_by_two_engines_is_not_shown_as_a_disagreement(setup):  # noqa: F811
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["second_ocr"] = {"adapter": "second_ocr"}
    setup[1].write_text(yaml.safe_dump(data))
    layer = "Unrelated layer words that the scan never printed anywhere at all. " * 8
    setup[3].update(
        defective=set(), texts={"local_text": layer}, warn={"local_text": ["ocr_text_layer"]}
    )
    assert launch(setup, pages="1")["status"] == "completed"
    page = _document(setup)["pages"][0]
    assert page["review_reasons"] == []
    assert min(c["agreement_ratio"] for c in page["comparisons"]) < 0.6
    report = _report(setup)
    assert "| 1 | nothing |" in _row(report, 1)
    entry = _entry(report, 1)
    assert "100% of words agree; counted as agreement" in entry
    assert "not counted: two of the job's own engines agree, which outweighs the layer" in entry
    assert "below" not in entry


def test_the_review_view_of_an_older_report_sets_held_readings_aside(tmp_path):
    from pageledger.document_report import _short_comparison

    job = job_fixture(tmp_path)
    page = job["pages"][0]
    _compared(page, 0.46)
    for attempt in page["attempts"]:
        attempt["warnings"] = ["low_confidence"]
    page.update(disposition="low_confidence", review_reasons=["low_confidence"])
    job["hold_policy"] = "0.8"
    report = build_document_report(job, tmp_path, report_format="0.6")
    assert "hold_causes" not in report["pages"][0]
    assert _short_comparison(report["pages"][0], report) == "none counted"


def test_a_reviewed_page_keeps_the_holds_its_review_settled(setup, tmp_path):  # noqa: F811
    from pageledger.processing import review_job

    setup[3].update(defective={2})
    launch(setup, pages="1-2")
    page = _document(setup)["pages"][1]
    receipt = tmp_path / "review.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "source_sha256": page["source_sha256"],
                "decisions": [
                    {
                        "page_id": page["page_id"],
                        "page_number": 2,
                        "disposition": "reviewed_text",
                        "selected_attempt": page["selected_output"]["attempt_id"],
                        "output_sha256": page["selected_output"]["sha256"],
                        "reason": "checked against the scan",
                        "reviewer": "A. Reader",
                        "reviewed_at": "2026-11-10T12:00:00Z",
                    }
                ],
            }
        )
    )
    review_job(setup[2], receipt)
    report = _report(setup)
    row = _row(report, 2)
    assert (
        "| The engine local_text may be missing text; the same is true of the engine local_ocr and the engine image. |"
        in row
    )
    assert (
        "| A person checked the text against the page: A. Reader, 2026-11-10T12:00:00Z, "
        "“checked against the scan” |" in row
    )
    assert "`reviewed_text`; held for `coverage_defect`; reviewed as `reviewed_text`" in (
        _entry(report, 2)
    )
    assert "- A person checked the text against the page: 1 page (2)." in report
    assert "Check the pages against the source, those that need a person first" in report
    assert verify_job(setup[2])["status"] == "pass"


def test_pages_left_by_a_paused_job_show_their_text_so_far(setup):  # noqa: F811
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["limits"]["max_image_pages"] = 1
    setup[1].write_text(yaml.safe_dump(data))
    launch(setup)
    row = _row(_report(setup), 3)
    assert (
        "| 3 | Not finished: waiting for the engine image. Held so far: The engine local_text "
        "may be missing text; the same is true of the engine local_ocr. | [The engine local_text](<"
        in row
    )
    assert "Waiting for the `image` stage." in _entry(_report(setup), 3)
    assert "For pages not finished, the text and holds are those so far" in _report(setup)


def test_a_blank_candidate_with_text_is_grouped_and_worded_apart(tmp_path):
    job = job_fixture(tmp_path)
    page = job["pages"][0]
    page["attempts"][0].update(stage="local_ocr", warnings=["empty_text", "low_confidence"])
    page.update(disposition="blank_candidate", review_reasons=["blank_candidate"])
    rendered = render_document_report(build_document_report(job, tmp_path))
    assert f"  - {wording.BLANK_WITH_TEXT}: 1 page (1)." in rendered
    assert "the page may be blank" not in rendered


def _causes_page() -> tuple[dict, dict]:
    report = {
        "engines": {
            "local_text": {"adapter": "pdf_text"},
            "local_ocr": {"adapter": "pdf_ocr", "lang": "eng"},
        },
        "source": {},
        "hold_policy": "0.9",
    }
    attempt = {"outcome": "completed", "raw_artifact": "r", "raw_sha256": "s"}
    page = {
        "review_reasons": ["coverage_defect"],
        "disposition": "coverage_defect",
        "selected_attempt": "ocr",
        "attempts": [
            {**attempt, "attempt_id": "layer", "stage": "local_text"},
            {**attempt, "attempt_id": "ocr", "stage": "local_ocr"},
        ],
        "selected_output": {"text": "Бѣлоконскій"},
        "hold_causes": [{"attempt_id": "ocr", "hold": "coverage_defect", "cause": "letters_lost"}],
    }
    return report, page


def test_a_hold_is_said_by_its_cause_and_the_reading_it_is_on():
    from pageledger.document_report import _group_keys, held_for, held_in_words

    report, page = _causes_page()
    assert held_for(report, page) == [
        ("coverage_defect", f"Tesseract OCR (eng) {wording.CAUSES['letters_lost']}")
    ]
    # Several causes: the selected reading's first; one on another reading says which
    # text was selected. Where a person reads first, two at most.
    page["hold_causes"].insert(
        0, {"attempt_id": "layer", "hold": "coverage_defect", "cause": "fragmented_text"}
    )
    page["review_reasons"].append("low_confidence")
    page["hold_causes"].append({"attempt_id": "ocr", "hold": "low_confidence", "cause": "x"})
    page["hold_causes"].append(
        {"attempt_id": "ocr", "hold": "low_confidence", "cause": "low_confidence"}
    )
    # Two flaws of one reading make one sentence, whatever holds they raised.
    assert [text for _hold, text in held_for(report, page)] == [
        f"Tesseract OCR (eng) {wording.CAUSES['letters_lost']} and was unsure of many words",
        "the selected text is Tesseract OCR (eng); the PDF's own text, another reading of the "
        f"page, {wording.CAUSES['fragmented_text']}, so it cannot confirm the selected text",
    ]
    page["review_reasons"].append("engine_disagreement")
    assert len(held_for(report, page)) == 3
    assert len(_group_keys(report, page)) == 2
    assert held_in_words(report, page, more="below").endswith(" 1 more cause below.")
    assert "unsure" in held_in_words(report, page, every=True)
    # Without recorded causes, the hold's own words, as before.
    page["review_reasons"].remove("engine_disagreement")
    del page["hold_causes"]
    assert [text for _hold, text in held_for(report, page)] == [
        "text may be missing or damaged in the reading",
        "the OCR engine was unsure of many words",
    ]


def test_an_earlier_ocr_s_flaw_says_nothing_confirms_the_selected_text():
    from pageledger.document_report import held_for

    report, page = _causes_page()
    page["attempts"][0]["warnings"] = ["ocr_text_layer", "joined_text"]
    page["hold_causes"] = [
        {"attempt_id": "layer", "hold": "coverage_defect", "cause": "joined_text"}
    ]
    assert held_for(report, page) == [
        (
            "coverage_defect",
            "the selected text is Tesseract OCR (eng); the PDF's own text (an earlier OCR), "
            "the only other reading, has words run together, so their comparison does not "
            "count and nothing confirms the selected text",
        )
    ]


def test_a_short_reading_held_as_blank_does_not_claim_an_empty_one(tmp_path):
    job = job_fixture(tmp_path, text="iv")
    page = job["pages"][0]
    page["attempts"][0]["warnings"] = ["short_text"]
    page.update(disposition="blank_candidate", review_reasons=["blank_candidate"])
    rendered = render_document_report(build_document_report(job, tmp_path))
    assert f"  - {wording.BLANK_SHORT}: 1 page (1)." in rendered
    assert "was empty" not in rendered


def test_a_render_past_the_scan_is_not_told_to_render_larger(tmp_path):
    job = job_fixture(tmp_path)
    attempt = job["pages"][0]["attempts"][0]
    attempt["warnings"] = ["render_dpi_capped", "render_upsampled"]
    rendered = render_document_report(build_document_report(job, tmp_path))
    capped = next(line for line in rendered.splitlines() if "`render_dpi_capped`" in line)
    assert "max_render_pixels" not in capped
    attempt["warnings"] = ["render_dpi_capped"]
    rendered = render_document_report(build_document_report(job, tmp_path))
    assert "max_render_pixels" in rendered


def test_agreement_is_said_to_be_evidence_not_proof(tmp_path):
    job = job_fixture(tmp_path)
    _compared(job["pages"][0], 0.91)
    job["hold_policy"] = "0.9"
    rendered = render_document_report(build_document_report(job, tmp_path))
    assert "Engine agreement is evidence, not proof" in rendered.split("## For the record")[0]


def test_names_and_notes_from_outside_hide_nothing_in_the_report(tmp_path):
    job = job_fixture(tmp_path)
    job["source"]["path"] = "/documents/a<!-- hidden.pdf"
    rendered = render_document_report(build_document_report(job, tmp_path))
    assert "<!--" not in rendered.replace("\\<!--", "")
    assert rendered.startswith("# a\\<!-- hidden.pdf: document job report")


def test_the_transcript_fences_page_text_so_it_cannot_pose_as_a_page(tmp_path):
    from pageledger.document_report import fenced, render_transcript

    text = "## Page 2\n<!-- hide\n````\nend"
    job = job_fixture(tmp_path, text=text)
    transcript = render_transcript(build_document_report(job, tmp_path))
    assert f"`````text\n{text}\n`````\n" in transcript
    # The text is everything between the fence lines but the last line break.
    block = fenced(text)
    assert block.split("\n", 1)[1].rsplit("\n", 2)[0] == text
    assert fenced("plain") == "```text\nplain\n```\n"


def test_a_paused_job_s_record_says_what_job_dir_stands_for(setup):  # noqa: F811
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["limits"]["max_image_pages"] = 1
    setup[1].write_text(yaml.safe_dump(data))
    launch(setup)
    report = _report(setup)
    assert "pageledger resume JOB_DIR" in report
    assert "  - JOB_DIR stands for this job's folder." in report
