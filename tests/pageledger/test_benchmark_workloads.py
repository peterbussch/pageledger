"""Frozen deterministic PageLedger benchmark workload contracts."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from pageledger.runner import run
from scripts.pageledger_bench.workloads import (
    ZeroWorkAdapter,
    generate_workload,
    load_frozen_manifest,
    sha256_path,
)

EXPECTED_COUNTS = {
    "primary": {
        "pages": 5_000,
        "categories": {
            "structured": 1_600,
            "noisy": 1_200,
            "historical-multiscript": 1_200,
            "clean-control": 1_000,
        },
    },
    "generalization": {
        "pages": 1_000,
        "categories": {
            "structured": 300,
            "noisy": 250,
            "historical-multiscript": 250,
            "clean-control": 200,
        },
    },
}


@pytest.mark.parametrize("name", ["primary", "generalization"])
def test_frozen_workload_membership_and_expected_aggregates(name: str, tmp_path: Path) -> None:
    """Each benchmark workload has fixed ordered membership and receipts."""
    manifest = load_frozen_manifest()
    spec = generate_workload(name, tmp_path)
    expected = EXPECTED_COUNTS[name]

    assert len(spec.page_specs) == expected["pages"]
    assert Counter(page.category for page in spec.page_specs) == expected["categories"]
    assert [page.category for page in spec.page_specs] == [
        page["category"] for page in spec.membership
    ]
    assert manifest["workloads"][name]["page_count"] == expected["pages"]
    assert manifest["workloads"][name]["category_counts"] == expected["categories"]
    assert manifest["workloads"][name]["expected"] == spec.expected


@pytest.mark.parametrize("name", ["primary", "generalization"])
def test_generation_is_byte_deterministic_and_hash_verified(name: str, tmp_path: Path) -> None:
    """Fresh roots produce exactly the files and hashes frozen in the manifest."""
    manifest = load_frozen_manifest()
    first = generate_workload(name, tmp_path / "first")
    second = generate_workload(name, tmp_path / "second")

    assert first.source_path.read_bytes() == second.source_path.read_bytes()
    assert first.config_path.read_bytes() == second.config_path.read_bytes()
    assert first.membership_path.read_bytes() == second.membership_path.read_bytes()
    for key, path in first.generated_paths.items():
        assert sha256_path(path) == manifest["workloads"][name]["hashes"][key]
        assert path.read_bytes() == second.generated_paths[key].read_bytes()


def test_zero_work_adapter_uses_preloaded_tuple_without_reading_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The benchmark adapter does O(1) indexed lookup after initialization."""
    spec = generate_workload("primary", tmp_path)
    adapter = ZeroWorkAdapter(spec.page_specs)
    expected = spec.page_specs[4_321]

    def fail_read_text(self: Path, *args: object, **kwargs: object) -> str:
        raise AssertionError(f"extract must not read source: {self}")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    assert adapter.page_count(spec.source_path) == 5_000
    result = adapter.extract(
        spec.source_path,
        page_id="doc_0001_page_4322",
        page_number=4_322,
        action="transcribe_text",
    )
    assert result.content == expected.content
    assert result.format == expected.format
    assert result.confidence == expected.confidence
    assert result.warnings == list(expected.warnings)
    assert result.usage == {
        "pages": 1,
        "tokens": expected.tokens,
        "compute_seconds": 0.0,
        "cost_usd": expected.cost_usd,
    }


