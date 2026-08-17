"""Canonical reproducibility profiles used by verified replay."""

from __future__ import annotations

import hashlib
import inspect
import json
import locale
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import uuid
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Any, cast

import yaml

from ._version import __version__
from .artifacts import ARTIFACT_PATHS

PROFILE_VERSION = "0.1"
_MATERIAL_KINDS = {"binary", "package", "model", "asset"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MUTABLE_VERSION_ALIASES = {
    "current",
    "head",
    "latest",
    "main",
    "master",
    "nightly",
    "rolling",
    "stable",
    "unknown",
    "unversioned",
}

_BUNDLE_VERSION = "0.1"
_CANONICAL_ARTIFACTS = dict(ARTIFACT_PATHS)
_FORBIDDEN_OPTION_KEYS = {
    "apikey",
    "apitoken",
    "token",
    "accesstoken",
    "authtoken",
    "bearertoken",
    "refreshtoken",
    "clientsecret",
    "secretkey",
    "password",
    "credential",
    "credentials",
    "authorization",
    "privatekey",
    "accesskey",
}


class ReplayError(ValueError):
    """A structured failure at the verified replay transport boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def profile_sha256(profile: Mapping[str, object]) -> str:
    """Hash a profile after removing its self-referential digest."""
    payload = dict(profile)
    payload.pop("profile_sha256", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_reproducibility_profile(adapter: Any) -> dict[str, Any] | None:
    """Build PageLedger's strict, path-free profile envelope for *adapter*."""
    hook = getattr(adapter, "reproducibility_profile", None)
    if hook is None:
        return None
    if not callable(hook):
        raise ValueError("adapter reproducibility_profile must be callable")
    try:
        supplied = hook()
        materials = _validate_materials(supplied)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"adapter reproducibility_profile is invalid: {exc}") from exc

    profile: dict[str, Any] = {
        "profile_version": PROFILE_VERSION,
        "pageledger": {
            "version": __version__,
            "code_sha256": _package_code_sha256(),
        },
        "adapter": {
            "module": type(adapter).__module__,
            "name": adapter.name,
            "version": adapter.version,
            "code_sha256": _adapter_code_sha256(adapter),
        },
        "runtime": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "preferred_encoding": locale.getpreferredencoding(False),
            "filesystem_encoding": sys.getfilesystemencoding(),
        },
        "materials": sorted(materials, key=lambda item: (item["kind"], item["name"])),
    }
    profile["profile_sha256"] = profile_sha256(profile)
    return profile


def _validate_materials(supplied: object) -> list[dict[str, str]]:
    if not isinstance(supplied, Mapping):
        raise ValueError("adapter reproducibility_profile must be a mapping")
    if set(supplied) != {"materials"}:
        raise ValueError(
            "adapter reproducibility_profile must contain exactly the 'materials' key"
        )
    materials = supplied["materials"]
    if not isinstance(materials, list):
        raise ValueError("adapter reproducibility_profile materials must be a list")

    validated: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, material in enumerate(materials):
        if not isinstance(material, Mapping):
            raise ValueError(
                f"adapter reproducibility_profile material {index} must be a mapping"
            )
        if set(material) != {"kind", "name", "version", "sha256"}:
            raise ValueError(
                "adapter reproducibility_profile materials must contain exactly "
                "kind, name, version, and sha256"
            )
        kind = material["kind"]
        name = material["name"]
        version = material["version"]
        digest = material["sha256"]
        if not all(isinstance(value, str) for value in (kind, name, version, digest)):
            raise ValueError(
                "adapter reproducibility_profile material fields must be strings"
            )
        if kind not in _MATERIAL_KINDS:
            raise ValueError(
                f"adapter reproducibility_profile material kind is invalid: {kind!r}"
            )
        if not name or not version:
            raise ValueError(
                "adapter reproducibility_profile material name and version must be non-empty"
            )
        if _looks_like_path(name) or _looks_like_path(version):
            raise ValueError(
                "adapter reproducibility_profile material names and versions must not contain paths"
            )
        if version.casefold() in _MUTABLE_VERSION_ALIASES:
            raise ValueError(
                "adapter reproducibility_profile material version must be an exact revision, not a mutable alias"
            )
        if _SHA256.fullmatch(digest) is None:
            raise ValueError(
                "adapter reproducibility_profile material sha256 must be lowercase SHA-256"
            )
        identity = (kind, name)
        if identity in seen:
            raise ValueError(
                "adapter reproducibility_profile materials must have unique kind/name pairs"
            )
        seen.add(identity)
        validated.append(
            {"kind": kind, "name": name, "version": version, "sha256": digest}
        )

    try:
        json.dumps(supplied, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"adapter reproducibility_profile must contain finite JSON data: {exc}"
        ) from exc
    return validated


def _looks_like_path(value: str) -> bool:
    return (
        value.startswith(("/", "\\", "~"))
        or "/" in value
        or "\\" in value
        or bool(re.match(r"^[A-Za-z]:[\\/]", value))
    )


