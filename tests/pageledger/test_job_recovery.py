"""Jobs that stop part-way can always be continued, and say why they stopped."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_processing import _batched, _failing_image, setup  # noqa: F401

from pageledger import processing, runner
from pageledger.checkpoint import read_record
from pageledger.processing import process, resume_job, review_job, verify_job

PAGES = "first page words here\fsecond page words\fthird page words\fpage four words\n"


def _job(tmp_path: Path, processing_config: dict, adapters: dict[str, str]) -> tuple:
    source = tmp_path / "sample.txt"
    source.write_text(PAGES, encoding="utf-8")
    folder = tmp_path / "adapters"
    folder.mkdir()
    for name, code in adapters.items():
        (folder / f"{name}.py").write_text(code, encoding="utf-8")
    config = tmp_path / "cfg.yml"
    config.write_text(yaml.safe_dump({"processing": processing_config}), encoding="utf-8")
    return source, config, folder, tmp_path / "jobs" / "x"


FLAKY = """
from pathlib import Path
from pageledger.adapters import TextAdapter
COUNT = Path(__file__).with_name("count")
class Ocr(TextAdapter):
    def __init__(self, *a, **k):
        n = int(COUNT.read_text()) + 1 if COUNT.exists() else 1
        COUNT.write_text(str(n))
        if n == 3:
            raise ValueError("model server not reachable")
        super().__init__(*a, **k)
"""


def test_a_stage_that_cannot_start_is_recorded_and_resume_reads_its_pages(tmp_path):
    source, config, adapters, out = _job(
        tmp_path,
        {
            "local_text": {"adapter": "text"},
            "local_ocr": {"adapter": "flaky:Ocr"},
            "escalate_on": ["hold", "always"],
            "batch": {"pages": 3, "concurrent": 1},
        },
        {"flaky": FLAKY},
    )
    result = process(source=source, config_path=config, out_dir=out, adapter_path=adapters)

    assert result["status"] == "halted"
    job = read_record(out / "job.json")
    stopped = [stage for stage in job["stages"] if stage["status"] == "not_started"]
    assert [stage["stage_id"] for stage in stopped] == ["local_ocr-0002"]
    assert "model server not reachable" in job["next_action"]
    assert "Nothing was read or sent" in job["next_action"]
    # The stage that finished before it is in the job and its report.
    page1 = next(page for page in job["pages"] if page["page_number"] == 1)
    assert [attempt["stage"] for attempt in page1["attempts"]] == ["local_text", "local_ocr"]
    assert verify_job(out)["status"] == "pass"

    assert resume_job(out, adapter_path=adapters)["status"] == "completed"
    job = read_record(out / "job.json")
    assert "local_ocr-0002-restart-1" in [stage["stage_id"] for stage in job["stages"]]
    assert all(
        any(attempt["stage"] == "local_ocr" for attempt in page["attempts"])
        for page in job["pages"]
    )
    assert verify_job(out)["status"] == "pass"


def test_a_stage_whose_folder_could_not_be_made_does_not_break_the_job(tmp_path, monkeypatch):
    source, config, adapters, out = _job(
        tmp_path,
        {"local_text": {"adapter": "text"}, "local_ocr": {"adapter": "text"}},
        {},
    )
    real = runner.run
    failed = []

    def run(**kwargs):
        if "local_ocr" in str(kwargs["out_dir"]) and not failed:
            failed.append(kwargs["out_dir"])
            raise OSError(28, "No space left on device")
        return real(**kwargs)

    monkeypatch.setattr(runner, "run", run)
    data = yaml.safe_load(config.read_text())
    data["processing"]["escalate_on"] = ["hold", "always"]
    config.write_text(yaml.safe_dump(data))

    assert process(source=source, config_path=config, out_dir=out)["status"] == "halted"
    assert not failed[0].exists()
    assert "No space left on device" in read_record(out / "job.json")["next_action"]
    assert verify_job(out)["status"] == "pass"
    assert resume_job(out)["status"] == "completed"
    assert verify_job(out)["status"] == "pass"


def test_a_stage_recorded_as_not_started_must_have_no_plan(tmp_path, monkeypatch):
    source, config, adapters, out = _job(
        tmp_path, {"local_text": {"adapter": "text"}, "local_ocr": {"adapter": "text"}}, {}
    )
    assert process(source=source, config_path=config, out_dir=out)["status"] == "completed"
    job = read_record(out / "job.json")
    job["stages"][0]["status"] = "not_started"
    with pytest.raises(ValueError, match="recorded as never started"):
        processing._refresh(job, out)


READER = """
from pageledger.adapters import TextAdapter
class Reader(TextAdapter):
    def __init__(self, evidence_dir=None):
        self.evidence_dir = evidence_dir
    def extract(self, source, *, page_id, page_number, action, prompt=None):
        raise RuntimeError(f"local model failed on page {page_number}")