@pytest.mark.parametrize("name", ["primary", "generalization"])
def test_frozen_expected_ledger_aggregates_are_truthful(name: str, tmp_path: Path) -> None:
    """The checked-in receipts match a real run with the zero-work adapter."""
    spec = generate_workload(name, tmp_path / "input")
    run_dir = tmp_path / "run"
    run(
        inputs=[spec.source_path],
        config_path=spec.config_path,
        out_dir=run_dir,
        dry_run=False,
        _loaded_adapter=ZeroWorkAdapter(spec.page_specs),
        _reproducibility_profile=None,
    )
    expected = spec.expected
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    provenance = [
        json.loads(line)
        for line in (run_dir / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    quality = [
        json.loads(line)
        for line in (run_dir / "quality.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    cost = json.loads((run_dir / "cost.json").read_text(encoding="utf-8"))
    rerun = (run_dir / "rerun-manifest.yml").read_text(encoding="utf-8")

    assert manifest["summary"]["pages_extracted"] == expected["output"]["pages_extracted"]
    assert manifest["summary"]["records_normalized"] == expected["normalized"]["records_normalized"]
    assert manifest["summary"]["quality_warning_pages"] == expected["quality"]["warning_pages"]
    assert Counter(entry["result"]["format"] for entry in provenance) == expected["output"]["format_counts"]
    assert Counter(path.suffix.lstrip(".") for path in (run_dir / "raw").iterdir()) == (
        expected["output"]["raw_extension_counts"]
    )
    actual_grades = Counter(entry["grade"] for entry in quality)
    assert {
        grade: actual_grades[grade]
        for grade in expected["grades"]["signals_only"]
    } == expected["grades"]["signals_only"]
    assert Counter(entry["reason"] for entry in audit["review_queue"]) == (
        expected["audit"]["review_queue_by_reason"]
    )
    assert len(audit["review_queue"]) == expected["audit"]["review_queue_items"]
    assert cost["tokens_total"] == expected["cost"]["tokens_total"]
    assert cost["cost_usd"] == expected["cost"]["cost_usd"]
    assert cost["cost_known"] is expected["cost"]["cost_known"]
    assert cost["cost_basis"] == expected["cost"]["basis"]
    assert len(list((run_dir / "normalized").iterdir())) == expected["normalized"]["files"]
    assert rerun.count("page_id:") == expected["audit"]["rerun_items"]


def test_manifest_protects_exact_contracts_and_bounds_the_future_run() -> None:
    """Benchmark policy is explicit; it never hides whole output trees."""
    manifest = load_frozen_manifest()

    assert manifest["generator"]["version"] == "1.0.0"
    assert set(manifest["schema_sha256"]) == {
        "audit.schema.json",
        "cost.schema.json",
        "manifest.schema.json",
        "normalized-page.schema.json",
        "provenance-line.schema.json",
        "quality-line.schema.json",
        "run-log-line.schema.json",
    }
    assert manifest["allowed_nondeterminism"] == {
        "audit.json": ["/run_id"],
        "audit.md": ["/run_id"],
        "cost.json": ["/run_id"],
        "manifest.json": [
            "/completed_at",
            "/config/source_paths/0",
            "/inputs/0/path",
            "/run_id",
            "/started_at",
        ],
        "provenance.jsonl": ["/*/extraction_seconds", "/*/run_id", "/*/source/path", "/*/timestamp"],
        "quality.jsonl": [],
        "rerun-manifest.yml": ["/created_at", "/items/*/source", "/parent_run_id", "/run_id"],
        "route-map.yml": ["/documents/0/source", "/generated_at", "/run_id"],
        "run.log": ["/*/run_id", "/*/timestamp"],
    }
    assert manifest["protected_paths"] == [
        "schemas/audit.schema.json",
        "schemas/cost.schema.json",
        "schemas/manifest.schema.json",
        "schemas/normalized-page.schema.json",
        "schemas/provenance-line.schema.json",
        "schemas/quality-line.schema.json",
        "schemas/run-log-line.schema.json",
        "scripts/pageledger_bench/__init__.py",
        "scripts/pageledger_bench/benchmark_manifest.json",
        "scripts/pageledger_bench/workloads.py",
        "tests/pageledger/test_benchmark_workloads.py",
    ]
    assert set(manifest["recipe_sha256"]) == {"scripts/pageledger_bench/workloads.py"}
    assert manifest["performance_gates"] == {
        "ledger_phase_ns_per_page_max": 220_000,
        "ledger_phase_speedup_min": 2.0,
        "throughput_pages_per_second_min": 4_500,
        "generalization_runtime_ratio_max": 1.05,
        "verify_runtime_ratio_max": 1.10,
    }
    assert manifest["max_unaccounted_timing_ratio"] == 0.05
    assert manifest["temp_output_cap_bytes"] == 2 * 1024**3
