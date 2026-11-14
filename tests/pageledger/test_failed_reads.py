"""Failed remote reads with the built-in vision adapter, whole pages, through a job.

The adapter keeps an empty partial reading with the exact image it sent on every
failed request. That is not a reading: a timeout is requested once more, and a
page whose retry fails too is held while the job goes on.
"""

from __future__ import annotations

import yaml
from test_tiled_jobs import _timing_out, tiled_job  # noqa: F401  (tiled_job is a fixture)

import pageledger.processing as processing
from pageledger.checkpoint import read_record
from pageledger.processing import process, resume_job, verify_job


def _untiled(tiled_job):  # noqa: F811
    source, config, root, log = tiled_job
    data = yaml.safe_load(config.read_text())
    del data["processing"]["tiling"]
    del data["processing"]["limits"]["max_image_requests"]
    config.write_text(yaml.safe_dump(data))
    return source, config, root, log


def _images(job):
    return [stage["stage_id"] for stage in job["stages"] if stage["stage"] == "image"]


def test_a_page_that_times_out_once_is_read_again_and_completes(tiled_job, monkeypatch):  # noqa: F811
    source, config, root, _ = _untiled(tiled_job)
    _timing_out(monkeypatch, 1)
    assert process(source=source, config_path=config, out_dir=root)["status"] == "completed"
    job = read_record(root / "job.json")
    assert _images(job) == ["image-0001", "image-0001-retry-1", "image-0002"]
    first = job["pages"][0]
    failed = next(a for a in first["attempts"] if a["attempt_id"].startswith("image-0001:"))
    # What the first request kept: the image it sent and no text.
    assert failed["outcome"] == "failed" and failed["input_evidence"]["kind"] == "page_image"
    assert (root / failed["raw_artifact"]).read_text() == ""
    assert first["selected_attempt"].startswith("image-0001-retry-1:")
    assert verify_job(root)["status"] == "pass"


def test_a_page_that_times_out_twice_is_held_and_the_job_goes_on(tiled_job, monkeypatch):  # noqa: F811
    source, config, root, _ = _untiled(tiled_job)
    _timing_out(monkeypatch, 1, 2)
    assert process(source=source, config_path=config, out_dir=root)["status"] == "completed"
    job = read_record(root / "job.json")
    assert _images(job) == ["image-0001", "image-0001-retry-1", "image-0002"]
    first, second = job["pages"]
    assert first["disposition"] == "provider_failure" and first["next_action"] == "review"
    assert second["selected_attempt"].startswith("image-0002:")
    assert verify_job(root)["status"] == "pass"


def test_a_page_that_kept_text_before_failing_still_halts(tiled_job, monkeypatch):  # noqa: F811
    from pageledger import vision
    from pageledger.adapters import AdapterFailure

    source, config, root, _ = _untiled(tiled_job)
    original = vision.VisionAdapter._result

    def kept_text(self, text, *args):
        return original(self, text or "half a page", *args)

    monkeypatch.setattr(vision.VisionAdapter, "_result", kept_text)
    monkeypatch.setattr(
        vision.VisionAdapter,
        "_post",
        lambda self, body: (_ for _ in ()).throw(AdapterFailure("MODEL_TIMEOUT")),
    )
    assert process(source=source, config_path=config, out_dir=root)["status"] == "halted"
    assert _images(read_record(root / "job.json")) == ["image-0001"]


def test_a_job_that_halted_on_one_timeout_before_the_fix_verifies_as_recorded(
    tiled_job,  # noqa: F811
    monkeypatch,
):
    source, config, root, log = _untiled(tiled_job)
    _timing_out(monkeypatch, 1)
    # Write the job as 0.8 and the first 0.9 builds did: an empty partial was output.
    with monkeypatch.context() as old:
        old.setattr(processing, "_nothing_read", lambda attempt: not attempt.get("raw_artifact"))
        assert process(source=source, config_path=config, out_dir=root)["status"] == "halted"
    recorded = read_record(root / "job.json")
    assert recorded["halt_reason"].startswith("attempt_stopped:image-0001:")
    # Rebuilding derives pages from the retained attempts, never the halt itself.
    assert verify_job(root)["status"] == "pass"
    rendered = len(log.read_text().splitlines())
    assert resume_job(root)["status"] == "halted"
    assert len(log.read_text().splitlines()) == rendered
    assert read_record(root / "job.json")["stages"] == recorded["stages"]
