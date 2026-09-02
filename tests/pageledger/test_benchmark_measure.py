"""One-process benchmark measurement, refusal, and evidence tests."""

from __future__ import annotations

import json
import mmap
import os
import posix
import pstats
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from pageledger.verify import verify_run
from scripts.pageledger_bench import measure as measure_module
from scripts.pageledger_bench.measure import BenchmarkError, measure_run
from scripts.pageledger_bench.oracle import compare_runs, validate_run
from scripts.pageledger_bench.workloads import (
    PageSpec,
    WorkloadSpec,
    generate_workload,
    sha256_path,
)

EXPECTED_LEDGER_PHASES = {
    "plan_setup",
    "page_control",
    "result_validation",
    "raw_artifact",
    "usage_budget_provenance",
    "quality",
    "alignment",
    "page_log_control",
    "halt_accounting_route",
    "grading",
    "policy_queues",
    "models",
    "audit_write",
    "ledger_jsonl_write",
    "cost_build_write",
    "rerun_build_write",
    "runlog_build_write",
    "manifest_commit",
    "result_return",
}

_CACHED_OS_DUP = os.dup
_CACHED_OS_WRITE = os.write
_NATIVE_POSIX_DUP = posix.dup
_NATIVE_POSIX_WRITE = posix.write


def _approved_freeze(tmp_path: Path):
    receipt_path = tmp_path / "approved-freeze.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    return SimpleNamespace(
        harness_sha="d" * 40,
        benchmark_manifest_sha256=sha256_path(
            Path(measure_module.__file__).with_name("benchmark_manifest.json")
        ),
        protected_paths_sha256={
            path: "a" * 64 for path in measure_module.REQUIRED_FREEZE_PATHS
        },
        free_space_floor_bytes=0,
        lock_path=(tmp_path / "pageledger-benchmark.lock").resolve(),
        receipt_path=receipt_path.resolve(),
        receipt_sha256=sha256_path(receipt_path),
    )


def _small_workload(tmp_path: Path, pages: int = 20) -> WorkloadSpec:
    """Build a private smoke fixture; production names remain manifest-frozen."""
    root = tmp_path / "prepared" / "smoke"
    root.mkdir(parents=True)
    page_specs = tuple(
        PageSpec(
            content=f"small deterministic page {number:04d}",
            format="text",
            confidence=0.99,
            warnings=(),
            tokens=1,
            cost_usd=0.0,
            category="clean-control",
        )
        for number in range(1, pages + 1)
    )
    source = root / "source.txt"
    config = root / "pageledger.yml"
    membership = root / "membership.json"
    source.write_text("\f".join(f"source {number}" for number in range(1, pages + 1)), encoding="utf-8")
    config.write_text(
        """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: zero-work
""",
        encoding="utf-8",
    )
    members = tuple(
        {
            "category": page.category,
            "confidence": page.confidence,
            "content": page.content,
            "cost_usd": page.cost_usd,
            "format": page.format,
            "page_number": index,
            "tokens": page.tokens,
            "warnings": list(page.warnings),
        }
        for index, page in enumerate(page_specs, 1)
    )
    membership.write_text(
        json.dumps({"generator_version": "test", "workload": "smoke", "pages": members})
        + "\n",
        encoding="utf-8",
    )
    return WorkloadSpec(
        name="smoke",
        page_specs=page_specs,
        source_path=source,
        config_path=config,
        membership_path=membership,
        membership=members,
        generated_paths={
            "source.txt": source,
            "pageledger.yml": config,
            "membership.json": membership,
        },
        expected={},
    )


def _smoke_validator(run_dir: Path, workload: WorkloadSpec):
    report = verify_run(run_dir)
    return SimpleNamespace(
        valid=report["status"] == "pass",
        errors=(),
        verifier_report=report,
        canonical={"smoke_pages": len(workload.page_specs)},
    )


def _smoke_equivalence(control: Path, trace: Path, workload: WorkloadSpec):
    left = _smoke_validator(control, workload)
    right = _smoke_validator(trace, workload)
    return SimpleNamespace(
        equivalent=left.valid and right.valid and left.canonical == right.canonical,
        errors=(),
        control=left,
        candidate=right,
    )


def _stable_boundary(_approval: object) -> dict[str, object]:
    return {"sha": "f" * 40, "branch": "test", "tracked_dirty": False}


