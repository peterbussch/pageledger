"""Independent, mutation-tested equivalence oracle for frozen benchmark runs."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from pageledger.artifacts import write_json, write_jsonl, write_yaml
from pageledger.runner import run
from scripts.pageledger_bench import oracle as oracle_module
from scripts.pageledger_bench.oracle import compare_runs, validate_run
from scripts.pageledger_bench.workloads import ZeroWorkAdapter, generate_workload


@pytest.fixture(scope="module")
def frozen_runs(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("benchmark-oracle")
    workload = generate_workload("generalization", root / "input")
    control = root / "control"
    candidate = root / "candidate"
    for out_dir in (control, candidate):
        run(
            inputs=[workload.source_path],
            config_path=workload.config_path,
            out_dir=out_dir,
            dry_run=False,
            _loaded_adapter=ZeroWorkAdapter(workload.page_specs),
            _reproducibility_profile=None,
        )
    return workload, control, candidate


def _copy_run(source: Path, tmp_path: Path) -> Path:
    destination = tmp_path / "mutated"
    shutil.copytree(source, destination)
    return destination


def _codes(receipt: object) -> set[str]:
    return {error.code for error in receipt.errors}


def _mutate_json(path: Path, change: Callable[[dict], None]) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    change(value)
    write_json(path, value)


def _mutate_jsonl(path: Path, change: Callable[[list[dict]], None]) -> None:
    values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    change(values)
    write_jsonl(path, values)


def test_independent_receipts_accept_fresh_generation_zero_runs(frozen_runs) -> None:
    workload, control, candidate = frozen_runs

    validation = validate_run(control, workload)
    equivalence = compare_runs(control, candidate, workload)

    assert validation.valid, validation.errors
    assert validation.verifier_report["status"] == "pass"
    assert equivalence.equivalent, equivalence.errors


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda run_dir: next((run_dir / "raw").iterdir()).write_bytes(b"raw drift\n"),
            "raw_sha256_mismatch",
        ),
        (
            lambda run_dir: (run_dir / "config-snapshot.yml").write_text(
                (run_dir / "config-snapshot.yml").read_text(encoding="utf-8") + "# drift\n",
                encoding="utf-8",
            ),
            "config_bytes_mismatch",
        ),
        (
            lambda run_dir: _mutate_jsonl(
                run_dir / "provenance.jsonl",
                lambda entries: entries.__setitem__(slice(0, 2), reversed(entries[:2])),
            ),
            "provenance_order_mismatch",
        ),
        (
            lambda run_dir: _mutate_jsonl(
                run_dir / "quality.jsonl",
                lambda entries: entries.__setitem__(slice(0, 2), reversed(entries[:2])),
            ),
            "quality_order_mismatch",
        ),
        (
            lambda run_dir: _mutate_jsonl(
                run_dir / "provenance.jsonl",
                lambda entries: entries[0]["result"]["warnings"].append("forged_warning"),
            ),
            "provenance_warning_mismatch",
        ),
        (
            lambda run_dir: _mutate_jsonl(
                run_dir / "quality.jsonl",
                lambda entries: entries[0].__setitem__("grade", "F"),
            ),
            "quality_grade_mismatch",
        ),
        (
            lambda run_dir: _mutate_json(
                run_dir / "cost.json",
                lambda value: value.__setitem__("cost_usd", value["cost_usd"] + 1),
            ),
            "cost_total_mismatch",
        ),
        (
            lambda run_dir: (run_dir / "audit.md").write_text(
                "# stale rendering\n", encoding="utf-8"
            ),
            "audit_markdown_mismatch",
        ),
        (
            lambda run_dir: (run_dir / "unexpected.txt").write_text("extra", encoding="utf-8"),
            "inventory_mismatch",
        ),
    ],
    ids=[
        "raw-byte-and-hash-drift",
        "config-byte-drift",
        "provenance-jsonl-order",
        "quality-jsonl-order",
        "warning-change",
        "grade-change",
        "cost-change",
        "stale-audit-markdown",
        "extra-file",
    ],
)
def test_mutations_fail_for_the_intended_oracle_rule(
    frozen_runs, tmp_path: Path, mutate: Callable[[Path], None], expected_code: str
) -> None:
    workload, control, _ = frozen_runs
    mutated = _copy_run(control, tmp_path)
    mutate(mutated)

    receipt = validate_run(mutated, workload)

    assert not receipt.valid
    assert expected_code in _codes(receipt), receipt.errors


def test_symlink_is_rejected_before_its_target_can_be_evidence(
    frozen_runs, tmp_path: Path
) -> None:
    workload, control, _ = frozen_runs
    mutated = _copy_run(control, tmp_path)
    raw = next((mutated / "raw").iterdir())
    outside = tmp_path / "outside"
    outside.write_bytes(raw.read_bytes())
    raw.unlink()
    raw.symlink_to(outside)

    receipt = validate_run(mutated, workload)

    assert not receipt.valid
    assert "symlink_forbidden" in _codes(receipt)


def test_route_page_order_drift_is_not_normalized(frozen_runs, tmp_path: Path) -> None:
    workload, control, _ = frozen_runs
    mutated = _copy_run(control, tmp_path)
    route_path = mutated / "route-map.yml"
    route = yaml.safe_load(route_path.read_text(encoding="utf-8"))
    route["documents"][0]["pages"][:2] = reversed(route["documents"][0]["pages"][:2])
    write_yaml(route_path, route)

    receipt = validate_run(mutated, workload)

    assert not receipt.valid
    assert "route_membership_mismatch" in _codes(receipt)


def test_frozen_schema_hash_drift_fails_closed(
    frozen_runs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload, control, _ = frozen_runs
    schemas = tmp_path / "schemas"
    shutil.copytree(oracle_module._SCHEMAS_DIR, schemas)
    (schemas / "audit.schema.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(oracle_module, "_SCHEMAS_DIR", schemas)

    receipt = validate_run(control, workload)

    assert not receipt.valid
    assert "schema_hash_mismatch" in _codes(receipt)


@pytest.mark.parametrize(
    ("filename", "expected_code"),
    [
        ("source.txt", "workload_source_hash_mismatch"),
        ("pageledger.yml", "workload_config_hash_mismatch"),
        ("membership.json", "workload_membership_hash_mismatch"),
    ],
)
def test_mutable_workload_files_cannot_become_oracle_truth(
    frozen_runs, tmp_path: Path, filename: str, expected_code: str
) -> None:
    workload, control, _ = frozen_runs
    workload_root = tmp_path / "workload"
    shutil.copytree(workload.source_path.parent, workload_root)
    paths = {
        "source.txt": workload_root / "source.txt",
        "pageledger.yml": workload_root / "pageledger.yml",
        "membership.json": workload_root / "membership.json",
    }
    paths[filename].write_bytes(paths[filename].read_bytes() + b"\n")
    changed = replace(
        workload,
        source_path=paths["source.txt"],
        config_path=paths["pageledger.yml"],
        membership_path=paths["membership.json"],
        generated_paths=paths,
    )

    receipt = validate_run(control, changed)

    assert not receipt.valid
    assert expected_code in _codes(receipt)


def test_mutable_workload_receipts_and_recipe_are_revalidated(
    frozen_runs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload, control, _ = frozen_runs
    changed = replace(workload, expected={**workload.expected, "quality": {"warning_pages": 0}})
    recipe = tmp_path / "workloads.py"
    recipe.write_text("# changed recipe\n", encoding="utf-8")
    monkeypatch.setattr(oracle_module, "_WORKLOAD_RECIPE_PATH", recipe)

    receipt = validate_run(control, changed)

    assert not receipt.valid
    assert {"workload_expected_mismatch", "recipe_hash_mismatch"} <= _codes(receipt)


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda run_dir: _mutate_json(
                run_dir / "manifest.json",
                lambda value: value["inputs"][0].__setitem__("path", "/tmp/unrelated-source.txt"),
            ),
            "source_path_relationship_mismatch",
        ),
        (
            lambda run_dir: _mutate_json(
                run_dir / "manifest.json",
                lambda value: value["config"]["source_paths"].__setitem__(
                    0, "/tmp/unrelated-config.yml"
                ),
            ),
            "config_path_relationship_mismatch",
        ),
        (
            lambda run_dir: _mutate_jsonl(
                run_dir / "provenance.jsonl",
                lambda entries: entries[0].__setitem__("timestamp", "1999-01-01T00:00:00Z"),
            ),
            "timestamp_relationship_mismatch",
        ),
        (
            lambda run_dir: _mutate_jsonl(
                run_dir / "run.log",
                lambda entries: entries[0].__setitem__("timestamp", "2099-01-01T00:00:00Z"),
            ),
            "timestamp_relationship_mismatch",
        ),
    ],
    ids=["source-path", "config-path", "provenance-window", "log-provenance-time"],
)
def test_declared_path_and_timestamp_leaves_require_coherent_relationships(
    frozen_runs, tmp_path: Path, mutate: Callable[[Path], None], expected_code: str
) -> None:
    workload, control, _ = frozen_runs
    mutated = _copy_run(control, tmp_path)
    mutate(mutated)

    receipt = validate_run(mutated, workload)

    assert not receipt.valid
    assert expected_code in _codes(receipt), receipt.errors


@pytest.mark.parametrize(
    ("artifact", "change", "expected_code"),
    [
        (
            "quality.jsonl",
            lambda entries: entries[0].__setitem__(
                "character_count", entries[0]["character_count"] + 1
            ),
            "quality_record_mismatch",
        ),
        (
            "quality.jsonl",
            lambda entries: entries[0]["grade_detail"]["reasons"].append("forged reason"),
            "quality_record_mismatch",
        ),
        (
            "provenance.jsonl",
            lambda entries: entries[0]["cost"].__setitem__("basis", "configured_rate"),
            "provenance_cost_mismatch",
        ),
        (
            "run.log",
            lambda entries: entries[0].__setitem__("level", "WARNING"),
            "run_log_record_mismatch",
        ),
    ],
)
def test_complete_per_page_ledger_records_are_independently_rederived(
    frozen_runs,
    tmp_path: Path,
    artifact: str,
    change: Callable[[list[dict]], None],
    expected_code: str,
) -> None:
    workload, control, _ = frozen_runs
    mutated = _copy_run(control, tmp_path)
    _mutate_jsonl(mutated / artifact, change)

    receipt = validate_run(mutated, workload)

    assert not receipt.valid
    assert expected_code in _codes(receipt), receipt.errors


def test_rerun_fixed_headers_are_independently_rederived(frozen_runs, tmp_path: Path) -> None:
    workload, control, _ = frozen_runs
    mutated = _copy_run(control, tmp_path)
    rerun_path = mutated / "rerun-manifest.yml"
    rerun = yaml.safe_load(rerun_path.read_text(encoding="utf-8"))
    rerun["max_rerun_depth"] = 3
    write_yaml(rerun_path, rerun)

    receipt = validate_run(mutated, workload)

    assert not receipt.valid
    assert "rerun_header_mismatch" in _codes(receipt)


@pytest.mark.parametrize(
    "relative",
    ["config-snapshot.yml", "audit.md", "route-map.yml", "raw"],
)
def test_missing_required_artifacts_return_structured_receipts(
    frozen_runs, tmp_path: Path, relative: str
) -> None:
    workload, control, _ = frozen_runs
    mutated = _copy_run(control, tmp_path)
    target = mutated / relative
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    receipt = validate_run(mutated, workload)

    assert not receipt.valid
    assert "required_artifact_missing" in _codes(receipt), receipt.errors


def test_canonical_run_rejects_top_level_symlink_without_reading_target(
    frozen_runs, tmp_path: Path
) -> None:
    _, control, _ = frozen_runs
    mutated = _copy_run(control, tmp_path)
    manifest = mutated / "manifest.json"
    outside = tmp_path / "outside-manifest.json"
    manifest.replace(outside)
    manifest.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink_forbidden"):
        oracle_module.canonical_run(mutated)


def test_external_raw_reference_is_rejected_before_external_bytes_are_read(
    frozen_runs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload, control, _ = frozen_runs
    mutated = _copy_run(control, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("must not become evidence", encoding="utf-8")
    _mutate_jsonl(
        mutated / "provenance.jsonl",
        lambda entries: entries[0]["result"].__setitem__("raw_artifact", "../outside.txt"),
    )
    original_open = Path.open

    def guarded_open(self: Path, *args: object, **kwargs: object):
        if self.resolve() == outside.resolve():
            raise AssertionError("oracle read external raw bytes")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    receipt = validate_run(mutated, workload)

    assert not receipt.valid
    assert "raw_artifact_path_invalid" in _codes(receipt), receipt.errors


def test_full_verifier_result_and_counts_are_part_of_equivalence(
    frozen_runs, monkeypatch: pytest.MonkeyPatch
) -> None:
    workload, control, candidate = frozen_runs
    real_verify_run = oracle_module.verify_run

    def drifted_verify_run(run_dir: Path) -> dict:
        report = real_verify_run(run_dir)
        if Path(run_dir) == candidate:
            report = json.loads(json.dumps(report))
            report["counts"]["quality_pages"] += 1
        return report

    monkeypatch.setattr(oracle_module, "verify_run", drifted_verify_run)

    receipt = compare_runs(control, candidate, workload)

    assert not receipt.equivalent
    assert "verifier_report_mismatch" in _codes(receipt)


def test_only_declared_identity_timestamp_and_extraction_timing_drift_compare_equal(
    frozen_runs, tmp_path: Path
) -> None:
    workload, control, _ = frozen_runs
    candidate = _copy_run(control, tmp_path)
    manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
    old_run_id = manifest["run_id"]
    new_run_id = "run-20990101T010203000000Z"
    consumed = tmp_path / "consumed"
    consumed.mkdir()
    source_path = consumed / "source.txt"
    config_path = consumed / "pageledger.yml"
    shutil.copyfile(workload.source_path, source_path)
    shutil.copyfile(workload.config_path, config_path)

    _mutate_json(
        candidate / "manifest.json",
        lambda value: (
            value.update(
                run_id=new_run_id,
                started_at="2099-01-01T01:02:03Z",
                completed_at="2099-01-01T01:02:04Z",
            ),
            value["inputs"][0].update(path=str(source_path)),
            value["config"]["source_paths"].__setitem__(0, str(config_path)),
        ),
    )
    _mutate_json(candidate / "audit.json", lambda value: value.update(run_id=new_run_id))
    _mutate_json(candidate / "cost.json", lambda value: value.update(run_id=new_run_id))

    extraction_total = 0.0

    def change_provenance(entries: list[dict]) -> None:
        nonlocal extraction_total
        for entry in entries:
            entry["run_id"] = new_run_id
            entry["timestamp"] = "2099-01-01T01:02:03Z"
            entry["source"]["path"] = str(source_path)
            entry["extraction_seconds"] = 0.001
            extraction_total += entry["extraction_seconds"]

    _mutate_jsonl(candidate / "provenance.jsonl", change_provenance)
    _mutate_json(
        candidate / "cost.json",
        lambda value: value["usage"].update(extraction_seconds=round(extraction_total, 3)),
    )
    for normalized_path in (candidate / "normalized").iterdir():
        _mutate_json(normalized_path, lambda value: value.update(run_id=new_run_id))

    route_path = candidate / "route-map.yml"
    route = yaml.safe_load(route_path.read_text(encoding="utf-8"))
    route.update(run_id=new_run_id, generated_at="2099-01-01T01:02:03Z")
    route["documents"][0]["source"] = str(source_path)
    write_yaml(route_path, route)
    rerun_path = candidate / "rerun-manifest.yml"
    rerun = yaml.safe_load(rerun_path.read_text(encoding="utf-8"))
    rerun.update(
        run_id=f"{new_run_id}-rerun",
        parent_run_id=new_run_id,
        created_at="2099-01-01T01:02:05Z",
    )
    for item in rerun["items"]:
        item["source"] = str(source_path)
    write_yaml(rerun_path, rerun)

    def change_log(entries: list[dict]) -> None:
        for entry in entries:
            entry["run_id"] = new_run_id
            entry["timestamp"] = "2099-01-01T01:02:03Z"

    _mutate_jsonl(candidate / "run.log", change_log)
    audit = json.loads((candidate / "audit.json").read_text(encoding="utf-8"))
    (candidate / "audit.md").write_text(
        oracle_module.render_audit_markdown(audit), encoding="utf-8"
    )

    assert old_run_id != new_run_id
    receipt = compare_runs(control, candidate, workload)
    assert receipt.equivalent, (receipt.errors, receipt.candidate.errors)
