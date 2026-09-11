"""The report is derived, byte-faithful evidence, including incomplete jobs."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import validate

from pageledger.document_report import (
    build_document_report,
    render_document_report,
    render_transcript,
    verify_document_report,
    write_document_report,
)


def job_fixture(root, text="Cafe\u0301\r\n原文\n"):
    raw = root / "attempts" / "local_text" / "raw" / "p1.md"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(text.encode())
    attempt = {"attempt_id": "a1", "stage": "local_text", "outcome": "completed",
               "raw_artifact": str(raw.relative_to(root)),
               "raw_sha256": hashlib.sha256(text.encode()).hexdigest(), "format": "markdown",
               "warnings": [], "classification": {"type": "text", "reason": "prose"},
               "alignment": None, "usage": {}, "failure": None}
    return {"schema_version": "0.1", "job_id": "job1", "created_at": "2026-09-11T12:00:00Z",
            "source": {"path": "/documents/source.pdf", "sha256": "a" * 64,
                       "page_count": 3, "annotations": {"status": "present", "count": 2}},
            "selected_pages": [1, 3], "status": "completed",
            "pages": [{"page_id": "p1", "page_number": 1, "source_sha256": "a" * 64,
                       "attempts": [attempt], "selected_attempt": "a1",
                       "disposition": "unreviewed_text", "review_reasons": [], "review": None,
                       "next_action": "review"},
                      {"page_id": "p3", "page_number": 3, "source_sha256": "a" * 64,
                       "attempts": [], "selected_attempt": None, "disposition": "pending",
                       "review_reasons": [], "review": None, "next_action": "local_text"}],
            "usage": {"attempt_pages": 1, "image_calls": 0, "tokens": None,
                      "cost_usd": None, "cost_known": False}, "limits": {},
            "links": {"article": None, "custody": None},
            "source_retention": {"capture": "present", "preservation": "unverified",
                                 "removal_eligibility": "not_assessed", "removed": False},
            "next_action": "Review source pages before relying on extracted text."}


def test_report_embeds_exact_output_bytes_and_hashes_final_transcript(tmp_path):
    job = job_fixture(tmp_path)
    before = copy.deepcopy(job)
    result = write_document_report(job, tmp_path)
    stored = json.loads((tmp_path / "document.json").read_text())
    assert stored == result
    assert job == before
    assert stored["pages"][0]["selected_output"]["text"] == "Cafe\u0301\r\n原文\n"
    transcript = (tmp_path / "transcript.md").read_bytes()
    assert "Cafe\u0301\r\n原文\n".encode() in transcript
    assert stored["transcript"]["sha256"] == hashlib.sha256(transcript).hexdigest()
    assert transcript == render_transcript(stored).encode()
    assert (tmp_path / "report.md").read_bytes() == render_document_report(stored).encode()
    assert stored["counts"] == {"source_pages": 3, "selected_pages": 2, "processed_pages": 1,
                                 "selected_outputs": 1, "unresolved_pages": 2}
    assert "#page=1" in render_transcript(stored)
    schema = json.loads((Path(__file__).parents[2] / "schemas/document.schema.json").read_text())
    validate(stored, schema)


@pytest.mark.parametrize('filename', ['synthetic#draft.pdf', 'synthetic?draft.pdf',
                                    'synthetic%20draft.pdf', 'synthetic 日本語.pdf'])
def test_source_links_preserve_filename_characters(tmp_path, filename):
    from urllib.parse import quote, unquote, urlsplit
    job = job_fixture(tmp_path)
    job['source']['path'] = f'/documents/{filename}'
    article = 'https://example.org/article?q=synthetic%20draft#section'
    job['links']['article'] = article
    report = write_document_report(job, tmp_path)
    source_link = report['pages'][0]['source_link']
    parsed = urlsplit(source_link)
    assert unquote(parsed.path) == job['source']['path']
    assert parsed.query == ''
    assert parsed.fragment == 'page=1'
    markdown = render_document_report(report)
    assert f"(<{quote(job['source']['path'], safe='/')}>)" in markdown
    assert f'(<{source_link}>)' in markdown
    assert f'(<{source_link}>)' in render_transcript(report)
    assert f'(<{article}>)' in markdown


def test_report_keeps_partial_evidence_without_selecting_it(tmp_path):
    job = job_fixture(tmp_path)
    partial = tmp_path / "attempts" / "partial.md"
    partial.write_bytes(b"Partial response")
    failed = {**job["pages"][0]["attempts"][0], "attempt_id": "a2", "stage": "image",
              "outcome": "failed", "raw_artifact": "attempts/partial.md",
              "raw_sha256": hashlib.sha256(partial.read_bytes()).hexdigest(),
              "failure": {"code": "MODEL_OUTPUT_TRUNCATED"}}
    job["pages"][0]["attempts"].append(failed)
    result = write_document_report(job, tmp_path)
    assert result["pages"][0]["selected_output"]["attempt_id"] == "a1"
    assert "attempts/partial.md" in render_document_report(result)
    assert "failed" in render_document_report(result)
    assert "MODEL_OUTPUT_TRUNCATED" in render_document_report(result)
    job["pages"][0]["selected_attempt"] = "a2"
    with pytest.raises(ValueError, match="completed"):
        write_document_report(job, tmp_path)


@pytest.mark.parametrize("mutation", ["tampered", "traversal", "absolute", "symlink", "non_utf8"])
def test_report_rejects_unsafe_or_changed_selected_evidence(tmp_path, mutation):
    job = job_fixture(tmp_path)
    selected = job["pages"][0]["attempts"][0]
    raw = tmp_path / selected["raw_artifact"]
    if mutation == "tampered":
        raw.write_bytes(b"Changed")
    elif mutation == "traversal":
        selected["raw_artifact"] = "../outside.md"
    elif mutation == "absolute":
        selected["raw_artifact"] = str(raw)
    elif mutation == "symlink":
        link = tmp_path / "linked"
        link.symlink_to(raw.parent, target_is_directory=True)
        selected["raw_artifact"] = "linked/p1.md"
    else:
        raw.write_bytes(b"\xff")
        selected["raw_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        write_document_report(job, tmp_path)
    assert not (tmp_path / "document.json").exists()


def test_failed_preflight_report_does_not_invent_source_page_count(tmp_path):
    job = job_fixture(tmp_path)
    job.update(status="halted", pages=[], selected_pages=[])
    job["source"].update(page_count=None, annotations={"status": "unknown", "count": None})
    result = write_document_report(job, tmp_path)
    assert result["counts"]["source_pages"] is None
    assert "unknown" in render_document_report(result)
    assert result["source_retention"] == job["source_retention"]


def test_report_distinguishes_annotation_inventory_and_retention_state(tmp_path):
    report = write_document_report(job_fixture(tmp_path), tmp_path)
    rendered = render_document_report(report)
    assert "Annotations: present (2)" in rendered
    assert "body extraction or citation completeness" in rendered
    assert "Preservation: unverified" in rendered
    assert "Removal eligibility: not_assessed" in rendered
    assert "Source removed: no" in rendered
    assert rendered.count("Next action:") == 1


def test_build_report_checks_evidence_without_mutating_output_directory(tmp_path):
    job = job_fixture(tmp_path)
    before = {str(path.relative_to(tmp_path)): path.read_bytes()
              for path in tmp_path.rglob("*") if path.is_file()}
    report = build_document_report(job, tmp_path)
    after = {str(path.relative_to(tmp_path)): path.read_bytes()
             for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after
    assert report["pages"][0]["selected_output"]["text"] == "Cafe\u0301\r\n原文\n"
    assert report == write_document_report(job, tmp_path)


@pytest.mark.parametrize("disposition", ["reviewed_text", "reviewed_blank"])
def test_report_cannot_publish_model_only_reviewed_status(tmp_path, disposition):
    job = job_fixture(tmp_path)
    job["pages"][0]["disposition"] = disposition
    with pytest.raises(ValueError, match="review"):
        build_document_report(job, tmp_path)


def test_report_receipt_must_match_the_selected_output(tmp_path):
    job = job_fixture(tmp_path)
    page = job["pages"][0]
    page["attempts"].append({**page["attempts"][0], "attempt_id": "a2"})
    page["disposition"] = "reviewed_text"
    page["review"] = {"schema_version": "0.1", "source_sha256": "a" * 64, "decisions": [{
        "page_id": "p1", "page_number": 1, "disposition": "reviewed_text",
        "selected_attempt": "a2", "output_sha256": page["attempts"][0]["raw_sha256"],
        "reason": "Compared source", "reviewer": "Peter", "reviewed_at": "2026-09-11T12:00:00Z"}]}
    with pytest.raises(ValueError, match="review"):
        build_document_report(job, tmp_path)


@pytest.mark.parametrize("artifact", ["transcript.md", "report.md", "attempts/local_text/raw/p1.md"])
def test_verification_detects_tampered_derived_or_raw_artifacts(tmp_path, artifact):
    write_document_report(job_fixture(tmp_path), tmp_path)
    (tmp_path / artifact).write_bytes(b"Tampered")
    with pytest.raises(ValueError):
        verify_document_report(tmp_path)


def test_schema_accepts_child_run_identity_and_image_evidence(tmp_path):
    job = job_fixture(tmp_path)
    job["usage"]["paid_cost_known"] = True
    job["usage"]["paid_tokens_known"] = True
    job["pages"][0]["attempts"][0].update(
        run_path="attempts/local_text", run_id="child1", page_id="p1", page_number=1,
        source_sha256="a" * 64, input_evidence=None,
    )
    report = build_document_report(job, tmp_path)
    schema = json.loads((Path(__file__).parents[2] / "schemas/document.schema.json").read_text())
    validate(report, schema)


def test_partial_token_accounting_is_not_presented_as_a_known_total(tmp_path):
    job = job_fixture(tmp_path)
    job["usage"].update(tokens=12, tokens_known=False)
    rendered = render_document_report(build_document_report(job, tmp_path))
    assert "tokens: unknown (known subtotal: 12)" in rendered


def test_report_preserves_replaced_human_review_receipts(tmp_path):
    job = job_fixture(tmp_path)
    page = job["pages"][0]
    first = {"schema_version": "0.1", "source_sha256": "a" * 64, "decisions": [{
        "page_id": "p1", "page_number": 1, "disposition": "reviewed_text",
        "selected_attempt": "a1", "output_sha256": page["attempts"][0]["raw_sha256"],
        "reason": "Compared source", "reviewer": "Peter", "reviewed_at": "2026-09-11T12:00:00Z"}]}
    second = copy.deepcopy(first)
    second["decisions"][0].update(reason="Confirmed after a second inspection",
                                   reviewed_at="2026-09-11T13:00:00Z")
    page.update(disposition="reviewed_text", review=second, review_history=[first, second],
                next_action="none")
    report = write_document_report(job, tmp_path)
    stored = json.loads((tmp_path / "document.json").read_bytes())
    assert stored["pages"][0]["review_history"] == [first, second]
    assert stored["pages"][0]["review"] == second
    assert stored["pages"][0]["selected_output"]["attempt_id"] == "a1"
    schema = json.loads((Path(__file__).parents[2] / "schemas/document.schema.json").read_text())
    validate(report, schema)


@pytest.mark.parametrize("disposition", ["source_defect", "handwriting", "unreadable", "illustration"])
def test_human_confirmed_source_problem_stays_unresolved(tmp_path, disposition):
    job = job_fixture(tmp_path)
    page = job["pages"][0]
    review = {"schema_version": "0.1", "source_sha256": "a" * 64, "decisions": [{
        "page_id": "p1", "page_number": 1, "disposition": disposition,
        "selected_attempt": None, "output_sha256": None,
        "reason": "Source still requires investigation", "reviewer": "Peter",
        "reviewed_at": "2026-09-11T12:00:00Z"}]}
    page.update(disposition=disposition, selected_attempt=None, review=review,
                review_history=[review], next_action="none")
    job["next_action"] = "Obtain an alternate source or resolve the remaining source problems."
    report = build_document_report(job, tmp_path)
    assert report["counts"]["unresolved_pages"] == 2
    rendered = render_document_report(report)
    assert "unresolved pages: 2" in rendered
    assert disposition in rendered
    assert "Obtain an alternate source" in rendered