def test_measure_small_smoke_records_complete_receipt_and_external_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload = _small_workload(tmp_path)
    out = tmp_path / "measurement"
    command = ["python", "-m", "scripts.pageledger_bench", "run", "--workload", "smoke"]
    evidence_order: list[str] = []
    runner_controls: list[tuple[Path, bool]] = []
    real_runner = measure_module.DEFAULT_RUNNER

    def getrusage(_who: int):
        evidence_order.append("rss")
        return SimpleNamespace(ru_maxrss=123)

    def validate(run_dir: Path, prepared: WorkloadSpec):
        evidence_order.append("oracle")
        return _smoke_validator(run_dir, prepared)

    def record_runner_controls(**kwargs: object):
        runner_controls.append(
            (Path(kwargs["out_dir"]), kwargs.get("_phase_observer") is not None)
        )
        return real_runner(**kwargs)

    monkeypatch.setattr(measure_module.resource, "getrusage", getrusage)
    approval = _approved_freeze(tmp_path)

    receipt = measure_run(
        "smoke",
        out,
        approved_freeze=approval,
        warmup_state="none",
        command=command,
        _prepared_workload=workload,
        _oracle_validator=validate,
        _equivalence_validator=_smoke_equivalence,
        _freeze_validator=lambda supplied: supplied,
        _boundary_validator=_stable_boundary,
        _runner=record_runner_controls,
    )

    assert receipt["receipt_version"] == 1
    assert receipt["command"] == command
    assert receipt["workload"]["name"] == "smoke"
    assert receipt["workload"]["page_count"] == 20
    assert receipt["workload"]["category_counts"] == {"clean-control": 20}
    assert set(receipt["workload"]["generated_sha256"]) == {
        "source.txt",
        "pageledger.yml",
        "membership.json",
    }
    assert set(receipt["frozen_hashes"]) == {
        "benchmark_manifest_sha256",
        "schema_sha256",
        "recipe_sha256",
        "workloads_sha256",
        "protected_paths_sha256",
    }
    assert len(receipt["git"]["sha"]) == 40
    assert set(receipt["host"]) >= {
        "os",
        "kernel",
        "cpu",
        "machine",
        "ram_bytes",
        "python",
        "dependencies",
        "filesystem",
        "temp_volume",
        "power_mode",
        "process_state",
        "os_page_cache",
    }
    assert receipt["host"]["os_page_cache"] == "uncontrolled"
    assert receipt["host"]["process_state"] == "fresh-process"
    assert receipt["host"]["warmup_state"] == "none"
    assert set(receipt["host"]) >= {"background_load", "thermal_state", "hardware"}
    assert set(receipt["host"]["runtime_content_sha256"]) >= {
        "python_executable",
        "pageledger_package",
        "PyYAML",
        "jsonschema",
    }
    assert receipt["host"]["power_mode"].get("mode") or receipt["host"]["power_mode"].get(
        "unavailable_reason"
    )

    timing = receipt["timing"]
    assert timing["clock"] == "time.perf_counter_ns"
    assert timing["total_wall_ns"] > 0
    assert timing["adapter"]["phase"] == "adapter_call"
    assert timing["adapter"]["count"] == 20
    assert EXPECTED_LEDGER_PHASES == set(timing["ledger"]["phases"])
    assert all(
        set(span) == {"count", "total_ns"}
        for span in timing["ledger"]["phases"].values()
    )
    assert timing["observer_total_ns"] == (
        timing["adapter"]["total_ns"] + timing["ledger"]["total_ns"]
    )
    assert timing["observer_total_ns"] + timing["unaccounted_ns"] <= (
        timing["total_wall_ns"] + timing["clock_granularity_ns"]
    )
    assert timing["unaccounted_ratio"] == pytest.approx(
        timing["unaccounted_ns"] / timing["total_wall_ns"]
    )
    assert timing["pages_per_second"] == pytest.approx(
        20 * 1_000_000_000 / timing["total_wall_ns"]
    )
    assert timing["ledger"]["ns_per_page"] == pytest.approx(
        timing["ledger"]["total_ns"] / 20
    )

    assert receipt["resources"]["peak_rss_bytes"] > 0
    assert evidence_order == ["rss", "oracle", "oracle"]
    assert runner_controls == [(out / "run", True), (out / "run", True)]
    assert receipt["output"]["runner_out_dir"] == str(out / "run")
    assert receipt["output"]["measured_control_run_dir"] == str(out / "control-run")
    assert receipt["output"]["control_relocation"] == {
        "from": str(out / "run"),
        "to": str(out / "control-run"),
        "atomic": True,
        "timed": False,
        "profiled": False,
    }
    assert receipt["output"]["regular_file_count"] == sum(
        1 for path in (out / "control-run").rglob("*") if path.is_file()
    )
    assert receipt["output"]["logical_bytes"] == sum(
        path.stat().st_size
        for path in (out / "control-run").rglob("*")
        if path.is_file()
    )
    assert receipt["output"]["logical_bytes"] <= receipt["output"]["cap_bytes"]
    assert receipt["output"]["free_space_floor_bytes"] == 0
    assert receipt["output"]["workspace_pre_receipt"]["logical_bytes"] <= (
        receipt["output"]["cap_bytes"]
    )
    assert receipt["output"]["workspace_pre_receipt"]["regular_file_count"] >= (
        receipt["output"]["regular_file_count"] + 2
    )
    assert receipt["validation"]["verify_report"]["status"] == "pass"
    assert receipt["validation"]["oracle"]["valid"] is True
    assert receipt["validation"]["schema_state"]["status"] == "matched"
    assert receipt["approved_freeze"]["harness_sha"] == approval.harness_sha
    assert receipt["benchmark_lock"]["path"] == str(approval.lock_path)
    assert receipt["benchmark_lock"]["acquired"] is True
    assert receipt["run_mutations"]["source"] == "untimed-trace-replay"
    assert receipt["run_mutations"]["events"][-1]["path"] == "manifest.json"
    assert receipt["trace_validation"]["timed"] is False
    assert receipt["trace_validation"]["profiled"] is False
    assert receipt["trace_validation"]["runner_calls"] == 1
    assert receipt["trace_validation"]["phase_sequence"]["valid"] is True
    assert receipt["trace_validation"]["phase_sequence"]["event_count"] == 192
    assert receipt["trace_validation"]["phase_sequence"]["adapter_count"] == 20
    assert receipt["trace_validation"]["interposition_policy_version"] == "2.4"
    assert receipt["trace_validation"]["equivalence"]["equivalent"] is True
    assert receipt["trace_validation"]["validation"]["valid"] is True
    assert receipt["trace_validation"]["inventory"]["regular_file_count"] == sum(
        1 for path in (out / "run").rglob("*") if path.is_file()
    )

    for key in ("pstats", "text"):
        evidence = receipt["profile"][key]
        path = Path(evidence["path"])
        assert path.parent == out
        assert path.is_file()
        assert evidence["sha256"] == sha256_path(path)
    stats = pstats.Stats(receipt["profile"]["pstats"]["path"])
    runner_entries = [
        values
        for (filename, _line, function), values in stats.stats.items()
        if Path(filename).name == "runner.py" and function == "run"
    ]
    assert len(runner_entries) == 1
    assert runner_entries[0][:2] == (1, 1)
    assert not any(function == "measure_run" for _filename, _line, function in stats.stats)
    assert not any(
        function in {"_audit", "_record", "_completed"}
        or "MutationTrace" in function
        for _filename, _line, function in stats.stats
    )
    assert not any(
        path.name.startswith("measurement")
        for path in (out / "control-run").rglob("*")
    )
    assert not (out / "control-run" / "profile.pstats").exists()
    assert (out / "control-run" / "manifest.json").stat().st_mtime_ns == max(
        path.stat().st_mtime_ns
        for path in (out / "control-run").rglob("*")
        if path.is_file()
    )

    receipt_path = out / "measurement.json"
    sidecar = out / "measurement.json.sha256"
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt
    assert sidecar.read_text(encoding="ascii").strip() == sha256_path(receipt_path)
    assert not list(out.glob(".*.tmp-*"))
    payload = dict(receipt)
    payload_hash = payload.pop("receipt_payload_sha256")
    assert payload_hash == measure_module.canonical_sha256(payload)


