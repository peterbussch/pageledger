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
    "mutation",
    [
        "forged_self_hash",
        "path_material",
        "mutable_alias",
        "hash_only_profile",
        "adapter_identity_mismatch",
    ],
)
def test_verify_run_rejects_invalid_manifest_reproducibility_profile(
    tmp_path: Path, mutation: str
) -> None:
    from pageledger.replay import profile_sha256
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    profile = manifest["extractors"][0]["reproducibility_profile"]
    if mutation == "forged_self_hash":
        profile["runtime"]["release"] = "forged-release"
    elif mutation == "path_material":
        profile["materials"] = [
            {"kind": "asset", "name": "/tmp/model", "version": "1.0", "sha256": "0" * 64}
        ]
    elif mutation == "mutable_alias":
        profile["materials"] = [
            {"kind": "model", "name": "model", "version": "latest", "sha256": "0" * 64}
        ]
    elif mutation == "hash_only_profile":
        manifest["extractors"][0]["reproducibility_profile"] = {"profile_sha256": "0" * 64}
    else:
        profile["adapter"]["name"] = "forged-adapter"
    if mutation in {"path_material", "mutable_alias", "adapter_identity_mismatch"}:
        profile["profile_sha256"] = profile_sha256(profile)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(run_dir)

    assert report["status"] == "fail"
    assert {item["code"] for item in report["errors"]} & {
        "profile_invalid", "profile_hash_mismatch"
    }


def test_verify_run_rejects_manifest_provenance_extractor_forgery(
    tmp_path: Path,
) -> None:
    from pageledger.replay import bundle_run, profile_sha256, replay_bundle
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)

    manifest_path = replay_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity = manifest["extractors"][0]
    identity["adapter"] = "forged-adapter"
    identity["version"] = "9.9"
    identity["reproducibility_profile"]["adapter"]["name"] = "forged-adapter"
    identity["reproducibility_profile"]["adapter"]["version"] = "9.9"
    identity["reproducibility_profile"]["profile_sha256"] = profile_sha256(
        identity["reproducibility_profile"]
    )
    forged_profile_hash = identity["reproducibility_profile"]["profile_sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    replay_path = replay_dir / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    for key in ("baseline_extractor", "local_extractor"):
        replay[key]["adapter"] = "forged-adapter"
        replay[key]["version"] = "9.9"
        replay[key]["reproducibility_profile_sha256"] = forged_profile_hash
    replay_path.write_text(json.dumps(replay), encoding="utf-8")

    report = verify_run(replay_dir)

    assert report["status"] == "fail"
    codes = {item["code"] for item in report["errors"]}
    assert "extractor_identity_mismatch" in codes
    assert "replay_linkage_mismatch" not in codes
    assert not ({"profile_invalid", "profile_hash_mismatch"} & codes)


def test_verify_run_returns_structured_failure_for_malformed_manifest_extractor(
    tmp_path: Path,
) -> None:
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["extractors"][0]["adapter"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(run_dir)

    assert report["status"] == "fail"
    assert "extractor_identity_mismatch" in _codes(report, "errors")


def test_verify_run_accepts_reordered_manifest_extractor_lists(tmp_path: Path) -> None:
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    extractor = manifest["extractors"][0]
    extractor["input_types"] = list(reversed(extractor["input_types"]))
    extractor["output_types"] = list(reversed(extractor["output_types"]))
    extractor["capabilities"] = list(reversed(extractor["capabilities"]))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(run_dir)

    assert report["status"] == "pass"


def test_verify_run_rejects_replay_without_manifest_extractor_identity(
    tmp_path: Path,
) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)
    manifest_path = replay_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["extractors"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(replay_dir)

    assert report["status"] == "fail"
    assert "replay_linkage_mismatch" in _codes(report, "errors")


def test_verify_run_accepts_manifest_extractor_membership_variants(
    tmp_path: Path,
) -> None:
    from pageledger.replay import profile_sha256
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    alternate = json.loads(json.dumps(manifest["extractors"][0]))
    alternate["version"] = "2.0"
    alternate["reproducibility_profile"]["adapter"]["version"] = "2.0"
    alternate["reproducibility_profile"]["profile_sha256"] = profile_sha256(
        alternate["reproducibility_profile"]
    )
    manifest["extractors"].append(alternate)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    provenance_path = run_dir / "provenance.jsonl"
    lines = provenance_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["extractor"]["adapter_version"] = "2.0"
    lines[0] = json.dumps(first)
    provenance_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = verify_run(run_dir)

    assert report["status"] == "pass"


def test_verify_run_accepts_routed_review_only_empty_provenance(tmp_path: Path) -> None:
    from pageledger.runner import run
    from pageledger.verify import verify_run

    base, source = _run(tmp_path)
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL, encoding="utf-8")
    route = yaml.safe_load((base / "route-map.yml").read_text(encoding="utf-8"))
    for document in route["documents"]:
        for page in document["pages"]:
            page["action"] = "review"
            page["reason"] = "explicit review"
    route_path = tmp_path / "review-route.yml"
    route_path.write_text(yaml.safe_dump(route, sort_keys=False), encoding="utf-8")
    out_dir = tmp_path / "review-only"
    run(
        inputs=[source],
        config_path=config,
        out_dir=out_dir,
        routes_path=route_path,
        dry_run=False,
    )

    report = verify_run(out_dir)

    assert report["status"] == "pass"


@pytest.mark.parametrize(
    "field",
    ["baseline_run_id", "replay_run_id", "bundle_manifest_sha256", "outcome", "replay_schema_version"],
)
def test_verify_run_rejects_changed_replay_linkage(tmp_path: Path, field: str) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)

    manifest_path = replay_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    replay_path = replay_dir / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    target = replay if field == "replay_run_id" else manifest
    if field in {"replay_schema_version", "outcome"}:
        target[field] = "9.9" if field == "replay_schema_version" else "deterministic_mismatch"
    elif field == "bundle_manifest_sha256":
        target[field] = "0" * 64
    else:
        target[field] = "changed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    replay_path.write_text(json.dumps(replay), encoding="utf-8")

    report = verify_run(replay_dir)

    assert report["status"] == "fail"
    assert "replay_linkage_mismatch" in _codes(report, "errors")


