"""Durable requests must not be repeated after a recoverable interruption."""

from __future__ import annotations

import json

import pytest

from pageledger import runner
from pageledger.adapters import ExtractionResult, TextAdapter
from pageledger.verify import verify_run


class CountingAdapter(TextAdapter):
    name = "counting"
    calls: list[int] = []
    failure: BaseException | None = None

    def extract(self, source, *, page_id, page_number, action, prompt):
        self.calls.append(page_number)
        if self.failure is not None:
            raise self.failure
        return ExtractionResult(
            content=f"page {page_number}",
            format="text",
            confidence=None,
            model=None,
            warnings=[],
            usage={"pages": 1, "tokens": 6},
        )


@pytest.fixture
def job(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("one\ftwo\fthree")
    config = tmp_path / "config.yml"
    config.write_text(
        "taxonomy:\n  page_types:\n    prose:\n      default_action: transcribe_text\nschema_version: '0.1'\nrun:\n  adapter: text\n"
    )
    adapter = CountingAdapter()
    adapter.calls = []
    monkeypatch.setattr(runner, "load_adapter", lambda *args: adapter)
    return source, config, tmp_path / "run", adapter


def launch(job, **kwargs):
    source, config, out, _adapter = job
    return runner.run(
        inputs=[source], config_path=config, out_dir=out, dry_run=False, resumable=True, **kwargs
    )


@pytest.mark.parametrize("phase", ["raw_artifact", "usage_budget_provenance", "page_log_control"])
def test_saved_response_survives_publication_interruption(job, phase):
    def interrupt(completed_phase, _duration):
        if completed_phase == phase:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        launch(job, _phase_observer=interrupt)
    assert job[3].calls == [1]
    retained = json.loads((job[2] / ".checkpoint/pages/doc_0001_page_0001.json").read_text())[
        "payload"
    ]
    result = runner.resume(job[2])
    assert result["status"] == "completed"
    assert job[3].calls == [1, 2, 3]
    assert verify_run(job[2])["status"] == "pass"
    provenance = [
        json.loads(line) for line in (job[2] / "provenance.jsonl").read_text().splitlines()
    ]
    assert len(provenance) == 3
    assert provenance[0]["timestamp"] == retained["started_at"]
    assert provenance[0]["extraction_seconds"] == retained["extraction_seconds"]
    assert provenance[0]["run_id"] == retained["run_id"] == result["run_id"]
    assert provenance[0]["cost"]["usd"] is None
    assert len({entry["run_id"] for entry in provenance}) == 1


def test_started_request_blocks_all_later_calls(job):
    job[3].failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        launch(job)
    job[3].failure = None
    with pytest.raises(ValueError, match="outcome_unknown"):
        runner.resume(job[2])
    assert job[3].calls == [1]


@pytest.mark.parametrize("damage", ["source", "config", "raw", "receipt", "response"])
def test_all_retained_evidence_is_validated_before_pending_calls(job, damage):
    def interrupt(phase, _duration):
        if phase == "page_log_control":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        launch(job, _phase_observer=interrupt)
    source, _config, out, adapter = job
    targets = {
        "source": source,
        "config": out / "config-snapshot.yml",
        "raw": out / "raw/doc_0001_page_0001.txt",
        "receipt": out / ".checkpoint/pages/doc_0001_page_0001.json",
        "response": out / ".checkpoint/pages/doc_0001_page_0001.json",
    }
    if damage == "receipt":
        targets[damage].unlink()
    else:
        targets[damage].write_text("damaged")
    with pytest.raises(ValueError):
        runner.resume(out)
    assert adapter.calls == [1]


def test_saved_response_before_raw_is_reused(job, monkeypatch):
    from pageledger import checkpoint

    original = checkpoint.Checkpoint.save

    def stop_after_response(self, page_id, record):
        original(self, page_id, record)
        if record["state"] == "response":
            raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(checkpoint.Checkpoint, "save", stop_after_response)
        with pytest.raises(KeyboardInterrupt):
            launch(job)
    assert not list((job[2] / "raw").iterdir())
    runner.resume(job[2])
    assert job[3].calls == [1, 2, 3]


def test_failure_before_response_commit_is_unknown(job, monkeypatch):
    from pageledger import checkpoint

    original = checkpoint.Checkpoint.save

    def stop_before_response(self, page_id, record):
        if record["state"] == "response":
            raise KeyboardInterrupt
        original(self, page_id, record)

    with monkeypatch.context() as patch:
        patch.setattr(checkpoint.Checkpoint, "save", stop_before_response)
        with pytest.raises(KeyboardInterrupt):
            launch(job)
    with pytest.raises(ValueError, match="outcome_unknown"):
        runner.resume(job[2])
    assert job[3].calls == [1]


def test_http_failure_retains_only_typed_metadata(job):
    class ProviderError(RuntimeError):
        http_status = 429
        code = "MODEL_HTTP_ERROR"
        stdout = "sensitive stdout"
        stderr = "sensitive stderr"

    job[3].failure = ProviderError("sensitive provider message")
    with pytest.raises(RuntimeError):
        launch(job)
    record_text = (job[2] / ".checkpoint/pages/doc_0001_page_0001.json").read_text()
    error = json.loads(record_text)["payload"]["error"]
    assert error == {
        "type": "ProviderError",
        "code": "MODEL_HTTP_ERROR",
        "http_status": 429,
        "cost_usd": None,
    }
    assert "sensitive" not in record_text
    with pytest.raises(ValueError, match="Known adapter failure"):
        runner.resume(job[2])
    assert job[3].calls == [1]


def test_budget_reconstruction_stops_pending_work(job):
    config = job[1]
    config.write_text(config.read_text() + "  budget:\n    max_tokens: 10\n")

    def interrupt(phase, _duration):
        if phase == "page_log_control":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        launch(job, _phase_observer=interrupt)
    with pytest.raises(runner.BudgetExceededError):
        runner.resume(job[2])
    assert job[3].calls == [1, 2]
    assert verify_run(job[2])["status"] == "pass"
    with pytest.raises(ValueError):
        runner.resume(job[2])
    assert job[3].calls == [1, 2]


def test_page_selection_and_finalized_resume_do_not_add_calls(job):
    result = launch(job, pages="2-3")
    before = {path.name: path.read_bytes() for path in job[2].iterdir() if path.is_file()}
    resumed = runner.resume(job[2])
    assert result["run_id"] == resumed["run_id"]
    assert job[3].calls == [2, 3]
    assert before == {path.name: path.read_bytes() for path in job[2].iterdir() if path.is_file()}


def test_source_changed_during_run_prevents_manifest_commit(job, monkeypatch):
    original = job[3].extract

    def extract(*args, **kwargs):
        result = original(*args, **kwargs)
        job[0].write_text("mutated source")
        return result

    monkeypatch.setattr(job[3], "extract", extract)
    with pytest.raises(ValueError, match="source identity"):
        launch(job)
    assert not (job[2] / "manifest.json").exists()
    with pytest.raises(ValueError, match="source identity"):
        runner.resume(job[2])


@pytest.mark.parametrize(
    "setting", ["  retry:\n    max_retries: 1\n", "  on_page_error: continue\n"]
)
def test_unsafe_execution_settings_rejected_before_writes(job, setting):
    job[1].write_text(job[1].read_text() + setting)
    with pytest.raises(ValueError, match="zero retries"):
        launch(job)
    assert not job[2].exists()
    assert not job[3].calls


def test_adapter_code_change_blocks_pending_calls(job, monkeypatch):
    def interrupt(phase, _duration):
        if phase == "page_log_control":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        launch(job, _phase_observer=interrupt)
    from pageledger import checkpoint

    monkeypatch.setattr(checkpoint, "_adapter_code_sha256", lambda adapter: "0" * 64)
    with pytest.raises(ValueError, match="adapter or package identity"):
        runner.resume(job[2])
    assert job[3].calls == [1]


def test_single_writer_rejects_second_resume(job):
    def interrupt(phase, _duration):
        if phase == "page_log_control":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        launch(job, _phase_observer=interrupt)
    from pageledger.checkpoint import writer_lock

    with writer_lock(job[2]):
        with pytest.raises(ValueError, match="active writer"):
            runner.resume(job[2])
    runner.resume(job[2])
    assert job[3].calls == [1, 2, 3]


def test_checkpoint_schemas_validate_each_durable_state(job, monkeypatch):
    from pathlib import Path

    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    from pageledger import checkpoint

    schemas = Path(__file__).resolve().parents[2] / "schemas"
    registry = Registry().with_resources(
        (path.name, Resource.from_contents(json.loads(path.read_text())))
        for path in schemas.glob("*.schema.json")
    )
    job_schema = json.loads((schemas / "checkpoint.schema.json").read_text())
    page_schema = json.loads((schemas / "checkpoint-page.schema.json").read_text())
    original = checkpoint.write_record

    def validate_write(path, payload):
        original(path, payload)
        schema = job_schema if path.name == "checkpoint.json" else page_schema
        Draft202012Validator(schema, registry=registry).validate(json.loads(path.read_text()))

    monkeypatch.setattr(checkpoint, "write_record", validate_write)
    launch(job)


def test_checkpoint_schema_rejects_negative_usage(job):
    from pathlib import Path

    from jsonschema import Draft202012Validator, ValidationError
    from referencing import Registry, Resource

    launch(job)
    schemas = Path(__file__).resolve().parents[2] / "schemas"
    registry = Registry().with_resources(
        (path.name, Resource.from_contents(json.loads(path.read_text())))
        for path in schemas.glob("*.schema.json")
    )
    page_schema = json.loads((schemas / "checkpoint-page.schema.json").read_text())
    validator = Draft202012Validator(page_schema, registry=registry)
    page_path = job[2] / ".checkpoint/pages/doc_0001_page_0001.json"
    page = json.loads(page_path.read_text())
    page["payload"]["result"]["usage"]["tokens"] = -1

    with pytest.raises(ValidationError):
        validator.validate(page)


def test_incomplete_verify_is_never_a_final_pass(job):
    job[3].failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        launch(job)
    report = verify_run(job[2])
    assert report["status"] == "fail"
    assert [error["code"] for error in report["errors"]] == ["run_incomplete"]


def test_finalized_alignment_survives_resume(job):
    from pageledger.aligner import align_run

    launch(job)
    schema = job[0].parent / "schema.yml"
    schema.write_text(
        "schema:\n  name: evidence\n  columns:\n    - name: value\n      type: string\n"
    )
    align_run(job[2], schema_path=schema)
    before = {p.name: p.read_bytes() for p in job[2].iterdir() if p.is_file()}
    runner.resume(job[2])
    assert before == {p.name: p.read_bytes() for p in job[2].iterdir() if p.is_file()}
    assert job[3].calls == [1, 2, 3]


@pytest.mark.parametrize(
    "damage", ["failed_type", "missing_planned_page", "state_gap", "job_unknown_field"]
)
def test_semantically_invalid_rehashed_records_fail_closed(job, damage):
    from pageledger.checkpoint import read_record, write_record

    def interrupt(phase, _duration):
        if phase == "page_log_control":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        launch(job, _phase_observer=interrupt)
    out = job[2]
    receipt_path = out / ".checkpoint/pages/doc_0001_page_0002.json"
    if damage == "failed_type":
        receipt = read_record(receipt_path)
        receipt.update(
            state="failed",
            started_at="2026-09-11T00:00:00Z",
            error={
                "type": "Error",
                "code": "adapter_failure",
                "http_status": True,
                "cost_usd": None,
            },
        )
        write_record(receipt_path, receipt)
    elif damage == "missing_planned_page":
        plan = read_record(out / "checkpoint.json")
        plan["documents"][0]["pages"].pop()
        write_record(out / "checkpoint.json", plan)
        (out / ".checkpoint/pages/doc_0001_page_0003.json").unlink()
    elif damage == "state_gap":
        receipt = read_record(receipt_path)
        receipt.update(state="started", started_at="2026-09-11T00:00:00Z")
        write_record(
            out / ".checkpoint/pages/doc_0001_page_0003.json",
            {**receipt, "page_id": "doc_0001_page_0003"},
        )
    else:
        plan = read_record(out / "checkpoint.json")
        plan["parent_run_id"] = "untrusted-parent"
        write_record(out / "checkpoint.json", plan)
    with pytest.raises(ValueError):
        runner.resume(out)
    assert job[3].calls == [1]


@pytest.mark.parametrize(
    "boundary,expected_before,expected_after",
    [
        ("started", [], []),
        ("response", [1], [1, 2, 3]),
        ("completed", [1], [1, 2, 3]),
    ],
)
def test_sigkill_releases_lock_and_preserves_request_boundary(
    tmp_path, boundary, expected_before, expected_after
):
    import os
    import signal
    import subprocess
    import sys
    import textwrap

    module = tmp_path / "durable_count.py"
    module.write_text(
        textwrap.dedent("""\
        import os
        from pageledger.adapters import TextAdapter
        class Adapter(TextAdapter):
            def extract(self, source, **kwargs):
                with source.with_suffix('.calls').open('a') as handle:
                    handle.write(str(kwargs['page_number']) + '\\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                return super().extract(source, **kwargs)
    """)
    )
    source = tmp_path / "source.txt"
    source.write_text("one\ftwo\fthree")
    config = tmp_path / "config.yml"
    config.write_text(
        "taxonomy:\n  page_types:\n    prose:\n      default_action: transcribe_text\n"
        "run:\n  adapter: durable_count:Adapter\n"
    )
    out = tmp_path / "run"
    script = """
import os, signal, sys
from pathlib import Path
from pageledger.checkpoint import Checkpoint
from pageledger.runner import run
original = Checkpoint.save
def save(self, page_id, record):
    original(self, page_id, record)
    if record['state'] == sys.argv[4]:
        os.kill(os.getpid(), signal.SIGKILL)
Checkpoint.save = save
run(inputs=[Path(sys.argv[1])], config_path=Path(sys.argv[2]),
    out_dir=Path(sys.argv[3]), adapter_path=Path(sys.argv[1]).parent,
    dry_run=False, resumable=True)
"""
    killed = subprocess.run(
        [sys.executable, "-c", script, str(source), str(config), str(out), boundary],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    calls = source.with_suffix(".calls")
    assert (
        [int(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    ) == expected_before
    resumed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pageledger",
            "resume",
            str(out),
            "--adapter-path",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env=os.environ.copy(),
    )
    assert resumed.returncode == (1 if boundary == "started" else 0), resumed.stderr
    assert (
        [int(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    ) == expected_after
    if boundary != "started":
        assert verify_run(out)["status"] == "pass"


def test_racing_initial_writer_cannot_enter_reserved_directory(job, monkeypatch):
    from pathlib import Path

    original = Path.mkdir
    out = job[2]
    reserved = False

    def competing_writer(path, *args, **kwargs):
        nonlocal reserved
        if path == out and not reserved:
            reserved = True
            original(path, parents=True)
            (path / "other-writer.txt").write_text("retained")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", competing_writer)
    with pytest.raises(ValueError, match="already exists"):
        launch(job)
    assert job[3].calls == []
    assert (out / "other-writer.txt").read_text() == "retained"


def test_saved_response_rejects_altered_alignment_before_more_calls(job, monkeypatch):
    from pageledger.checkpoint import Checkpoint

    original = job[3].extract

    def extract(*args, **kwargs):
        original(*args, **kwargs)
        return ExtractionResult(
            content=[{"value": "retained"}],
            format="json",
            confidence=None,
            model=None,
            warnings=[],
            usage={"pages": 1},
        )

    monkeypatch.setattr(job[3], "extract", extract)
    job[1].write_text(
        job[1].read_text()
        + "schema:\n  name: evidence\n  columns:\n    - name: value\n      type: string\n"
    )
    with monkeypatch.context() as patch:

        def stop_before_completion(*args, **kwargs):
            raise KeyboardInterrupt

        patch.setattr(Checkpoint, "complete", stop_before_completion)
        with pytest.raises(KeyboardInterrupt):
            launch(job)
    normalized = job[2] / "normalized/doc_0001_page_0001.json"
    assert normalized.is_file()
    normalized.write_text("{}")
    with pytest.raises(ValueError, match="Normalized"):
        runner.resume(job[2])
    assert job[3].calls == [1]


def test_finalized_resume_rejects_changed_source(job):
    launch(job)
    job[0].write_text("changed")
    with pytest.raises(ValueError, match="source identity"):
        runner.resume(job[2])
    assert job[3].calls == [1, 2, 3]


def test_imported_routes_resume_without_original_file_or_default_action(job):
    import yaml

    job[1].write_text(job[1].read_text().replace("transcribe_text", "extract_table"))
    routes = job[0].parent / "routes.yml"
    routes.write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "run_id": "routing",
                "generated_at": "2026-09-11T00:00:00Z",
                "classifier": {"adapter": None, "model": None, "prompt_hash": None},
                "documents": [
                    {
                        "source": str(job[0]),
                        "page_count": 3,
                        "pages": [
                            {
                                "page_id": f"doc_0001_page_{n:04d}",
                                "page_number": n,
                                "type": "prose",
                                "confidence": None,
                                "action": "transcribe_text",
                                "reason": "explicit route",
                            }
                            for n in (1, 2, 3)
                        ],
                    }
                ],
            }
        )
    )

    def interrupt(phase, _duration):
        if phase == "page_log_control":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        launch(job, routes_path=routes, _phase_observer=interrupt)
    routes.unlink()
    result = runner.resume(job[2])
    assert result["status"] == "completed"
    assert job[3].calls == [1, 2, 3]
    assert verify_run(job[2])["status"] == "pass"


@pytest.mark.parametrize("phase", ["response", "completed"])
def test_saved_page_at_exact_budget_cap_replays_without_next_request(job, monkeypatch, phase):
    from pageledger.checkpoint import Checkpoint

    source, config, out, adapter = job
    config.write_text(config.read_text() + "  budget:\n    max_tokens: 6\n")
    original = Checkpoint.save

    def interrupt(self, page_id, record):
        original(self, page_id, record)
        if record["state"] == phase:
            raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(Checkpoint, "save", interrupt)
        with pytest.raises(KeyboardInterrupt):
            launch(job)
    with pytest.raises(runner.BudgetExceededError):
        runner.resume(out)
    assert adapter.calls == [1]
    assert verify_run(out)["status"] == "pass"
