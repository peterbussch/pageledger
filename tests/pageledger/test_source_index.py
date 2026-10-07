"""Moved originals are found by content hash; sealed records keep their recorded paths."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from test_processing import _limit_attempts, setup  # noqa: F401  (setup is a fixture)
from test_replay import _run_text
from test_rerun import _do_rerun, _parent_with_flagged_page
from test_resume import CountingAdapter

from pageledger import runner
from pageledger.checkpoint import read_record
from pageledger.processing import resume_job, verify_job
from pageledger.source_index import (
    ENV_VAR,
    SourceUnavailable,
    find_indexed,
    index_files,
    resolve_source,
)
from pageledger.verify import verify_run


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _move(source: Path, tmp_path: Path) -> Path:
    moved = tmp_path / "Research_Library" / "soviet-dc" / f"SDC-076 Renamed{source.suffix}"
    moved.parent.mkdir(parents=True, exist_ok=True)
    source.rename(moved)
    return moved


def _index(tmp_path: Path, rows: list[tuple[str, Path]], name: str = "source-index.tsv") -> Path:
    index = tmp_path / name
    lines = ["sha256\tpath\tnote", "# relocated originals"]
    lines += [f"{sha}\t{path}\textra column" for sha, path in rows]
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index


def _codes(report: dict, key: str) -> set[str]:
    return {issue["code"] for issue in report[key]}


# The resolver


def test_recorded_path_wins_and_index_finds_a_moved_file(tmp_path, monkeypatch):
    source = tmp_path / "a.txt"
    source.write_text("alpha")
    sha = _sha(source)
    assert resolve_source(str(source), sha) == source

    moved = _move(source, tmp_path)
    with pytest.raises(SourceUnavailable):
        resolve_source(str(source), sha)
    monkeypatch.setenv(ENV_VAR, str(_index(tmp_path, [(sha.upper(), moved)])))
    assert resolve_source(str(source), sha) == moved


def test_wrong_digest_entry_is_never_returned(tmp_path, monkeypatch):
    source = tmp_path / "a.txt"
    source.write_text("alpha")
    sha = _sha(source)
    impostor = tmp_path / "impostor.txt"
    impostor.write_text("different bytes")
    source.unlink()
    monkeypatch.setenv(ENV_VAR, str(_index(tmp_path, [(sha, impostor)])))
    with pytest.raises(SourceUnavailable, match="changed or is missing"):
        resolve_source(str(source), sha)
    assert find_indexed(sha) is None


def test_several_index_files_skip_unreadable_and_relative_entries(tmp_path, monkeypatch):
    source = tmp_path / "a.txt"
    source.write_text("alpha")
    sha = _sha(source)
    moved = _move(source, tmp_path)
    first = _index(tmp_path, [(sha, Path("relative/a.txt"))], "first.tsv")
    second = _index(tmp_path, [(sha, moved)], "second.tsv")
    undecodable = tmp_path / "binary.tsv"
    undecodable.write_bytes(b"\xff\xfe\x00")
    paths = [tmp_path / "absent.tsv", undecodable, first, second]
    monkeypatch.setenv(ENV_VAR, os.pathsep.join(map(str, paths)))
    assert resolve_source(str(source), sha) == moved


def test_default_index_location_and_empty_variable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    default = home / ".config" / "pageledger" / "source-index.tsv"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(ENV_VAR)
    assert index_files() == []
    default.parent.mkdir(parents=True)
    default.write_text("")
    assert index_files() == [default]
    monkeypatch.setenv(ENV_VAR, "")
    assert index_files() == []


# Resumable runs


class SourceRecordingAdapter(CountingAdapter):
    sources: list[Path] = []

    def extract(self, source, **kwargs):
        self.sources.append(source)
        return super().extract(source, **kwargs)


@pytest.fixture
def interrupted(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("one\ftwo\fthree")
    config = tmp_path / "config.yml"
    config.write_text(
        "taxonomy:\n  page_types:\n    prose:\n      default_action: transcribe_text\n"
        "schema_version: '0.1'\nrun:\n  adapter: text\n"
    )
    adapter = SourceRecordingAdapter()
    adapter.calls, adapter.sources = [], []
    monkeypatch.setattr(runner, "load_adapter", lambda *args: adapter)

    def interrupt(phase, _duration):
        if phase == "raw_artifact":
            raise KeyboardInterrupt

    out = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        runner.run(
            inputs=[source],
            config_path=config,
            out_dir=out,
            dry_run=False,
            resumable=True,
            _phase_observer=interrupt,
        )
    assert adapter.calls == [1]
    return source.resolve(), out, adapter


def test_resume_reads_moved_source_through_index_and_keeps_recorded_path(
    interrupted, tmp_path, monkeypatch
):
    source, out, adapter = interrupted
    sealed = (out / "checkpoint.json").read_bytes()
    moved = _move(source, tmp_path)

    with pytest.raises(ValueError, match="Recovery source identity changed or source is missing"):
        runner.resume(out)
    assert adapter.calls == [1]

    monkeypatch.setenv(ENV_VAR, str(_index(tmp_path, [(_sha(moved), moved)])))
    assert runner.resume(out)["status"] == "completed"
    assert adapter.calls == [1, 2, 3]
    assert adapter.sources[1:] == [moved, moved]

    assert (out / "checkpoint.json").read_bytes() == sealed
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["inputs"][0]["path"] == str(source)
    provenance = [json.loads(line) for line in (out / "provenance.jsonl").read_text().splitlines()]
    assert {entry["source"]["path"] for entry in provenance} == {str(source)}

    report = verify_run(out)
    assert report["status"] == "pass"
    assert "source_relocated" in _codes(report, "warnings")
    assert "source_missing" not in _codes(report, "warnings")
    relocated = next(w for w in report["warnings"] if w["code"] == "source_relocated")
    assert relocated["source"] == str(source) and relocated["located"] == str(moved)

    # A finalized run resumes as a no-op through the index too.
    assert runner.resume(out)["resumed"] is True
    monkeypatch.setenv(ENV_VAR, "")
    assert "source_missing" in _codes(verify_run(out), "warnings")
    with pytest.raises(ValueError, match="source is missing"):
        runner.resume(out)


def test_resume_rejects_an_index_entry_with_other_bytes(interrupted, tmp_path, monkeypatch):
    source, out, adapter = interrupted
    sha = _sha(source)
    source.unlink()
    impostor = tmp_path / "impostor.txt"
    impostor.write_text("one\ftwo\fTHREE")
    monkeypatch.setenv(ENV_VAR, str(_index(tmp_path, [(sha, impostor)])))
    with pytest.raises(ValueError, match="source is missing"):
        runner.resume(out)
    assert adapter.calls == [1]


# Document jobs


def test_document_job_resumes_and_verifies_after_its_source_moves(
    setup,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    source, _config, root, shared = setup
    assert _limit_attempts(setup, 2)["status"] == "paused_budget"
    recorded = str(source.resolve())
    sealed_job = (root / "job.json").read_bytes()
    children = sorted(root.glob("attempts/*/checkpoint.json"))
    sealed_children = [path.read_bytes() for path in children]
    moved = _move(source, tmp_path)

    with pytest.raises(ValueError, match="Document job source changed or is missing"):
        resume_job(root, raise_limits={"max_attempt_pages": 3})
    assert verify_job(root)["status"] == "fail"
    assert (root / "job.json").read_bytes() == sealed_job

    home = tmp_path / "home"
    index = home / ".config" / "pageledger" / "source-index.tsv"
    index.parent.mkdir(parents=True)
    index.write_text(f"{_sha(moved)}\t{moved}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(ENV_VAR)

    assert verify_job(root)["status"] == "pass"
    calls = len(shared["calls"])
    assert resume_job(root, raise_limits={"max_attempt_pages": 3})["status"] == "completed"
    assert len(shared["calls"]) == calls + 1
    assert verify_job(root)["status"] == "pass"

    job = read_record(root / "job.json")
    assert job["source"]["path"] == recorded
    assert [path.read_bytes() for path in children] == sealed_children
    new_children = sorted(set(root.glob("attempts/*/checkpoint.json")) - set(children))
    assert len(new_children) == 1
    # The new child records the path it actually read; the sha binds it to the job.
    assert read_record(new_children[0])["inputs"][0]["path"] == str(moved)


# Rerun and bundle


def test_rerun_reads_relocated_parent_source(tmp_path, monkeypatch):
    source, parent = _parent_with_flagged_page(tmp_path)
    moved = _move(source, tmp_path)
    with pytest.raises(ValueError, match="Rerun source no longer exists"):
        _do_rerun(parent, tmp_path)

    monkeypatch.setenv(ENV_VAR, str(_index(tmp_path, [(_sha(moved), moved)])))
    result, child = _do_rerun(parent, tmp_path)
    assert result["summary"]["pages_extracted"] == 1
    assert json.loads((child / "manifest.json").read_text())["inputs"][0]["path"] == str(moved)
    assert verify_run(child)["status"] == "pass"


def test_bundle_copies_relocated_source(tmp_path, monkeypatch):
    from pageledger.replay import ReplayError, bundle_run, validate_bundle

    run_dir, source, _ = _run_text(tmp_path)
    moved = _move(source, tmp_path)
    with pytest.raises(ReplayError):
        bundle_run(run_dir, tmp_path / "refused")

    monkeypatch.setenv(ENV_VAR, str(_index(tmp_path, [(_sha(moved), moved)])))
    bundle_run(run_dir, tmp_path / "bundle")
    bundle = validate_bundle(tmp_path / "bundle")
    copied = tmp_path / "bundle" / bundle["sources"][0]["path"]
    assert copied.read_bytes() == moved.read_bytes()
