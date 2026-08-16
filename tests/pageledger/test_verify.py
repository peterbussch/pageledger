"""Relational integrity checks for completed PageLedger run directories."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

MINIMAL = """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
"""


def _run(tmp_path: Path) -> tuple[Path, Path]:
    from pageledger.runner import run

    source = tmp_path / "doc.txt"
    source.write_text("short\fa second clean page of text\n", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(MINIMAL, encoding="utf-8")
    out_dir = tmp_path / "run"
    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)
    return out_dir, source


def _codes(report: dict, kind: str) -> set[str]:
    return {issue["code"] for issue in report[kind]}


def test_verify_run_accepts_coherent_ledger(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    report = verify_run(out_dir)

    assert report["status"] == "pass"
    assert report["errors"] == []
    assert report["counts"]["routed_pages"] == 2
    assert report["counts"]["extracted_pages"] == 2
    assert report["counts"]["normalized_records"] == 0
    assert report["counts"]["quality_warning_pages"] == 1


@pytest.mark.parametrize(
    "artifact",
    [
        "config-snapshot.yml",
        "route-map.yml",
        "raw",
        "normalized",
        "audit.json",
        "audit.md",
        "provenance.jsonl",
        "quality.jsonl",
        "cost.json",
        "run.log",
        "rerun-manifest.yml",
    ],
)
def test_verify_run_reports_missing_manifest_declared_artifacts(tmp_path, artifact):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    path = out_dir / artifact
    if path.is_dir():
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
    else:
        path.unlink()

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "artifact_missing" in _codes(report, "errors")


def test_verify_run_checks_config_hash_and_internal_identity(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    (out_dir / "config-snapshot.yml").write_text(MINIMAL + "# changed\n", encoding="utf-8")
    cost_path = out_dir / "cost.json"
    cost = json.loads(cost_path.read_text())
    cost["run_id"] = "another-run"
    cost_path.write_text(json.dumps(cost), encoding="utf-8")

    report = verify_run(out_dir)

    assert {"config_hash_mismatch", "run_id_mismatch"} <= _codes(report, "errors")


def test_verify_run_handles_malformed_manifest_sections(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["config"] = None
    manifest["inputs"] = None
    manifest["summary"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "manifest_structure_invalid" in _codes(report, "errors")


def test_verify_run_reports_source_hash_and_raw_page_identity_mismatches(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    provenance_path = out_dir / "provenance.jsonl"
    provenance = [json.loads(line) for line in provenance_path.read_text().splitlines()]
    provenance[0]["source"]["sha256"] = "0" * 64
    provenance[0]["result"]["raw_artifact"] = provenance[1]["result"]["raw_artifact"]
    provenance_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in provenance), encoding="utf-8"
    )

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert {"source_identity_mismatch", "page_identity_mismatch"} <= _codes(
        report, "errors"
    )


def test_verify_run_checks_route_page_and_quality_totals(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    route_path = out_dir / "route-map.yml"
    route = yaml.safe_load(route_path.read_text())
    route["documents"][0]["pages"][1]["page_id"] = route["documents"][0]["pages"][0][
        "page_id"
    ]
    route_path.write_text(yaml.safe_dump(route), encoding="utf-8")
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["summary"]["quality_warning_pages"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(out_dir)

    assert {"duplicate_route_page_id", "quality_warning_count_mismatch"} <= _codes(
        report, "errors"
    )


def test_verify_run_enforces_route_action_accounting(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["summary"]["pages_routed_review"] = 1
    manifest["summary"]["pages_skipped"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(out_dir)

    assert {
        "routed_review_count_mismatch",
        "skipped_page_count_mismatch",
        "page_accounting_mismatch",
    } <= _codes(report, "errors")


def test_verify_run_checks_provenance_quality_raw_and_normalized(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    (out_dir / "raw" / "doc_0001_page_0001.txt").unlink()
    quality_path = out_dir / "quality.jsonl"
    quality = [json.loads(line) for line in quality_path.read_text().splitlines()]
    quality[0]["page_number"] = 2
    quality_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in quality), encoding="utf-8"
    )
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["summary"]["records_normalized"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(out_dir)

    assert {
        "raw_artifact_missing",
        "page_number_mismatch",
        "normalized_record_count_mismatch",
    } <= _codes(report, "errors")


def test_verify_run_detects_modified_raw_artifact(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    raw_path = out_dir / "raw" / "doc_0001_page_0001.txt"
    raw_path.write_text("tampered extraction output", encoding="utf-8")

    report = verify_run(out_dir)

    assert "raw_artifact_hash_mismatch" in _codes(report, "errors")


def test_verify_run_warns_for_legacy_provenance_without_raw_hash(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    provenance_path = out_dir / "provenance.jsonl"
    provenance = [
        json.loads(line) for line in provenance_path.read_text().splitlines()
    ]
    for entry in provenance:
        entry["result"].pop("raw_sha256")
    provenance_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in provenance),
        encoding="utf-8",
    )

    report = verify_run(out_dir)

    assert report["status"] == "pass"
    assert "legacy_evidence_incomplete" in _codes(report, "warnings")


def test_verify_run_detects_stale_audit_rendering(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    (out_dir / "audit.md").write_text("stale audit rendering\n", encoding="utf-8")

    report = verify_run(out_dir)

    assert "audit_render_mismatch" in _codes(report, "errors")


def test_verify_run_checks_audit_rerun_and_cost_references(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    audit_path = out_dir / "audit.json"
    audit = json.loads(audit_path.read_text())
    audit["review_queue"][0]["page_id"] = "missing-page"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    rerun_path = out_dir / "rerun-manifest.yml"
    rerun = yaml.safe_load(rerun_path.read_text())
    rerun["items"][0]["page_number"] = 999
    rerun_path.write_text(yaml.safe_dump(rerun), encoding="utf-8")
    cost_path = out_dir / "cost.json"
    cost = json.loads(cost_path.read_text())
    cost["pages_extracted"] = 99
    cost_path.write_text(json.dumps(cost), encoding="utf-8")

    report = verify_run(out_dir)

    assert {
        "unknown_page_reference",
        "page_number_mismatch",
        "cost_page_count_mismatch",
    } <= _codes(report, "errors")


def test_verify_run_rejects_edited_rerun_plan(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    rerun_path = out_dir / "rerun-manifest.yml"
    rerun = yaml.safe_load(rerun_path.read_text())
    rerun["items"][0]["reason"] = "manual_override"
    rerun_path.write_text(yaml.safe_dump(rerun), encoding="utf-8")

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "rerun_plan_mismatch" in _codes(report, "errors")


def test_verify_run_warns_when_external_source_is_missing(tmp_path):
    from pageledger.verify import verify_run

    out_dir, source = _run(tmp_path)
    source.unlink()

    report = verify_run(out_dir)

    assert report["status"] == "pass"
    assert "source_missing" in _codes(report, "warnings")


def test_verify_run_uses_routed_absolute_path_for_relative_input(
    tmp_path, monkeypatch
):
    from pageledger.runner import run
    from pageledger.verify import verify_run

    source = tmp_path / "doc.txt"
    source.write_text("a complete relative-source page\n", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(MINIMAL, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out_dir = tmp_path / "run"
    run(
        inputs=[Path("doc.txt")],
        config_path=Path("pageledger.yml"),
        out_dir=out_dir,
        dry_run=False,
    )
    monkeypatch.chdir(tmp_path.parent)

    report = verify_run(out_dir)

    assert report["status"] == "pass"
    assert "source_missing" not in _codes(report, "warnings")


def test_verify_run_rejects_hash_from_a_different_declared_source(tmp_path):
    from pageledger.runner import run
    from pageledger.verify import verify_run

    sources = [tmp_path / "one.txt", tmp_path / "two.txt"]
    sources[0].write_text("the first source document\n", encoding="utf-8")
    sources[1].write_text("the second source document\n", encoding="utf-8")
    config = tmp_path / "pageledger.yml"
    config.write_text(MINIMAL, encoding="utf-8")
    out_dir = tmp_path / "run"
    run(inputs=sources, config_path=config, out_dir=out_dir, dry_run=False)
    provenance_path = out_dir / "provenance.jsonl"
    provenance = [json.loads(line) for line in provenance_path.read_text().splitlines()]
    provenance[0]["source"]["sha256"] = provenance[1]["source"]["sha256"]
    provenance_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in provenance), encoding="utf-8"
    )

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "source_identity_mismatch" in _codes(report, "errors")


def test_verify_run_warns_on_incomplete_legacy_provenance(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    provenance_path = out_dir / "provenance.jsonl"
    provenance = [json.loads(line) for line in provenance_path.read_text().splitlines()]
    provenance[0]["source"].pop("sha256")
    provenance_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in provenance), encoding="utf-8"
    )

    report = verify_run(out_dir)

    assert report["status"] == "pass"
    assert "legacy_evidence_incomplete" in _codes(report, "warnings")


def test_verify_run_returns_structured_failure_for_malformed_artifact(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    (out_dir / "quality.jsonl").write_text("{not json}\n", encoding="utf-8")

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    issue = next(issue for issue in report["errors"] if issue["code"] == "artifact_malformed")
    assert issue["artifact"] == "quality.jsonl"


def test_verify_run_reports_missing_or_malformed_manifest(tmp_path):
    from pageledger.verify import verify_run

    out_dir = tmp_path / "run"
    out_dir.mkdir()
    missing = verify_run(out_dir)
    assert missing["status"] == "fail"
    assert _codes(missing, "errors") == {"manifest_missing"}

    (out_dir / "manifest.json").write_text("nope", encoding="utf-8")
    malformed = verify_run(out_dir)
    assert malformed["status"] == "fail"
    assert _codes(malformed, "errors") == {"manifest_malformed"}