def _hash_named_files(files: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for relative, path in sorted(files, key=lambda item: item[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            if not path.is_file():
                continue
            digest.update(path.read_bytes())
        except OSError as exc:
            raise ValueError(f"Cannot hash reproducibility material '{relative}': {exc}") from exc
    return digest.hexdigest()


def _package_code_sha256() -> str:
    package_dir = Path(__file__).resolve().parent
    files = [
        (path.relative_to(package_dir).as_posix(), path)
        for path in package_dir.glob("*.py")
        if path.is_file()
    ]
    return _hash_named_files(files)


def _adapter_code_sha256(adapter: Any) -> str:
    source = inspect.getsourcefile(type(adapter))
    if not source:
        raise ValueError("Cannot locate regular source/module file for adapter code")
    path = Path(source)
    if not path.is_file():
        raise ValueError("Cannot locate regular source/module file for adapter code")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"Cannot read adapter source/module file: {exc}") from exc


def package_material(name: str) -> dict[str, str]:
    """Return a deterministic material descriptor for an installed distribution."""
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError as exc:
        raise ValueError(f"Cannot locate package distribution '{name}'") from exc
    files = distribution.files
    if files is None:
        raise ValueError(f"Cannot enumerate files for package distribution '{name}'")
    named_files: list[tuple[str, Path]] = []
    for relative in files:
        path = Path(str(distribution.locate_file(relative)))
        if path.is_file():
            named_files.append((Path(relative).as_posix(), path))
    if not named_files:
        raise ValueError(f"Cannot hash files for package distribution '{name}'")
    return {
        "kind": "package",
        "name": name,
        "version": distribution.version,
        "sha256": _hash_named_files(named_files),
    }


def binary_material(name: str, path: str, version: str) -> dict[str, str]:
    """Return a material descriptor for an executable without storing its path."""
    executable = Path(path)
    if not executable.is_file():
        raise ValueError(f"Cannot locate regular executable for '{name}'")
    try:
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"Cannot hash executable for '{name}': {exc}") from exc
    if version.casefold() in _MUTABLE_VERSION_ALIASES or version in {name}:
        version = f"sha256:{digest}"
    return {"kind": "binary", "name": name, "version": version, "sha256": digest}


def model_material(name: str, path: Path, version: str = "unknown") -> dict[str, str]:
    """Return a material descriptor for a trained model file."""
    if not path.is_file():
        raise ValueError(f"Cannot locate trained-data model '{name}'")
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"Cannot hash trained-data model '{name}': {exc}") from exc
    if version.casefold() in _MUTABLE_VERSION_ALIASES:
        version = f"sha256:{digest}"
    return {"kind": "model", "name": name, "version": version, "sha256": digest}


