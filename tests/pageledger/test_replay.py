"""Reproducibility profile contracts."""

from __future__ import annotations

import hashlib
import json
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
        dry_run=False,
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
        "default_action: transcribe_text", "default_action: review"
    ).replace("adapter: text", "adapter: review_adapter:ReviewAdapter")
    out, _, _ = _run_text(tmp_path, config_text=config, adapter_path=adapter_dir)
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
