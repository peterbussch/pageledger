"""A document job owns cross-attempt recovery, budgets and evidence."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
import yaml
from test_image_evidence import image_descriptor

from pageledger import runner
from pageledger.adapters import ExtractionResult, TextAdapter
from pageledger.checkpoint import Checkpoint, file_digest, read_record
from pageledger.processing import process, resume_job, review_job, verify_job

TEXT = "The synthetic document contains complete readable prose for this source page. " * 8


class StageAdapter(TextAdapter):
    calls = []
    stage = "local_text"
    failure = None
    defective = set()

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        self.calls.append((self.stage, page_number))
        if self.failure and self.stage == "image":
            raise self.failure
        defective = page_number in self.defective and not (
            getattr(self, "text_only_defective", False) and self.stage != "local_text"
        )
        warnings = ["coverage_defect"] if defective else []
        evidence = (
            image_descriptor(Path(self.evidence_dir).parent, source, page_number, prompt)
            if self.stage in {"image", "second_opinion"}
            else None
        )
        content = TEXT
        if self.stage == "local_text" and getattr(self, "numeric", False):
            content = TEXT.replace("source page", "source page 13")
        if self.stage == "local_ocr" and getattr(self, "different", False):
            content = (
                TEXT.replace("source page", "source page 14")
                if getattr(self, "numeric", False)
                else "Unrelated OCR result with entirely different words. " * 8
            )
        if self.stage in {"local_text", "local_ocr"} and getattr(self, "generative_only", False):
            content = ""
        if self.stage == "local_text" and getattr(self, "scanned", False):
            content = ""
        if self.stage == "image" and getattr(self, "generative_only", False):
            content = TEXT
        return ExtractionResult(
            content,
            "text",
            1.0,
            "gemini-test-returned" if evidence else "synthetic",
            warnings,
            {"pages": 1, "tokens": 10, "cost_usd": None},
            input_evidence=evidence,
        )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text(TEXT + "\f" + TEXT + "\f" + TEXT)
    config = tmp_path / "config.yml"
    data = {
        "schema_version": "0.1",
        "run": {"adapter": "text"},
        "processing": {stage: {"adapter": stage} for stage in ("local_text", "local_ocr", "image")},
        "taxonomy": {"page_types": {"prose": {"default_action": "transcribe_text"}}},
    }
    data["processing"]["limits"] = {"max_image_pages": 2, "max_attempt_pages": 10}
    config.write_text(yaml.safe_dump(data))
    shared = {"calls": [], "defective": {2, 3}, "failure": None}

    def adapter(name, *args, **kwargs):
        value = StageAdapter()
        value.calls = shared["calls"]
        object.__setattr__(value, "stage", name)
        object.__setattr__(value, "failure", shared["failure"])
        object.__setattr__(value, "defective", shared["defective"])
        object.__setattr__(value, "different", shared.get("different", False))
        object.__setattr__(value, "numeric", shared.get("numeric", False))
        object.__setattr__(value, "generative_only", shared.get("generative_only", False))
        object.__setattr__(value, "text_only_defective", shared.get("text_only_defective", False))
        object.__setattr__(value, "scanned", shared.get("scanned", False))
        object.__setattr__(
            value,
            "capabilities",
            ("generative",) if name == "image" and shared.get("generative_only") else (),
        )
        object.__setattr__(value, "evidence_dir", args[0].get("evidence_dir") if args else None)
        return value

    monkeypatch.setattr(runner, "load_adapter", adapter)
    return source, config, tmp_path / "job", shared


def launch(setup, **kwargs):
    source, config, out, _ = setup
    return process(source=source, config_path=config, out_dir=out, **kwargs)


def test_serial_escalation_preserves_exact_denominator_and_review_holds(setup):
    result = launch(setup, pages="2-3")
    assert result["status"] == "completed"
    job = read_record(setup[2] / "job.json")
    assert job["source"]["page_count"] == 3
    assert job["selected_pages"] == [2, 3]
    assert setup[3]["calls"] == [
        ("local_text", 2),
        ("local_text", 3),
        ("local_ocr", 2),
        ("local_ocr", 3),
        ("image", 2),
        ("image", 3),
    ]
    assert all(page["disposition"] == "coverage_defect" for page in job["pages"])
    assert all(len(page["attempts"]) == 3 for page in job["pages"])
    assert verify_job(setup[2])["status"] == "pass"
    assert (setup[2] / "report.md").is_file()


def test_job_language_config_reaches_child_quality_lines(setup):
    data = yaml.safe_load(setup[1].read_text())
    data["language"] = {"script": "Cyrillic"}
    data["processing"]["limits"]["max_image_pages"] = 3
    setup[1].write_text(yaml.safe_dump(data))

    assert launch(setup)["status"] == "completed"
    job = read_record(setup[2] / "job.json")
    first_stage = next(stage for stage in job["stages"] if stage["stage"] == "local_text")
    lines = (setup[2] / first_stage["run_path"] / "quality.jsonl").read_text().splitlines()
    quality = json.loads(lines[0])
    assert "script_mismatch" in quality["warnings"]


def test_cross_engine_disagreement_with_held_attempt_is_not_review_evidence(setup):
    setup[3]["defective"] = {1}
    setup[3]["text_only_defective"] = True
    setup[3]["different"] = True
    launch(setup, pages="1")
    page = read_record(setup[2] / "job.json")["pages"][0]
    assert "engine_disagreement" not in page["review_reasons"]
    assert page["next_action"] == "review"


def test_numeric_disagreement_with_held_attempt_is_not_review_evidence(setup):
    setup[3]["defective"] = {1}
    setup[3]["text_only_defective"] = True
    setup[3]["numeric"] = True
    setup[3]["different"] = True
    launch(setup, pages="1")
    page = read_record(setup[2] / "job.json")["pages"][0]
    assert "numeric_disagreement" not in page["review_reasons"]


def test_held_text_layer_is_not_comparison_evidence_against_clean_ocr(setup):
    setup[3]["defective"] = {1}
    setup[3]["text_only_defective"] = True
    setup[3]["different"] = True
    launch(setup, pages="1")
    page = read_record(setup[2] / "job.json")["pages"][0]
    assert page["selected_attempt"].startswith("local_ocr-")
    assert "engine_disagreement" not in page["review_reasons"]
    assert "numeric_disagreement" not in page["review_reasons"]


def test_generative_only_selection_is_unconfirmed(setup):
    setup[3]["defective"] = {1}
    setup[3]["generative_only"] = True
    launch(setup, pages="1")
    page = read_record(setup[2] / "job.json")["pages"][0]
    assert page["selected_attempt"].startswith("image-")
    assert "unconfirmed_model_output" in page["review_reasons"]


def test_clean_classic_selection_is_not_held_by_generative_second_opinion(setup):
    from pageledger.processing_policy import assess_page

    page = {
        "attempts": [
            {
                "attempt_id": "classic",
                "outcome": "completed",
                "raw_artifact": "a",
                "raw_sha256": "x",
                "text": "same",
                "adapter_capabilities": [],
            },
            {
                "attempt_id": "model",
                "outcome": "completed",
                "raw_artifact": "b",
                "raw_sha256": "y",
                "text": "same",
                "adapter_capabilities": ["generative"],
            },
        ],
        "comparisons": [
            {
                "left_attempt": "classic",
                "right_attempt": "model",
                "agreement_ratio": 1.0,
                "number_differences": [],
            }
        ],
    }
    assert "unconfirmed_model_output" not in assess_page(page)["review_reasons"]


def test_agreeing_second_attempt_confirms_generative_selection():
    from pageledger.processing_policy import assess_page

    page = {
        "attempts": [
            {
                "attempt_id": "classic",
                "outcome": "completed",
                "raw_artifact": "a",
                "raw_sha256": "x",
                "text": "same",
                "adapter_capabilities": [],
                "warnings": ["coverage_defect"],
            },
            {
                "attempt_id": "model",
                "outcome": "completed",
                "raw_artifact": "b",
                "raw_sha256": "y",
                "text": "same",
                "adapter_capabilities": ["generative"],
            },
        ],
        "comparisons": [
            {
                "left_attempt": "classic",
                "right_attempt": "model",
                "agreement_ratio": 1.0,
                "number_differences": [],
            }
        ],
    }
    assessed = assess_page(page)
    assert assessed["selected_attempt"] == "model"
    assert "unconfirmed_model_output" in assessed["review_reasons"]


def test_benchmark_samples_source_page_numbers(setup):
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["benchmark"] = {"stage": "local_ocr", "every_nth_page": 2}
    setup[1].write_text(yaml.safe_dump(data))
    launch(setup)
    assert ("local_ocr", 2) in setup[3]["calls"]
    assert ("local_ocr", 1) not in setup[3]["calls"]


def test_child_completion_before_job_commit_is_adopted_without_repeating(setup, monkeypatch):
    from pageledger import processing

    original = processing._refresh

    def stop(job, root):
        if any((root / stage["run_path"] / "manifest.json").exists() for stage in job["stages"]):
            raise KeyboardInterrupt
        return original(job, root)

    with monkeypatch.context() as patch:
        patch.setattr(processing, "_refresh", stop)
        with pytest.raises(KeyboardInterrupt):
            launch(setup)
    resume_job(setup[2])
    assert setup[3]["calls"].count(("local_text", 1)) == 1
    assert len(setup[3]["calls"]) == 7


def test_started_image_outcome_halts_all_later_calls(setup):
    setup[3]["failure"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        launch(setup)
    before = list(setup[3]["calls"])
    result = resume_job(setup[2])
    assert result["status"] == "halted"
    assert setup[3]["calls"] == before
    assert read_record(setup[2] / "job.json")["pages"][1]["disposition"] == "outcome_unknown"


def test_shared_image_budget_stops_queue(setup):
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["limits"]["max_image_pages"] = 1
    setup[1].write_text(yaml.safe_dump(data))
    assert launch(setup)["status"] == "paused_budget"
    assert [call for call in setup[3]["calls"] if call[0] == "image"] == [("image", 2)]
    assert read_record(setup[2] / "job.json")["usage"]["image_calls"] == 1
    assert resume_job(setup[2], raise_limits={"max_image_pages": 2})["status"] == "completed"
    assert [call for call in setup[3]["calls"] if call[0] == "image"] == [
        ("image", 2),
        ("image", 3),
    ]
    assert verify_job(setup[2])["status"] == "pass"


def test_source_mutation_stops_resume_before_calls(setup, monkeypatch):
    original = Checkpoint.save

    def stop(self, page_id, record):
        original(self, page_id, record)
        if record["state"] == "response":
            raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoint, "save", stop)
        with pytest.raises(KeyboardInterrupt):
            launch(setup)
    before = list(setup[3]["calls"])
    setup[0].write_text("changed")
    with pytest.raises(ValueError, match="source"):
        resume_job(setup[2])
    assert setup[3]["calls"] == before


def test_bound_reviewed_blank_and_source_defect_skip_all_engines(setup, tmp_path):
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "source_sha256": file_digest(setup[0]),
                "decisions": [
                    dict(
                        page_id=f"doc_0001_page_{n:04d}",
                        page_number=n,
                        disposition=disposition,
                        selected_attempt=None,
                        output_sha256=None,
                        reviewer="synthetic reviewer",
                        reason="Visual inspection of source page",
                        reviewed_at="2026-09-11T12:00:00Z",
                    )
                    for n, disposition in [
                        (1, "reviewed_blank"),
                        (2, "source_defect"),
                        (3, "illustration"),
                    ]
                ],
            }
        )
    )
    result = launch(setup, review_path=review)
    assert result["status"] == "completed"
    assert setup[3]["calls"] == []
    assert verify_job(setup[2])["status"] == "pass"
    review_job(setup[2], review)
    assert setup[3]["calls"] == []


def test_tampered_selected_output_fails_verification(setup):
    launch(setup)
    job = read_record(setup[2] / "job.json")
    selected = job["pages"][0]["attempts"][0]["raw_artifact"]
    (setup[2] / selected).write_text("changed")
    assert verify_job(setup[2])["status"] == "fail"
    with pytest.raises(ValueError):
        resume_job(setup[2])


def test_invalid_pdf_gets_failed_container_report_without_invented_pages(setup):
    pdf = setup[0].with_suffix(".pdf")
    pdf.write_bytes(b"%PDF-invalid synthetic container")
    result = process(source=pdf, config_path=setup[1], out_dir=setup[2])
    assert result["status"] == "halted"
    job = read_record(setup[2] / "job.json")
    assert job["source"]["page_count"] is None
    assert job["pages"] == []
    assert setup[3]["calls"] == []
    assert "source_container_invalid" in job["halt_reason"]


def _encrypted_pdf(path, *, algorithm, user_password=""):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt(user_password=user_password, owner_password="owner", algorithm=algorithm)
    writer.write(path)
    return path


def test_source_inspection_reads_pdf_with_only_an_owner_password(tmp_path):
    pytest.importorskip("pypdf")
    pytest.importorskip("cryptography")
    from pageledger.processing_source import inspect_source

    source = _encrypted_pdf(tmp_path / "restricted.pdf", algorithm="AES-256")
    assert inspect_source(source) == (1, {"status": "none", "count": 0})


def test_pdf_that_needs_a_password_halts_with_a_typed_reason(setup):
    pytest.importorskip("pypdf")
    pdf = _encrypted_pdf(setup[0].with_suffix(".pdf"), algorithm="RC4-128", user_password="s3cret")
    result = process(source=pdf, config_path=setup[1], out_dir=setup[2])
    assert result["status"] == "halted"
    job = read_record(setup[2] / "job.json")
    assert job["halt_reason"] == "source_container_invalid:unsupported_encryption"
    assert "password" in job["next_action"]
    assert "s3cret" not in (setup[2] / "job.json").read_text(encoding="utf-8")
    assert setup[3]["calls"] == []


def test_uncertain_ocr_page_is_reported_as_uncertain_not_incomplete(setup, monkeypatch):
    from pageledger.processing_policy import HOLD_POLICY

    extract = StageAdapter.extract

    def uncertain(self, source, **kwargs):
        result = extract(self, source, **kwargs)
        return dataclasses.replace(result, warnings=["low_confidence"] if result.warnings else [])

    monkeypatch.setattr(StageAdapter, "extract", uncertain)
    result = launch(setup, pages="2")
    job = read_record(setup[2] / "job.json")
    assert job["hold_policy"] == HOLD_POLICY
    assert job["pages"][0]["disposition"] == "low_confidence"
    report = (setup[2] / "report.md").read_text(encoding="utf-8")
    assert "The engine was unsure of some words" in report
    assert "Possible missing or incomplete content" not in report
    assert verify_job(setup[2])["status"] == "pass", result


def _limit_attempts(setup, max_attempt_pages):
    source, config, out, shared = setup
    data = yaml.safe_load(config.read_text())
    data["processing"]["limits"]["max_attempt_pages"] = max_attempt_pages
    config.write_text(yaml.safe_dump(data))
    shared["defective"] = set()
    return process(source=source, config_path=config, out_dir=out)


def test_attempt_limit_processes_a_prefix_then_pauses(setup):
    result = _limit_attempts(setup, 2)
    assert result["status"] == "paused_budget"
    job = read_record(setup[2] / "job.json")
    assert job["usage"]["attempt_pages"] == 2
    assert job["halt_reason"] == "budget:max_attempt_pages"
    assert "--raise-limit max_attempt_pages=" in job["next_action"]
    assert verify_job(setup[2])["status"] == "pass"

    resumed = resume_job(setup[2], raise_limits={"max_attempt_pages": 3})

    assert resumed["status"] == "completed"
    job = read_record(setup[2] / "job.json")
    assert job["usage"]["attempt_pages"] == 3
    assert job["limits"]["max_attempt_pages"] == 3
    raised = [(h["limit"], h["previous"], h["value"]) for h in job["limits_history"]]
    assert raised == [("max_attempt_pages", 2, 3)]
    assert verify_job(setup[2])["status"] == "pass"


def test_paused_job_stays_paused_without_a_higher_limit(setup):
    _limit_attempts(setup, 2)
    assert resume_job(setup[2])["status"] == "paused_budget"
    for bad in ({"max_attempt_pages": 2}, {"max_attempt_pages": 1}, {"max_retries": 5}):
        with pytest.raises(ValueError):
            resume_job(setup[2], raise_limits=bad)
    assert read_record(setup[2] / "job.json")["usage"]["attempt_pages"] == 2


def test_limits_can_only_be_raised_on_a_paused_job(setup):
    assert launch(setup)["status"] == "completed"
    with pytest.raises(ValueError, match="paused"):
        resume_job(setup[2], raise_limits={"max_attempt_pages": 50})


def test_resume_cli_raises_a_limit(setup, capsys):
    from pageledger.cli import main

    _limit_attempts(setup, 1)
    code = main(["resume", str(setup[2]), "--raise-limit", "max_attempt_pages=3", "--json"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_unknown_paid_cost_stops_before_second_image(setup):
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["limits"]["max_cost_usd"] = 5
    setup[1].write_text(yaml.safe_dump(data))
    assert launch(setup)["status"] == "halted"
    job = read_record(setup[2] / "job.json")
    assert job["halt_reason"] == "budget:unknown_paid_cost"
    assert job["usage"]["cost_usd"] is None
    assert job["usage"]["image_calls"] == 1
    assert verify_job(setup[2])["status"] == "pass"


def test_token_budget_accumulates_across_local_and_ocr(setup):
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["limits"]["max_tokens"] = 35
    setup[1].write_text(yaml.safe_dump(data))
    assert launch(setup)["status"] == "halted"
    assert setup[3]["calls"] == [
        ("local_text", 1),
        ("local_text", 2),
        ("local_text", 3),
        ("local_ocr", 2),
    ]
    job = read_record(setup[2] / "job.json")
    assert job["usage"]["tokens"] == 40
    assert verify_job(setup[2])["status"] == "pass"


def test_partial_image_failure_is_retained_never_selected_or_retried(setup, monkeypatch):
    from pageledger.adapters import AdapterFailure

    original = StageAdapter.extract

    def clipped(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self.stage == "image":
            raise AdapterFailure("MODEL_OUTPUT_TRUNCATED", partial_result=result)
        return result

    monkeypatch.setattr(StageAdapter, "extract", clipped)
    assert launch(setup)["status"] == "halted"
    job = read_record(setup[2] / "job.json")
    page = job["pages"][1]
    assert page["disposition"] == "provider_failure"
    assert page["selected_attempt"].startswith("local_text:")
    partial = page["attempts"][-1]
    assert partial["failure"]["code"] == "MODEL_OUTPUT_TRUNCATED"
    assert (setup[2] / partial["raw_artifact"]).read_text() == TEXT
    calls = list(setup[3]["calls"])
    assert resume_job(setup[2])["status"] == "halted"
    assert setup[3]["calls"] == calls
    assert verify_job(setup[2])["status"] == "pass"
    (setup[2] / partial["raw_artifact"]).unlink()
    assert verify_job(setup[2])["status"] == "fail"
    assert not (setup[2] / partial["raw_artifact"]).exists()


def test_forged_internally_consistent_report_is_not_authoritative_over_job(setup):
    from pageledger.document_report import render_document_report

    launch(setup)
    path = setup[2] / "document.json"
    report = json.loads(path.read_text())
    report["source_retention"]["preservation"] = "preserved"
    path.write_text(json.dumps(report))
    (setup[2] / "report.md").write_text(render_document_report(report))
    assert verify_job(setup[2])["status"] == "fail"


def test_verify_job_rejects_selected_output_without_its_attempt(setup):
    launch(setup, pages="1")
    path = setup[2] / "document.json"
    report = json.loads(path.read_text())
    assert report["pages"][0]["selected_output"] is not None
    report["pages"][0]["attempts"] = []
    path.write_text(json.dumps(report))

    result = verify_job(setup[2])

    assert result["status"] == "fail"
    assert "selected output attempt is missing" in result["error"]


def test_verify_job_accepts_a_legacy_report_without_format_marker(setup):
    from pageledger.checkpoint import read_record
    from pageledger.document_report import (
        build_document_report,
        render_document_report,
        render_transcript,
    )

    launch(setup)
    report = build_document_report(read_record(setup[2] / "job.json"), setup[2], report_format=None)
    (setup[2] / "document.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (setup[2] / "transcript.md").write_text(render_transcript(report))
    (setup[2] / "report.md").write_text(render_document_report(report))

    assert verify_job(setup[2])["status"] == "pass"


def test_scanned_page_read_cleanly_by_ocr_does_not_need_a_person(setup):
    from pageledger.processing_policy import HOLD_POLICY

    setup[3].update(scanned=True, defective=set())
    assert launch(setup, pages="1")["status"] == "completed"
    job = read_record(setup[2] / "job.json")
    assert job["hold_policy"] == HOLD_POLICY
    page = job["pages"][0]
    assert page["selected_attempt"].startswith("local_ocr")
    assert page["disposition"] == "unreviewed_text"
    assert page["review_reasons"] == []
    assert "Pages needing a person: 0" in (setup[2] / "report.md").read_text(encoding="utf-8")
    assert verify_job(setup[2])["status"] == "pass"


def test_job_written_by_0_6_0_still_verifies_with_its_blank_hold(setup, monkeypatch):
    import pageledger.processing as processing_module

    setup[3].update(scanned=True, defective=set())
    with monkeypatch.context() as patch:
        patch.setattr(processing_module, "HOLD_POLICY", "0.6")
        assert launch(setup, pages="1")["status"] == "completed"
    job = read_record(setup[2] / "job.json")
    assert job["hold_policy"] == "0.6"
    assert job["pages"][0]["disposition"] == "blank_candidate"
    assert verify_job(setup[2])["status"] == "pass"


def test_verify_job_rebuilds_legacy_hold_policy_artifacts(setup):
    from pageledger.checkpoint import write_record
    from pageledger.document_report import (
        build_document_report,
        render_document_report,
        render_transcript,
    )

    launch(setup)
    job_path = setup[2] / "job.json"
    job = read_record(job_path)
    job.pop("hold_policy", None)
    for page in job["pages"]:
        page.pop("comparisons", None)
        for attempt in page["attempts"]:
            attempt.pop("adapter_capabilities", None)
    write_record(job_path, job)
    report = build_document_report(job, setup[2], report_format="0.5.1")
    (setup[2] / "document.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (setup[2] / "transcript.md").write_text(render_transcript(report))
    (setup[2] / "report.md").write_text(render_document_report(report))
    assert verify_job(setup[2])["status"] == "pass"


@pytest.mark.parametrize(
    "reason,label",
    [
        ("engine_disagreement", "Engines disagree"),
        ("numeric_disagreement", "Engines read numbers differently"),
        ("unconfirmed_model_output", "Model output not confirmed by another engine"),
    ],
)
def test_new_review_dispositions_render_in_report(setup, reason, label):
    from pageledger.document_report import build_document_report, render_document_report

    launch(setup, pages="1")
    job = read_record(setup[2] / "job.json")
    job["pages"][0]["disposition"] = reason
    job["pages"][0]["review_reasons"] = [reason]
    rendered = render_document_report(build_document_report(job, setup[2]))
    assert label in rendered


def test_read_only_source_inspection_counts_annotations_without_exposing_contents(tmp_path):
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

    from pageledger.processing_source import inspect_source

    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    page[NameObject("/Annots")] = ArrayObject(
        [
            writer._add_object(
                DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Annot"),
                        NameObject("/Subtype"): NameObject("/Text"),
                        NameObject("/Contents"): TextStringObject(
                            "Synthetic private annotation, never report its body"
                        ),
                    }
                )
            )
            for _ in range(19)
        ]
    )
    source = tmp_path / "annotations.pdf"
    writer.write(source)
    before = file_digest(source)
    assert inspect_source(source) == (1, {"status": "present", "count": 19})
    assert file_digest(source) == before


def test_declared_pdf_count_mismatch_fails_without_reindexing(tmp_path):
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, NumberObject

    from pageledger.processing_source import inspect_source

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer._pages.get_object()[NameObject("/Count")] = NumberObject(130)
    source = tmp_path / "miscount.pdf"
    writer.write(source)
    with pytest.raises(ValueError, match="Declared PDF page count"):
        inspect_source(source)


def test_process_cli_and_resume_job_dispatch(setup, capsys):
    from pageledger.cli import main

    setup[3]["defective"] = set()
    assert (
        main(
            ["process", str(setup[0]), "--config", str(setup[1]), "--out", str(setup[2]), "--json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert main(["resume", str(setup[2]), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["job_id"]
    assert main(["verify-job", str(setup[2]), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"
    assert setup[3]["calls"] == [("local_text", 1), ("local_text", 2), ("local_text", 3)]


@pytest.mark.parametrize("failure", [False, True])
def test_job_and_document_artifacts_validate_against_schemas(setup, failure):
    from jsonschema import validate

    if failure:
        setup[3]["failure"] = RuntimeError("synthetic provider unavailable")
    launch(setup)
    schemas = Path(__file__).resolve().parents[2] / "schemas"
    for name in ("job", "document"):
        validate(
            json.loads((setup[2] / f"{name}.json").read_text()),
            json.loads((schemas / f"{name}.schema.json").read_text()),
        )


def test_dollar_cap_is_enforced_within_local_batch(setup, monkeypatch):
    from dataclasses import replace

    original = StageAdapter.extract

    def billed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        return replace(result, usage={**result.usage, "cost_usd": 2.0})

    monkeypatch.setattr(StageAdapter, "extract", billed)
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["limits"]["max_cost_usd"] = 1.0
    setup[1].write_text(yaml.safe_dump(data))
    assert launch(setup)["status"] == "halted"
    assert setup[3]["calls"] == [("local_text", 1)]
    assert read_record(setup[2] / "job.json")["usage"]["known_cost_usd"] == 2.0


def test_initialization_interruption_produces_explicit_halt_without_calls(setup, monkeypatch):
    def stop(*args):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoint, "initialize", stop)
        with pytest.raises(KeyboardInterrupt):
            launch(setup)
    assert resume_job(setup[2])["status"] == "halted"
    assert setup[3]["calls"] == []
    assert read_record(setup[2] / "job.json")["halt_reason"] == "initialization_incomplete"
    assert verify_job(setup[2])["status"] == "pass"


@pytest.mark.parametrize("field", ["page_id", "disposition"])
def test_malformed_review_scalar_leaves_job_unchanged(setup, tmp_path, field):
    launch(setup)
    job = read_record(setup[2] / "job.json")
    chosen = job["pages"][0]["attempts"][0]
    decision = dict(
        page_id=chosen["page_id"],
        page_number=1,
        disposition="reviewed_text",
        selected_attempt=chosen["attempt_id"],
        output_sha256=chosen["raw_sha256"],
        reason="Checked against source",
        reviewer="Synthetic reviewer",
        reviewed_at="2026-09-11T18:00:00Z",
    )
    decision[field] = []
    receipt = {
        "schema_version": "0.1",
        "source_sha256": job["source"]["sha256"],
        "decisions": [decision],
    }
    review = tmp_path / "decisions.json"
    review.write_text(json.dumps(receipt))
    before = {p: p.read_bytes() for p in setup[2].rglob("*") if p.is_file()}
    calls = list(setup[3]["calls"])

    with pytest.raises(ValueError, match="Review"):
        review_job(setup[2], review)

    assert {p: p.read_bytes() for p in setup[2].rglob("*") if p.is_file()} == before
    assert setup[3]["calls"] == calls


def test_replacing_review_retains_history_without_calls(setup, tmp_path):
    launch(setup)
    job = read_record(setup[2] / "job.json")
    chosen = job["pages"][0]["attempts"][0]
    receipt = {
        "schema_version": "0.1",
        "source_sha256": job["source"]["sha256"],
        "decisions": [
            dict(
                page_id=chosen["page_id"],
                page_number=1,
                disposition="reviewed_text",
                selected_attempt=chosen["attempt_id"],
                output_sha256=chosen["raw_sha256"],
                reason="Checked against source",
                reviewer="Synthetic reviewer",
                reviewed_at="2026-09-11T18:00:00Z",
            )
        ],
    }
    review = tmp_path / "decisions.json"
    review.write_text(json.dumps(receipt))
    review_job(setup[2], review)
    receipt["decisions"][0]["reason"] = "Rechecked source and notes"
    review.write_text(json.dumps(receipt))
    review_job(setup[2], review)
    review_job(setup[2], review)
    page = read_record(setup[2] / "job.json")["pages"][0]
    assert len(page["review_history"]) == 2
    assert page["review"] == page["review_history"][-1]
    assert len(setup[3]["calls"]) == 7
    assert verify_job(setup[2])["status"] == "pass"


def test_image_configuration_cannot_silently_skip_disabled_ocr(setup):
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["local_ocr"] = None
    setup[1].write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="local_ocr"):
        launch(setup)
    assert not setup[2].exists()


def test_native_non_token_usage_does_not_block_known_image_token_budget(setup, monkeypatch):
    from dataclasses import replace

    original = StageAdapter.extract

    def tokens(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        return replace(
            result, usage={**result.usage, "tokens": 10 if self.stage == "image" else None}
        )

    monkeypatch.setattr(StageAdapter, "extract", tokens)
    data = yaml.safe_load(setup[1].read_text())
    data["processing"]["limits"]["max_tokens"] = 100
    setup[1].write_text(yaml.safe_dump(data))
    assert launch(setup)["status"] == "completed"
    job = read_record(setup[2] / "job.json")
    assert job["usage"]["image_calls"] == 2
    assert job["usage"]["tokens"] == 20
    assert job["usage"]["tokens_known"] is False
    assert job["usage"]["paid_tokens_known"] is True


def test_review_during_interruption_cannot_execute_an_obsolete_pending_plan(
    setup, monkeypatch, tmp_path
):
    def stop(**kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(runner, "run", stop)
        with pytest.raises(KeyboardInterrupt):
            launch(setup)
    receipt = {
        "schema_version": "0.1",
        "source_sha256": file_digest(setup[0]),
        "decisions": [
            dict(
                page_id="doc_0001_page_0001",
                page_number=1,
                disposition="source_defect",
                selected_attempt=None,
                output_sha256=None,
                reviewer="Synthetic reviewer",
                reason="Missing source strokes",
                reviewed_at="2026-09-11T18:00:00Z",
            )
        ],
    }
    review = tmp_path / "review.json"
    review.write_text(json.dumps(receipt))
    assert review_job(setup[2], review)["status"] == "halted"
    assert resume_job(setup[2])["status"] == "halted"
    assert setup[3]["calls"] == []


@pytest.mark.parametrize("limit,value", [("max_tokens", 10), ("max_cost_usd", 1.0)])
def test_reaching_exact_budget_cap_stops_before_next_local_request(
    setup, monkeypatch, limit, value
):
    from dataclasses import replace

    original = StageAdapter.extract

    def billed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        return replace(result, usage={**result.usage, "cost_usd": 1.0})

    monkeypatch.setattr(StageAdapter, "extract", billed)
    config = yaml.safe_load(setup[1].read_text())
    config["processing"]["limits"][limit] = value
    setup[1].write_text(yaml.safe_dump(config))
    assert launch(setup)["status"] == "halted"
    assert setup[3]["calls"] == [("local_text", 1)]
    assert verify_job(setup[2])["status"] == "pass"


def test_refresh_checks_shared_source_once_for_all_child_runs(setup, monkeypatch):
    from pageledger.processing import _refresh

    launch(setup)
    job = read_record(setup[2] / "job.json")
    original = Path.open
    reads = []

    def counted(path, *args, **kwargs):
        if path == setup[0]:
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted)
    _refresh(job, setup[2], materialize=False)
    assert len(reads) == 1


@pytest.mark.parametrize("completed", [False, True])
def test_resume_rejects_child_using_a_different_config(setup, monkeypatch, tmp_path, completed):
    def stop(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(runner, "run", stop)
        with pytest.raises(KeyboardInterrupt):
            launch(setup)
    job = read_record(setup[2] / "job.json")
    stage = job["stages"][0]
    alternate = tmp_path / "alternate.yml"
    config = yaml.safe_load((setup[2] / stage["config_path"]).read_text())
    config["run"]["adapter"] = "alternate_engine"
    alternate.write_text(yaml.safe_dump(config))
    kwargs = dict(
        inputs=[setup[0]],
        config_path=alternate,
        out_dir=setup[2] / stage["run_path"],
        dry_run=False,
        pages=",".join(map(str, stage["pages"])),
        resumable=True,
    )
    if completed:
        runner.run(**kwargs)
    else:
        with monkeypatch.context() as patch:
            patch.setattr(Checkpoint, "extract", stop)
            with pytest.raises(KeyboardInterrupt):
                runner.run(**kwargs)
    before = list(setup[3]["calls"])
    with pytest.raises(ValueError, match="configuration"):
        resume_job(setup[2])
    assert setup[3]["calls"] == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("budget", {"max_pages": 1}),
        ("pricing", {"cost_per_page": 5}),
        ("grading", {"review_below_grade": "C"}),
        ("rerun_if", [{"grade_below": "C"}]),
        ("quarantine_if", [{"grade_below": "D"}]),
        ("adapter_options", {"dpi": 400}),
        ("max_rerun_depth", 2),
        ("max_consecutive_failures", 3),
    ],
)
def test_process_rejects_run_controls_it_would_ignore(setup, key, value):
    data = yaml.safe_load(setup[1].read_text())
    data["run"][key] = value
    setup[1].write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match=rf"run\.{key} is ignored by process"):
        launch(setup)
    assert not setup[2].exists()


def test_process_warns_about_redundant_run_adapter_and_taxonomy(setup):
    result = launch(setup)
    assert result["status"] == "completed"
    warnings = result["config_warnings"]
    assert any("run.adapter is ignored by process" in w for w in warnings)
    assert any("taxonomy is ignored by process" in w for w in warnings)


def test_process_without_run_section_has_no_config_warnings(setup):
    data = yaml.safe_load(setup[1].read_text())
    del data["run"]
    del data["taxonomy"]
    setup[1].write_text(yaml.safe_dump(data))
    result = launch(setup)
    assert result["status"] == "completed"
    assert "config_warnings" not in result