def test_verify_run_reports_replay_artifact_missing_or_malformed(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)
    manifest_path = replay_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    replay_path = replay_dir / "replay.json"
    replay_path.unlink()

    missing = verify_run(replay_dir)
    assert "replay_artifact_missing" in _codes(missing, "errors")

    replay_path.write_text("not json", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    malformed = verify_run(replay_dir)
    assert "replay_artifact_malformed" in _codes(malformed, "errors")


def test_verify_run_rejects_duplicate_common_comparison_page_ids(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)
    replay_path = replay_dir / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["comparison"]["pages"].append(dict(replay["comparison"]["pages"][0]))
    replay["raw"]["equal"] += 1
    replay_path.write_text(json.dumps(replay), encoding="utf-8")

    report = verify_run(replay_dir)

    assert report["status"] == "fail"
    assert "replay_linkage_mismatch" in _codes(report, "errors")


def test_verify_run_rejects_raw_equal_contradiction_with_consistent_counters(
    tmp_path: Path,
) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)
    replay_path = replay_dir / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    page = replay["comparison"]["pages"][0]
    page_id = page["page_id"]
    page["raw_equal"] = False
    replay["raw"] = {
        "equal": 0,
        "different": 1,
        "missing": 0,
        "different_page_ids": [page_id],
        "missing_page_ids": [],
    }
    replay["outcome"] = "deterministic_mismatch"
    replay_path.write_text(json.dumps(replay), encoding="utf-8")
    manifest_path = replay_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outcome"] = "deterministic_mismatch"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(replay_dir)

    assert report["status"] == "fail"
    assert "replay_linkage_mismatch" in _codes(report, "errors")


def test_verify_run_rejects_replay_raw_hash_divergence_from_provenance(
    tmp_path: Path,
) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)
    replay_path = replay_dir / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    forged_hash = "0" * 64
    replay["comparison"]["pages"][0]["raw_sha256_a"] = forged_hash
    replay["comparison"]["pages"][0]["raw_sha256_b"] = forged_hash
    replay_path.write_text(json.dumps(replay), encoding="utf-8")

    report = verify_run(replay_dir)

    assert report["status"] == "fail"
    assert "replay_linkage_mismatch" in _codes(report, "errors")


def test_verify_run_rejects_bogus_replay_extractor_identity(
    tmp_path: Path,
) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)
    replay_path = replay_dir / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["baseline_extractor"]["adapter"] = "bogus"
    replay["local_extractor"]["adapter"] = "bogus"
    replay["baseline_extractor"]["version"] = "bogus"
    replay["local_extractor"]["version"] = "bogus"
    replay_path.write_text(json.dumps(replay), encoding="utf-8")

    report = verify_run(replay_dir)

    assert report["status"] == "fail"
    assert "replay_linkage_mismatch" in _codes(report, "errors")


