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
    needs_person,
    render_document_report,
    summary_lines,
)
from pageledger.export import export_job
from pageledger.processing import resume_job, verify_job
from pageledger.processing_policy import _HOLD_ORDER, _WARNING_HOLDS, REVIEW_DISPOSITIONS
from pageledger.review_html import write_review_html

ROOT = Path(__file__).resolve().parents[2]
TEI = "{http://www.tei-c.org/ns/1.0}"


def _report(setup) -> str:  # noqa: F811
    """A job's report.md, checked against its document.json for what must agree."""
    report = (setup[2] / "report.md").read_text(encoding="utf-8")
    document = _document(setup)
    reasons = {reason for page in document["pages"] for reason in page["review_reasons"]}
    # A comparison is said to count as a disagreement only on a page held for one.
    if "engine_disagreement" not in reasons:
        assert "counted as disagreement" not in report
    # Every hold on a job's page names its evidence.
    assert "no single reading carries it" not in report
    for page in document["pages"]:
        entry = _entry(report, page["page_number"])
        for reason in page["review_reasons"]:
            assert f"(`{reason}`)" in entry, (page["page_number"], reason)
        if page["selected_output"] is not None:
            assert page["selected_output"]["attempt_id"] in entry
    return report


def _entry(report: str, number: int) -> str:
    """What the report says about one page: its section, its table row or its line."""
    body = report.split("\n## For the record", 1)[0]
    section = re.search(rf"\n### Page \[{number}\b.*?(?=\n### |\n## )", body, re.S)
    if section:
        return section.group()
    line = re.search(rf"\n(?:\| |- Page )\[{number}\b[^\n]*", body)
    assert line, f"page {number} is not in the report"
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
        "Status: finished. Every stage this job is configured to run has run.",
        "",
        "Source: source.txt; 2 of its 3 pages selected, 2 read.",
    ]
    assert "Need a person" not in _report(setup)
    assert (
        "- Text selected and nothing held the page; no person has checked it "
        "(`unreviewed_text`): 2 pages (1-2)." in opening
    )
    assert (
        "Next: `pageledger export JOB_DIR --format md --out source.md` writes the selected "
        "text out." in opening
    )
    assert opening[-1] == "JOB_DIR stands for the folder that holds this report."
    assert verify_job(setup[2])["status"] == "pass"


def test_an_all_held_job_groups_its_pages_by_reason(setup):  # noqa: F811
    setup[3].update(defective={1, 2})
    assert launch(setup, pages="1-2")["status"] == "completed"
    report = _report(setup)
    assert "- Need a person: 2 pages (1-2):" in report
    assert (
        "  - Text may be missing or damaged in the reading (`coverage_defect`): 2 pages (1-2)."
        in report
    )
    assert "shows the 2 pages that need a person beside the page images." in report
    assert report.count("\n### Page ") == 2
    assert "## Other pages" not in report
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
        "Status: halted. book.pdf uses an encryption handler that pypdf cannot read. "
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
    assert report.splitlines()[2].startswith(
        "Status: paused at a limit. Reached max_image_pages (1)."
    )
    assert "- Not finished: waiting for the Image extraction stage: 1 page (3)." in report
    assert "## Pages not finished" in report
    # A page still waiting for a stage is not counted as needing a person.
    document = _document(setup)
    assert [needs_person(page, document) for page in document["pages"]] == [False, True, False]
    assert "Next, for the pages read so far: `pageledger review JOB_DIR" in report
    assert verify_job(setup[2])["status"] == "pass"