def test_production_defaults_are_frozen_generator_and_strict_oracle() -> None:
    assert measure_module.DEFAULT_WORKLOAD_FACTORY is generate_workload
    assert measure_module.DEFAULT_ORACLE_VALIDATOR is validate_run
    assert measure_module.DEFAULT_EQUIVALENCE_VALIDATOR is compare_runs
    assert measure_module.DEFAULT_FREEZE_VALIDATOR is measure_module._validate_approved_freeze


def test_timing_arithmetic_uses_direct_spans_and_never_wall_minus_adapter() -> None:
    events = [
        ("plan_setup", 11),
        ("adapter_call", 7),
        ("page_control", 13),
        ("adapter_call", 5),
        ("manifest_commit", 17),
        ("result_return", 19),
    ]

    timing = measure_module._summarize_timing(events, total_wall_ns=100, pages=2)

    assert timing["adapter"] == {"phase": "adapter_call", "count": 2, "total_ns": 12}
    assert timing["ledger"]["total_ns"] == 60
    assert timing["ledger"]["ns_per_page"] == 30
    assert timing["observer_total_ns"] == 72
    assert timing["unaccounted_ns"] == 28
    assert timing["unaccounted_ratio"] == pytest.approx(0.28)
    assert timing["ledger"]["total_ns"] != 100 - 12
    assert timing["ledger"]["phases"] == {
        "manifest_commit": {"count": 1, "total_ns": 17},
        "page_control": {"count": 1, "total_ns": 13},
        "plan_setup": {"count": 1, "total_ns": 11},
        "result_return": {"count": 1, "total_ns": 19},
    }