def test_verify_run_rejects_malformed_replay_extractor_options(
    tmp_path: Path,
) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)
    replay_path = replay_dir / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["local_extractor"]["options"] = []
    replay_path.write_text(json.dumps(replay), encoding="utf-8")

    report = verify_run(replay_dir)

    assert report["status"] == "fail"
    assert "replay_artifact_malformed" in _codes(report, "errors")


def test_verify_run_rejects_overlapping_page_only_comparison_ids(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)
    replay_path = replay_dir / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["outcome"] = "deterministic_mismatch"
    replay["comparison"]["pages_only_in_a"] = ["doc_0001_page_0001"]
    replay["comparison"]["pages_only_in_b"] = ["doc_0001_page_0001"]
    replay["raw"]["missing"] = 1
    replay["raw"]["missing_page_ids"] = ["doc_0001_page_0001"]
    replay_path.write_text(json.dumps(replay), encoding="utf-8")
    manifest_path = replay_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outcome"] = "deterministic_mismatch"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(replay_dir)

    assert report["status"] == "fail"
    assert "replay_linkage_mismatch" in _codes(report, "errors")


def test_verify_run_rejects_invalid_extractor_evidence_for_evidence_outcome(
    tmp_path: Path,
) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)
    replay_path = replay_dir / "replay.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["outcome"] = "evidence_compared"
    replay["baseline_extractor"]["deterministic"] = None
    replay["baseline_extractor"]["capabilities"] = ["local"]
    replay_path.write_text(json.dumps(replay), encoding="utf-8")
    manifest_path = replay_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outcome"] = "evidence_compared"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(replay_dir)

    assert report["status"] == "fail"
    assert "replay_artifact_malformed" in _codes(report, "errors")


def test_verify_run_converts_optional_replay_loader_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pageledger.verify as verify_module
    from pageledger.replay import bundle_run, replay_bundle

    run_dir, _ = _run(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_dir = tmp_path / "replayed"
    replay_bundle(bundle_dir, replay_dir)
    original_load = verify_module._load_artifact

    def raising_load(key: str, path: Path) -> object:
        if key == "replay":
            raise RuntimeError("unexpected loader failure")
        return original_load(key, path)

    monkeypatch.setattr(verify_module, "_load_artifact", raising_load)
    report = verify_module.verify_run(replay_dir)

    assert report["status"] == "fail"
    assert "replay_artifact_malformed" in _codes(report, "errors")


def test_verify_run_does_not_follow_manifest_symlink(tmp_path, monkeypatch):
    from pathlib import Path

    import pageledger.verify as verify_module

    out_dir, _ = _run(tmp_path)
    manifest_path = out_dir / "manifest.json"
    outside = tmp_path / "outside-manifest.json"
    manifest_path.replace(outside)
    manifest_path.symlink_to(outside)
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):  # noqa: ANN001, ANN202
        if path.resolve() == outside.resolve():
            raise AssertionError("verifier read an out-of-run manifest")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    report = verify_module.verify_run(out_dir)

    assert report["status"] == "fail"
    assert "manifest_path_invalid" in _codes(report, "errors")


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