def test_an_interrupted_job_is_unfinished_not_corrupt(setup, capsys):  # noqa: F811
    setup[3]["failure"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        launch(setup)
    report = _report(setup)
    assert report.splitlines()[2].startswith("Status: not finished.")
    assert "- Not finished: waiting for the Image extraction stage: 2 pages (2-3)." in report
    assert "Need a person" not in report
    assert "Next: `pageledger resume JOB_DIR` finishes the job." in report

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
    assert _report(setup).splitlines()[2].startswith("Status: halted.")


def test_summary_lines_name_the_job_folder_in_the_terminal(tmp_path):
    job = job_fixture(tmp_path)
    report = build_document_report(job, tmp_path)
    lines = summary_lines(report, "jobs/book")
    assert any("pageledger export jobs/book --format md" in line for line in lines)
    assert not any("JOB_DIR" in line for line in lines)


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
    assert "spells 0 words with ѣ where another reading of the page spells 16: it lost the " in (
        report
    )
    assert (
        "The Local text (the PDF's own text, an earlier OCR) reading "
        "`local_text:doc_0001_page_0001` and the Local OCR reading "
        "`local_ocr-0001:doc_0001_page_0001`: 50% of words agree; not counted, because the "
        "Local OCR reading `local_ocr-0001:doc_0001_page_0001` is held itself." in report
    )
    assert "- `historical_orthography` on 1 page (1):" in report
    assert "such as `orus` (see docs/engine-recipes.md)" in report
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
    held = rendered.split("### Page", 1)[1].split("\n## ", 1)[0]
    assert "- The OCR engine was unsure of many words (`low_confidence`)." in held
    assert "- Comparisons the job recorded (they do not clear the holds above):" in held
    assert "91% of words agree; counted as agreement." in held
    assert "agree (" not in rendered


def test_a_scan_with_ocr_text_is_not_called_simply_blank(tmp_path):
    job = job_fixture(tmp_path, text="")
    page = job["pages"][0]
    page["attempts"][0]["warnings"] = ["empty_text"]
    ocr = tmp_path / "attempts" / "local_ocr" / "raw" / "p1.md"
    ocr.parent.mkdir(parents=True)
    ocr.write_text("Twelve words of OCR text read from the scanned page here today.\n")
    import hashlib

    page["attempts"].append(
        {
            **page["attempts"][0],
            "attempt_id": "a2",
            "stage": "local_ocr",
            "raw_artifact": str(ocr.relative_to(tmp_path)),
            "raw_sha256": hashlib.sha256(ocr.read_bytes()).hexdigest(),
            "warnings": ["low_confidence"],
        }
    )
    page.update(
        selected_attempt="a2",
        disposition="blank_candidate",
        review_reasons=["low_confidence", "blank_candidate"],
    )
    job["hold_policy"] = "0.9"
    rendered = render_document_report(build_document_report(job, tmp_path))
    assert (
        "came back empty (a scanned PDF page has no text of its own). The selected reading "
        "has 12 words, but no reading by an engine that is not generative, with no hold of "
        "its own, confirms that the page has text." in rendered
    )


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
    line = "- Need a person: 1 page (2):"
    assert line in printed.splitlines()
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
    assert {k: v[0] for k, v in _table(holds).items()} == wording.HOLDS
    assert " ".join(wording.BLANK_WITH_TEXT.split()) in " ".join(holds.split())
    assert {k: v[0] for k, v in _table(decisions).items()} == wording.REVIEWED
    table = _table(warnings)
    assert {k: v[0] for k, v in table.items()} == wording.WARNINGS
    assert {k: v[1] for k, v in table.items() if v[1]} == wording.ADVICE
    assert wording.UNKNOWN_WARNING in warnings


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

    for target in re.findall(r"\(<([^>]*)>\)", old):
        assert f"(<{target}>)" in new, target
    for digest in re.findall(r"`([0-9a-f]{64})`", old):
        assert digest in new
    for fact in (
        "MODEL_OUTPUT_TRUNCATED",
        "outcome failed",
        "multi_column_rows",
        "tiles: 4 planned, 3 sent and paid for, 2 read, failed: t02, cut through text: t03",
        "Second opinion | 0 (no page met its trigger)",
        "none (0)" if False else "present (2)",
        "Changed elsewhere: review-job on 2026-11-10T00:00:00Z in /x",
        "unverified",
        "not_assessed",
        "Review source pages before relying on extracted text.",
        "42%",
    ):
        assert fact in old and fact in new, fact


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
    row = _entry(report, 1)
    assert "| 100% of words agree | nothing |" in row
    assert "below" not in row


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
    row = _entry(report, 2)
    assert "Text may be missing or damaged in the reading (`coverage_defect`)" in row
    assert "A person checked the text against the page (`reviewed_text`), by A. Reader" in row
    assert "- A person checked the text against the page (`reviewed_text`): 1 page (2)." in report
    assert "Check the pages against the source, those that need a person first" in report
    assert verify_job(setup[2])["status"] == "pass"


def test_pages_left_by_a_paused_job_show_their_text_so_far(setup):  # noqa: F811
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["limits"]["max_image_pages"] = 1
    setup[1].write_text(yaml.safe_dump(data))
    launch(setup)
    entry = _entry(_report(setup), 3)
    assert "Text so far: the Local text reading" in entry
    assert "Held so far for: Text may be missing or damaged" in entry
    assert "What is shown for these pages is provisional" in _report(setup)


def test_a_blank_candidate_with_text_is_grouped_and_worded_apart(tmp_path):
    job = job_fixture(tmp_path)
    page = job["pages"][0]
    page["attempts"][0]["warnings"] = ["empty_text", "low_confidence"]
    page.update(disposition="blank_candidate", review_reasons=["blank_candidate"])
    rendered = render_document_report(build_document_report(job, tmp_path))
    assert f"  - {wording.BLANK_WITH_TEXT} (`blank_candidate`): 1 page (1)." in rendered
    assert "the page may be blank" not in rendered
