"""One-process benchmark measurement, refusal, and evidence tests."""

from __future__ import annotations

import json
import pstats
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from pageledger.verify import verify_run
from scripts.pageledger_bench import measure as measure_module
from scripts.pageledger_bench.measure import BenchmarkError, measure_run
from scripts.pageledger_bench.oracle import validate_run
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


def test_measure_small_smoke_records_complete_receipt_and_external_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload = _small_workload(tmp_path)
    out = tmp_path / "measurement"
    command = ["python", "-m", "scripts.pageledger_bench", "run", "--workload", "smoke"]
    evidence_order: list[str] = []

    def getrusage(_who: int):
        evidence_order.append("rss")
        return SimpleNamespace(ru_maxrss=123)

    def validate(run_dir: Path, prepared: WorkloadSpec):
        evidence_order.append("oracle")
        return _smoke_validator(run_dir, prepared)

    monkeypatch.setattr(measure_module.resource, "getrusage", getrusage)

    receipt = measure_run(
        "smoke",
        out,
        process_state="fresh-process",
        command=command,
        _prepared_workload=workload,
        _oracle_validator=validate,
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
    assert evidence_order == ["rss", "oracle"]
    assert receipt["output"]["regular_file_count"] == sum(
        1 for path in (out / "run").rglob("*") if path.is_file()
    )
    assert receipt["output"]["logical_bytes"] == sum(
        path.stat().st_size for path in (out / "run").rglob("*") if path.is_file()
    )
    assert receipt["output"]["logical_bytes"] <= receipt["output"]["cap_bytes"]
    assert receipt["output"]["workspace_pre_receipt"]["logical_bytes"] <= (
        receipt["output"]["cap_bytes"]
    )
    assert receipt["output"]["workspace_pre_receipt"]["regular_file_count"] >= (
        receipt["output"]["regular_file_count"] + 2
    )
    assert receipt["validation"]["verify_report"]["status"] == "pass"
    assert receipt["validation"]["oracle"]["valid"] is True
    assert receipt["validation"]["schema_state"]["status"] == "matched"

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
    assert not any(path.name.startswith("measurement") for path in (out / "run").rglob("*"))
    assert not (out / "run" / "profile.pstats").exists()
    assert (out / "run" / "manifest.json").stat().st_mtime_ns == max(
        path.stat().st_mtime_ns for path in (out / "run").rglob("*") if path.is_file()
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
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(BenchmarkError, match="does not already exist"):
        measure_run("primary", existing, process_state="fresh-process")

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(BenchmarkError, match="symlink"):
        measure_run("primary", link / "child", process_state="fresh-process")


def test_refuses_protected_hash_drift_before_creating_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    protected = repository / "frozen.txt"
    protected.parent.mkdir()
    protected.write_text("changed", encoding="utf-8")
    monkeypatch.setattr(measure_module, "_REPOSITORY_ROOT", repository)
    monkeypatch.setattr(measure_module, "_git_blob_bytes", lambda relative: b"frozen")
    out = tmp_path / "measurement"

    with pytest.raises(BenchmarkError, match="protected benchmark path drift"):
        measure_module._protected_hashes(["frozen.txt"])

    assert not out.exists()


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
    from scripts.pageledger_bench.__main__ import main

    out = tmp_path / "result"
    argv = ["run", "--workload", "generalization", "--out", str(out), "--process-state", "warm"]
    assert main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "pass"
    assert seen["workload"] == "generalization"
    assert seen["out"] == out
    assert seen["kwargs"]["process_state"] == "warm"
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


def test_timing_contract_refuses_missing_phases_or_adapter_calls() -> None:
    complete = {
        "adapter": {"count": 2},
        "ledger": {"phases": {phase: {"count": 1, "total_ns": 1} for phase in EXPECTED_LEDGER_PHASES}},
    }
    measure_module._require_timing_contract(complete, pages=2)

    missing = json.loads(json.dumps(complete))
    missing["ledger"]["phases"].pop("quality")
    with pytest.raises(BenchmarkError, match="direct phase set"):
        measure_module._require_timing_contract(missing, pages=2)

    wrong_adapter_count = json.loads(json.dumps(complete))
    wrong_adapter_count["adapter"]["count"] = 1
    with pytest.raises(BenchmarkError, match="adapter span count"):
        measure_module._require_timing_contract(wrong_adapter_count, pages=2)
