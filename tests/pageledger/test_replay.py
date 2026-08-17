"""Reproducibility profile contracts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

MINIMAL_CONFIG = """\
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: text
"""


def _run_text(
    tmp_path: Path,
    *,
    name: str = "run",
    config_text: str = MINIMAL_CONFIG,
    source_text: str = "first page of stable text\fsecond page of stable text\n",
    adapter_path: Path | None = None,
    pages: str | None = None,
    routes_path: Path | None = None,
    dry_run: bool = False,
) -> tuple[Path, Path, Path]:
    from pageledger.runner import run

    source = tmp_path / f"{name}-source.txt"
    source.write_text(source_text, encoding="utf-8")
    config = tmp_path / f"{name}-config.yml"
    config.write_text(config_text, encoding="utf-8")
    out = tmp_path / name
    run(
        inputs=[source],
        config_path=config,
        out_dir=out,
        dry_run=dry_run,
        adapter_path=adapter_path,
        pages=pages,
        routes_path=routes_path,
    )
    return out, source, config


def test_text_profile_is_stable_and_self_hashing() -> None:
    from pageledger.adapters import TextAdapter
    from pageledger.replay import build_reproducibility_profile, profile_sha256

    profile = build_reproducibility_profile(TextAdapter())
    assert profile is not None
    assert profile["profile_version"] == "0.1"
    assert profile["pageledger"]["version"]
    assert len(profile["pageledger"]["code_sha256"]) == 64
    assert len(profile["adapter"]["code_sha256"]) == 64
    assert profile["materials"] == []
    assert profile["profile_sha256"] == profile_sha256(profile)


def test_custom_deterministic_adapter_without_hook_has_no_profile() -> None:
    from pageledger.replay import build_reproducibility_profile

    class CustomDeterministicAdapter:
        name = "custom"
        version = "1.0"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("local",)

    assert build_reproducibility_profile(CustomDeterministicAdapter()) is None


def _run_custom_adapter_without_profile(
    tmp_path: Path, *, action: str = "review"
) -> tuple[Path, Path, Path]:
    adapter_dir = tmp_path / "adapters"
    adapter_dir.mkdir()
    (adapter_dir / "review_adapter.py").write_text(
        "from pageledger.adapters import ExtractionResult\n"
        "class ReviewAdapter:\n"
        "    name = 'review-custom'\n"
        "    version = '1.0'\n"
        "    deterministic = True\n"
        "    input_types = ('text',)\n"
        "    output_types = ('text',)\n"
        "    capabilities = ('local',)\n"
        "    def supports(self, action): return action == 'transcribe_text'\n"
        "    def extract(self, source, *, page_id, page_number, action, prompt=None):\n"
        "        return ExtractionResult(content='', format='text', confidence=None, model=None, warnings=[], usage={'pages': 1})\n",
        encoding="utf-8",
    )
    config = MINIMAL_CONFIG.replace(
        "default_action: transcribe_text", f"default_action: {action}"
    ).replace("adapter: text", "adapter: review_adapter:ReviewAdapter")
    return _run_text(tmp_path, config_text=config, adapter_path=adapter_dir)


def test_profile_rejects_nonfinite_or_unexpected_hook_data() -> None:
    from pageledger.replay import build_reproducibility_profile

    class AdapterReturning:
        name = "custom"
        version = "1.0"
        deterministic = True
        input_types = ("text",)
        output_types = ("text",)
        capabilities = ("local",)

        def reproducibility_profile(self) -> dict[str, object]:
            return {"materials": [], "path": "/tmp/x"}

    with pytest.raises(ValueError, match="reproducibility_profile"):
        build_reproducibility_profile(AdapterReturning())


def test_profile_rejects_path_material_values() -> None:
    from pageledger.replay import build_reproducibility_profile

    class AdapterReturning:
        name = "custom"
        version = "1.0"

        def reproducibility_profile(self) -> dict[str, object]:
            return {
                "materials": [
                    {
                        "kind": "asset",
                        "name": "/tmp/model",
                        "version": "1.0",
                        "sha256": "0" * 64,
                    }
                ]
            }

    with pytest.raises(ValueError, match="paths"):
        build_reproducibility_profile(AdapterReturning())


@pytest.mark.parametrize("alias", ["LATEST", "main", "master", "stable", "current", "rolling", "nightly", "HEAD", "unknown"])
def test_profile_rejects_mutable_material_version_aliases(alias: str) -> None:
    from pageledger.replay import build_reproducibility_profile

    class AdapterReturning:
        name = "custom"
        version = "1.0"

        def reproducibility_profile(self) -> dict[str, object]:
            return {
                "materials": [
                    {
                        "kind": "asset",
                        "name": "fixture",
                        "version": alias,
                        "sha256": "0" * 64,
                    }
                ]
            }

    with pytest.raises(ValueError, match="exact revision"):
        build_reproducibility_profile(AdapterReturning())


def test_model_material_uses_content_revision_when_version_is_unknown(tmp_path: Path) -> None:
    from pageledger.replay import binary_material, model_material

    model = tmp_path / "eng.traineddata"
    model.write_bytes(b"trained model bytes")
    digest = hashlib.sha256(model.read_bytes()).hexdigest()
    material = model_material("tesseract:eng.traineddata", model)
    assert material["version"] == f"sha256:{digest}"
    assert material["version"] != "unknown"

    executable = tmp_path / "tool"
    executable.write_bytes(b"tool bytes")
    executable_material = binary_material("tool", str(executable), "unknown")
    assert executable_material["version"].startswith("sha256:")
    assert executable_material["version"] != "unknown"


def test_execute_manifest_records_profile_but_provenance_does_not(tmp_path: Path) -> None:
    out, _, _ = _run_text(tmp_path)
    manifest = json.loads((out / "manifest.json").read_text())
    provenance = json.loads((out / "provenance.jsonl").read_text().splitlines()[0])
    assert manifest["extractors"][0]["reproducibility_profile"]["profile_sha256"]
    assert "reproducibility_profile" not in provenance["extractor"]


def test_review_only_custom_adapter_without_hook_gets_planned_entry(tmp_path: Path) -> None:
    out, _, _ = _run_custom_adapter_without_profile(tmp_path)
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["extractors"] == [
        {
            "name": "review-custom",
            "adapter": "review-custom",
            "model": None,
            "version": "1.0",
            "prompt_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "deterministic": True,
            "input_types": ["text"],
            "output_types": ["text"],
            "capabilities": ["local"],
        }
    ]
    assert "reproducibility_profile" not in manifest["extractors"][0]


def test_bundle_rejects_successful_custom_adapter_without_profile(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run

    run_dir, _, _ = _run_custom_adapter_without_profile(tmp_path, action="transcribe_text")
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["execution_mode"] == "execute"
    assert manifest["status"] == "completed"
    with pytest.raises(ReplayError) as exc_info:
        bundle_run(run_dir, tmp_path / "bundle")
    assert exc_info.value.code == "profile_missing"
    assert str(exc_info.value) == "Deterministic non-cloud extractor lacks a profile"


def test_bundle_text_run_has_portable_layout(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run, validate_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    result = bundle_run(run_dir, bundle_dir)
    bundle = json.loads((bundle_dir / "bundle.json").read_text())
    assert result["bundle_dir"] == str(bundle_dir.resolve())
    assert bundle["bundle_schema_version"] == "0.1"
    assert bundle["baseline"]["run_id"] == manifest["run_id"]
    assert bundle["baseline"]["manifest"] == "baseline/manifest.json"
    assert bundle["replay"]["config"] == "baseline/config-snapshot.yml"
    assert bundle["replay"]["route_map"] == "replay-route-map.yml"
    assert bundle["sources"][0]["path"] == "sources/source-0001.txt"
    assert yaml.safe_load((bundle_dir / "replay-route-map.yml").read_text())["documents"][0]["source"] == "sources/source-0001.txt"
    assert all(entry["path"] != "bundle.json" for entry in bundle["files"])
    assert validate_bundle(bundle_dir)["baseline"]["run_id"] == manifest["run_id"]


def _refresh_bundle_inventory(bundle_dir: Path) -> dict:
    bundle_path = bundle_dir / "bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["files"] = [
        {
            "path": path.relative_to(bundle_dir).as_posix(),
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(bundle_dir.rglob("*"))
        if path.is_file() and path.relative_to(bundle_dir).as_posix() != "bundle.json"
    ]
    bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return bundle


def _refresh_manifest_metadata(bundle_dir: Path) -> dict:
    bundle_path = bundle_dir / "bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    manifest_path = bundle_dir / "baseline" / "manifest.json"
    bundle["baseline"]["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _refresh_bundle_inventory(bundle_dir)


@pytest.mark.parametrize("new_name", ["sources/source-0001.TXT", "sources/nested/source-0001.txt"])
def test_bundle_rejects_noncanonical_source_filename(tmp_path: Path, new_name: str) -> None:
    from pageledger.replay import ReplayError, bundle_run, validate_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    bundle = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    old_name = bundle["sources"][0]["path"]
    old_path = bundle_dir / old_name
    new_path = bundle_dir / new_name
    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    bundle["sources"][0]["path"] = new_name
    bundle["files"] = [
        {**entry, "path": new_name} if entry["path"] == old_name else entry
        for entry in bundle["files"]
    ]
    route = yaml.safe_load((bundle_dir / "replay-route-map.yml").read_text(encoding="utf-8"))
    route["documents"][0]["source"] = new_name
    (bundle_dir / "replay-route-map.yml").write_text(yaml.safe_dump(route, sort_keys=False), encoding="utf-8")
    (bundle_dir / "bundle.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_bundle_inventory(bundle_dir)
    with pytest.raises(ReplayError):
        validate_bundle(bundle_dir)


def test_bundle_rejects_regular_file_outside_canonical_allowlist(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run, validate_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    (bundle_dir / "baseline" / "rogue.txt").write_text("undeclared", encoding="utf-8")
    _refresh_bundle_inventory(bundle_dir)
    with pytest.raises(ReplayError, match="undeclared"):
        validate_bundle(bundle_dir)


def test_bundle_nested_bundle_json_is_not_root_inventory_exemption(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run, validate_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    nested = bundle_dir / "baseline" / "nested" / "bundle.json"
    nested.parent.mkdir()
    nested.write_text("nested", encoding="utf-8")
    _refresh_bundle_inventory(bundle_dir)
    with pytest.raises(ReplayError, match="undeclared"):
        validate_bundle(bundle_dir)


def test_bundle_profile_validation_rejects_path_and_mutable_material(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run, profile_sha256, validate_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    bundle = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    profile = bundle["baseline"]["extractor"]["reproducibility_profile"]
    profile["materials"] = [{"kind": "asset", "name": "/tmp/model", "version": "latest", "sha256": "0" * 64}]
    profile["profile_sha256"] = profile_sha256(profile)
    manifest_path = bundle_dir / "baseline" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["extractors"][0]["reproducibility_profile"] = profile
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    bundle["baseline"]["extractor"]["reproducibility_profile"] = profile
    bundle["baseline"]["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (bundle_dir / "bundle.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_bundle_inventory(bundle_dir)
    with pytest.raises(ReplayError, match="paths|exact revision"):
        validate_bundle(bundle_dir)


def test_bundle_positive_prose_and_budget_token_text_is_not_scanned(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run

    config = MINIMAL_CONFIG.replace(
        "run:\n  adapter: text", "run:\n  adapter: text\n  budget:\n    max_tokens: 10"
    )
    run_dir, _, _ = _run_text(tmp_path, config_text=config, source_text="ordinary token prose\fsecond page\n")
    bundle_run(run_dir, tmp_path / "bundle")


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "api-key", "api_token", "token", "access-token", "auth_token", "bearer-token",
        "refresh_token", "client-secret", "secret_key", "password", "credential",
        "credentials", "authorization", "private-key", "access_key",
    ],
)
def test_bundle_rejects_exact_forbidden_credential_keys(tmp_path: Path, forbidden_key: str) -> None:
    from pageledger.replay import ReplayError, bundle_run, validate_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    bundle = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    bundle["baseline"]["extractor"]["options"] = {forbidden_key: "secret"}
    (bundle_dir / "bundle.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ReplayError, match="Credential option key"):
        validate_bundle(bundle_dir)


def test_bundle_rejects_dry_run(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run

    run_dir, _, _ = _run_text(tmp_path, dry_run=True)
    with pytest.raises(ReplayError, match="execute mode"):
        bundle_run(run_dir, tmp_path / "bundle")


@pytest.mark.parametrize(
    ("manifest_update", "message"),
    [
        ({"run_depth": 1}, "generation zero"),
        ({"status": "failed"}, "status"),
        ({"summary": {"pages_failed": 1}}, "failed"),
        ({"summary": {"pages_not_attempted": 1}}, "unattempted"),
    ],
)
def test_bundle_rejects_generation_and_incomplete_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest_update: dict, message: str
) -> None:
    import pageledger.verify as verify_module
    from pageledger.replay import ReplayError, bundle_run

    run_dir, _, _ = _run_text(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if "summary" in manifest_update:
        manifest["summary"].update(manifest_update["summary"])
    else:
        manifest.update(manifest_update)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(verify_module, "verify_run", lambda _: {"status": "pass"})
    with pytest.raises(ReplayError, match=message):
        bundle_run(run_dir, tmp_path / "bundle")


@pytest.mark.parametrize("source_state", ["changed", "missing"])
def test_bundle_rejects_changed_or_missing_source(tmp_path: Path, source_state: str) -> None:
    from pageledger.replay import ReplayError, bundle_run

    run_dir, source, _ = _run_text(tmp_path)
    if source_state == "changed":
        source.write_text("changed bytes", encoding="utf-8")
    else:
        source.unlink()
    with pytest.raises(ReplayError, match="[Ss]ource"):
        bundle_run(run_dir, tmp_path / "bundle")


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_bundle_rejects_linked_source(tmp_path: Path, link_kind: str) -> None:
    from pageledger.replay import ReplayError, bundle_run

    run_dir, source, _ = _run_text(tmp_path)
    if link_kind == "hardlink":
        (tmp_path / "source-alias.txt").hardlink_to(source)
    else:
        target = tmp_path / "source-target.txt"
        source.rename(target)
        source.symlink_to(target)
    with pytest.raises(ReplayError, match="Unsafe bundle file"):
        bundle_run(run_dir, tmp_path / "bundle")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported on this platform")
def test_bundle_rejects_fifo_source(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run

    run_dir, source, _ = _run_text(tmp_path)
    source.unlink()
    os.mkfifo(source)
    with pytest.raises(ReplayError, match="Unsafe bundle file"):
        bundle_run(run_dir, tmp_path / "bundle")


def test_bundle_rejects_noncanonical_manifest_artifact_key(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run

    run_dir, _, _ = _run_text(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["rogue"] = "rogue.txt"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ReplayError):
        bundle_run(run_dir, tmp_path / "bundle")


def test_bundle_rejects_wrong_manifest_artifact_path_at_canonical_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pageledger.verify as verify_module
    from pageledger.replay import ReplayError, bundle_run

    run_dir, _, _ = _run_text(tmp_path)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["audit"] = "wrong-audit.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(verify_module, "verify_run", lambda _: {"status": "pass"})
    with pytest.raises(ReplayError) as exc_info:
        bundle_run(run_dir, tmp_path / "bundle")
    assert exc_info.value.code == "artifact_declarations_invalid"
    assert str(exc_info.value) == "Manifest artifact declarations are not canonical"


def test_bundle_rejects_generation_one_from_real_rerun_api(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run
    from pageledger.runner import rerun

    parent_dir, _, config_path = _run_text(
        tmp_path, source_text="short\fclean second page with plenty of ordinary text\n"
    )
    child_dir = tmp_path / "rerun"
    result = rerun(parent_dir=parent_dir, config_path=config_path, out_dir=child_dir)
    child_manifest = json.loads((child_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result["rerun_depth"] == 1
    assert child_manifest["run_depth"] == 1
    with pytest.raises(ReplayError) as exc_info:
        bundle_run(child_dir, tmp_path / "bundle")
    assert exc_info.value.code == "run_ineligible"
    assert str(exc_info.value) == "Replay bundles require generation zero"


@pytest.mark.parametrize("unsafe_path", ["/tmp/escape", "../escape"])
def test_bundle_rejects_unsafe_inventory_paths(tmp_path: Path, unsafe_path: str) -> None:
    from pageledger.replay import ReplayError, bundle_run, validate_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    bundle = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    bundle["files"][0]["path"] = unsafe_path
    (bundle_dir / "bundle.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ReplayError, match="Unsafe bundle path"):
        validate_bundle(bundle_dir)


def test_bundle_rejects_duplicate_source_mapping(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run, validate_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    bundle = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    duplicate = dict(bundle["sources"][0])
    duplicate["index"] = 2
    bundle["sources"].append(duplicate)
    (bundle_dir / "bundle.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ReplayError, match="source paths"):
        validate_bundle(bundle_dir)


def test_bundle_rejects_wrong_canonical_artifact_path(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run, validate_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    bundle = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    bundle["replay"]["config"] = "baseline/wrong-config.yml"
    (bundle_dir / "bundle.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ReplayError, match="canonical"):
        validate_bundle(bundle_dir)


@pytest.mark.parametrize("option_mapping", ["adapter_options", "hook_options"])
def test_bundle_rejects_forbidden_key_in_config_option_mapping(
    tmp_path: Path, option_mapping: str
) -> None:
    from pageledger.replay import ReplayError, bundle_run, validate_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    config_path = bundle_dir / "baseline" / "config-snapshot.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if option_mapping == "adapter_options":
        config.setdefault("run", {})[option_mapping] = {"api_key": "secret"}
    else:
        config.setdefault("classify", {})[option_mapping] = {"token": "secret"}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    manifest_path = bundle_dir / "baseline" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["config"]["sha256"] = hashlib.sha256(config_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_manifest_metadata(bundle_dir)
    with pytest.raises(ReplayError, match="Credential option key"):
        validate_bundle(bundle_dir)


def test_bundle_positive_raw_log_citation_and_ordinary_option_values_are_not_scanned(
    tmp_path: Path,
) -> None:
    from pageledger.replay import bundle_run, validate_bundle

    config = MINIMAL_CONFIG.replace(
        "run:\n  adapter: text", "dataset_citation:\n  label: raw/log/citation token\n  text: api_key prose\nrun:\n  adapter: text"
    )
    run_dir, _, _ = _run_text(tmp_path, config_text=config, source_text="raw token prose\fsecond page\n")
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    run_log = bundle_dir / "baseline" / "run.log"
    log_lines = []
    for line in run_log.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        entry["error"] = "raw/log/citation token"
        log_lines.append(json.dumps(entry))
    run_log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    _refresh_bundle_inventory(bundle_dir)
    bundle = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    options = {"mode": "ordinary token value", "max_tokens": 10}
    bundle["baseline"]["extractor"]["options"] = options
    manifest_path = bundle_dir / "baseline" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["extractors"][0]["options"] = options
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (bundle_dir / "bundle.json").write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_manifest_metadata(bundle_dir)
    assert validate_bundle(bundle_dir)["baseline"]["run_id"] == manifest["run_id"]