def test_refuses_existing_output_and_output_symlink(tmp_path: Path) -> None:
    approval = _approved_freeze(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(BenchmarkError, match="does not already exist"):
        measure_run(
            "primary", existing, approved_freeze=approval, warmup_state="none"
        )

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(BenchmarkError, match="symlink"):
        measure_run(
            "primary", link / "child", approved_freeze=approval, warmup_state="none"
        )


def test_refuses_projected_low_disk_and_actual_cap(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkError, match="projected output"):
        measure_module._check_capacity(
            tmp_path,
            projected_bytes=11,
            cap_bytes=10,
            free_bytes=100,
        )
    with pytest.raises(BenchmarkError, match="free disk"):
        measure_module._check_capacity(
            tmp_path,
            projected_bytes=10,
            cap_bytes=100,
            free_bytes=9,
        )

    root = tmp_path / "actual"
    root.mkdir()
    (root / "large").write_bytes(b"12345")
    with pytest.raises(BenchmarkError, match="actual output"):
        measure_module._regular_file_inventory(root, cap_bytes=4)


def test_inventory_refuses_symlinks_and_counts_only_regular_files(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    (root / "regular").write_bytes(b"123")
    outside = tmp_path / "outside"
    outside.write_bytes(b"do not count")
    (root / "linked").symlink_to(outside)

    with pytest.raises(BenchmarkError, match="symlink"):
        measure_module._regular_file_inventory(root, cap_bytes=100)


def test_cli_forwards_exact_command_and_refusal_is_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def fake_measure(workload_name: str, out_dir: Path, **kwargs: object):
        seen.update(workload=workload_name, out=out_dir, kwargs=kwargs)
        return {"status": "pass", "receipt": str(out_dir / "measurement.json")}

    monkeypatch.setattr(measure_module, "measure_run", fake_measure)
    approval = _approved_freeze(tmp_path)
    monkeypatch.setattr(measure_module, "load_approved_freeze", lambda _path: approval)
    from scripts.pageledger_bench.__main__ import main

    out = tmp_path / "result"
    argv = [
        "run",
        "--workload",
        "generalization",
        "--out",
        str(out),
        "--freeze-receipt",
        str(approval.receipt_path),
        "--warmup-state",
        "post-untimed-warmup",
    ]
    assert main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "pass"
    assert seen["workload"] == "generalization"
    assert seen["out"] == out
    assert seen["kwargs"]["approved_freeze"] is approval
    assert seen["kwargs"]["warmup_state"] == "post-untimed-warmup"
    assert seen["kwargs"]["command"][-len(argv) :] == argv

    def refuse(*args: object, **kwargs: object):
        raise BenchmarkError("output_exists", "measurement output does not already exist")

    monkeypatch.setattr(measure_module, "measure_run", refuse)
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["code"] == "output_exists"
    assert "output does not already exist" in captured.err


def test_rss_normalization_is_platform_correct() -> None:
    assert measure_module._normalize_peak_rss(123, "Darwin") == (123, "bytes")
    assert measure_module._normalize_peak_rss(123, "Linux") == (123 * 1024, "KiB")


def test_phase_counts_match_emitted_events() -> None:
    events = [("page_control", 1), ("page_control", 2), ("adapter_call", 3)]
    timing = measure_module._summarize_timing(events, total_wall_ns=10, pages=1)
    counts = Counter(name for name, _ in events)

    assert timing["ledger"]["phases"]["page_control"]["count"] == counts["page_control"]
    assert timing["adapter"]["count"] == counts["adapter_call"]


def test_timing_contract_requires_exact_counts_order_and_unaccounted_ceiling() -> None:
    expected = measure_module._expected_phase_names(2)
    complete = [(name, 1) for name in expected]
    timing = measure_module._summarize_timing(complete, total_wall_ns=len(complete), pages=2)
    measure_module._require_timing_contract(
        complete, timing, pages=2, max_unaccounted_ratio=0.05
    )

    wrong_per_page = complete.copy()
    wrong_per_page.pop(wrong_per_page.index(("quality", 1)))
    wrong_timing = measure_module._summarize_timing(
        wrong_per_page, total_wall_ns=len(wrong_per_page), pages=2
    )
    with pytest.raises(BenchmarkError, match="phase event sequence"):
        measure_module._require_timing_contract(
            wrong_per_page, wrong_timing, pages=2, max_unaccounted_ratio=0.05
        )

    wrong_order = complete.copy()
    wrong_order[1], wrong_order[2] = wrong_order[2], wrong_order[1]
    with pytest.raises(BenchmarkError, match="phase event sequence"):
        measure_module._require_timing_contract(
            wrong_order, timing, pages=2, max_unaccounted_ratio=0.05
        )

    high_gap = measure_module._summarize_timing(complete, total_wall_ns=1000, pages=2)
    with pytest.raises(BenchmarkError, match="unaccounted timing ratio"):
        measure_module._require_timing_contract(
            complete, high_gap, pages=2, max_unaccounted_ratio=0.05
        )


def test_approved_freeze_refuses_manifest_harness_and_dirty_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    manifest = repository / "manifest.json"
    frozen = repository / "frozen.py"
    manifest.write_bytes(b"manifest\n")
    frozen.write_bytes(b"frozen\n")
    manifest_hash = sha256_path(manifest)
    frozen_hash = sha256_path(frozen)
    approval = SimpleNamespace(
        harness_sha="1" * 40,
        benchmark_manifest_sha256=manifest_hash,
        protected_paths_sha256={"frozen.py": frozen_hash},
        free_space_floor_bytes=0,
        lock_path=tmp_path / "lock",
        receipt_path=tmp_path / "freeze.json",
        receipt_sha256="2" * 64,
    )
    monkeypatch.setattr(measure_module, "_REPOSITORY_ROOT", repository)
    monkeypatch.setattr(measure_module, "_MANIFEST_PATH", manifest)
    monkeypatch.setattr(measure_module, "REQUIRED_FREEZE_PATHS", frozenset({"frozen.py"}))
    monkeypatch.setattr(measure_module, "_git_commit_exists", lambda sha: True)
    monkeypatch.setattr(measure_module, "_git_is_ancestor", lambda ancestor, head: True)
    monkeypatch.setattr(
        measure_module,
        "_git_blob_bytes_at",
        lambda sha, path: b"manifest\n" if path == "manifest.json" else b"frozen\n",
    )
    monkeypatch.setattr(measure_module, "_git_status_porcelain", lambda: "")
    assert (
        measure_module._validate_approved_freeze(approval, candidate_head="2" * 40)
        is approval
    )

    manifest.write_bytes(b"drift\n")
    with pytest.raises(BenchmarkError, match="approved benchmark manifest"):
        measure_module._validate_approved_freeze(approval, candidate_head="2" * 40)
    manifest.write_bytes(b"manifest\n")

    monkeypatch.setattr(
        measure_module,
        "_git_blob_bytes_at",
        lambda sha, path: b"manifest\n" if path == "manifest.json" else b"changed\n",
    )
    with pytest.raises(BenchmarkError, match="approved harness commit bytes"):
        measure_module._validate_approved_freeze(approval, candidate_head="2" * 40)
    monkeypatch.setattr(
        measure_module,
        "_git_blob_bytes_at",
        lambda sha, path: b"manifest\n" if path == "manifest.json" else b"frozen\n",
    )
    monkeypatch.setattr(measure_module, "_git_status_porcelain", lambda: " M pageledger/runner.py")
    with pytest.raises(BenchmarkError, match="tracked-dirty"):
        measure_module._validate_approved_freeze(approval, candidate_head="2" * 40)


def test_candidate_boundary_requires_approved_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval = _approved_freeze(tmp_path)
    monkeypatch.setattr(
        measure_module,
        "_git_fingerprint",
        lambda: {"sha": "e" * 40, "branch": "candidate", "tracked_dirty": False},
    )
    monkeypatch.setattr(
        measure_module, "_validate_approved_freeze", lambda value, **_kwargs: value
    )
    monkeypatch.setattr(measure_module, "_git_is_ancestor", lambda ancestor, head: False)

    with pytest.raises(BenchmarkError, match="not an ancestor"):
        measure_module._capture_candidate_boundary(approval)


@pytest.mark.parametrize(
    ("final_boundary", "message"),
    [
        (
            {"sha": "f" * 40, "branch": "test", "tracked_dirty": True},
            "tracked-dirty",
        ),
        (
            {"sha": "e" * 40, "branch": "test", "tracked_dirty": False},
            "changed across measured run",
        ),
    ],
)
def test_measurement_boundary_refuses_dirty_race_or_head_change(
    tmp_path: Path,
    final_boundary: dict[str, object],
    message: str,
) -> None:
    workload = _small_workload(tmp_path, pages=1)
    approval = _approved_freeze(tmp_path)
    clean = {"sha": "f" * 40, "branch": "test", "tracked_dirty": False}
    states = iter([clean, clean, final_boundary])

    with pytest.raises(BenchmarkError, match=message):
        measure_run(
            "smoke",
            tmp_path / "measurement",
            approved_freeze=approval,
            warmup_state="none",
            _prepared_workload=workload,
            _oracle_validator=_smoke_validator,
            _equivalence_validator=_smoke_equivalence,
            _freeze_validator=lambda supplied: supplied,
            _boundary_validator=lambda _supplied: next(states),
        )

    assert not (tmp_path / "measurement" / "measurement.json").exists()


def test_freeze_receipt_requires_exact_external_contract(tmp_path: Path) -> None:
    path = tmp_path / "freeze.json"
    payload = {
        "harness_sha": "1" * 40,
        "benchmark_manifest_sha256": "2" * 64,
        "protected_paths_sha256": {
            relative: "3" * 64 for relative in measure_module.REQUIRED_FREEZE_PATHS
        },
        "free_space_floor_bytes": 1024,
        "lock_path": str((tmp_path / "lock").resolve()),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    approval = measure_module.load_approved_freeze(path)
    assert approval.free_space_floor_bytes == 1024
    assert approval.receipt_sha256 == sha256_path(path)

    payload["unexpected"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BenchmarkError, match="exactly"):
        measure_module.load_approved_freeze(path)

    payload.pop("unexpected")
    payload["free_space_floor_bytes"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BenchmarkError, match="positive integer"):
        measure_module.load_approved_freeze(path)


def test_evidence_root_must_be_outside_candidate_worktree(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkError, match="outside the candidate Git worktree"):
        measure_module._require_external_path(
            Path(measure_module.__file__).parent / "candidate-output"
        )
    measure_module._require_external_path(tmp_path / "external-output")


def test_capacity_reserves_predeclared_floor(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkError, match="free-space floor"):
        measure_module._check_capacity(
            tmp_path,
            projected_bytes=10,
            cap_bytes=100,
            free_bytes=59,
            free_space_floor_bytes=50,
        )


def test_receipt_last_publication_preflights_cap_and_never_leaves_pass_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "measurement.json"
    sidecar = tmp_path / "measurement.json.sha256"
    receipt_bytes = b'{"status":"pass"}\n'
    sidecar_bytes = b"a" * 65
    with pytest.raises(BenchmarkError, match="final evidence"):
        measure_module._publish_receipt(
            receipt,
            receipt_bytes,
            sidecar,
            sidecar_bytes,
            existing_workspace_bytes=10,
            cap_bytes=10 + len(receipt_bytes) + len(sidecar_bytes) - 1,
        )
    assert not receipt.exists()
    assert not sidecar.exists()

    original = measure_module._atomic_write_bytes

    def fail_sidecar(path: Path, value: bytes) -> None:
        raise OSError("sidecar failed")

    monkeypatch.setattr(measure_module, "_atomic_write_bytes", fail_sidecar)
    with pytest.raises(OSError, match="sidecar failed"):
        measure_module._publish_receipt(
            receipt,
            receipt_bytes,
            sidecar,
            sidecar_bytes,
            existing_workspace_bytes=0,
            cap_bytes=1000,
        )
    assert not receipt.exists()

    def fail_receipt(path: Path, value: bytes) -> None:
        if path == receipt:
            raise OSError("receipt replace failed")
        original(path, value)

    monkeypatch.setattr(measure_module, "_atomic_write_bytes", fail_receipt)
    with pytest.raises(OSError, match="receipt replace failed"):
        measure_module._publish_receipt(
            receipt,
            receipt_bytes,
            sidecar,
            sidecar_bytes,
            existing_workspace_bytes=0,
            cap_bytes=1000,
        )
    assert sidecar.exists()
    assert not receipt.exists()


@pytest.mark.parametrize("failure_point", ["file_fsync", "directory_fsync"])
def test_receipt_post_replace_failure_removes_visible_pass_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    receipt = tmp_path / "measurement.json"
    sidecar = tmp_path / "measurement.json.sha256"
    receipt_bytes = b'{"status":"pass"}\n'
    sidecar_bytes = b"a" * 65
    real_fsync_file = measure_module._fsync_file
    real_fsync_directory = measure_module._fsync_directory
    directory_calls = 0

    def fsync_file(path: Path) -> None:
        if failure_point == "file_fsync" and path == receipt:
            raise OSError("receipt file fsync failed after replace")
        real_fsync_file(path)

    def fsync_directory(path: Path) -> None:
        nonlocal directory_calls
        directory_calls += 1
        if failure_point == "directory_fsync" and directory_calls == 2:
            raise OSError("receipt directory fsync failed after replace")
        real_fsync_directory(path)

    monkeypatch.setattr(measure_module, "_fsync_file", fsync_file)
    monkeypatch.setattr(measure_module, "_fsync_directory", fsync_directory)

    with pytest.raises(OSError, match="failed after replace"):
        measure_module._publish_receipt(
            receipt,
            receipt_bytes,
            sidecar,
            sidecar_bytes,
            existing_workspace_bytes=0,
            cap_bytes=1000,
        )

    assert sidecar.exists()
    assert not receipt.exists()


def test_benchmark_lock_is_nonblocking_and_records_identity(tmp_path: Path) -> None:
    lock_path = tmp_path / "benchmark.lock"
    with measure_module._benchmark_lock(lock_path) as identity:
        assert identity["acquired"] is True
        assert identity["path"] == str(lock_path)
        with pytest.raises(BenchmarkError, match="already held"):
            with measure_module._benchmark_lock(lock_path):
                pass


def test_manifest_last_uses_mutation_trace_not_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload = _small_workload(tmp_path, pages=1)
    out = tmp_path / "measurement"
    approval = _approved_freeze(tmp_path)
    real_runner = measure_module.DEFAULT_RUNNER
    def adversary(**kwargs: object):
        result = real_runner(**kwargs)
        run_dir = Path(kwargs["out_dir"])
        manifest = run_dir / "manifest.json"
        raw = next((run_dir / "raw").iterdir())
        raw.write_bytes(raw.read_bytes())
        stamp = manifest.stat().st_mtime_ns
        os.utime(raw, ns=(stamp, stamp))
        return result

    with pytest.raises(BenchmarkError, match="final trace mutation|unhandled mutation"):
        measure_run(
            "smoke",
            out,
            approved_freeze=approval,
            warmup_state="none",
            _prepared_workload=workload,
            _oracle_validator=_smoke_validator,
            _equivalence_validator=_smoke_equivalence,
            _freeze_validator=lambda supplied: supplied,
            _boundary_validator=_stable_boundary,
            _runner=adversary,
        )


def test_completed_trace_rejects_preopened_descriptor_at_manifest_boundary(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "trace-run"
    raw = run_dir / "raw.txt"
    trace = measure_module._CompletedMutationTrace(run_dir)
    handle = None

    with pytest.raises(
        BenchmarkError, match=r"live descriptor alias.*raw\.txt"
    ) as exc_info:
        try:
            with trace:
                run_dir.mkdir()
                handle = raw.open("w", encoding="utf-8")
                handle.write("before manifest")
                handle.flush()
                assert not any(event["path"] == "raw.txt" for event in trace.events)
                (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
                handle.seek(0)
                handle.write("after manifest")
        finally:
            if handle is not None and not handle.closed:
                handle.close()

    assert exc_info.value.code == "trace_descriptor_alias"
    assert raw.read_text(encoding="utf-8") == "before manifest"


def test_completed_trace_rejects_writable_mmap_created_before_manifest(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "mmap"
    artifact = run_dir / "artifact.bin"
    trace = measure_module._CompletedMutationTrace(run_dir)
    handle = None
    mapping = None

    with pytest.raises(
        BenchmarkError, match=r"live descriptor alias.*artifact\.bin"
    ) as exc_info:
        try:
            with trace:
                run_dir.mkdir()
                handle = artifact.open("w+b")
                handle.write(b"before")
                handle.flush()
                mapping = mmap.mmap(handle.fileno(), 6, access=mmap.ACCESS_WRITE)
                assert trace._unhandled == [
                    "unknown filesystem audit event mmap.__new__:artifact.bin"
                ]
                handle.close()
                handle = None
                (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
                mapping[:] = b"after!"
        finally:
            if mapping is not None:
                mapping.close()
            if handle is not None and not handle.closed:
                handle.close()

    assert exc_info.value.code == "trace_descriptor_alias"
    assert artifact.read_bytes() == b"before"


def test_completed_trace_rejects_direct_os_dup_alias_at_manifest_boundary(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "dup-mmap"
    artifact = run_dir / "artifact.bin"
    trace = measure_module._CompletedMutationTrace(run_dir)
    original_dup = os.dup
    handle = None
    duplicate = None

    with pytest.raises(
        BenchmarkError, match=r"live descriptor alias.*artifact\.bin"
    ) as exc_info:
        try:
            with trace:
                assert os.dup is original_dup
                run_dir.mkdir()
                handle = artifact.open("w+b")
                handle.write(b"before")
                handle.flush()
                duplicate = os.dup(handle.fileno())
                handle.close()
                handle = None
                (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
        finally:
            if handle is not None and not handle.closed:
                handle.close()
            if duplicate is not None:
                os.close(duplicate)

    assert exc_info.value.code == "trace_descriptor_alias"
    assert os.dup is original_dup
    assert artifact.read_bytes() == b"before"


@pytest.mark.parametrize(
    "duplicate_descriptor",
    [
        pytest.param(_CACHED_OS_DUP, id="cached-os-dup"),
        pytest.param(_NATIVE_POSIX_DUP, id="native-posix-dup"),
    ],
)
@pytest.mark.skipif(
    sys.version_info < (3, 13),
    reason="mmap trackfd=False is required to close every alias before manifest",
)
def test_completed_trace_rejects_alias_backed_mmap_after_descriptor_cleanup(
    tmp_path: Path, duplicate_descriptor: Callable[[int], int]
) -> None:
    run_dir = tmp_path / "alias-mmap"
    artifact = run_dir / "artifact.bin"
    trace = measure_module._CompletedMutationTrace(run_dir)
    handle = None
    duplicate = None
    mapping = None

    with pytest.raises(
        BenchmarkError, match=r"mmap\.__new__:artifact\.bin"
    ) as exc_info:
        try:
            with trace:
                run_dir.mkdir()
                handle = artifact.open("w+b")
                handle.write(b"before")
                handle.flush()
                duplicate = duplicate_descriptor(handle.fileno())
                mapping = mmap.mmap(
                    duplicate, 6, access=mmap.ACCESS_WRITE, trackfd=False
                )
                os.close(duplicate)
                duplicate = None
                handle.close()
                handle = None
                (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
                mapping[:] = b"after!"
                mapping.flush()
                mapping.close()
                mapping = None
            measure_module._require_manifest_last(trace.events)
        finally:
            if mapping is not None:
                mapping.close()
            if handle is not None and not handle.closed:
                handle.close()
            if duplicate is not None:
                os.close(duplicate)

    assert exc_info.value.code == "trace_unhandled_mutation"
    assert artifact.read_bytes() == b"after!"


@pytest.mark.parametrize(
    ("duplicate_descriptor", "raw_write"),
    [
        pytest.param(_CACHED_OS_DUP, _CACHED_OS_WRITE, id="cached-os"),
        pytest.param(_NATIVE_POSIX_DUP, _NATIVE_POSIX_WRITE, id="native-posix"),
    ],
)
def test_completed_trace_rejects_live_alias_before_post_manifest_raw_write(
    tmp_path: Path,
    duplicate_descriptor: Callable[[int], int],
    raw_write: Callable[[int, bytes], int],
) -> None:
    run_dir = tmp_path / "alias-write"
    artifact = run_dir / "artifact.bin"
    trace = measure_module._CompletedMutationTrace(run_dir)
    handle = None
    duplicate = None

    with pytest.raises(
        BenchmarkError, match=r"live descriptor alias.*artifact\.bin"
    ) as exc_info:
        try:
            with trace:
                run_dir.mkdir()
                handle = artifact.open("w+b")
                handle.write(b"before")
                handle.flush()
                duplicate = duplicate_descriptor(handle.fileno())
                handle.close()
                handle = None
                (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
                raw_write(duplicate, b"after!")
            measure_module._require_manifest_last(trace.events)
        finally:
            if handle is not None and not handle.closed:
                handle.close()
            if duplicate is not None:
                os.close(duplicate)

    assert exc_info.value.code == "trace_descriptor_alias"
    assert artifact.read_bytes() == b"before"


@pytest.mark.parametrize("move", ["rename", "replace"])
@pytest.mark.parametrize(
    ("duplicate_descriptor", "raw_write"),
    [
        pytest.param(_CACHED_OS_DUP, _CACHED_OS_WRITE, id="cached-os"),
        pytest.param(_NATIVE_POSIX_DUP, _NATIVE_POSIX_WRITE, id="native-posix"),
    ],
)
def test_completed_trace_rejects_inbound_preopened_file_alias_after_move(
    tmp_path: Path,
    move: str,
    duplicate_descriptor: Callable[[int], int],
    raw_write: Callable[[int, bytes], int],
) -> None:
    run_dir = tmp_path / f"inbound-{move}"
    artifact = run_dir / "artifact.bin"
    external = tmp_path / f"external-{move}.bin"
    external.write_bytes(b"before")
    trace = measure_module._CompletedMutationTrace(run_dir)
    handle = external.open("r+b")
    duplicate = None

    with pytest.raises(
        BenchmarkError, match=r"live descriptor alias.*artifact\.bin"
    ) as exc_info:
        try:
            with trace:
                run_dir.mkdir()
                if move == "replace":
                    artifact.write_bytes(b"replaced")
                getattr(external, move)(artifact)
                duplicate = duplicate_descriptor(handle.fileno())
                handle.close()
                (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
                raw_write(duplicate, b"after!")
            measure_module._require_manifest_last(trace.events)
        finally:
            if not handle.closed:
                handle.close()
            if duplicate is not None:
                os.close(duplicate)

    assert exc_info.value.code == "trace_descriptor_alias"
    assert artifact.read_bytes() == b"before"


@pytest.mark.parametrize("move", ["rename", "replace"])
def test_completed_trace_updates_retained_identity_for_internal_move(
    tmp_path: Path, move: str
) -> None:
    run_dir = tmp_path / f"internal-{move}"
    source = run_dir / "source.bin"
    destination = run_dir / "destination.bin"
    trace = measure_module._CompletedMutationTrace(run_dir)

    with trace:
        run_dir.mkdir()
        source.write_bytes(b"source")
        if move == "replace":
            destination.write_bytes(b"destination")
        getattr(source, move)(destination)
        with destination.open("ab") as handle:
            handle.write(b"!")
        (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

    measure_module._require_manifest_last(trace.events)
    assert destination.read_bytes() == b"source!"


def test_completed_trace_refuses_ambiguous_inbound_hardlink_paths(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "inbound-hardlinks"
    first_external = tmp_path / "first-external.bin"
    second_external = tmp_path / "second-external.bin"
    first_external.write_bytes(b"shared")
    os.link(first_external, second_external)
    trace = measure_module._CompletedMutationTrace(run_dir)

    with pytest.raises(BenchmarkError, match="one tracked file identity") as exc_info:
        with trace:
            run_dir.mkdir()
            first_external.rename(run_dir / "first.bin")
            second_external.rename(run_dir / "second.bin")

    assert exc_info.value.code == "trace_file_identity_collision"


@pytest.mark.parametrize("departure", ["rename", "remove"])
def test_completed_trace_retains_departed_identity_for_live_alias(
    tmp_path: Path, departure: str
) -> None:
    run_dir = tmp_path / f"departed-{departure}"
    artifact = run_dir / "artifact.bin"
    trace = measure_module._CompletedMutationTrace(run_dir)
    duplicate = None

    with pytest.raises(
        BenchmarkError, match=r"live descriptor alias.*artifact\.bin"
    ) as exc_info:
        try:
            with trace:
                run_dir.mkdir()
                with artifact.open("w+b") as handle:
                    handle.write(b"before")
                    handle.flush()
                    duplicate = _CACHED_OS_DUP(handle.fileno())
                if departure == "rename":
                    artifact.rename(tmp_path / "departed.bin")
                else:
                    artifact.unlink()
                (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
        finally:
            if duplicate is not None:
                os.close(duplicate)

    assert exc_info.value.code == "trace_descriptor_alias"


def test_completed_trace_registers_copyfile_destination_identity(tmp_path: Path) -> None:
    run_dir = tmp_path / "copyfile"
    external = tmp_path / "copy-source.bin"
    artifact = run_dir / "artifact.bin"
    external.write_bytes(b"copied")
    trace = measure_module._CompletedMutationTrace(run_dir)

    with trace:
        run_dir.mkdir()
        measure_module.runner_module.copyfile(external, artifact)
        metadata = artifact.stat()
        assert trace._tracked_identities[(metadata.st_dev, metadata.st_ino)] == artifact
        (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

    measure_module._require_manifest_last(trace.events)


def test_completed_trace_preserves_dup2_keyword_signature_for_unrelated_fds(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "dup2-keywords"
    source = os.open(os.devnull, os.O_RDWR)
    destination = os.dup(source)

    try:
        with measure_module._CompletedMutationTrace(run_dir):
            run_dir.mkdir()
            assert (
                os.dup2(fd=source, fd2=destination, inheritable=False)
                == destination
            )
            os.fstat(destination)
            (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
    finally:
        os.close(destination)
        os.close(source)


@pytest.mark.parametrize(
    "operation",
    ["dup2"] + (["dup3"] if hasattr(os, "dup3") else []),
)
def test_completed_trace_resolves_descriptor_replacement_alias_by_identity(
    tmp_path: Path, operation: str
) -> None:
    run_dir = tmp_path / operation
    trace = measure_module._CompletedMutationTrace(run_dir)
    destination = os.open(os.devnull, os.O_RDWR)

    try:
        with pytest.raises(
            BenchmarkError, match=r"live descriptor alias.*source\.bin"
        ) as exc_info:
            with trace:
                run_dir.mkdir()
                with (run_dir / "source.bin").open("w+b") as source_handle:
                    source_handle.write(b"before")
                    source_handle.flush()
                    if operation == "dup2":
                        os.dup2(source_handle.fileno(), destination)
                    else:
                        os.dup3(source_handle.fileno(), destination, 0)
                (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
    finally:
        os.close(destination)

    assert exc_info.value.code == "trace_descriptor_alias"


@pytest.mark.parametrize("exceptional_exit", [False, True], ids=["normal", "exceptional"])
def test_completed_trace_never_interposes_descriptor_duplication_functions(
    tmp_path: Path, exceptional_exit: bool
) -> None:
    originals = {
        "dup": os.dup,
        "dup2": os.dup2,
        "dup3": getattr(os, "dup3", None),
    }
    trace = measure_module._CompletedMutationTrace(tmp_path / "restore")

    def assert_unchanged() -> None:
        assert os.dup is originals["dup"]
        assert os.dup2 is originals["dup2"]
        assert getattr(os, "dup3", None) is originals["dup3"]

    if exceptional_exit:
        with pytest.raises(RuntimeError, match="trace body failed"):
            with trace:
                assert_unchanged()
                raise RuntimeError("trace body failed")
    else:
        with trace:
            assert_unchanged()

    assert os.dup is originals["dup"]
    assert os.dup2 is originals["dup2"]
    assert getattr(os, "dup3", None) is originals["dup3"]


def test_completed_trace_permits_unrelated_descriptor_duplication(tmp_path: Path) -> None:
    run_dir = tmp_path / "unrelated-duplication"
    source = os.open(os.devnull, os.O_RDWR)
    destination = os.dup(source)
    duplicate = None

    try:
        with measure_module._CompletedMutationTrace(run_dir):
            run_dir.mkdir()
            duplicate = os.dup(source)
            os.fstat(duplicate)
            os.close(duplicate)
            duplicate = None
            assert os.dup2(source, destination) == destination
            os.fstat(destination)
            if hasattr(os, "dup3"):
                assert os.dup3(source, destination, 0) == destination
                os.fstat(destination)
            (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
    finally:
        if duplicate is not None:
            os.close(duplicate)
        os.close(destination)
        os.close(source)


def test_completed_trace_permits_provably_unrelated_writable_mmap(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "trace"
    external = tmp_path / "external.bin"
    external.write_bytes(b"before")
    descriptor = os.open(external, os.O_RDWR)
    duplicate = _NATIVE_POSIX_DUP(descriptor)
    mapping = None

    try:
        with measure_module._CompletedMutationTrace(run_dir):
            run_dir.mkdir()
            mapping = mmap.mmap(duplicate, 6, access=mmap.ACCESS_WRITE)
            (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
            mapping[:] = b"after!"
            mapping.flush()
            mapping.close()
            mapping = None
    finally:
        if mapping is not None:
            mapping.close()
        os.close(duplicate)
        os.close(descriptor)

    assert external.read_bytes() == b"after!"


def test_completed_trace_uses_identity_not_reused_descriptor_number(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "descriptor-reuse"
    trace = measure_module._CompletedMutationTrace(run_dir)
    replacement = None

    try:
        with trace:
            run_dir.mkdir()
            handle = (run_dir / "artifact.bin").open("w+b")
            tracked_descriptor = handle.fileno()
            handle.write(b"before")
            handle.close()

            replacement = os.open(os.devnull, os.O_RDWR)
            if replacement != tracked_descriptor:
                os.dup2(replacement, tracked_descriptor)
                os.close(replacement)
                replacement = tracked_descriptor
            resolved = trace._path_from_fd(replacement)
            assert resolved is None or not trace._inside(resolved)
            sys.audit("pageledger_bench.test_noise", replacement)
            os.close(replacement)
            replacement = None
            (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
    finally:
        if replacement is not None:
            os.close(replacement)


def test_completed_trace_refuses_failed_descriptor_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "scan-failure"

    def fail_scan(_path: object):
        raise OSError("descriptor namespace unavailable")

    monkeypatch.setattr(os, "scandir", fail_scan)

    with pytest.raises(BenchmarkError, match="could not enumerate live descriptors") as exc_info:
        with measure_module._CompletedMutationTrace(run_dir):
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

    assert exc_info.value.code == "trace_descriptor_scan_failed"


def test_completed_trace_refuses_incomplete_descriptor_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "scan-incomplete"

    class EmptyScan:
        def __enter__(self):
            return iter(())

        def __exit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(os, "scandir", lambda _path: EmptyScan())

    with pytest.raises(BenchmarkError, match="did not report its sentinel") as exc_info:
        with measure_module._CompletedMutationTrace(run_dir):
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

    assert exc_info.value.code == "trace_descriptor_scan_failed"


def test_completed_trace_refuses_process_creation(tmp_path: Path) -> None:
    run_dir = tmp_path / "process-creation"

    with pytest.raises(BenchmarkError, match="process creation") as exc_info:
        with measure_module._CompletedMutationTrace(run_dir):
            run_dir.mkdir()
            subprocess.run([sys.executable, "-c", "pass"], check=True)

    assert exc_info.value.code == "trace_process_creation"


def test_completed_trace_requires_sequential_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(threading, "active_count", lambda: 2)

    with pytest.raises(BenchmarkError, match="single-threaded") as exc_info:
        with measure_module._CompletedMutationTrace(tmp_path / "concurrent"):
            pass

    assert exc_info.value.code == "trace_concurrency_unsupported"


def test_completed_trace_preserves_fcntl_duplicate_refusal(tmp_path: Path) -> None:
    run_dir = tmp_path / "fcntl-duplication"
    artifact = run_dir / "artifact.bin"
    duplicate = None

    with pytest.raises(BenchmarkError, match=r"fcntl\.fcntl:artifact\.bin") as exc_info:
        try:
            with measure_module._CompletedMutationTrace(run_dir):
                run_dir.mkdir()
                with artifact.open("w+b") as handle:
                    duplicate = measure_module.fcntl.fcntl(
                        handle.fileno(), measure_module.fcntl.F_DUPFD, 0
                    )
        finally:
            if duplicate is not None:
                os.close(duplicate)

    assert exc_info.value.code == "trace_unhandled_mutation"


def test_completed_trace_permits_unrelated_unknown_audit_noise(tmp_path: Path) -> None:
    run_dir = tmp_path / "unknown-noise"

    with measure_module._CompletedMutationTrace(run_dir):
        run_dir.mkdir()
        sys.audit("pageledger_bench.test_noise", object())


def test_completed_trace_fails_on_unhandled_or_unclosed_writes(tmp_path: Path) -> None:
    unhandled_root = tmp_path / "unhandled"
    with pytest.raises(BenchmarkError, match="unhandled mutation mechanism"):
        with measure_module._CompletedMutationTrace(unhandled_root):
            unhandled_root.mkdir()
            with open(unhandled_root / "built-in.txt", "w", encoding="utf-8") as handle:
                handle.write("not interposed")

    unclosed_root = tmp_path / "unclosed"
    dangling = None
    with pytest.raises(BenchmarkError, match="unmatched write opens"):
        with measure_module._CompletedMutationTrace(unclosed_root):
            unclosed_root.mkdir()
            dangling = (unclosed_root / "open.txt").open("w", encoding="utf-8")
            dangling.write("not closed")
    assert dangling is not None
    dangling.close()


@pytest.mark.parametrize("mutation", ["hardlink", "symlink", "truncate", "metadata"])
def test_completed_trace_rejects_previously_unhandled_filesystem_mutations(
    tmp_path: Path, mutation: str
) -> None:
    run_dir = tmp_path / mutation
    external = tmp_path / f"{mutation}-source"
    external.write_text("outside", encoding="utf-8")

    with pytest.raises(BenchmarkError, match="unhandled mutation mechanism"):
        with measure_module._CompletedMutationTrace(run_dir):
            run_dir.mkdir()
            manifest = run_dir / "manifest.json"
            manifest.write_text("{}\n", encoding="utf-8")
            if mutation == "hardlink":
                directory_fd = os.open(run_dir, os.O_RDONLY)
                try:
                    os.link(external, "late-hardlink", dst_dir_fd=directory_fd)
                finally:
                    os.close(directory_fd)
            elif mutation == "symlink":
                os.symlink(external, run_dir / "late-symlink")
            elif mutation == "truncate":
                os.truncate(manifest, 1)
            else:
                os.chmod(manifest, 0o600)


def test_completed_trace_unknown_in_root_filesystem_event_is_default_denied(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "unknown"
    trace = measure_module._CompletedMutationTrace(run_dir)

    with pytest.raises(BenchmarkError, match="unknown filesystem audit event"):
        with trace:
            run_dir.mkdir()
            trace._audit("os.future_filesystem_mutation", (run_dir / "late",))


def test_subprocess_timeouts_are_bounded_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: object, **kwargs: object):
        raise subprocess.TimeoutExpired(cmd="probe", timeout=1)

    monkeypatch.setattr(measure_module.subprocess, "run", timeout)
    assert measure_module._optional_command(["ps"]) == {
        "available": False,
        "unavailable_reason": "command timed out after 5.0 seconds: ps",
    }
    with pytest.raises(BenchmarkError, match="timed out"):
        measure_module._git_text("status", "--porcelain")