def bundle_run(run_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Create an inspectable, portable bundle from a verified execute run."""
    from .verify import verify_run

    run_root = _resolve_directory(run_dir, "run_path_invalid")
    report = verify_run(run_root)
    if report.get("status") != "pass":
        raise ReplayError("run_not_verified", "Run directory did not pass verification")
    manifest = _read_json_object(run_root / "manifest.json", "manifest")
    _check_bundle_eligibility(manifest)
    _validate_manifest_artifacts(manifest)
    extractor = _ordinary_extractor_identity(manifest)
    config_source = _declared_file(run_root, manifest, "config_snapshot")
    config_data = _read_yaml_mapping(config_source, "config snapshot")
    _check_config_credentials(config_data)
    _check_credentials(extractor.get("options", {}))

    source_records, source_paths = _bundle_sources(run_root, manifest)
    route_source = _declared_file(run_root, manifest, "route_map")
    route_map = _read_yaml_mapping(route_source, "route map")
    _check_source_route(run_root, manifest, route_map, source_paths)

    requested = Path(out_dir).expanduser()
    if requested.exists() or requested.is_symlink():
        raise ReplayError("bundle_output_exists", f"Bundle output already exists: {requested}")
    parent = requested.parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{requested.name}.tmp-{uuid.uuid4().hex}"
    try:
        temporary.mkdir()
        (temporary / "baseline").mkdir()
        (temporary / "sources").mkdir()
        _copy_baseline(run_root, manifest, temporary)
        for source_record, source in zip(source_records, source_paths, strict=True):
            suffix = source.suffix.lower()
            destination = temporary / source_record["path"]
            destination = destination.with_name(destination.name + suffix) if suffix else destination
            source_record["path"] = destination.relative_to(temporary).as_posix()
            shutil.copyfile(source, destination)

        _rewrite_route_map(route_map, source_records)
        (temporary / "replay-route-map.yml").write_text(
            yaml.safe_dump(route_map, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        files = _inventory(temporary)
        baseline_manifest = temporary / "baseline" / "manifest.json"
        bundle = {
            "bundle_schema_version": _BUNDLE_VERSION,
            "baseline": {
                "run_id": manifest["run_id"],
                "manifest": "baseline/manifest.json",
                "manifest_sha256": _sha256_file(baseline_manifest),
                "execution_mode": manifest["execution_mode"],
                "run_depth": manifest["run_depth"],
                "extractor": extractor,
            },
            "replay": {
                "config": "baseline/config-snapshot.yml",
                "route_map": "replay-route-map.yml",
            },
            "sources": source_records,
            "files": files,
        }
        (temporary / "bundle.json").write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        validate_bundle(temporary)
        temporary.rename(requested)
    except Exception:
        if temporary.exists() or temporary.is_symlink():
            _remove_temporary(temporary)
        raise
    result = {"bundle_dir": str(requested.resolve())}
    result["bundle_sha256"] = _sha256_file(requested / "bundle.json")
    return result


def validate_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Validate an untrusted replay bundle without importing jsonschema."""
    root = _resolve_directory(bundle_dir, "bundle_path_invalid")
    bundle_path = root / "bundle.json"
    _require_regular(bundle_path, "bundle.json")
    bundle = _read_json_object(bundle_path, "bundle")
    _exact_keys(bundle, {"bundle_schema_version", "baseline", "replay", "sources", "files"}, "bundle")
    if bundle["bundle_schema_version"] != _BUNDLE_VERSION:
        _fail("bundle_schema_version_invalid", "Unsupported bundle schema version")
    baseline = bundle["baseline"]
    replay = bundle["replay"]
    if not isinstance(baseline, dict) or not isinstance(replay, dict):
        _fail("bundle_structure_invalid", "Bundle baseline and replay must be mappings")
    _exact_keys(
        baseline,
        {"run_id", "manifest", "manifest_sha256", "execution_mode", "run_depth", "extractor"},
        "bundle baseline",
    )
    _exact_keys(replay, {"config", "route_map"}, "bundle replay")
    if baseline["manifest"] != "baseline/manifest.json":
        _fail("bundle_path_invalid", "Bundle baseline manifest path is not canonical")
    if replay["config"] != "baseline/config-snapshot.yml" or replay["route_map"] != "replay-route-map.yml":
        _fail("bundle_path_invalid", "Bundle replay paths are not canonical")
    if not isinstance(baseline["run_id"], str) or not baseline["run_id"]:
        _fail("bundle_structure_invalid", "Bundle baseline run_id must be a non-empty string")
    if baseline["execution_mode"] != "execute" or type(baseline["run_depth"]) is not int or baseline["run_depth"] != 0:
        _fail("bundle_ineligible", "Bundle baseline is not an execute generation-zero run")
    if not _is_sha256(baseline["manifest_sha256"]):
        _fail("bundle_hash_invalid", "Bundle baseline manifest hash is invalid")
    extractor = baseline["extractor"]
    if not isinstance(extractor, dict):
        _fail("bundle_structure_invalid", "Bundle baseline extractor must be a mapping")
    extractor = cast(dict[str, Any], extractor)
    _validate_extractor_identity(extractor)
    _check_credentials(extractor.get("options", {}))
    sources = bundle["sources"]
    files = bundle["files"]
    if not isinstance(sources, list) or not isinstance(files, list):
        _fail("bundle_structure_invalid", "Bundle sources and files must be lists")
    _validate_declared_paths(root, baseline, replay, sources, files)

    manifest_path = root / baseline["manifest"]
    manifest = _read_json_object(manifest_path, "baseline manifest")
    if manifest.get("run_id") != baseline["run_id"]:
        _fail("bundle_manifest_mismatch", "Bundle run_id differs from baseline manifest")
    if _sha256_file(manifest_path) != baseline["manifest_sha256"]:
        _fail("bundle_hash_mismatch", "Baseline manifest hash does not match bundle")
    _check_bundle_eligibility(manifest)
    identity = _ordinary_extractor_identity(manifest)
    if _canonical(identity) != _canonical(extractor):
        _fail("bundle_extractor_mismatch", "Bundle extractor differs from baseline manifest")
    _validate_baseline_artifacts(root, manifest)
    _validate_transport_allowlist(root, manifest, sources)
    from .verify import verify_run

    baseline_report = verify_run(root / "baseline")
    if baseline_report.get("status") != "pass":
        _fail("baseline_not_verified", "Transported baseline artifacts did not pass verification")
    config_path = root / replay["config"]
    config = _read_yaml_mapping(config_path, "bundle config snapshot")
    _check_config_credentials(config)
    _validate_sources_against_manifest(root, manifest, sources)
    _validate_source_files(root, sources)
    route_map = _read_yaml_mapping(root / replay["route_map"], "bundle route map")
    source_paths = [entry["path"] for entry in sources]
    _check_portable_route(route_map, manifest, source_paths, sources)
    return bundle


def replay_bundle(
    bundle_dir: Path,
    out_dir: Path,
    *,
    adapter_path: Path | None = None,
) -> dict[str, Any]:
    """Replay a verified bundle through the ordinary PageLedger run path."""
    from .adapters import load_adapter
    from .aligner import align_run
    from .compare import compare_runs
    from .config import load_config
    from .runner import _apply_adapter_path, _effective_adapter, run
    from .verify import verify_run

    bundle = validate_bundle(bundle_dir)
    root = Path(bundle_dir).expanduser().resolve()
    bundle_sha256 = _sha256_file(root / "bundle.json")
    requested_out = Path(out_dir).expanduser()
    if requested_out.exists() or requested_out.is_symlink():
        raise ReplayError("replay_output_exists", f"Replay output already exists: {requested_out}")
    if adapter_path is not None:
        trusted_path = Path(adapter_path).expanduser().resolve()
        if trusted_path == root or root in trusted_path.parents:
            raise ReplayError(
                "adapter_path_inside_bundle",
                "Trusted adapter path must not be inside the bundle",
            )
    else:
        trusted_path = None

    baseline_manifest = _read_json_object(root / "baseline" / "manifest.json", "baseline manifest")
    baseline_extractor = cast(dict[str, Any], bundle["baseline"]["extractor"])
    config_path = root / "baseline" / "config-snapshot.yml"
    config = load_config(config_path, validate_adapter=False)
    adapter_name, adapter_options = _effective_adapter(config, 0)
    if adapter_name is None:
        raise ReplayError("extractor_identity_mismatch", "Bundle config has no effective adapter")
    try:
        _apply_adapter_path(trusted_path)
        adapter = load_adapter(adapter_name, adapter_options)
    except Exception as exc:
        raise ReplayError(
            "extractor_identity_mismatch",
            "Local adapter could not be loaded",
        ) from exc

    local_profile: dict[str, Any] | None = None
    local_extractor = _runtime_extractor_identity(adapter, adapter_options, None)
    if _identity_without_profile(local_extractor) != _identity_without_profile(baseline_extractor):
        raise ReplayError(
            "extractor_identity_mismatch",
            "Local adapter identity does not match the baseline",
        )
    cloud = "cloud" in {str(value).casefold() for value in baseline_extractor["capabilities"]}
    if bool(getattr(adapter, "deterministic", False)) and not cloud:
        try:
            local_profile = build_reproducibility_profile(adapter)
        except Exception as exc:
            raise ReplayError(
                "incompatible_environment",
                "Local reproducibility profile could not be established",
            ) from exc
        recorded_profile = baseline_extractor.get("reproducibility_profile")
        if not isinstance(recorded_profile, dict) or local_profile is None:
            raise ReplayError(
                "incompatible_environment",
                "Local reproducibility profile does not match the baseline",
            )
        if profile_sha256(local_profile) != profile_sha256(recorded_profile):
            raise ReplayError(
                "incompatible_environment",
                "Local reproducibility profile does not match the baseline",
            )

    local_extractor["reproducibility_profile"] = local_profile

    source_records = cast(list[dict[str, Any]], bundle["sources"])
    source_paths = [root / record["path"] for record in source_records]
    route_path: Path | None = None
    replay_route = root / "replay-route-map.yml"
    run_kwargs: dict[str, Any] = {
        "inputs": source_paths,
        "config_path": config_path,
        "out_dir": requested_out,
        "dry_run": False,
        "adapter_path": trusted_path,
    }
    if "routing" in baseline_manifest:
        route = _read_yaml_mapping(replay_route, "portable route map")
        documents = route.get("documents")
        if not isinstance(documents, list) or len(documents) != len(source_paths):
            raise ReplayError("route_map_invalid", "Portable route map does not match bundle sources")
        for document, source in zip(documents, source_paths, strict=True):
            if not isinstance(document, dict):
                raise ReplayError("route_map_invalid", "Portable route map document is invalid")
            document["source"] = str(source.resolve())
        with tempfile.TemporaryDirectory(prefix="pageledger-replay-route-") as temp:
            route_path = Path(temp) / "route-map.yml"
            route_path.write_text(yaml.safe_dump(route, sort_keys=False), encoding="utf-8")
            run_kwargs["routes_path"] = route_path
            run_result = run(**run_kwargs)
    else:
        if len(source_paths) == 1 and source_records[0].get("pages"):
            run_kwargs["pages"] = source_records[0]["pages"]
        run_result = run(**run_kwargs)

    alignment = baseline_manifest.get("alignment")
    if isinstance(alignment, dict) and alignment.get("schema_source") != "config_snapshot":
        align_run(requested_out, schema_path=root / "baseline" / "align-schema-snapshot.yml")

    verification = verify_run(requested_out)
    if verification.get("status") != "pass":
        raise ReplayError("replay_verification_failed", "Replay run did not pass verification")
    comparison = compare_runs(root / "baseline", requested_out)
    pages = comparison.get("pages", [])
    different_page_ids = [
        page["page_id"] for page in pages
        if page.get("raw_equal") is False
    ]
    missing_page_ids = [
        page["page_id"] for page in pages
        if page.get("raw_equal") is None
    ]
    missing_page_ids.extend(comparison.get("pages_only_in_a", []))
    missing_page_ids.extend(comparison.get("pages_only_in_b", []))
    raw = {
        "equal": comparison.get("raw_equal_total", 0),
        "different": comparison.get("raw_different_total", 0),
        "missing": comparison.get("raw_missing_total", 0)
        + len(comparison.get("pages_only_in_a", []))
        + len(comparison.get("pages_only_in_b", [])),
        "different_page_ids": sorted(set(different_page_ids)),
        "missing_page_ids": sorted(set(missing_page_ids)),
    }
    deterministic = bool(baseline_extractor["deterministic"]) and not cloud
    outcome = (
        "exact"
        if deterministic and raw["different"] == 0 and raw["missing"] == 0
        else "deterministic_mismatch"
        if deterministic
        else "evidence_compared"
    )
    replay_evidence = {
        "replay_schema_version": _BUNDLE_VERSION,
        "bundle_manifest_sha256": bundle_sha256,
        "baseline_run_id": baseline_manifest["run_id"],
        "replay_run_id": run_result["run_id"],
        "baseline_extractor": _replay_extractor_identity(baseline_extractor),
        "local_extractor": _replay_extractor_identity(local_extractor),
        "profile_match": True if deterministic else None,
        "outcome": outcome,
        "raw": raw,
        "comparison": comparison,
    }
    _atomic_write_json(requested_out / "replay.json", replay_evidence)
    updated_manifest = _read_json_object(requested_out / "manifest.json", "replay manifest")
    artifacts = dict(cast(dict[str, Any], updated_manifest.get("artifacts", {})))
    artifacts["replay"] = "replay.json"
    updated_manifest["artifacts"] = artifacts
    updated_manifest.update(
        {
            "replay_schema_version": _BUNDLE_VERSION,
            "baseline_run_id": baseline_manifest["run_id"],
            "bundle_manifest_sha256": bundle_sha256,
            "outcome": outcome,
        }
    )
    _atomic_write_json(requested_out / "manifest.json", updated_manifest)
    if verify_run(requested_out).get("status") != "pass":
        raise ReplayError("replay_verification_failed", "Final replay evidence does not verify")
    return {
        "outcome": outcome,
        "run_id": run_result["run_id"],
        "out_dir": str(requested_out.resolve()),
        "baseline_run_id": baseline_manifest["run_id"],
        "bundle_manifest_sha256": bundle_sha256,
        "profile_match": True if deterministic else None,
        "raw": raw,
    }


def _runtime_extractor_identity(
    adapter: Any, options: dict[str, Any], profile: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "adapter": adapter.name,
        "version": adapter.version,
        "deterministic": bool(adapter.deterministic),
        "input_types": sorted(str(item) for item in adapter.input_types),
        "output_types": sorted(str(item) for item in adapter.output_types),
        "capabilities": sorted(str(item) for item in adapter.capabilities),
        "options": dict(options),
        "reproducibility_profile": profile,
    }


def _identity_without_profile(identity: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in identity.items() if key != "reproducibility_profile"}


def _replay_extractor_identity(identity: dict[str, Any]) -> dict[str, Any]:
    profile = identity.get("reproducibility_profile")
    return {
        key: identity[key]
        for key in ("adapter", "version", "deterministic", "input_types", "output_types", "capabilities", "options")
    } | {"reproducibility_profile_sha256": profile.get("profile_sha256") if isinstance(profile, dict) else None}


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _check_bundle_eligibility(manifest: dict[str, Any]) -> None:
    if manifest.get("execution_mode") != "execute":
        _fail("run_ineligible", "Replay bundles require execute mode")
    if type(manifest.get("run_depth")) is not int or manifest.get("run_depth") != 0:
        _fail("run_ineligible", "Replay bundles require generation zero")
    if manifest.get("status") not in {"completed", "partial"}:
        _fail("run_ineligible", "Run status is not eligible for replay bundling")
    summary = manifest.get("summary")
    if not isinstance(summary, dict):
        _fail("run_ineligible", "Manifest summary is missing")
    summary = cast(dict[str, Any], summary)
    for key in ("pages_failed", "pages_not_attempted", "pages_routed_review"):
        value = summary.get(key, 0)
        if type(value) is not int or value < 0:
            _fail("run_ineligible", f"Manifest summary {key} is invalid")
    if summary.get("pages_failed", 0) != 0 or summary.get("pages_not_attempted", 0) != 0:
        _fail("run_ineligible", "Run contains failed or unattempted pages")
    if manifest["status"] == "partial" and summary.get("pages_routed_review", 0) <= 0:
        _fail("run_ineligible", "Partial run has no review-routed pages")


def _ordinary_extractor_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    entries = manifest.get("extractors")
    if not isinstance(entries, list) or not entries:
        _fail("extractor_missing", "Manifest has no extractor entries")
    entries = cast(list[Any], entries)
    identities = []
    for entry in entries:
        if not isinstance(entry, dict):
            _fail("extractor_invalid", "Manifest extractor entry must be a mapping")
        entry = cast(dict[str, Any], entry)
        options = entry.get("options", {})
        if not isinstance(options, dict):
            _fail("extractor_invalid", "Manifest extractor options must be a mapping")
        for field in ("input_types", "output_types", "capabilities"):
            value = entry.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                _fail("extractor_invalid", f"Manifest extractor {field} must be a list of strings")
        try:
            json.dumps(options, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            _fail("extractor_invalid", "Manifest extractor options must contain finite JSON", exc)
        identity = {
            "adapter": entry.get("adapter"),
            "version": entry.get("version"),
            "deterministic": entry.get("deterministic"),
            "input_types": sorted(entry.get("input_types", [])),
            "output_types": sorted(entry.get("output_types", [])),
            "capabilities": sorted(entry.get("capabilities", [])),
            "options": options,
            "reproducibility_profile": entry.get("reproducibility_profile"),
        }
        _validate_extractor_identity(identity)
        identities.append(identity)
    first = identities[0]
    for candidate in identities[1:]:
        if _canonical(candidate) != _canonical(first):
            _fail("extractor_conflict", "Manifest extractor entries have conflicting identities")
    return first


def _validate_extractor_identity(identity: dict[str, Any]) -> None:
    _exact_keys(
        identity,
        {"adapter", "version", "deterministic", "input_types", "output_types", "capabilities", "options", "reproducibility_profile"},
        "extractor identity",
    )
    if not isinstance(identity["adapter"], str) or not identity["adapter"]:
        _fail("extractor_invalid", "Extractor adapter must be a non-empty string")
    if not isinstance(identity["version"], str) or not identity["version"]:
        _fail("extractor_invalid", "Extractor version must be a non-empty string")
    if not isinstance(identity["deterministic"], bool):
        _fail("extractor_invalid", "Extractor deterministic must be boolean")
    for field in ("input_types", "output_types", "capabilities"):
        value = identity[field]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            _fail("extractor_invalid", f"Extractor {field} must be a list of strings")
        if value != sorted(set(value)):
            _fail("extractor_invalid", f"Extractor {field} must be sorted and unique")
    if not isinstance(identity["options"], dict):
        _fail("extractor_invalid", "Extractor options must be a mapping")
    profile = identity["reproducibility_profile"]
    cloud = "cloud" in {item.casefold() for item in identity["capabilities"]}
    if profile is None:
        if identity["deterministic"] and not cloud:
            _fail("profile_missing", "Deterministic non-cloud extractor lacks a profile")
        return
    _validate_profile(profile, identity)


def _validate_profile(profile: Any, identity: dict[str, Any]) -> None:
    if not isinstance(profile, dict):
        _fail("profile_invalid", "Reproducibility profile must be a mapping or null")
    profile = cast(dict[str, Any], profile)
    _exact_keys(profile, {"profile_version", "pageledger", "adapter", "runtime", "materials", "profile_sha256"}, "profile")
    if profile.get("profile_version") != PROFILE_VERSION:
        _fail("profile_invalid", "Unsupported reproducibility profile version")
    for section, keys in {
        "pageledger": {"version", "code_sha256"},
        "adapter": {"module", "name", "version", "code_sha256"},
        "runtime": {"python_implementation", "python_version", "system", "release", "machine", "preferred_encoding", "filesystem_encoding"},
    }.items():
        value = profile.get(section)
        if not isinstance(value, dict):
            _fail("profile_invalid", f"Profile {section} must be a mapping")
        value = cast(dict[str, Any], value)
        _exact_keys(value, keys, f"profile {section}")
        if any(not isinstance(value[item], str) or not value[item] for item in keys if item != "code_sha256"):
            _fail("profile_invalid", f"Profile {section} contains invalid strings")
        if "code_sha256" in value and not _is_sha256(value["code_sha256"]):
            _fail("profile_invalid", f"Profile {section} code hash is invalid")
    adapter_profile = profile["adapter"]
    if adapter_profile["name"] != identity["adapter"] or adapter_profile["version"] != identity["version"]:
        _fail("profile_invalid", "Profile adapter identity disagrees with extractor")
    materials = profile.get("materials")
    if not isinstance(materials, list):
        _fail("profile_invalid", "Profile materials must be a list")
    materials = cast(list[Any], materials)
    previous: tuple[str, str] | None = None
    for material in materials:
        if not isinstance(material, dict):
            _fail("profile_invalid", "Profile material must be a mapping")
        _exact_keys(material, {"kind", "name", "version", "sha256"}, "profile material")
        if any(not isinstance(material[key], str) or not material[key] for key in ("kind", "name", "version")):
            _fail("profile_invalid", "Profile material has invalid fields")
        if material["kind"] not in _MATERIAL_KINDS or not _is_sha256(material["sha256"]):
            _fail("profile_invalid", "Profile material kind or hash is invalid")
        if _looks_like_path(material["name"]) or _looks_like_path(material["version"]):
            _fail("profile_invalid", "Profile material names and versions must not contain paths")
        if material["version"].casefold() in _MUTABLE_VERSION_ALIASES:
            _fail("profile_invalid", "Profile material version must be an exact revision")
        pair = (material["kind"], material["name"])
        if previous is not None and pair <= previous:
            _fail("profile_invalid", "Profile materials must be sorted and unique")
        previous = pair
    if not _is_sha256(profile.get("profile_sha256")) or profile_sha256(profile) != profile["profile_sha256"]:
        _fail("profile_hash_mismatch", "Reproducibility profile self-hash does not match")


def _bundle_sources(run_root: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Path]]:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        _fail("source_missing", "Manifest has no source inputs")
    inputs = cast(list[Any], inputs)
    records: list[dict[str, Any]] = []
    paths: list[Path] = []
    seen: set[Path] = set()
    for index, entry in enumerate(inputs, 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            _fail("source_invalid", "Manifest source entry is invalid")
        source = Path(entry["path"]).expanduser()
        _require_regular(source, f"source {entry['path']}")
        resolved = source.resolve()
        if resolved in seen:
            _fail("source_duplicate", "Manifest contains duplicate source paths")
        seen.add(resolved)
        expected = entry.get("sha256")
        if not _is_sha256(expected) or _sha256_file(source) != expected:
            _fail("source_changed", f"Source does not match manifest: {source}")
        portable = f"sources/source-{index:04d}"
        record: dict[str, Any] = {
            "index": index,
            "path": portable,
            "sha256": expected,
            "size": source.stat().st_size,
            "page_count": entry.get("page_count"),
        }
        if "pages" in entry:
            record["pages"] = entry["pages"]
        records.append(record)
        paths.append(source)
    return records, paths


def _validate_manifest_artifacts(manifest: dict[str, Any]) -> dict[str, str]:
    declarations = manifest.get("artifacts")
    if not isinstance(declarations, dict) or declarations != _CANONICAL_ARTIFACTS:
        _fail("artifact_declarations_invalid", "Manifest artifact declarations are not canonical")
    return cast(dict[str, str], declarations)


def _copy_baseline(run_root: Path, manifest: dict[str, Any], destination: Path) -> None:
    manifest_out = destination / "baseline" / "manifest.json"
    shutil.copyfile(run_root / "manifest.json", manifest_out)
    declarations = _validate_manifest_artifacts(manifest)
    for key, relative in declarations.items():
        if not isinstance(relative, str):
            _fail("artifact_path_invalid", f"Artifact path for {key} is invalid")
        safe_relative = _safe_relative(relative)
        source = run_root / safe_relative.rstrip("/")
        if key in {"raw_dir", "normalized_dir"}:
            _copy_tree(source, destination / "baseline" / safe_relative.rstrip("/"))
        else:
            _require_regular(source, safe_relative)
            target = destination / "baseline" / safe_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    alignment = manifest.get("alignment")
    if isinstance(alignment, dict) and alignment.get("schema_source") != "config_snapshot":
        snapshot = run_root / "align-schema-snapshot.yml"
        _require_regular(snapshot, "align-schema-snapshot.yml")
        shutil.copyfile(snapshot, destination / "baseline" / snapshot.name)


def _copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        _fail("artifact_path_invalid", f"Artifact directory is unsafe: {source}")
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if path.is_dir():
            if path.is_symlink():
                _fail("artifact_file_unsafe", f"Unsafe artifact path: {path}")
            (target / relative).mkdir(parents=True, exist_ok=True)
        else:
            _require_regular(path, str(path))
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target / relative)


def _rewrite_route_map(route_map: dict[str, Any], sources: list[dict[str, Any]]) -> None:
    documents = route_map.get("documents")
    if not isinstance(documents, list) or len(documents) != len(sources):
        _fail("route_source_mismatch", "Route map documents do not match manifest sources")
    documents = cast(list[Any], documents)
    for document, source in zip(documents, sources, strict=True):
        if not isinstance(document, dict):
            _fail("route_source_mismatch", "Route map document is invalid")
        document["source"] = source["path"]


def _inventory(root: Path) -> list[dict[str, Any]]:
    paths: list[Path] = []
    for path in _walk_no_follow(root):
        if path.relative_to(root).as_posix() == "bundle.json":
            continue
        if path.is_file():
            paths.append(path)
    entries = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        entries.append({"path": relative, "size": path.stat().st_size, "sha256": _sha256_file(path)})
    return entries


def _validate_declared_paths(root: Path, baseline: dict[str, Any], replay: dict[str, Any], sources: list[Any], files: list[Any]) -> None:
    paths: list[str] = []
    for value in (baseline["manifest"], replay["config"], replay["route_map"]):
        paths.append(_safe_relative(value))
    if not isinstance(sources, list) or not isinstance(files, list):
        _fail("bundle_structure_invalid", "Bundle sources and files must be lists")
    sources = cast(list[Any], sources)
    files = cast(list[Any], files)
    for entry in sources:
        if not isinstance(entry, dict):
            _fail("source_invalid", "Bundle source entry must be a mapping")
        _exact_keys(entry, {"index", "path", "sha256", "size", "page_count", "pages"} if "pages" in entry else {"index", "path", "sha256", "size", "page_count"}, "bundle source")
        if type(entry.get("index")) is not int or entry["index"] < 1:
            _fail("source_invalid", "Bundle source index must be a positive integer")
        if not _is_sha256(entry.get("sha256")):
            _fail("source_invalid", "Bundle source hash is invalid")
        if type(entry.get("size")) is not int or entry["size"] < 0:
            _fail("source_invalid", "Bundle source size must be a non-negative integer")
        if type(entry.get("page_count")) is not int or entry["page_count"] < 0:
            _fail("source_invalid", "Bundle source page_count must be a non-negative integer")
        if "pages" in entry and not isinstance(entry["pages"], str):
            _fail("source_invalid", "Bundle source pages must be a string")
        source_path = _safe_relative(entry.get("path"))
        if Path(source_path).parent.as_posix() != "sources":
            _fail("source_path_invalid", "Bundle source must be directly beneath sources/")
        paths.append(source_path)
    if [entry.get("index") for entry in sources] != list(range(1, len(sources) + 1)):
        _fail("source_order_invalid", "Bundle source indexes must be consecutive")
    if len({entry.get("path") for entry in sources}) != len(sources):
        _fail("source_duplicate", "Bundle source paths must be unique")
    seen_files: set[str] = set()
    previous = ""
    for entry in files:
        if not isinstance(entry, dict):
            _fail("inventory_invalid", "Bundle inventory entry must be a mapping")
        _exact_keys(entry, {"path", "size", "sha256"}, "bundle inventory")
        relative = _safe_relative(entry.get("path"))
        if relative in seen_files:
            _fail("inventory_duplicate", f"Duplicate bundle inventory path: {relative}")
        if previous and relative <= previous:
            _fail("inventory_order_invalid", "Bundle inventory paths must be sorted")
        previous = relative
        seen_files.add(relative)
        paths.append(relative)
        if not isinstance(entry["size"], int) or isinstance(entry["size"], bool) or entry["size"] < 0 or not _is_sha256(entry["sha256"]):
            _fail("inventory_invalid", f"Invalid bundle inventory metadata: {relative}")
        candidate = root / relative
        _require_regular(candidate, relative)
        if candidate.stat().st_size != entry["size"] or _sha256_file(candidate) != entry["sha256"]:
            _fail("bundle_hash_mismatch", f"Bundle inventory hash or size mismatch: {relative}")
    actual = {
        path.relative_to(root).as_posix()
        for path in _walk_no_follow(root)
        if path.relative_to(root).as_posix() != "bundle.json"
    }
    if actual != seen_files:
        _fail("inventory_mismatch", "Bundle inventory does not match transported files")
    for required in (baseline["manifest"], replay["config"], replay["route_map"]):
        if required not in seen_files:
            _fail("inventory_missing", f"Bundle inventory omits {required}")
    if any(entry["path"] not in seen_files for entry in sources):
        _fail("inventory_missing", "Bundle inventory omits a source")


def _validate_sources_against_manifest(root: Path, manifest: dict[str, Any], sources: list[Any]) -> None:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != len(sources):
        _fail("source_manifest_mismatch", "Bundle sources disagree with baseline manifest")
    inputs = cast(list[Any], inputs)
    for original, transported in zip(inputs, sources, strict=True):
        if not isinstance(original, dict):
            _fail("source_manifest_mismatch", "Baseline manifest source is invalid")
        if original.get("sha256") != transported.get("sha256") or original.get("page_count") != transported.get("page_count"):
            _fail("source_manifest_mismatch", "Bundle source metadata disagrees with baseline manifest")
        if "pages" in original and original.get("pages") != transported.get("pages"):
            _fail("source_manifest_mismatch", "Bundle source page selection disagrees with baseline manifest")
        original_path = original.get("path")
        if not isinstance(original_path, str):
            _fail("source_manifest_mismatch", "Baseline manifest source path is invalid")
        expected = _expected_source_path(transported["index"], original_path)
        if transported.get("path") != expected:
            _fail("source_path_invalid", "Bundle source filename does not match baseline input suffix")


def _expected_source_path(index: int, original_path: str) -> str:
    suffix = Path(original_path).suffix.lower()
    return f"sources/source-{index:04d}{suffix}"


def _validate_source_files(root: Path, sources: list[Any]) -> None:
    for entry in sources:
        path = root / entry["path"]
        _require_regular(path, entry["path"])
        if path.stat().st_size != entry["size"] or _sha256_file(path) != entry["sha256"]:
            _fail("source_hash_mismatch", f"Bundle source hash or size mismatch: {entry['path']}")


def _validate_baseline_artifacts(root: Path, manifest: dict[str, Any]) -> None:
    declarations = _validate_manifest_artifacts(manifest)
    for key, value in declarations.items():
        if not isinstance(value, str):
            _fail("artifact_path_invalid", f"Baseline artifact path is invalid: {key}")
        relative = _safe_relative(value).rstrip("/")
        path = root / "baseline" / relative
        if key in {"raw_dir", "normalized_dir"}:
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                _fail("artifact_missing", f"Missing baseline artifact directory: {relative}", exc)
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                _fail("artifact_file_unsafe", f"Unsafe baseline artifact directory: {relative}")
        else:
            _require_regular(path, relative)
    alignment = manifest.get("alignment")
    if isinstance(alignment, dict) and alignment.get("schema_source") != "config_snapshot":
        snapshot = root / "baseline" / "align-schema-snapshot.yml"
        _require_regular(snapshot, "align-schema-snapshot.yml")
        if alignment.get("schema_sha256") != _sha256_file(snapshot):
            _fail("bundle_hash_mismatch", "Alignment schema snapshot hash does not match manifest")


def _validate_transport_allowlist(root: Path, manifest: dict[str, Any], sources: list[Any]) -> None:
    """Reject regular files outside the canonical baseline/source/replay layout."""
    allowed = {
        "baseline/manifest.json",
        "replay-route-map.yml",
        *(entry["path"] for entry in sources),
    }
    for key, relative in _validate_manifest_artifacts(manifest).items():
        if key in {"raw_dir", "normalized_dir"}:
            prefix = f"baseline/{relative.rstrip('/')}/"
            allowed.update(
                path.relative_to(root).as_posix()
                for path in _walk_no_follow(root / "baseline" / relative.rstrip("/"))
                if path.relative_to(root).as_posix().startswith(prefix)
            )
        elif key == "route_map":
            allowed.add("baseline/route-map.yml")
        else:
            allowed.add(f"baseline/{relative}")
    alignment = manifest.get("alignment")
    if isinstance(alignment, dict) and alignment.get("schema_source") != "config_snapshot":
        allowed.add("baseline/align-schema-snapshot.yml")
    actual = {
        path.relative_to(root).as_posix()
        for path in _walk_no_follow(root)
        if path.relative_to(root).as_posix() != "bundle.json"
    }
    if actual != allowed:
        unexpected = sorted(actual - allowed)
        _fail("inventory_mismatch", f"Bundle contains undeclared transported files: {unexpected}")


def _check_source_route(run_root: Path, manifest: dict[str, Any], route_map: dict[str, Any], source_paths: list[Path]) -> None:
    docs = route_map.get("documents")
    inputs = manifest.get("inputs")
    if not isinstance(docs, list) or not isinstance(inputs, list) or len(docs) != len(inputs):
        _fail("route_source_mismatch", "Route map source mappings are incomplete")
    docs = cast(list[Any], docs)
    inputs = cast(list[Any], inputs)
    for document, original, source in zip(docs, inputs, source_paths, strict=True):
        if not isinstance(document, dict) or not isinstance(original, dict):
            _fail("route_source_mismatch", "Route map source mapping is invalid")
        route_source = document.get("source")
        if not isinstance(route_source, str) or Path(route_source).expanduser().resolve() != source.resolve():
            _fail("route_source_mismatch", "Route map source disagrees with manifest input")
        if document.get("source_sha256") != original.get("sha256"):
            _fail("route_source_mismatch", "Route map source hash disagrees with manifest")


def _check_portable_route(route_map: dict[str, Any], manifest: dict[str, Any], source_paths: list[str], sources: list[Any]) -> None:
    docs = route_map.get("documents")
    if not isinstance(docs, list) or len(docs) != len(sources):
        _fail("route_source_mismatch", "Portable route map documents do not match sources")
    docs = cast(list[Any], docs)
    seen: set[str] = set()
    for document, path, source in zip(docs, source_paths, sources, strict=True):
        if not isinstance(document, dict) or document.get("source") != path:
            _fail("route_source_mismatch", "Portable route map source disagrees with bundle")
        if path in seen:
            _fail("source_duplicate", "Portable route map contains duplicate source mapping")
        seen.add(path)
        if document.get("source_sha256") != source.get("sha256") or document.get("page_count") != source.get("page_count"):
            _fail("route_source_mismatch", "Portable route metadata disagrees with bundle sources")
    if route_map.get("run_id") != manifest.get("run_id"):
        _fail("route_source_mismatch", "Portable route run_id disagrees with baseline manifest")


def _check_credentials(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = "".join(character for character in key.casefold() if character.isalnum()) if isinstance(key, str) else ""
            if normalized in _FORBIDDEN_OPTION_KEYS:
                _fail("credential_key_forbidden", f"Credential option key is forbidden: {key}")
            _check_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _check_credentials(child)


def _check_config_credentials(config: Any) -> None:
    """Inspect only adapter option mappings; prose/citations are not scanned."""
    if not isinstance(config, Mapping):
        return
    run = config.get("run")
    if not isinstance(run, Mapping):
        return
    _scan_approved_option_mappings(config)


def _scan_approved_option_mappings(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in {"adapter_options", "hook_options"}:
                _check_credentials(child)
            else:
                _scan_approved_option_mappings(child)
    elif isinstance(value, list):
        for child in value:
            _scan_approved_option_mappings(child)


def _declared_file(root: Path, manifest: dict[str, Any], key: str) -> Path:
    declarations = manifest.get("artifacts")
    if not isinstance(declarations, dict) or not isinstance(declarations.get(key), str):
        _fail("artifact_declaration_missing", f"Manifest does not declare {key}")
    declarations = cast(dict[str, Any], declarations)
    relative = _safe_relative(declarations[key])
    path = root / relative.rstrip("/")
    _require_regular(path, relative)
    return path


def _resolve_directory(path: Path, code: str) -> Path:
    root = Path(path).expanduser()
    try:
        resolved = root.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(code, f"Cannot resolve directory: {path}", exc)
    if not resolved.is_dir() or root.is_symlink():
        _fail(code, f"Directory is missing or unsafe: {path}")
    return resolved


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail("bundle_malformed", f"Cannot parse {label}: {exc}")
    if not isinstance(value, dict):
        _fail("bundle_structure_invalid", f"{label} must be a mapping")
    return value


def _read_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    _require_regular(path, label)
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        _fail("bundle_malformed", f"Cannot parse {label}: {exc}")
    if not isinstance(value, dict):
        _fail("bundle_structure_invalid", f"{label} must be a mapping")
    return value


def _safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value:
        _fail("bundle_path_unsafe", f"Unsafe bundle path: {value}")
    relative = Path(value)
    if "\x00" in value or "\\" in value or relative.is_absolute() or ".." in relative.parts:
        _fail("bundle_path_unsafe", f"Unsafe bundle path: {value}")
    return relative.as_posix()


def _require_regular(path: Path, label: str) -> None:
    try:
        st = path.lstat()
    except (OSError, ValueError) as exc:
        _fail("bundle_file_missing", f"Missing bundle file: {label}", exc)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        _fail("bundle_file_unsafe", f"Unsafe bundle file: {label}")


def _walk_no_follow(root: Path) -> list[Path]:
    result: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in list(dirnames):
            path = directory_path / name
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                _fail("bundle_file_unsafe", f"Unsafe bundle path: {path.relative_to(root)}")
        for name in filenames:
            path = directory_path / name
            _require_regular(path, path.relative_to(root).as_posix())
            result.append(path)
    return result


def _remove_temporary(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        _fail("bundle_structure_invalid", f"{label} has unexpected or missing fields")


def _fail(code: str, message: str, cause: BaseException | None = None) -> Any:
    if cause is None:
        raise ReplayError(code, message)
    raise ReplayError(code, message) from cause
