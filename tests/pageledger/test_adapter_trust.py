"""A job, run or bundle from disk never makes PageLedger import code it names."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from pageledger import runner
from pageledger.checkpoint import digest, file_digest

CONFIG = """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
"""


def _cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pageledger", *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=cwd,
        env=os.environ.copy(),
    )


def _evil_job(tmp_path: Path, marker: Path, spec: str = "os:makedirs") -> Path:
    source = tmp_path / "book.txt"
    source.write_text("first page\fsecond page\n", encoding="utf-8")
    made = _cli("process", str(source), "--out", str(tmp_path / "job"), "--json", cwd=tmp_path)
    assert made.returncode == 0, made.stderr
    job_dir = tmp_path / "job"
    envelope = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    job = envelope["payload"]
    job["status"] = "processing"
    stage = job["stages"][0]
    stage["status"] = "planned"
    shutil.rmtree(job_dir / stage["run_path"])
    config_path = job_dir / stage["config_path"]
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["run"]["adapter"] = spec
    config["run"]["adapter_options"] = {"name": str(marker)} if spec == "os:makedirs" else {}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    stage["config_sha256"] = file_digest(config_path)
    for page in job["pages"]:
        page["attempts"] = []
    envelope["sha256"] = digest(job)
    (job_dir / "job.json").write_text(json.dumps(envelope), encoding="utf-8")
    return job_dir


def _evil_run(tmp_path: Path, marker: Path) -> Path:
    source = tmp_path / "book.txt"
    source.write_text("one\ftwo\fthree", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(CONFIG, encoding="utf-8")
    out = tmp_path / "run"

    def interrupt(phase, _duration):
        if phase == "page_log_control":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        runner.run(
            inputs=[source],
            config_path=config,
            out_dir=out,
            dry_run=False,
            resumable=True,
            _phase_observer=interrupt,
        )
    snapshot = out / "config-snapshot.yml"
    data = yaml.safe_load(snapshot.read_text(encoding="utf-8"))
    data["run"]["adapter"] = "os:makedirs"
    data["run"]["adapter_options"] = {"name": str(marker)}
    snapshot.write_text(yaml.safe_dump(data), encoding="utf-8")
    envelope = json.loads((out / "checkpoint.json").read_text(encoding="utf-8"))
    envelope["payload"]["config_sha256"] = file_digest(snapshot)
    envelope["sha256"] = digest(envelope["payload"])
    (out / "checkpoint.json").write_text(json.dumps(envelope), encoding="utf-8")
    return out


def _evil_bundle(tmp_path: Path, marker: Path) -> Path:
    from pageledger.replay import bundle_run

    source = tmp_path / "book.txt"
    source.write_text("one\ftwo\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(CONFIG, encoding="utf-8")
    runner.run(inputs=[source], config_path=config, out_dir=tmp_path / "run", dry_run=False)
    bundle = tmp_path / "bundle"
    bundle_run(tmp_path / "run", bundle)

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    snapshot = bundle / "baseline" / "config-snapshot.yml"
    data = yaml.safe_load(snapshot.read_text(encoding="utf-8"))
    data["run"]["adapter"] = "os:makedirs"
    data["run"]["adapter_options"] = {"name": str(marker)}
    snapshot.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    manifest_path = bundle / "baseline" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    def fix(value):
        if isinstance(value, dict):
            if value.get("path") == "config-snapshot.yml" and "sha256" in value:
                value["sha256"] = sha(snapshot)
            for item in value.values():
                fix(item)
        elif isinstance(value, list):
            for item in value:
                fix(item)

    fix(manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    record = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    for entry in record["files"]:
        path = bundle / entry["path"]
        entry["sha256"] = sha(path)
        entry["size"] = path.stat().st_size
    record["baseline"]["manifest_sha256"] = sha(manifest_path)
    (bundle / "bundle.json").write_text(json.dumps(record, indent=2, sort_keys=True))
    return bundle


@pytest.mark.parametrize("unrelated_path", [False, True])
def test_resume_of_a_received_job_never_imports_its_adapter(tmp_path, unrelated_path):
    marker = tmp_path / "PWNED"
    job_dir = _evil_job(tmp_path, marker)
    before = (job_dir / "job.json").read_bytes()
    extra = ["--adapter-path", str(tmp_path / "mine")] if unrelated_path else []
    (tmp_path / "mine").mkdir()
    verified = _cli("verify", str(job_dir), cwd=tmp_path)
    assert "os:makedirs" in verified.stdout + verified.stderr
    resumed = _cli("resume", str(job_dir), *extra, cwd=tmp_path)
    assert resumed.returncode != 0
    assert "os:makedirs" in resumed.stderr
    assert "--adapter-path" in resumed.stderr
    assert not marker.exists()
    assert (job_dir / "job.json").read_bytes() == before


@pytest.mark.parametrize("unrelated_path", [False, True])
def test_resume_of_a_received_run_never_imports_its_adapter(tmp_path, unrelated_path):
    marker = tmp_path / "PWNED"
    run_dir = _evil_run(tmp_path, marker)
    (tmp_path / "mine").mkdir()
    extra = ["--adapter-path", str(tmp_path / "mine")] if unrelated_path else []
    resumed = _cli("resume", str(run_dir), *extra, cwd=tmp_path)
    assert resumed.returncode != 0
    assert "os:makedirs" in resumed.stderr
    assert not marker.exists()


@pytest.mark.parametrize("unrelated_path", [False, True])
def test_replay_of_a_received_bundle_never_imports_its_adapter(tmp_path, unrelated_path):
    marker = tmp_path / "PWNED"
    bundle = _evil_bundle(tmp_path, marker)
    (tmp_path / "mine").mkdir()
    extra = ["--adapter-path", str(tmp_path / "mine")] if unrelated_path else []
    replayed = _cli("replay", str(bundle), "--out", str(tmp_path / "again"), *extra, cwd=tmp_path)
    assert replayed.returncode != 0
    assert "os:makedirs" in replayed.stderr
    assert not marker.exists()
    assert not (tmp_path / "again").exists()


def test_a_refused_job_resumes_once_its_adapter_folder_is_named(tmp_path):
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "own_reader.py").write_text(
        "from pageledger.adapters import TextAdapter\nclass Adapter(TextAdapter):\n    pass\n",
        encoding="utf-8",
    )
    job_dir = _evil_job(tmp_path, tmp_path / "unused", "own_reader:Adapter")
    before = (job_dir / "job.json").read_bytes()
    refused = _cli("resume", str(job_dir), cwd=tmp_path)
    assert refused.returncode != 0
    assert "own_reader:Adapter" in refused.stderr
    assert (job_dir / "job.json").read_bytes() == before
    resumed = _cli("resume", str(job_dir), "--adapter-path", str(mine), cwd=tmp_path)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads((job_dir / "job.json").read_text())["payload"]["status"] == "completed"


def test_code_inside_the_record_is_refused_even_under_adapter_path(tmp_path):
    marker = tmp_path / "PWNED"
    job_dir = _evil_job(tmp_path, marker, "planted:Adapter")
    (job_dir / "planted.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).mkdir()\n", encoding="utf-8"
    )
    resumed = _cli("resume", str(job_dir), "--adapter-path", str(job_dir), cwd=tmp_path)
    assert resumed.returncode != 0
    assert "planted:Adapter" in resumed.stderr
    assert not marker.exists()
