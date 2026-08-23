"""Reproducibility profile contracts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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

NONDETERMINISTIC_ADAPTER = """\
from pageledger.adapters import ExtractionResult

class Adapter:
    name = "cloudish"
    version = "1.0"
    deterministic = False
    input_types = ("text",)
    output_types = ("text",)
    capabilities = ("cloud",)

    def supports(self, action):
        return action == "transcribe_text"

    def reproducibility_profile(self):
        return {'materials': []}

    def extract(self, source, *, page_id, page_number, action, prompt=None):
        content = source.read_text(encoding="utf-8").split("\\f")[page_number - 1]
        return ExtractionResult(
            content=content,
            format="text",
            confidence=None,
            model="cloudish-fixed-fixture",
            warnings=[],
            usage={"pages": 1, "tokens": None, "compute_seconds": None, "cost_usd": None},
        )
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


def test_worker_envelope_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pageledger._replay_worker as worker

    bundle = tmp_path / "bundle"
    out_dir = tmp_path / "out"
    result_path = tmp_path / "result.json"
    request_id = "request-success"
    success = {
        "outcome": "exact",
        "run_id": "run-child",
        "out_dir": str(out_dir.resolve()),
        "baseline_run_id": "run-base",
        "bundle_manifest_sha256": "0" * 64,
        "profile_match": True,
        "raw": {
            "equal": 1,
            "different": 0,
            "missing": 0,
            "different_page_ids": [],
            "missing_page_ids": [],
        },
    }
    monkeypatch.setattr(worker, "_replay_bundle_in_process", lambda *args, **kwargs: success)

    assert worker.main([request_id, str(result_path), str(bundle), str(out_dir), ""]) == 0
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "protocol_version": "0.1",
        "request_id": request_id,
        "ok": True,
        "result": success,
    }


def test_worker_envelope_replay_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pageledger._replay_worker as worker
    from pageledger.replay import ReplayError

    result_path = tmp_path / "result.json"
    monkeypatch.setattr(
        worker,
        "_replay_bundle_in_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ReplayError("incompatible_environment", "profile mismatch")
        ),
    )

    assert worker.main(["request-error", str(result_path), str(tmp_path / "bundle"), str(tmp_path / "out"), ""]) == 1
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "protocol_version": "0.1",
        "request_id": "request-error",
        "ok": False,
        "error": {"code": "incompatible_environment", "message": "profile mismatch"},
    }


def test_worker_envelope_redacts_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pageledger._replay_worker as worker

    result_path = tmp_path / "result.json"
    monkeypatch.setattr(
        worker,
        "_replay_bundle_in_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret details")),
    )

    assert worker.main(["request-generic", str(result_path), str(tmp_path / "bundle"), str(tmp_path / "out"), ""]) == 1
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload == {
        "protocol_version": "0.1",
        "request_id": "request-generic",
        "ok": False,
        "error": {
            "code": "replay_worker_failed",
            "message": "Replay worker failed without a valid result.",
        },
    }
    assert "secret details" not in result_path.read_text(encoding="utf-8")