def test_verify_run_checks_alignment_schema_hash(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["alignment"] = {
        "aligned_at": "2026-08-16T00:00:00Z",
        "schema_source": "config_snapshot",
        "schema_sha256": "0" * 64,
        "pageledger_version": "0.3.0a1",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "alignment_schema_hash_mismatch" in _codes(report, "errors")


def test_verify_run_requires_external_alignment_schema_snapshot(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["alignment"] = {
        "aligned_at": "2026-08-16T00:00:00Z",
        "schema_source": "/external/schema.yml",
        "schema_sha256": "0" * 64,
        "pageledger_version": "0.3.0a1",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "alignment_schema_snapshot_missing" in _codes(report, "errors")


def test_verify_run_does_not_follow_external_alignment_schema_symlink(
    tmp_path, monkeypatch
):
    import pageledger.verify as verify_module

    out_dir, _ = _run(tmp_path)
    outside = tmp_path / "outside-schema.yml"
    outside.write_text("schema: {name: outside, columns: []}\n", encoding="utf-8")
    (out_dir / "align-schema-snapshot.yml").symlink_to(outside)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["alignment"] = {
        "aligned_at": "2026-08-16T00:00:00Z",
        "schema_source": "/external/schema.yml",
        "schema_sha256": "0" * 64,
        "pageledger_version": "0.3.0a1",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_sha256 = verify_module._sha256

    def guarded_sha256(path):  # noqa: ANN001, ANN202
        if path.resolve() == outside.resolve():
            raise AssertionError("verifier read an out-of-run schema snapshot")
        return original_sha256(path)

    monkeypatch.setattr(verify_module, "_sha256", guarded_sha256)

    report = verify_module.verify_run(out_dir)

    assert report["status"] == "fail"
    assert "alignment_schema_snapshot_invalid" in _codes(report, "errors")


def test_verify_run_handles_alignment_schema_symlink_loop(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    (out_dir / "align-schema-snapshot.yml").symlink_to("align-schema-snapshot.yml")
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["alignment"] = {
        "aligned_at": "2026-08-16T00:00:00Z",
        "schema_source": "/external/schema.yml",
        "schema_sha256": "0" * 64,
        "pageledger_version": "0.3.0a1",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "alignment_schema_snapshot_invalid" in _codes(report, "errors")


def test_verify_run_handles_manifest_artifact_symlink_loop(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    raw_dir = out_dir / "raw"
    for child in raw_dir.iterdir():
        child.unlink()
    raw_dir.rmdir()
    raw_dir.symlink_to("raw")

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "artifact_path_invalid" in _codes(report, "errors")


def test_verify_run_handles_embedded_null_artifact_path(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["raw_dir"] = "raw\x00escaped"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "artifact_path_invalid" in _codes(report, "errors")


def test_verify_run_handles_run_directory_symlink_loop(tmp_path):
    from pageledger.verify import verify_run

    loop = tmp_path / "loop"
    loop.symlink_to("loop")

    report = verify_run(loop)

    assert report["status"] == "fail"
    assert "run_path_invalid" in _codes(report, "errors")


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


def test_verify_run_does_not_read_raw_artifact_outside_declared_directory(
    tmp_path, monkeypatch
):
    import pageledger.verify as verify_module

    out_dir, _ = _run(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be read as run evidence", encoding="utf-8")
    provenance_path = out_dir / "provenance.jsonl"
    entries = [
        json.loads(line)
        for line in provenance_path.read_text(encoding="utf-8").splitlines()
    ]
    entries[0]["result"]["raw_artifact"] = "../outside.txt"
    provenance_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )
    original_sha256 = verify_module._sha256

    def guarded_sha256(path):  # noqa: ANN001, ANN202
        if path.resolve() == outside.resolve():
            raise AssertionError("verifier read an out-of-run raw artifact")
        return original_sha256(path)

    monkeypatch.setattr(verify_module, "_sha256", guarded_sha256)

    report = verify_module.verify_run(out_dir)

    assert report["status"] == "fail"
    assert "raw_artifact_path_invalid" in _codes(report, "errors")


def test_verify_run_does_not_read_outside_raw_when_declaration_is_missing(
    tmp_path, monkeypatch
):
    import pageledger.verify as verify_module

    out_dir, _ = _run(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be read as run evidence", encoding="utf-8")
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["artifacts"]["raw_dir"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    provenance_path = out_dir / "provenance.jsonl"
    entries = [
        json.loads(line)
        for line in provenance_path.read_text(encoding="utf-8").splitlines()
    ]
    entries[0]["result"]["raw_artifact"] = "../outside.txt"
    provenance_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )
    original_sha256 = verify_module._sha256

    def guarded_sha256(path):  # noqa: ANN001, ANN202
        if path.resolve() == outside.resolve():
            raise AssertionError("verifier read an out-of-run raw artifact")
        return original_sha256(path)

    monkeypatch.setattr(verify_module, "_sha256", guarded_sha256)

    report = verify_module.verify_run(out_dir)

    assert report["status"] == "fail"
    assert "artifact_declaration_missing" in _codes(report, "errors")
    assert "raw_artifact_path_invalid" in _codes(report, "errors")


def test_verify_run_rejects_raw_symlink_without_following_inventory_target(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    raw_path = out_dir / "raw" / "doc_0001_page_0001.txt"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside raw directory", encoding="utf-8")
    raw_path.unlink()
    raw_path.symlink_to(outside)

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "raw_artifact_path_invalid" in _codes(report, "errors")


def test_verify_run_handles_raw_artifact_symlink_loop(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    raw_path = out_dir / "raw" / "doc_0001_page_0001.txt"
    raw_path.unlink()
    raw_path.symlink_to(raw_path.name)

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "raw_artifact_path_invalid" in _codes(report, "errors")


def test_verify_run_does_not_follow_normalized_symlink(tmp_path, monkeypatch):
    from pathlib import Path

    import pageledger.verify as verify_module

    out_dir, _ = _run(tmp_path)
    outside = tmp_path / "outside-normalized.json"
    outside.write_text('{"outside": true}\n', encoding="utf-8")
    (out_dir / "normalized" / "outside.json").symlink_to(outside)
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):  # noqa: ANN001, ANN202
        if path.resolve() == outside.resolve():
            raise AssertionError("verifier read an out-of-run normalized artifact")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    report = verify_module.verify_run(out_dir)

    assert report["status"] == "fail"
    assert "normalized_artifact_path_invalid" in _codes(report, "errors")


def test_verify_run_rejects_missing_raw_hash_from_new_run(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    provenance_path = out_dir / "provenance.jsonl"
    provenance = [
        json.loads(line) for line in provenance_path.read_text().splitlines()
    ]
    provenance[0]["result"].pop("raw_sha256")
    provenance_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in provenance),
        encoding="utf-8",
    )

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "raw_artifact_hash_missing" in _codes(report, "errors")


def test_verify_run_fails_closed_for_legacy_provenance_without_raw_hash(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["pageledger_version"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
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

    assert report["status"] == "fail"
    assert "legacy_evidence_incomplete" in _codes(report, "warnings")
    assert "raw_artifact_hash_missing" in _codes(report, "errors")


def test_verify_run_detects_stale_audit_rendering(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    (out_dir / "audit.md").write_text("stale audit rendering\n", encoding="utf-8")

    report = verify_run(out_dir)

    assert "audit_render_mismatch" in _codes(report, "errors")


def test_verify_run_returns_structured_failure_for_malformed_audit(tmp_path, capsys):
    from pageledger.cli import main
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    audit_path = out_dir / "audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    del audit["run_id"]
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    report = verify_run(out_dir)
    assert report["status"] == "fail"
    assert "run_id_mismatch" in _codes(report, "errors")
    assert "artifact_structure_invalid" in _codes(report, "errors")

    exit_code = main(["verify-run", str(out_dir), "--json"])
    assert exit_code == 1
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["status"] == "fail"
    assert "artifact_structure_invalid" in _codes(cli_report, "errors")


def test_verify_run_returns_structured_failure_for_non_mapping_audit_item(tmp_path):
    from pageledger.verify import verify_run

    out_dir, _ = _run(tmp_path)
    audit_path = out_dir / "audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["review_queue"] = [1]
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "artifact_structure_invalid" in _codes(report, "errors")


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


def test_verify_run_derives_adapter_chain_from_config_snapshot(tmp_path):
    from pageledger.runner import run
    from pageledger.verify import verify_run

    source = tmp_path / "chain.txt"
    source.write_text("short\n", encoding="utf-8")
    config = tmp_path / "chain.yml"
    config.write_text(
        MINIMAL.replace("adapter: text", "adapter_order: [text, text]"),
        encoding="utf-8",
    )
    out_dir = tmp_path / "chain-run"
    run(inputs=[source], config_path=config, out_dir=out_dir, dry_run=False)

    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["escalation"]["adapter_order"] = ["text", "evil"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    rerun_path = out_dir / "rerun-manifest.yml"
    rerun = yaml.safe_load(rerun_path.read_text(encoding="utf-8"))
    rerun["escalation"]["adapter_order"] = ["text", "evil"]
    rerun["escalation"]["next_adapter"] = "evil"
    rerun_path.write_text(yaml.safe_dump(rerun, sort_keys=False), encoding="utf-8")

    report = verify_run(out_dir)

    assert report["status"] == "fail"
    assert "manifest_escalation_mismatch" in _codes(report, "errors")
    assert "rerun_plan_mismatch" in _codes(report, "errors")


def test_verify_run_warns_when_external_source_is_missing(tmp_path):
    from pageledger.verify import verify_run

    out_dir, source = _run(tmp_path)
    source.unlink()

    report = verify_run(out_dir)

    assert report["status"] == "pass"
    assert "source_missing" in _codes(report, "warnings")


def test_portable_baseline_verification_skips_external_source_and_rerun_semantics(
    tmp_path, monkeypatch
):
    import pageledger.verify as verify_module

    out_dir, _ = _run(tmp_path)
    original_load = verify_module._load_artifact

    def guarded_load(key, path):
        if key == "rerun_manifest":
            raise AssertionError("portable verification parsed rerun-manifest.yml")
        return original_load(key, path)

    def forbidden_external_check(*args, **kwargs):
        raise AssertionError("portable verification touched an external source")

    monkeypatch.setattr(verify_module, "_load_artifact", guarded_load)
    monkeypatch.setattr(verify_module, "_check_external_sources", forbidden_external_check)

    report = verify_module.verify_run(
        out_dir,
        check_external_sources=False,
        check_rerun_manifest=False,
    )

    assert report["status"] == "pass"


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