"""


def test_a_failed_local_reader_in_a_batch_does_not_skip_the_rest_of_the_batch(tmp_path):
    source, config, adapters, out = _job(
        tmp_path,
        {
            "local_text": {"adapter": "text"},
            "local_ocr": {"adapter": "text"},
            "local_image": {"adapter": "reader:Reader"},
            "escalate_on": ["hold", "always"],
            "batch": {"pages": 3, "concurrent": 1},
            "limits": {"max_local_image_pages": 10},
        },
        {"reader": READER},
    )
    process(source=source, config_path=config, out_dir=out, adapter_path=adapters)
    job = read_record(out / "job.json")
    read = sorted(
        number
        for stage in job["stages"]
        if stage["stage"] == "local_image"
        for number in stage["pages"]
    )
    assert read == [1, 2, 3, 4]
    assert all(page["next_action"] != "local_image" for page in job["pages"])


def test_a_progress_listener_that_fails_does_not_stop_the_job(tmp_path):
    source, config, adapters, out = _job(
        tmp_path, {"local_text": {"adapter": "text"}, "local_ocr": {"adapter": "text"}}, {}
    )
    heard = []

    def listener(event):
        heard.append(event)
        raise BrokenPipeError(32, "Broken pipe")

    result = process(source=source, config_path=config, out_dir=out, progress=listener)
    assert result["status"] == "completed"
    assert len(heard) == 1
    job = read_record(out / "job.json")
    assert all(stage["status"] == "completed" for stage in job["stages"])


def test_a_bad_review_file_leaves_no_job_folder(tmp_path):
    source, config, adapters, out = _job(tmp_path, {"local_text": {"adapter": "text"}}, {})
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": "0.1", "source_sha256": "00", "decisions": []}))
    with pytest.raises(ValueError, match="another document"):
        process(source=source, config_path=config, out_dir=out, review_path=bad)
    assert not out.exists()
    with pytest.raises(ValueError, match="Review file not found: .*nothere.json"):
        process(
            source=source, config_path=config, out_dir=out, review_path=tmp_path / "nothere.json"
        )
    assert not out.exists()


def test_review_names_a_missing_review_file(tmp_path):
    source, config, adapters, out = _job(tmp_path, {"local_text": {"adapter": "text"}}, {})
    process(source=source, config_path=config, out_dir=out)
    for name in ("nothere.json", "nothere.csv"):
        with pytest.raises(ValueError, match=f"Review file not found: .*{name}"):
            review_job(out, tmp_path / name)


def test_a_huge_page_range_is_refused_before_it_is_expanded(tmp_path):
    source, config, adapters, out = _job(tmp_path, {"local_text": {"adapter": "text"}}, {})
    with pytest.raises(ValueError, match="Page 5 is past the end: the source has 4 pages"):
        process(source=source, config_path=config, out_dir=out, pages="2,3-10000000000000")
    assert not out.exists()


def test_an_empty_review_of_an_interrupted_job_lets_it_continue(tmp_path, monkeypatch):
    source, config, adapters, out = _job(
        tmp_path,
        {
            "local_text": {"adapter": "text"},
            "local_ocr": {"adapter": "text"},
            "escalate_on": ["hold", "always"],
        },
        {},
    )
    real = processing._launch
    calls = []

    def launch(root, source, stage, adapter_path):
        calls.append(stage["stage_id"])
        if stage["stage_id"] == "local_ocr-0002":
            raise KeyboardInterrupt
        return real(root, source, stage, adapter_path)

    monkeypatch.setattr(processing, "_launch", launch)
    with pytest.raises(KeyboardInterrupt):
        process(source=source, config_path=config, out_dir=out)
    monkeypatch.setattr(processing, "_launch", real)
    job = read_record(out / "job.json")
    assert job["status"] == "processing"
    receipt = tmp_path / "empty.json"
    receipt.write_text(
        json.dumps(
            {"schema_version": "0.1", "source_sha256": job["source"]["sha256"], "decisions": []}
        )
    )
    assert review_job(out, receipt)["status"] == "processing"
    assert resume_job(out)["status"] == "completed"
    assert verify_job(out)["status"] == "pass"


def test_a_lexicon_is_named_in_words_not_as_a_record():
    name = processing._lexicon_name(
        {"provider": "wordlist", "path": "/lists/ru.txt", "sha256": "ab" * 32, "words": 3}
    )
    assert name == "the word list /lists/ru.txt (SHA-256 abababababab…)"
    assert "{" not in processing._lexicon_name(
        {"provider": "pymorphy3", "language": "ru", "version": "2.0", "dictionary": "2.4"}
    )


def test_a_retry_that_could_not_start_is_still_owed(setup, monkeypatch):  # noqa: F811
    _batched(setup, pages=None, max_image_pages=7)
    _failing_image(monkeypatch, {2: 1})
    real = runner.run
    stopped = []

    def run(**kwargs):
        if "-retry-" in str(kwargs["out_dir"]) and not stopped:
            stopped.append(kwargs["out_dir"])
            raise OSError(13, "Permission denied", str(kwargs["out_dir"]))
        return real(**kwargs)

    monkeypatch.setattr(runner, "run", run)
    source, config, out, _ = setup
    assert process(source=source, config_path=config, out_dir=out)["status"] == "halted"
    job = read_record(out / "job.json")
    assert "Permission denied" in job["next_action"] and "Errno" not in job["next_action"]
    assert resume_job(out)["status"] == "completed"
    page = read_record(out / "job.json")["pages"][1]
    assert [a["outcome"] for a in page["attempts"] if a["stage"] == "image"] == [
        "failed",
        "completed",
    ]
    assert verify_job(out)["status"] == "pass"