def test_worker_response_missing_file_fails_closed(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, _read_worker_response

    with pytest.raises(ReplayError) as error:
        _read_worker_response(
            tmp_path / "missing.json",
            expected_root=tmp_path,
            request_id="request",
            returncode=0,
            expected_out=tmp_path / "out",
        )
    assert error.value.code == "replay_worker_failed"
    assert str(error.value) == "Replay worker failed without a valid result."


def test_worker_response_contradictions_fail_closed(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, _read_worker_response

    output = tmp_path / "out"
    result = {
        "outcome": "exact",
        "run_id": "run",
        "out_dir": str(output.resolve()),
        "baseline_run_id": "base",
        "bundle_manifest_sha256": "0" * 64,
        "profile_match": True,
        "raw": {
            "equal": 1,
            "different": 0,
            "missing": 0,
            "different_page_ids": [],
            "missing_page_ids": [],
        },
    }
    valid = {"protocol_version": "0.1", "request_id": "id", "ok": True, "result": result}
    cases: list[tuple[str, object, int]] = [
        ("malformed", "{", 0),
        ("unknown-field", {**valid, "extra": True}, 0),
        ("missing-field", {key: value for key, value in valid.items() if key != "ok"}, 0),
        ("protocol", {**valid, "protocol_version": "9"}, 0),
        ("request", {**valid, "request_id": "other"}, 0),
        ("exit-error", {"protocol_version": "0.1", "request_id": "id", "ok": False, "error": {"code": "incompatible_environment", "message": "profile mismatch"}}, 0),
        ("exit-success", valid, 1),
        ("outcome", {**valid, "result": {**result, "outcome": "unknown"}}, 0),
        ("count", {**valid, "result": {**result, "raw": {**result["raw"], "equal": True}}}, 0),
        ("path", {**valid, "result": {**result, "out_dir": str(tmp_path / "other")}}, 0),
        ("page-ids", {**valid, "result": {**result, "raw": {**result["raw"], "different_page_ids": ["p2", "p1"]}}}, 0),
        ("overlapping-page-ids", {**valid, "result": {**result, "raw": {**result["raw"], "different": 1, "missing": 1, "different_page_ids": ["p1"], "missing_page_ids": ["p1"]}}}, 0),
        ("exact-profile", {**valid, "result": {**result, "profile_match": False}}, 0),
        ("exact-difference", {**valid, "result": {**result, "raw": {**result["raw"], "different": 1}}}, 0),
        ("mismatch-without-difference", {**valid, "result": {**result, "outcome": "deterministic_mismatch"}}, 0),
        ("mismatch-null-profile", {**valid, "result": {**result, "outcome": "deterministic_mismatch", "profile_match": None, "raw": {**result["raw"], "different": 1, "different_page_ids": ["p1"]}}}, 0),
        ("mismatch-false-profile", {**valid, "result": {**result, "outcome": "deterministic_mismatch", "profile_match": False, "raw": {**result["raw"], "different": 1, "different_page_ids": ["p1"]}}}, 0),
        ("evidence-profile", {**valid, "result": {**result, "outcome": "evidence_compared"}}, 0),
        ("page-count", {**valid, "result": {**result, "raw": {**result["raw"], "different": 1, "different_page_ids": []}}}, 0),
    ]
    for name, payload, returncode in cases:
        path = tmp_path / f"{name}.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ReplayError) as error:
            _read_worker_response(
                path,
                expected_root=tmp_path,
                request_id="id",
                returncode=returncode,
                expected_out=output,
            )
        assert error.value.code == "replay_worker_failed", name
        assert str(error.value) == "Replay worker failed without a valid result.", name

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"0" * (1_048_576 + 1))
    with pytest.raises(ReplayError) as error:
        _read_worker_response(
            oversized,
            expected_root=tmp_path,
            request_id="id",
            returncode=0,
            expected_out=output,
        )
    assert error.value.code == "replay_worker_failed"

    duplicate = tmp_path / "duplicate.json"
    encoded_result = json.dumps(result, separators=(",", ":"))
    duplicate.write_text(
        '{"protocol_version":"0.1","request_id":"id","ok":true,"result":'
        + encoded_result
        + ',"result":'
        + encoded_result
        + "}",
        encoding="utf-8",
    )
    with pytest.raises(ReplayError) as error:
        _read_worker_response(
            duplicate,
            expected_root=tmp_path,
            request_id="id",
            returncode=0,
            expected_out=output,
        )
    assert error.value.code == "replay_worker_failed"

    deeply_nested = tmp_path / "deeply-nested.json"
    deeply_nested.write_text("[" * 2_000 + "0" + "]" * 2_000, encoding="utf-8")
    with pytest.raises(ReplayError) as error:
        _read_worker_response(
            deeply_nested,
            expected_root=tmp_path,
            request_id="id",
            returncode=0,
            expected_out=output,
        )
    assert error.value.code == "replay_worker_failed"


def test_worker_response_preserves_known_error_and_success(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, _read_worker_response

    output = tmp_path / "out"
    error_path = tmp_path / "error.json"
    error_path.write_text(
        json.dumps(
            {
                "protocol_version": "0.1",
                "request_id": "error",
                "ok": False,
                "error": {"code": "incompatible_environment", "message": "profile mismatch"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ReplayError) as error:
        _read_worker_response(
            error_path,
            expected_root=tmp_path,
            request_id="error",
            returncode=1,
            expected_out=output,
        )
    assert error.value.code == "incompatible_environment"
    assert str(error.value) == "profile mismatch"

    result = {
        "outcome": "exact",
        "run_id": "run",
        "out_dir": str(output.resolve()),
        "baseline_run_id": "base",
        "bundle_manifest_sha256": "0" * 64,
        "profile_match": True,
        "raw": {"equal": 1, "different": 0, "missing": 0, "different_page_ids": [], "missing_page_ids": []},
    }
    success_path = tmp_path / "success.json"
    success_path.write_text(json.dumps({"protocol_version": "0.1", "request_id": "success", "ok": True, "result": result}), encoding="utf-8")
    assert _read_worker_response(success_path, expected_root=tmp_path, request_id="success", returncode=0, expected_out=output) == result


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


def test_private_validate_reproducibility_profile_returns_validated_envelope() -> None:
    from pageledger.adapters import TextAdapter
    from pageledger.replay import (
        _validate_reproducibility_profile,
        build_reproducibility_profile,
    )

    adapter = TextAdapter()
    profile = build_reproducibility_profile(adapter)
    assert profile is not None
    identity = {
        "adapter": adapter.name,
        "version": adapter.version,
    }

    assert _validate_reproducibility_profile(profile, identity) == profile


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


def test_relocated_text_replay_is_exact_without_original_source(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    run_dir, source, _ = _run_text(tmp_path)
    bundle_run(run_dir, tmp_path / "bundle")
    source.unlink()
    moved = tmp_path / "other-root" / "bundle"
    moved.parent.mkdir()
    shutil.copytree(tmp_path / "bundle", moved)
    result = replay_bundle(moved, tmp_path / "replayed")
    assert result["outcome"] == "exact"
    assert result["raw"]["different"] == 0
    assert result["bundle_manifest_sha256"] == hashlib.sha256(
        (moved / "bundle.json").read_bytes()
    ).hexdigest()
    assert verify_run(tmp_path / "replayed")["status"] == "pass"


def test_relocated_replay_never_hashes_original_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pageledger.verify as verify_module
    from pageledger.replay import bundle_run, replay_bundle

    run_dir, source, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    original_hash = verify_module._sha256
    original_path = source.resolve()

    def guarded_hash(path: Path) -> str:
        if path.resolve() == original_path:
            raise AssertionError("replay hashed the original external source")
        return original_hash(path)

    monkeypatch.setattr(verify_module, "_sha256", guarded_hash)
    result = replay_bundle(bundle_dir, tmp_path / "replayed")
    assert result["outcome"] == "exact"


def test_custom_adapter_profile_mismatch_fails_before_output_creation(
    tmp_path: Path,
) -> None:
    from pageledger.replay import ReplayError, bundle_run, replay_bundle

    adapter_dir = tmp_path / "trusted-adapters"
    adapter_dir.mkdir()
    adapter = (
        "from pageledger.adapters import ExtractionResult\n"
        "class Adapter:\n"
        "    name = 'profile-fixture'\n"
        "    version = '1.0'\n"
        "    deterministic = True\n"
        "    input_types = ('text',)\n"
        "    output_types = ('text',)\n"
        "    capabilities = ('local',)\n"
        "    def supports(self, action): return action == 'transcribe_text'\n"
        "    def reproducibility_profile(self):\n"
        "        return {'materials': [{'kind': 'asset', 'name': 'fixture', 'version': VERSION, 'sha256': '" + "0" * 64 + "'}]}\n"
        "    def extract(self, source, *, page_id, page_number, action, prompt=None):\n"
        "        return ExtractionResult(content='stable', format='text', confidence=None, model=None, warnings=[], usage={'pages': 1})\n"
    )
    (adapter_dir / "profile_fixture.py").write_text("VERSION = 'A'\n" + adapter, encoding="utf-8")
    config = MINIMAL_CONFIG.replace("adapter: text", "adapter: profile_fixture:Adapter")
    run_dir, _, _ = _run_text(tmp_path, config_text=config, adapter_path=adapter_dir)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    (adapter_dir / "profile_fixture.py").write_text("VERSION = 'B'\n" + adapter, encoding="utf-8")
    with pytest.raises(ReplayError) as error:
        replay_bundle(bundle_dir, tmp_path / "never-created", adapter_path=adapter_dir)
    assert error.value.code == "incompatible_environment"
    assert not (tmp_path / "never-created").exists()


def test_nondeterministic_adapter_is_evidence_compared(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run, replay_bundle

    adapter_dir = tmp_path / "trusted-adapters"
    adapter_dir.mkdir()
    (adapter_dir / "cloudish.py").write_text(NONDETERMINISTIC_ADAPTER, encoding="utf-8")
    config = MINIMAL_CONFIG.replace("adapter: text", "adapter: cloudish:Adapter")
    run_dir, _, _ = _run_text(tmp_path, config_text=config, adapter_path=adapter_dir)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    result = replay_bundle(bundle_dir, tmp_path / "replayed", adapter_path=adapter_dir)
    assert result["outcome"] == "evidence_compared"
    baseline = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    replay_manifest = json.loads((tmp_path / "replayed" / "manifest.json").read_text(encoding="utf-8"))
    replay_evidence = json.loads((tmp_path / "replayed" / "replay.json").read_text(encoding="utf-8"))
    expected_profile = baseline["extractors"][0]["reproducibility_profile"]
    assert replay_manifest["extractors"][0]["reproducibility_profile"] == expected_profile
    assert replay_evidence["local_extractor"]["reproducibility_profile_sha256"] == expected_profile["profile_sha256"]
    assert replay_evidence["profile_match"] is None


def test_matching_profile_raw_mismatch_is_inspectable_and_verifiable(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.verify import verify_run

    adapter_dir = tmp_path / "trusted-adapters"
    adapter_dir.mkdir()
    state = tmp_path / "adapter-state.txt"
    state.write_text("0", encoding="utf-8")
    (adapter_dir / "stateful.py").write_text(
        "from pageledger.adapters import ExtractionResult\n"
        "from pathlib import Path\n"
        f"STATE = Path({str(state)!r})\n"
        "class Adapter:\n"
        "    name = 'stateful'\n"
        "    version = '1.0'\n"
        "    deterministic = True\n"
        "    input_types = ('text',)\n"
        "    output_types = ('text',)\n"
        "    capabilities = ('local',)\n"
        "    def supports(self, action): return action == 'transcribe_text'\n"
        "    def reproducibility_profile(self): return {'materials': []}\n"
        "    def extract(self, source, *, page_id, page_number, action, prompt=None):\n"
        "        value = int(STATE.read_text()) + 1\n"
        "        STATE.write_text(str(value))\n"
        "        return ExtractionResult(content=f'variant-{value}', format='text', confidence=None, model='stateful', warnings=[], usage={'pages': 1})\n",
        encoding="utf-8",
    )
    config = MINIMAL_CONFIG.replace("adapter: text", "adapter: stateful:Adapter")
    run_dir, _, _ = _run_text(tmp_path, config_text=config, adapter_path=adapter_dir)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replayed = tmp_path / "replayed"
    result = replay_bundle(bundle_dir, replayed, adapter_path=adapter_dir)
    assert result["outcome"] == "deterministic_mismatch"
    assert result["raw"]["different"] == 2
    assert (replayed / "replay.json").is_file()
    assert verify_run(replayed)["status"] == "pass"


def test_single_adapter_instance_is_reused_through_replay(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run, replay_bundle

    adapter_dir = tmp_path / "trusted-adapters"
    adapter_dir.mkdir()
    counter = tmp_path / "adapter-constructions.txt"
    counter.write_text("0", encoding="utf-8")
    (adapter_dir / "constructor_stateful.py").write_text(
        "from pageledger.adapters import ExtractionResult\n"
        "from pathlib import Path\n"
        f"COUNTER = Path({str(counter)!r})\n"
        "class Adapter:\n"
        "    name = 'constructor-stateful'\n"
        "    version = '1.0'\n"
        "    deterministic = True\n"
        "    input_types = ('text',)\n"
        "    output_types = ('text',)\n"
        "    capabilities = ('local',)\n"
        "    def __init__(self):\n"
        "        value = int(COUNTER.read_text(encoding='utf-8')) + 1\n"
        "        COUNTER.write_text(str(value), encoding='utf-8')\n"
        "        self.variant = f'variant-{value}'\n"
        "    def supports(self, action): return action == 'transcribe_text'\n"
        "    def reproducibility_profile(self): return {'materials': []}\n"
        "    def extract(self, source, *, page_id, page_number, action, prompt=None):\n"
        "        return ExtractionResult(content=self.variant, format='text', confidence=None, model='stateful', warnings=[], usage={'pages': 1})\n",
        encoding="utf-8",
    )
    config = MINIMAL_CONFIG.replace(
        "adapter: text", "adapter: constructor_stateful:Adapter"
    )
    run_dir, _, _ = _run_text(tmp_path, config_text=config, adapter_path=adapter_dir)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    counter.write_text("0", encoding="utf-8")

    replayed = tmp_path / "replayed"
    result = replay_bundle(bundle_dir, replayed, adapter_path=adapter_dir)

    assert result["outcome"] == "exact"
    assert counter.read_text(encoding="utf-8") == "1"
    assert (replayed / "raw" / "doc_0001_page_0001.txt").read_text(
        encoding="utf-8"
    ) == "variant-1"


def test_pdf_text_exact_replay(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.runner import run
    from pageledger.verify import verify_run

    source = tmp_path / "document.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with source.open("wb") as handle:
        writer.write(handle)
    config = tmp_path / "pdf.yml"
    config.write_text(MINIMAL_CONFIG.replace("adapter: text", "adapter: pdf_text"), encoding="utf-8")
    run_dir = tmp_path / "run"
    run(inputs=[source], config_path=config, out_dir=run_dir, dry_run=False)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    source.unlink()
    result = replay_bundle(bundle_dir, tmp_path / "replayed")
    assert result["outcome"] == "exact"
    assert verify_run(tmp_path / "replayed")["status"] == "pass"


def test_recorded_pages_selection_is_replayed(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run, replay_bundle

    run_dir, _, _ = _run_text(tmp_path, pages="2")
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    result = replay_bundle(bundle_dir, tmp_path / "replayed")
    assert result["outcome"] == "exact"
    replay_manifest = json.loads((tmp_path / "replayed" / "manifest.json").read_text())
    assert replay_manifest["inputs"][0]["pages"] == "2"
    assert replay_manifest["summary"]["pages_extracted"] == 1


def test_routed_partial_review_replay_preserves_review_routes(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.runner import run

    base, source, config = _run_text(tmp_path, name="base")
    route = yaml.safe_load((base / "route-map.yml").read_text())
    for document in route["documents"]:
        for page in document["pages"]:
            page["action"] = "review"
            page["reason"] = "explicit review"
    supplied_route = tmp_path / "review-route.yml"
    supplied_route.write_text(yaml.safe_dump(route, sort_keys=False), encoding="utf-8")
    run_dir = tmp_path / "routed"
    run(inputs=[source], config_path=config, out_dir=run_dir, dry_run=False, routes_path=supplied_route)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    result = replay_bundle(bundle_dir, tmp_path / "replayed")
    assert result["outcome"] == "exact"
    replay_manifest = json.loads((tmp_path / "replayed" / "manifest.json").read_text())
    assert replay_manifest["status"] == "partial"
    assert replay_manifest["summary"]["pages_routed_review"] == 2


def test_multi_input_replay_preserves_order(tmp_path: Path) -> None:
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.runner import run

    source_a = tmp_path / "a.txt"
    source_b = tmp_path / "b.txt"
    source_a.write_text("alpha\n", encoding="utf-8")
    source_b.write_text("beta\n", encoding="utf-8")
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    run_dir = tmp_path / "run"
    run(inputs=[source_a, source_b], config_path=config, out_dir=run_dir, dry_run=False)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    replay_bundle(bundle_dir, tmp_path / "replayed")
    provenance = [
        json.loads(line)
        for line in (tmp_path / "replayed" / "provenance.jsonl").read_text().splitlines()
    ]
    assert [entry["source"]["path"] for entry in provenance] == [
        str((tmp_path / "bundle" / "sources" / "source-0001.txt").resolve()),
        str((tmp_path / "bundle" / "sources" / "source-0002.txt").resolve()),
    ]


def test_external_alignment_snapshot_replays_without_original_schema(
    tmp_path: Path,
) -> None:
    from pageledger.aligner import align_run
    from pageledger.replay import bundle_run, replay_bundle
    from pageledger.runner import run

    adapter_dir = tmp_path / "trusted-adapters"
    adapter_dir.mkdir()
    (adapter_dir / "tableish.py").write_text(
        "from pageledger.adapters import ExtractionResult\n"
        "class Adapter:\n"
        "    name = 'tableish'\n"
        "    version = '1.0'\n"
        "    deterministic = True\n"
        "    input_types = ('text',)\n"
        "    output_types = ('markdown_table',)\n"
        "    capabilities = ('local',)\n"
        "    def supports(self, action): return action == 'transcribe_text'\n"
        "    def reproducibility_profile(self): return {'materials': []}\n"
        "    def extract(self, source, *, page_id, page_number, action, prompt=None):\n"
        "        return ExtractionResult(content='| place | total |\\n| --- | --- |\\n| X | 1 |', format='markdown_table', confidence=None, model='tableish', warnings=[], usage={'pages': 1})\n",
        encoding="utf-8",
    )
    source = tmp_path / "table.txt"
    source.write_text("table\n", encoding="utf-8")
    config = tmp_path / "table.yml"
    config.write_text(MINIMAL_CONFIG.replace("adapter: text", "adapter: tableish:Adapter"), encoding="utf-8")
    run_dir = tmp_path / "run"
    run(inputs=[source], config_path=config, out_dir=run_dir, dry_run=False, adapter_path=adapter_dir)
    schema = tmp_path / "external-schema.yml"
    schema.write_text(
        "name: table\ncolumns:\n  - {name: place, type: string, required: true}\n  - {name: total, type: integer, required: true}\n",
        encoding="utf-8",
    )
    align_run(run_dir, schema_path=schema)
    schema.unlink()
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    result = replay_bundle(bundle_dir, tmp_path / "replayed", adapter_path=adapter_dir)
    assert result["outcome"] == "exact"
    replay_manifest = json.loads((tmp_path / "replayed" / "manifest.json").read_text())
    assert replay_manifest["alignment"]["schema_sha256"]
    assert (tmp_path / "replayed" / "normalized" / "doc_0001_page_0001.json").is_file()


def test_replay_blocks_implicit_bundle_adapter_import_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from pageledger.replay import ReplayError, bundle_run, replay_bundle
    from pageledger.runner import run

    trusted = tmp_path / "trusted"
    trusted.mkdir()
    marker = tmp_path / "bundle-import-marker"
    (trusted / "source-0001.py").write_text(
        "from pageledger.adapters import ExtractionResult\n"
        "class Adapter:\n"
        "    name = 'source-0001'\n"
        "    version = '1.0'\n"
        "    deterministic = False\n"
        "    input_types = ('text',)\n"
        "    output_types = ('text',)\n"
        "    capabilities = ('local',)\n"
        "    def supports(self, action): return action == 'transcribe_text'\n"
        "    def extract(self, source, *, page_id, page_number, action, prompt=None):\n"
        "        return ExtractionResult(content='safe', format='text', confidence=None, model='safe', warnings=[], usage={'pages': 1})\n",
        encoding="utf-8",
    )
    payload = tmp_path / "payload.py"
    payload.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG.replace("adapter: text", "adapter: source-0001:Adapter"), encoding="utf-8")
    run_dir = tmp_path / "run"
    run(inputs=[payload], config_path=config, out_dir=run_dir, dry_run=False, adapter_path=trusted)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    sys.modules.pop("source-0001", None)
    sys.path[:] = [entry for entry in sys.path if entry != str(trusted.resolve())]
    monkeypatch.chdir(bundle_dir / "sources")
    original_path = list(sys.path)
    sys.path.insert(0, str(bundle_dir / "sources"))
    active_path = list(sys.path)
    try:
        with pytest.raises(ReplayError) as error:
            replay_bundle(bundle_dir, tmp_path / "never-created")
        assert error.value.code in {"extractor_identity_mismatch", "incompatible_environment"}
        assert not marker.exists()
        assert sys.path == active_path
    finally:
        sys.path[:] = original_path


def test_replay_trusted_adapter_path_succeeds_and_restores_import_state(
    tmp_path: Path,
) -> None:
    from pageledger.replay import bundle_run, replay_bundle

    adapter_dir = tmp_path / "trusted-adapters"
    adapter_dir.mkdir()
    (adapter_dir / "safe.py").write_text(
        "from pageledger.adapters import ExtractionResult\n"
        "class Adapter:\n"
        "    name = 'safe'\n"
        "    version = '1.0'\n"
        "    deterministic = False\n"
        "    input_types = ('text',)\n"
        "    output_types = ('text',)\n"
        "    capabilities = ('local',)\n"
        "    def supports(self, action): return action == 'transcribe_text'\n"
        "    def extract(self, source, *, page_id, page_number, action, prompt=None):\n"
        "        return ExtractionResult(content='safe', format='text', confidence=None, model='safe', warnings=[], usage={'pages': 1})\n",
        encoding="utf-8",
    )
    config = MINIMAL_CONFIG.replace("adapter: text", "adapter: safe:Adapter")
    run_dir, _, _ = _run_text(tmp_path, config_text=config, adapter_path=adapter_dir)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    import sys

    before = list(sys.path)
    result = replay_bundle(bundle_dir, tmp_path / "replayed", adapter_path=adapter_dir)
    assert result["outcome"] == "evidence_compared"
    assert sys.path == before


def test_replay_trusted_adapter_executes_in_fresh_child_without_parent_import_changes(
    tmp_path: Path,
) -> None:
    import sys

    from pageledger.replay import bundle_run, replay_bundle

    module_name = "replay_ab_adapter"
    dependency_name = "replay_ab_fresh_dependency"
    adapter_a = tmp_path / "adapter-a"
    adapter_b = tmp_path / "adapter-b"
    adapter_a.mkdir()
    adapter_b.mkdir()
    common = (
        "from pageledger.adapters import ExtractionResult\n"
        "class Adapter:\n"
        "    name = 'same-module'\n"
        "    version = '1.0'\n"
        "    deterministic = False\n"
        "    input_types = ('text',)\n"
        "    output_types = ('text',)\n"
        "    capabilities = ('local',)\n"
        "    def supports(self, action): return action == 'transcribe_text'\n"
        "    def extract(self, source, *, page_id, page_number, action, prompt=None):\n"
        "        return ExtractionResult(content=MARKER, format='text', confidence=None, model=None, warnings=[], usage={'pages': 1})\n"
    )
    (adapter_a / f"{module_name}.py").write_text(
        common.replace("MARKER", repr("A")), encoding="utf-8"
    )
    (adapter_b / f"{module_name}.py").write_text(
        (f"import {dependency_name}\n" + common).replace("MARKER", repr("B")),
        encoding="utf-8",
    )
    (adapter_b / f"{dependency_name}.py").write_text("VALUE = 'fresh'\n", encoding="utf-8")
    config = MINIMAL_CONFIG.replace("adapter: text", f"adapter: {module_name}:Adapter")
    run_dir, _, _ = _run_text(tmp_path, config_text=config, adapter_path=adapter_a)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)

    before_modules = dict(sys.modules)
    before_path = list(sys.path)
    replayed = tmp_path / "replayed"
    replay_bundle(bundle_dir, replayed, adapter_path=adapter_b)

    raw = next((replayed / "raw").glob("*"))
    assert raw.read_text(encoding="utf-8") == "B"
    assert sys.path == before_path
    assert sys.modules == before_modules
    assert dependency_name not in sys.modules


def test_replay_isolates_stale_transitive_dependency_and_executes_trusted_path(
    tmp_path: Path,
) -> None:
    import sys

    from pageledger.replay import bundle_run, replay_bundle

    adapter_a = tmp_path / "adapter-a"
    adapter_b = tmp_path / "adapter-b"
    adapter_a.mkdir()
    adapter_b.mkdir()
    adapter_source = (
        "from pageledger.adapters import ExtractionResult\n"
        "from shared_dependency import VALUE\n"
        "class Adapter:\n"
        "    name = 'transitive-fixture'\n"
        "    version = '1.0'\n"
        "    deterministic = True\n"
        "    input_types = ('text',)\n"
        "    output_types = ('text',)\n"
        "    capabilities = ('local',)\n"
        "    def supports(self, action): return action == 'transcribe_text'\n"
        "    def reproducibility_profile(self): return {'materials': []}\n"
        "    def extract(self, source, *, page_id, page_number, action, prompt=None):\n"
        "        return ExtractionResult(content=VALUE, format='text', confidence=None, model=None, warnings=[], usage={'pages': 1})\n"
    )
    for directory, value in ((adapter_a, "A"), (adapter_b, "B")):
        (directory / "transitive_fixture.py").write_text(adapter_source, encoding="utf-8")
        (directory / "shared_dependency.py").write_text(f"VALUE = '{value}'\n", encoding="utf-8")
    config = MINIMAL_CONFIG.replace("adapter: text", "adapter: transitive_fixture:Adapter")
    run_dir, _, _ = _run_text(
        tmp_path,
        config_text=config,
        source_text="one page\n",
        adapter_path=adapter_a,
    )
    cached_a_dependency = sys.modules["shared_dependency"]
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    original_path = list(sys.path)
    replayed = tmp_path / "replayed"
    result = replay_bundle(bundle_dir, replayed, adapter_path=adapter_b)
    assert result["outcome"] == "deterministic_mismatch"
    assert result["raw"]["different"] == 1
    assert (replayed / "raw" / "doc_0001_page_0001.txt").read_text(encoding="utf-8") == "B"
    assert sys.modules["shared_dependency"] is cached_a_dependency
    assert sys.path == original_path


def test_profile_hook_cannot_import_bundle_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from pageledger.replay import ReplayError, bundle_run, replay_bundle
    from pageledger.runner import run

    trusted = tmp_path / "trusted"
    trusted.mkdir()
    marker = tmp_path / "profile-hook-marker"
    (trusted / "trusted_adapter.py").write_text(
        "from pageledger.adapters import ExtractionResult\n"
        "class Adapter:\n"
        "    name = 'trusted'\n"
        "    version = '1.0'\n"
        "    deterministic = True\n"
        "    input_types = ('text',)\n"
        "    output_types = ('text',)\n"
        "    capabilities = ('local',)\n"
        "    def supports(self, action): return action == 'transcribe_text'\n"
        "    def reproducibility_profile(self):\n"
        "        import importlib\n"
        "        importlib.import_module('source-0001')\n"
        "        return {'materials': []}\n"
        "    def extract(self, source, *, page_id, page_number, action, prompt=None):\n"
        "        return ExtractionResult(content='safe', format='text', confidence=None, model='safe', warnings=[], usage={'pages': 1})\n",
        encoding="utf-8",
    )
    (trusted / "source-0001.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    payload = tmp_path / "source-0001.py"
    payload.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yml"
    config.write_text(MINIMAL_CONFIG.replace("adapter: text", "adapter: trusted_adapter:Adapter"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "run"
    run(inputs=[payload], config_path=config, out_dir=run_dir, dry_run=False, adapter_path=trusted)
    assert marker.exists()
    marker.unlink()
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    (trusted / "source-0001.py").unlink()
    sys.modules.pop("source-0001", None)
    sys.modules.pop("trusted_adapter", None)
    trusted_entry = str(trusted.resolve())
    sys.path[:] = [entry for entry in sys.path if entry != trusted_entry]
    monkeypatch.chdir(bundle_dir / "sources")
    with pytest.raises(ReplayError) as error:
        replay_bundle(bundle_dir, tmp_path / "never-created", adapter_path=trusted)
    assert error.value.code == "incompatible_environment"
    assert not marker.exists()
    assert not (tmp_path / "never-created").exists()


def test_replay_rejects_tampered_bundle_before_output_creation(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run, replay_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    (bundle_dir / "sources" / "source-0001.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(ReplayError) as error:
        replay_bundle(bundle_dir, tmp_path / "never-created")
    assert error.value.code in {"bundle_hash_mismatch", "source_hash_mismatch"}
    assert not (tmp_path / "never-created").exists()


def test_bundle_rejects_route_decision_tampering_after_inventory_refresh(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run, replay_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    route_path = bundle_dir / "replay-route-map.yml"
    route = yaml.safe_load(route_path.read_text(encoding="utf-8"))
    route["documents"][0]["pages"][0]["action"] = "skip"
    route["documents"][0]["pages"][0]["reason"] = "attacker changed the plan"
    route_path.write_text(yaml.safe_dump(route, sort_keys=False), encoding="utf-8")
    _refresh_bundle_inventory(bundle_dir)

    output = tmp_path / "never-created"
    with pytest.raises(ReplayError) as error:
        replay_bundle(bundle_dir, output)
    assert error.value.code == "route_map_mismatch"
    assert not output.exists()


def test_replay_rejects_adapter_path_inside_bundle(tmp_path: Path) -> None:
    from pageledger.replay import ReplayError, bundle_run, replay_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    with pytest.raises(ReplayError) as error:
        replay_bundle(bundle_dir, tmp_path / "never-created", adapter_path=bundle_dir)
    assert error.value.code == "adapter_path_inside_bundle"
    assert not (tmp_path / "never-created").exists()


def test_replay_worker_startup_is_fixed_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import sys

    import pageledger.replay as replay_module
    from pageledger.replay import replay_bundle

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    output = tmp_path / "output"
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> object:
        captured["command"] = command
        captured["kwargs"] = kwargs
        count = int(command[5])
        worker_args = command[6 + count :]
        result_path = Path(worker_args[1])
        expected_out = worker_args[3]
        result = {
            "outcome": "exact",
            "run_id": "child",
            "out_dir": expected_out,
            "baseline_run_id": "base",
            "bundle_manifest_sha256": "0" * 64,
            "profile_match": True,
            "raw": {
                "equal": 0,
                "different": 0,
                "missing": 0,
                "different_page_ids": [],
                "missing_page_ids": [],
            },
        }
        result_path.write_text(
            json.dumps(
                {
                    "protocol_version": "0.1",
                    "request_id": worker_args[0],
                    "ok": True,
                    "result": result,
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(replay_module.subprocess, "run", fake_run)
    replay_bundle(bundle, output)
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:4] == [sys.executable, "-I", "-S", "-c"]
    assert command[4] == replay_module._WORKER_BOOTSTRAP
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["check"] is False
    assert kwargs["cwd"] != Path.cwd()
    assert kwargs["cwd"] != output
    assert Path(command[-2]) == output.resolve()


def test_replay_ignores_pythonpath_pageledger_and_sitecustomize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pageledger.replay import bundle_run, replay_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    poison = tmp_path / "poison"
    (poison / "pageledger").mkdir(parents=True)
    marker = tmp_path / "sitecustomize-marker"
    (poison / "pageledger" / "__init__.py").write_text(
        "raise RuntimeError('poison pageledger imported')\n", encoding="utf-8"
    )
    (poison / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(poison))
    result = replay_bundle(bundle_dir, tmp_path / "replayed")
    assert result["outcome"] == "exact"
    assert not marker.exists()


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


@pytest.mark.parametrize("alias", ["./baseline/audit.json", "baseline//audit.json", "baseline/./audit.json"])
def test_bundle_rejects_noncanonical_inventory_aliases(tmp_path: Path, alias: str) -> None:
    from pageledger.replay import ReplayError, bundle_run, validate_bundle

    run_dir, _, _ = _run_text(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_run(run_dir, bundle_dir)
    bundle_path = bundle_dir / "bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    for entry in bundle["files"]:
        if entry["path"] == "baseline/audit.json":
            entry["path"] = alias
            break
    else:
        pytest.fail("baseline audit inventory entry missing")
    bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(ReplayError) as error:
        validate_bundle(bundle_dir)
    assert error.value.code == "bundle_path_unsafe"


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
