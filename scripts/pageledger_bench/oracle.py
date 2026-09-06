"""Schema-complete independent oracle for frozen PageLedger benchmark runs."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import stat
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from pageledger.artifacts import render_audit_markdown
from pageledger.verify import verify_run
from scripts.pageledger_bench.workloads import WorkloadSpec, load_frozen_manifest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS_DIR = _REPOSITORY_ROOT / "schemas"
_WORKLOAD_RECIPE_PATH = _REPOSITORY_ROOT / "scripts/pageledger_bench/workloads.py"
_JSON_SCHEMAS = {
    "audit.json": "audit.schema.json",
    "cost.json": "cost.schema.json",
    "manifest.json": "manifest.schema.json",
}
_JSONL_SCHEMAS = {
    "provenance.jsonl": "provenance-line.schema.json",
    "quality.jsonl": "quality-line.schema.json",
    "run.log": "run-log-line.schema.json",
}
_TOP_LEVEL_FILES = {
    "audit.json",
    "audit.md",
    "config-snapshot.yml",
    "cost.json",
    "manifest.json",
    "provenance.jsonl",
    "quality.jsonl",
    "rerun-manifest.yml",
    "route-map.yml",
    "run.log",
}
_RAW_EXTENSIONS = {
    "text": "txt",
    "markdown": "markdown",
    "json": "json",
    "csv": "csv",
    "markdown_table": "markdown_table",
}
_ALIGNABLE = {"json", "csv", "markdown_table"}
_PREREFORM_LETTERS = frozenset("\u0463\u0462\u0473\u0472\u0475\u0474")
_TERMINAL_HARD_SIGN = re.compile(r"[\u044a\u042a](?![^\W\d_])")
_SENTINELS = {
    "run_id": "<run-id>",
    "timestamp": "<timestamp>",
    "source": "<source-path>",
    "config": "<config-path>",
    "extraction": "<extraction-seconds>",
}


@dataclass(frozen=True)
class OracleError:
    code: str
    message: str
    artifact: str | None = None


@dataclass(frozen=True)
class ValidationReceipt:
    valid: bool
    errors: tuple[OracleError, ...]
    verifier_report: dict[str, Any]
    canonical: dict[str, Any] | None


@dataclass(frozen=True)
class EquivalenceReceipt:
    equivalent: bool
    errors: tuple[OracleError, ...]
    control: ValidationReceipt
    candidate: ValidationReceipt


def validate_run(run_dir: Path, workload: WorkloadSpec) -> ValidationReceipt:
    """Validate one generation-zero benchmark run without trusting ``verify_run``."""
    root = Path(run_dir)
    errors: list[OracleError] = []
    manifest = load_frozen_manifest()
    _validate_frozen_workload(workload, manifest, errors)
    workload_valid = not errors
    schemas = _load_frozen_schemas(manifest, errors)
    inventory = _regular_file_inventory(root, errors)
    _require_artifacts(root, errors)
    unsafe_inventory = any(
        error.code
        in {
            "run_directory_invalid",
            "symlink_forbidden",
            "non_regular_forbidden",
            "required_artifact_missing",
        }
        for error in errors
    )
    loaded = None if unsafe_inventory else _load_artifacts(root, schemas, errors)

    if loaded is not None and workload_valid:
        _validate_inventory(inventory, workload, errors)
        try:
            _validate_contract(root, workload, loaded, errors)
        except (IndexError, KeyError, OSError, TypeError, ValueError) as exc:
            _add(errors, "contract_validation_failed", str(exc))

    report = verify_run(root)
    if report.get("status") != "pass":
        _add(errors, "verifier_failed", "pageledger.verify.verify_run did not pass")
    canonical = None
    if loaded is not None and not errors and report.get("status") == "pass":
        try:
            canonical = _canonical_loaded(root, loaded)
        except (KeyError, OSError, TypeError, UnicodeError, ValueError) as exc:
            _add(errors, "canonicalization_failed", str(exc))
    return ValidationReceipt(not errors, tuple(errors), report, canonical)


def compare_runs(
    control: Path, candidate: Path, workload: WorkloadSpec
) -> EquivalenceReceipt:
    """Compare two independently validated runs under the frozen path rules."""
    left = validate_run(control, workload)
    right = validate_run(candidate, workload)
    errors: list[OracleError] = []
    if left.valid and right.valid:
        if left.canonical != right.canonical:
            _add(errors, "canonical_run_mismatch", "canonical artifact trees differ")
        left_report = _canonical_verifier_report(left.verifier_report)
        right_report = _canonical_verifier_report(right.verifier_report)
        if left_report != right_report:
            _add(errors, "verifier_report_mismatch", "complete verifier reports differ")
    else:
        if not left.valid:
            _add(errors, "control_invalid", "control run failed independent validation")
        if not right.valid:
            _add(errors, "candidate_invalid", "candidate run failed independent validation")
    return EquivalenceReceipt(not errors, tuple(errors), left, right)


def canonical_run(run_dir: Path) -> dict[str, Any]:
    """Load and canonicalize a run using only manifest-declared exact paths."""
    root = Path(run_dir)
    errors: list[OracleError] = []
    inventory = _regular_file_inventory(root, errors)
    _require_artifacts(root, errors)
    if errors:
        raise ValueError(_error_text(errors))
    schemas = _load_frozen_schemas(load_frozen_manifest(), errors)
    loaded = _load_artifacts(root, schemas, errors)
    if loaded is not None:
        _validate_canonical_inventory(inventory, loaded, errors)
        _validate_generation_zero_identity(
            loaded["manifest.json"],
            loaded["route-map.yml"],
            loaded["provenance.jsonl"],
            loaded["audit.json"],
            loaded["rerun-manifest.yml"],
            loaded["cost.json"],
            loaded,
            errors,
        )
        _validate_path_and_timestamp_relationships(
            None,
            loaded["manifest.json"],
            loaded["route-map.yml"],
            loaded["provenance.jsonl"],
            loaded["rerun-manifest.yml"],
            loaded["run.log"],
            errors,
        )
    if errors or loaded is None:
        raise ValueError(_error_text(errors) or "run artifacts could not be loaded")
    return _canonical_loaded(root, loaded)


def _validate_frozen_workload(
    workload: WorkloadSpec,
    manifest: dict[str, Any],
    errors: list[OracleError],
) -> None:
    frozen_workloads = manifest.get("workloads", {})
    frozen = frozen_workloads.get(workload.name)
    if not isinstance(frozen, dict):
        _add(errors, "workload_name_mismatch", f"unknown frozen workload {workload.name!r}")
        return
    if len(workload.page_specs) != frozen.get("page_count"):
        _add(errors, "workload_page_count_mismatch", "workload page count differs")
    categories = Counter(page.category for page in workload.page_specs)
    if dict(categories) != frozen.get("category_counts"):
        _add(errors, "workload_category_mismatch", "workload categories differ")
    if workload.expected != frozen.get("expected"):
        _add(errors, "workload_expected_mismatch", "workload receipts differ")
    derived_membership = tuple(
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
        for index, page in enumerate(workload.page_specs, 1)
    )
    if workload.membership != derived_membership:
        _add(errors, "workload_membership_mismatch", "in-memory membership differs")
    expected_paths = {
        "source.txt": workload.source_path,
        "pageledger.yml": workload.config_path,
        "membership.json": workload.membership_path,
    }
    if workload.generated_paths != expected_paths:
        _add(errors, "workload_generated_paths_mismatch", "generated path mapping differs")
    hashes = frozen.get("hashes", {})
    file_contracts = (
        ("source.txt", workload.source_path, "workload_source_hash_mismatch"),
        ("pageledger.yml", workload.config_path, "workload_config_hash_mismatch"),
        ("membership.json", workload.membership_path, "workload_membership_hash_mismatch"),
    )
    for filename, path, code in file_contracts:
        try:
            actual = _sha256(path)
        except OSError as exc:
            _add(errors, code, str(exc), filename)
        else:
            if actual != hashes.get(filename):
                _add(errors, code, f"{filename} differs from frozen hash", filename)
    try:
        membership_document = json.loads(workload.membership_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _add(errors, "workload_membership_mismatch", str(exc), "membership.json")
    else:
        expected_document = {
            "generator_version": manifest["generator"]["version"],
            "workload": workload.name,
            "pages": list(derived_membership),
        }
        if membership_document != expected_document:
            _add(errors, "workload_membership_mismatch", "membership document differs")
    expected_recipe_hash = manifest.get("recipe_sha256", {}).get(
        "scripts/pageledger_bench/workloads.py"
    )
    try:
        recipe_hash = _sha256(_WORKLOAD_RECIPE_PATH)
    except OSError as exc:
        _add(errors, "recipe_hash_mismatch", str(exc))
    else:
        if recipe_hash != expected_recipe_hash:
            _add(errors, "recipe_hash_mismatch", "workload recipe differs from frozen hash")


def _load_frozen_schemas(
    manifest: dict[str, Any], errors: list[OracleError]
) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for filename, expected_hash in manifest["schema_sha256"].items():
        path = _SCHEMAS_DIR / filename
        try:
            payload = path.read_bytes()
        except OSError as exc:
            _add(errors, "schema_missing", str(exc), filename)
            continue
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            _add(errors, "schema_hash_mismatch", f"frozen hash changed for {filename}", filename)
            continue
        try:
            schema = json.loads(payload)
            Draft202012Validator.check_schema(schema)
            schemas[filename] = schema
        except Exception as exc:  # jsonschema exposes several schema exceptions
            _add(errors, "schema_invalid", str(exc), filename)
    return schemas


def _regular_file_inventory(root: Path, errors: list[OracleError]) -> set[str]:
    inventory: set[str] = set()
    if not root.is_dir() or root.is_symlink():
        _add(errors, "run_directory_invalid", "run directory must be a real directory")
        return inventory
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            _add(errors, "inventory_unreadable", str(exc), relative)
            continue
        if stat.S_ISLNK(mode):
            _add(errors, "symlink_forbidden", "symbolic links are not run evidence", relative)
        elif stat.S_ISREG(mode):
            inventory.add(relative)
        elif not stat.S_ISDIR(mode):
            _add(errors, "non_regular_forbidden", "non-regular run entry", relative)
    return inventory


def _require_artifacts(root: Path, errors: list[OracleError]) -> None:
    for filename in sorted(_TOP_LEVEL_FILES):
        path = root / filename
        if path.is_symlink() or not path.is_file():
            _add(errors, "required_artifact_missing", "required regular file is missing", filename)
    for dirname in ("raw", "normalized"):
        path = root / dirname
        if path.is_symlink() or not path.is_dir():
            _add(errors, "required_artifact_missing", "required real directory is missing", dirname)


def _validate_canonical_inventory(
    inventory: set[str], data: dict[str, Any], errors: list[OracleError]
) -> None:
    expected = set(_TOP_LEVEL_FILES)
    for entry in data["provenance.jsonl"]:
        relative = entry.get("result", {}).get("raw_artifact")
        if not _safe_relative_artifact(relative, "raw"):
            _add(errors, "raw_artifact_path_invalid", "raw reference is not contained")
        else:
            expected.add(relative)
    expected.update(f"normalized/{filename}" for filename, _ in data["normalized"])
    if inventory != expected:
        _add(errors, "inventory_mismatch", "canonical run file inventory differs")


def _load_artifacts(
    root: Path,
    schemas: dict[str, dict[str, Any]],
    errors: list[OracleError],
) -> dict[str, Any] | None:
    loaded: dict[str, Any] = {}
    for filename, schema_name in _JSON_SCHEMAS.items():
        value = _read_json(root / filename, filename, errors)
        if value is not None:
            loaded[filename] = value
            _schema_validate(value, schemas.get(schema_name), filename, errors)
    for filename, schema_name in _JSONL_SCHEMAS.items():
        entries = _read_jsonl(root / filename, filename, errors)
        if entries is not None:
            loaded[filename] = entries
            for index, entry in enumerate(entries, 1):
                _schema_validate(
                    entry, schemas.get(schema_name), f"{filename}:{index}", errors
                )
    for filename in ("route-map.yml", "rerun-manifest.yml"):
        value = _read_yaml(root / filename, filename, errors)
        if value is not None:
            loaded[filename] = value
    normalized: list[tuple[str, dict[str, Any]]] = []
    normalized_schema = schemas.get("normalized-page.schema.json")
    normalized_dir = root / "normalized"
    if normalized_dir.is_dir() and not normalized_dir.is_symlink():
        for path in sorted(normalized_dir.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_file():
                continue
            value = _read_json(path, path.relative_to(root).as_posix(), errors)
            if value is not None:
                normalized.append((path.name, value))
                _schema_validate(value, normalized_schema, path.name, errors)
    loaded["normalized"] = normalized
    return loaded if all(name in loaded for name in _JSON_SCHEMAS | _JSONL_SCHEMAS) else None


def _validate_inventory(
    inventory: set[str], workload: WorkloadSpec, errors: list[OracleError]
) -> None:
    expected = set(_TOP_LEVEL_FILES)
    for index, page in enumerate(workload.page_specs, 1):
        page_id = _page_id(index)
        expected.add(f"raw/{page_id}.{_RAW_EXTENSIONS[page.format]}")
        if page.format in _ALIGNABLE:
            expected.add(f"normalized/{page_id}.json")
    if inventory != expected:
        extra = sorted(inventory - expected)
        missing = sorted(expected - inventory)
        _add(
            errors,
            "inventory_mismatch",
            f"exact file inventory differs; extra={extra[:3]} missing={missing[:3]}",
        )


def _validate_contract(
    root: Path,
    workload: WorkloadSpec,
    data: dict[str, Any],
    errors: list[OracleError],
) -> None:
    manifest = data["manifest.json"]
    provenance = data["provenance.jsonl"]
    quality = data["quality.jsonl"]
    route = data["route-map.yml"]
    audit = data["audit.json"]
    rerun = data["rerun-manifest.yml"]
    cost = data["cost.json"]
    normalized = data["normalized"]

    _validate_generation_zero_identity(manifest, route, provenance, audit, rerun, cost, data, errors)
    _validate_path_and_timestamp_relationships(
        workload, manifest, route, provenance, rerun, data["run.log"], errors
    )
    _validate_config_and_source(root, workload, manifest, route, errors)
    routes = _validate_route(route, workload, errors)
    _validate_provenance(root, workload, provenance, routes, errors)
    _validate_normalized(workload, normalized, provenance, errors)
    _validate_quality(workload, quality, errors)
    expected_quality = [
        _expected_quality_record(page, index)
        for index, page in enumerate(workload.page_specs, 1)
    ]
    expected_audit = _expected_audit(manifest["run_id"], routes, workload)
    if audit != expected_audit:
        _add(errors, "audit_queue_mismatch", "audit queues do not re-derive from evidence")
    try:
        markdown = (root / "audit.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _add(errors, "audit_markdown_unreadable", str(exc), "audit.md")
    else:
        if markdown != render_audit_markdown(audit):
            _add(errors, "audit_markdown_mismatch", "audit.md is not the exact audit.json rendering")
    route_source = route["documents"][0]["source"] if route.get("documents") else None
    _validate_rerun(rerun, expected_audit, route_source, expected_quality, errors)
    _validate_cost(cost, provenance, workload, errors)
    _validate_summaries(manifest, workload, quality, normalized, audit, cost, errors)
    _validate_log(data["run.log"], provenance, manifest, workload, errors)


def _validate_generation_zero_identity(
    manifest: dict[str, Any],
    route: dict[str, Any],
    provenance: list[dict[str, Any]],
    audit: dict[str, Any],
    rerun: dict[str, Any],
    cost: dict[str, Any],
    data: dict[str, Any],
    errors: list[OracleError],
) -> None:
    run_id = manifest.get("run_id")
    if manifest.get("parent_run_id") is not None or manifest.get("run_depth") != 0:
        _add(errors, "generation_zero_required", "benchmark runs must be profile-free generation zero")
    if any("reproducibility_profile" in item for item in manifest.get("extractors", [])):
        _add(errors, "profile_forbidden", "generation-zero benchmark runs must remain profile-free")
    expected_extractor = {
        "name": "zero-work",
        "adapter": "zero-work",
        "model": "pageledger-zero-work-v1",
        "version": "1.0.0",
        "prompt_hash": hashlib.sha256(b"").hexdigest(),
        "deterministic": True,
        "input_types": ["text"],
        "output_types": ["text", "markdown", "json", "csv", "markdown_table"],
        "capabilities": ["benchmark", "local", "preloaded-results"],
    }
    if manifest.get("extractors") != [expected_extractor]:
        _add(errors, "extractor_identity_mismatch", "manifest extractor identity differs")
    checks: Iterable[tuple[str, Any]] = (
        ("route-map.yml", route.get("run_id")),
        ("audit.json", audit.get("run_id")),
        ("cost.json", cost.get("run_id")),
        ("rerun parent", rerun.get("parent_run_id")),
    )
    for artifact, value in checks:
        if value != run_id:
            _add(errors, "run_id_relationship_mismatch", f"{artifact} does not link to manifest run_id")
    if rerun.get("run_id") != f"{run_id}-rerun":
        _add(errors, "rerun_id_relationship_mismatch", "rerun run_id is not parent run_id plus -rerun")
    for artifact in ("provenance.jsonl", "run.log"):
        if any(entry.get("run_id") != run_id for entry in data[artifact]):
            _add(errors, "run_id_relationship_mismatch", f"{artifact} contains foreign run identity")
    if any(entry.get("run_id") != run_id for _, entry in data["normalized"]):
        _add(errors, "run_id_relationship_mismatch", "normalized files contain foreign run identity")
    started = manifest.get("started_at")
    completed = manifest.get("completed_at")
    if not isinstance(started, str) or not isinstance(completed, str) or started > completed:
        _add(errors, "timestamp_relationship_mismatch", "manifest timestamps are invalid or reversed")


def _validate_path_and_timestamp_relationships(
    workload: WorkloadSpec | None,
    manifest: dict[str, Any],
    route: dict[str, Any],
    provenance: list[dict[str, Any]],
    rerun: dict[str, Any],
    log: list[dict[str, Any]],
    errors: list[OracleError],
) -> None:
    inputs = manifest.get("inputs", [])
    documents = route.get("documents", [])
    source_values: list[Any] = []
    if len(inputs) == 1:
        source_values.append(inputs[0].get("path"))
    if len(documents) == 1:
        source_values.append(documents[0].get("source"))
    source_values.extend(entry.get("source", {}).get("path") for entry in provenance)
    source_values.extend(item.get("source") for item in rerun.get("items", []))
    resolved_sources = [
        _resolve_consumed_file(value, "source_path_relationship_mismatch", errors)
        for value in source_values
    ]
    present_sources = [path for path in resolved_sources if path is not None]
    if not present_sources or any(path != present_sources[0] for path in present_sources[1:]):
        _add(errors, "source_path_relationship_mismatch", "source paths do not resolve to one file")
    expected_source_hash = (
        _sha256(workload.source_path)
        if workload is not None
        else inputs[0].get("sha256")
        if len(inputs) == 1
        else None
    )
    if present_sources and _sha256(present_sources[0]) != expected_source_hash:
        _add(errors, "source_path_relationship_mismatch", "consumed source bytes differ")

    config_sources = manifest.get("config", {}).get("source_paths")
    if not isinstance(config_sources, list) or len(config_sources) != 1:
        _add(errors, "config_path_relationship_mismatch", "one config source path is required")
    else:
        config_path = _resolve_consumed_file(
            config_sources[0], "config_path_relationship_mismatch", errors
        )
        expected_config_hash = (
            _sha256(workload.config_path)
            if workload is not None
            else manifest.get("config", {}).get("sha256")
        )
        if config_path is not None and _sha256(config_path) != expected_config_hash:
            _add(errors, "config_path_relationship_mismatch", "consumed config bytes differ")

    started = _utc_timestamp(manifest.get("started_at"), errors)
    completed = _utc_timestamp(manifest.get("completed_at"), errors)
    generated = _utc_timestamp(route.get("generated_at"), errors)
    created = _utc_timestamp(rerun.get("created_at"), errors)
    if started is not None and generated != started:
        _add(errors, "timestamp_relationship_mismatch", "route generation must equal run start")
    if completed is not None and created is not None and created < completed:
        _add(errors, "timestamp_relationship_mismatch", "rerun creation precedes completion")
    if len(log) != len(provenance):
        _add(errors, "timestamp_relationship_mismatch", "log/provenance event counts differ")
    for prov, log_entry in zip(provenance, log, strict=False):
        prov_raw = prov.get("timestamp")
        log_raw = log_entry.get("timestamp")
        prov_time = _utc_timestamp(prov_raw, errors)
        log_time = _utc_timestamp(log_raw, errors)
        if prov_raw != log_raw or prov_time != log_time:
            _add(errors, "timestamp_relationship_mismatch", "log timestamp differs from provenance")
        if (
            started is not None
            and completed is not None
            and prov_time is not None
            and not started <= prov_time <= completed
        ):
            _add(errors, "timestamp_relationship_mismatch", "extraction timestamp outside run window")


def _resolve_consumed_file(
    value: Any, code: str, errors: list[OracleError]
) -> Path | None:
    if not isinstance(value, str) or not value:
        _add(errors, code, "path is not a non-empty string")
        return None
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        _add(errors, code, f"path cannot be resolved: {exc}")
        return None
    if not path.is_file():
        _add(errors, code, "path does not resolve to a regular file")
        return None
    return path


def _utc_timestamp(value: Any, errors: list[OracleError]) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        _add(errors, "timestamp_relationship_mismatch", "timestamp must be UTC Z form")
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _add(errors, "timestamp_relationship_mismatch", "timestamp is not parseable UTC")
        return None
    if parsed.tzinfo != timezone.utc:
        _add(errors, "timestamp_relationship_mismatch", "timestamp is not UTC")
        return None
    return parsed


def _validate_config_and_source(
    root: Path,
    workload: WorkloadSpec,
    manifest: dict[str, Any],
    route: dict[str, Any],
    errors: list[OracleError],
) -> None:
    try:
        if (root / "config-snapshot.yml").read_bytes() != workload.config_path.read_bytes():
            _add(errors, "config_bytes_mismatch", "config snapshot differs byte-for-byte")
    except OSError as exc:
        _add(errors, "config_bytes_mismatch", str(exc))
    source_hash = _sha256(workload.source_path)
    config_hash = _sha256(workload.config_path)
    inputs = manifest.get("inputs", [])
    if (
        len(inputs) != 1
        or inputs[0].get("sha256") != source_hash
        or inputs[0].get("page_count") != len(workload.page_specs)
    ):
        _add(errors, "source_contract_mismatch", "manifest input does not match frozen source")
    if manifest.get("config", {}).get("sha256") != config_hash:
        _add(errors, "config_hash_mismatch", "manifest config hash differs from frozen bytes")
    documents = route.get("documents")
    if not isinstance(documents, list) or len(documents) != 1:
        _add(errors, "route_yaml_contract_invalid", "route map must contain exactly one document")
    elif documents[0].get("source_sha256") != source_hash:
        _add(errors, "source_contract_mismatch", "route source hash differs from frozen source")


def _validate_route(
    route: dict[str, Any], workload: WorkloadSpec, errors: list[OracleError]
) -> list[dict[str, Any]]:
    required = {"schema_version", "pageledger_version", "run_id", "generated_at", "classifier", "documents"}
    if set(route) != required or route.get("schema_version") != "0.1":
        _add(errors, "route_yaml_contract_invalid", "route-map top-level contract differs")
        return []
    documents = route.get("documents", [])
    pages = documents[0].get("pages", []) if len(documents) == 1 else []
    if len(documents) == 1 and (
        set(documents[0]) != {"source", "source_sha256", "page_count", "pages"}
        or documents[0].get("page_count") != len(workload.page_specs)
    ):
        _add(errors, "route_yaml_contract_invalid", "route document fields/count differ")
    expected_ids = [_page_id(index) for index in range(1, len(workload.page_specs) + 1)]
    actual_ids = [page.get("page_id") for page in pages if isinstance(page, dict)]
    actual_numbers = [page.get("page_number") for page in pages if isinstance(page, dict)]
    if actual_ids != expected_ids or actual_numbers != list(range(1, len(expected_ids) + 1)):
        _add(errors, "route_membership_mismatch", "route pages do not preserve frozen membership order")
    for page in pages:
        if set(page) != {"page_id", "page_number", "type", "confidence", "action", "reason"}:
            _add(errors, "route_yaml_contract_invalid", "route page fields differ")
            break
        if page["type"] != "prose" or page["action"] != "transcribe_text" or page["reason"] != "configured_adapter":
            _add(errors, "route_membership_mismatch", "route semantics differ from frozen config")
            break
    return pages


def _validate_provenance(
    root: Path,
    workload: WorkloadSpec,
    entries: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    errors: list[OracleError],
) -> None:
    expected_ids = [_page_id(index) for index in range(1, len(workload.page_specs) + 1)]
    if [entry.get("page_id") for entry in entries] != expected_ids:
        _add(errors, "provenance_order_mismatch", "provenance JSONL order differs from membership")
    source_hash = _sha256(workload.source_path)
    for index, (entry, page) in enumerate(zip(entries, workload.page_specs, strict=False), 1):
        page_id = _page_id(index)
        result = entry.get("result", {})
        usage = entry.get("usage", {})
        expected_usage = {
            "pages": 1,
            "tokens": page.tokens,
            "compute_seconds": 0.0,
            "cost_usd": page.cost_usd,
        }
        if entry.get("metrics") != usage or usage != expected_usage:
            _add(errors, "usage_metrics_mismatch", f"usage/metrics differ for {page_id}")
        expected_extractor = {
            "adapter": "zero-work",
            "adapter_version": "1.0.0",
            "model": "pageledger-zero-work-v1",
            "prompt_hash": hashlib.sha256(b"").hexdigest(),
            "deterministic": True,
            "input_types": ["text"],
            "output_types": ["text", "markdown", "json", "csv", "markdown_table"],
            "capabilities": ["benchmark", "local", "preloaded-results"],
        }
        if entry.get("extractor") != expected_extractor:
            _add(errors, "extractor_identity_mismatch", f"extractor differs for {page_id}")
        expected_cost = {"usd": page.cost_usd, "basis": "adapter_reported"}
        if entry.get("cost") != expected_cost:
            _add(errors, "provenance_cost_mismatch", f"cost evidence differs for {page_id}")
        extraction_seconds = entry.get("extraction_seconds")
        if (
            isinstance(extraction_seconds, bool)
            or not isinstance(extraction_seconds, (int, float))
            or extraction_seconds < 0
        ):
            _add(errors, "provenance_timing_mismatch", f"invalid extraction timing for {page_id}")
        if result.get("warnings") != list(page.warnings):
            _add(errors, "provenance_warning_mismatch", f"adapter warnings differ for {page_id}")
        expected_result = (page.format, page.confidence, f"raw/{page_id}.{_RAW_EXTENSIONS[page.format]}")
        if (result.get("format"), result.get("confidence"), result.get("raw_artifact")) != expected_result:
            _add(errors, "provenance_result_mismatch", f"result differs for {page_id}")
        if entry.get("source", {}).get("page_number") != index or entry.get("source", {}).get("sha256") != source_hash:
            _add(errors, "provenance_source_mismatch", f"source evidence differs for {page_id}")
        if index <= len(routes):
            expected_route = {
                "type": routes[index - 1]["type"],
                "action": routes[index - 1]["action"],
                "route_confidence": routes[index - 1]["confidence"],
            }
            if entry.get("route") != expected_route:
                _add(errors, "provenance_route_mismatch", f"route evidence differs for {page_id}")
        declared_raw = result.get("raw_artifact")
        expected_relative = f"raw/{page_id}.{_RAW_EXTENSIONS[page.format]}"
        raw_path = _contained_raw_path(root, declared_raw, expected_relative, errors)
        if raw_path is None:
            continue
        actual_hash = _sha256(raw_path)
        if actual_hash != result.get("raw_sha256"):
            _add(errors, "raw_sha256_mismatch", f"raw bytes do not match provenance for {page_id}")
        expected_bytes = _expected_raw_bytes(page.content)
        actual_bytes = raw_path.read_bytes()
        if actual_bytes != expected_bytes and actual_hash == result.get("raw_sha256"):
            _add(errors, "raw_content_mismatch", f"raw content differs from frozen page {page_id}")
        expected_fixed = {
            "schema_version": "0.1",
            "page_id": page_id,
            "source": {"page_number": index, "sha256": source_hash},
            "route": {
                "type": "prose",
                "action": "transcribe_text",
                "route_confidence": None,
            },
            "extractor": expected_extractor,
            "result": {
                "format": page.format,
                "confidence": page.confidence,
                "warnings": list(page.warnings),
                "raw_artifact": expected_relative,
                "raw_sha256": hashlib.sha256(expected_bytes).hexdigest(),
            },
            "usage": expected_usage,
            "metrics": expected_usage,
            "cost": expected_cost,
        }
        actual_fixed = copy.deepcopy(entry)
        actual_fixed.pop("run_id", None)
        actual_fixed.pop("timestamp", None)
        actual_fixed.pop("extraction_seconds", None)
        actual_fixed.get("source", {}).pop("path", None)
        if actual_fixed != expected_fixed:
            _add(errors, "provenance_record_mismatch", f"complete provenance differs for {page_id}")


def _contained_raw_path(
    root: Path,
    declared: Any,
    expected: str,
    errors: list[OracleError],
) -> Path | None:
    if declared != expected or not _safe_relative_artifact(declared, "raw"):
        _add(errors, "raw_artifact_path_invalid", f"raw reference must be exactly {expected}")
        return None
    try:
        resolved_root = root.resolve(strict=True)
        raw_dir = (resolved_root / "raw").resolve(strict=True)
        candidate = resolved_root / declared
        if candidate.is_symlink():
            raise ValueError("raw artifact is a symlink")
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        _add(errors, "raw_artifact_path_invalid", str(exc))
        return None
    if resolved.parent != raw_dir or not resolved.is_file():
        _add(errors, "raw_artifact_path_invalid", "raw artifact is outside the real raw directory")
        return None
    return resolved


def _safe_relative_artifact(value: Any, directory: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return not path.is_absolute() and path.parts[:1] == (directory,) and ".." not in path.parts


def _validate_normalized(
    workload: WorkloadSpec,
    normalized: list[tuple[str, dict[str, Any]]],
    provenance: list[dict[str, Any]],
    errors: list[OracleError],
) -> dict[str, dict[str, Any]]:
    expected_ids = [
        _page_id(index)
        for index, page in enumerate(workload.page_specs, 1)
        if page.format in _ALIGNABLE
    ]
    actual_ids = [entry.get("page_id") for _, entry in normalized]
    if actual_ids != expected_ids:
        _add(errors, "normalized_order_mismatch", "normalized filenames/pages differ")
    provenance_by_page = {entry.get("page_id"): entry for entry in provenance}
    records = 0
    for filename, entry in normalized:
        page_id = entry.get("page_id")
        records += len(entry.get("records", []))
        prov = provenance_by_page.get(page_id, {})
        if filename != f"{page_id}.json" or entry.get("raw_artifact") != prov.get("result", {}).get("raw_artifact"):
            _add(errors, "normalized_linkage_mismatch", f"normalized linkage differs for {page_id}")
        metrics = entry.get("metrics", {})
        if metrics.get("row_count") != len(entry.get("records", [])):
            _add(errors, "normalized_count_mismatch", f"row count differs for {page_id}")
        if metrics.get("coercion_error_count") != len(entry.get("coercion_errors", [])):
            _add(errors, "normalized_count_mismatch", f"coercion count differs for {page_id}")
        for check in entry.get("checks", []):
            if check.get("rows_checked") != check.get("rows_passed", 0) + check.get("rows_failed", 0):
                _add(errors, "normalized_count_mismatch", f"check counts differ for {page_id}")
    if records != workload.expected["normalized"]["records_normalized"]:
        _add(errors, "normalized_count_mismatch", "normalized record total differs")
    if _normalized_hash(normalized) != workload.expected["normalized"]["canonical_sha256"]:
        _add(errors, "normalized_content_mismatch", "normalized canonical content hash differs")
    return {str(entry.get("page_id")): entry for _, entry in normalized}


def _validate_quality(
    workload: WorkloadSpec,
    entries: list[dict[str, Any]],
    errors: list[OracleError],
) -> None:
    expected_ids = [_page_id(index) for index in range(1, len(workload.page_specs) + 1)]
    if [entry.get("page_id") for entry in entries] != expected_ids:
        _add(errors, "quality_order_mismatch", "quality JSONL order differs from membership")
    for index, (entry, page) in enumerate(zip(entries, workload.page_specs, strict=False), 1):
        page_id = _page_id(index)
        expected = _expected_quality_record(page, index)
        if entry != expected:
            _add(errors, "quality_record_mismatch", f"complete quality record differs for {page_id}")
            if entry.get("warnings") != expected["warnings"]:
                _add(errors, "quality_warning_mismatch", f"quality warnings differ for {page_id}")
            if (
                entry.get("grade") != expected["grade"]
                or entry.get("grade_basis") != expected["grade_basis"]
                or entry.get("grade_detail") != expected["grade_detail"]
            ):
                _add(errors, "quality_grade_mismatch", f"grade differs for {page_id}")


def _expected_quality_record(page: Any, index: int) -> dict[str, Any]:
    text = (
        page.content
        if isinstance(page.content, str)
        else json.dumps(page.content, ensure_ascii=False, sort_keys=True, allow_nan=False)
    )
    token_lengths = _letter_token_lengths(text)
    metrics = _expected_text_metrics(text, token_lengths)
    warnings = list(page.warnings)
    if page.category == "noisy":
        warnings.append("replacement_characters")
    elif page.category == "historical-multiscript":
        warnings.append("historical_orthography")
    grade, basis, detail = _expected_grade_detail(page, warnings)
    return {
        "schema_version": "0.1",
        "page_id": _page_id(index),
        "page_number": index,
        "adapter": "zero-work",
        "character_count": len(text),
        "word_count": len(token_lengths),
        "confidence": page.confidence,
        "confidence_detail": None,
        "warnings": warnings,
        "text_quality": metrics,
        "embedded_text_comparison": None,
        "output_integrity": {
            "instruction_markers": [],
            "parent_character_count": None,
            "character_delta": None,
            "character_ratio": None,
        },
        "grade": grade,
        "grade_basis": basis,
        "grade_detail": detail,
    }


def _expected_grade_detail(
    page: Any, warnings: list[str]
) -> tuple[str, str, dict[str, Any]]:
    confidence = page.confidence
    if confidence >= 0.90:
        confidence_band = "A"
    elif confidence >= 0.80:
        confidence_band = "B"
    elif confidence >= 0.70:
        confidence_band = "C"
    elif confidence >= 0.55:
        confidence_band = "D"
    else:
        confidence_band = "F"
    reasons: list[str] = []
    if confidence_band != "A":
        reasons.append(f"confidence {confidence:.2f} in {confidence_band} band")
    warning_count = len(warnings)
    warning_band = ("A", "B", "C", "D")[min(warning_count, 3)]
    if warnings:
        reasons.append(
            f"{warning_count} quality warning{'s' if warning_count != 1 else ''}: "
            + ", ".join(warnings)
        )
    signals_grade = _worst_grade(confidence_band, warning_band)
    schema_grade: str | None = None
    coverage: float | None = None
    pass_rate: float | None = None
    if page.format in _ALIGNABLE:
        coverage = 1.0
        if page.format == "json":
            schema_grade, pass_rate = "A", 1.0
        elif page.format == "csv":
            schema_grade, pass_rate = "B", None
            reasons.append("1 coercion error cap schema grade at B")
        else:
            schema_grade, pass_rate = "D", 0.0
            reasons.append("arithmetic_pass_rate 0.00 in D band")
    basis = "schema_aware" if schema_grade is not None else "signals_only"
    grade = _worst_grade(signals_grade, schema_grade) if schema_grade else signals_grade
    return grade, basis, {
        "signals_grade": signals_grade,
        "schema_grade": schema_grade,
        "confidence_band": confidence_band,
        "warning_count": warning_count,
        "required_column_coverage": coverage,
        "arithmetic_pass_rate": pass_rate,
        "reasons": reasons,
    }


def _letter_token_lengths(text: str) -> list[int]:
    lengths: list[int] = []
    current = 0
    for character in text:
        category = unicodedata.category(character)
        if category.startswith("L") or (category.startswith("M") and current):
            current += 1
        elif character in {"\u200c", "\u200d"} and current:
            continue
        elif current:
            lengths.append(current)
            current = 0
    if current:
        lengths.append(current)
    return lengths


def _expected_text_metrics(text: str, token_lengths: list[int]) -> dict[str, Any]:
    character_count = len(text)
    suspicious = sum(_suspicious_symbol(character) for character in text)
    letter_count = sum(unicodedata.category(char).startswith("L") for char in text)
    latin_count = sum(
        unicodedata.category(char).startswith("L")
        and "LATIN" in unicodedata.name(char, "")
        for char in text
    )
    token_count = len(token_lengths)
    return {
        "replacement_character_count": text.count("\ufffd"),
        "control_character_count": sum(
            ord(char) < 32 and char not in {"\n", "\r", "\t", "\f"} for char in text
        ),
        "suspicious_symbol_count": suspicious,
        "suspicious_symbol_ratio": 0.0 if not text else round(suspicious / character_count, 4),
        "alpha_token_count": token_count,
        "mean_token_length": None if not token_count else round(sum(token_lengths) / token_count, 2),
        "max_token_length": max(token_lengths, default=0),
        "short_token_ratio": (
            None
            if not token_count
            else round(sum(length <= 2 for length in token_lengths) / token_count, 4)
        ),
        "whitespace_character_ratio": (
            0.0 if not text else round(sum(char.isspace() for char in text) / character_count, 4)
        ),
        "latin_letter_ratio": 0.0 if not letter_count else round(latin_count / letter_count, 4),
        "prereform_letter_count": sum(char in _PREREFORM_LETTERS for char in text),
        "terminal_hard_sign_count": len(_TERMINAL_HARD_SIGN.findall(text)),
    }


def _suspicious_symbol(character: str) -> bool:
    if character in {"_", "|", "\\", "/", "{", "}", "[", "]", "•"}:
        return True
    if character.isalnum() or character.isspace():
        return False
    if unicodedata.category(character)[0] in {"L", "M", "N", "P", "Z"}:
        return False
    if character in ".,;:!?()'\"-$%&+=*#@<>«»„“”‘’‚—–…·§№°":
        return False
    return not character.isascii()


def _worst_grade(*grades: str) -> str:
    order = {grade: index for index, grade in enumerate(("A", "B", "C", "D", "F"))}
    return max(grades, key=order.__getitem__)


def _expected_audit(
    run_id: str,
    routes: list[dict[str, Any]],
    workload: WorkloadSpec,
) -> dict[str, Any]:
    review: list[dict[str, Any]] = []
    for index, (route, page) in enumerate(
        zip(routes, workload.page_specs, strict=False), 1
    ):
        entry = _expected_quality_record(page, index)
        base = {
            "page_id": entry["page_id"],
            "page_number": entry["page_number"],
            "type": route["type"],
            "confidence": route["confidence"],
            "grade": entry["grade"],
            "grade_basis": entry["grade_basis"],
        }
        if entry["warnings"]:
            review.append({**base, "action": "review", "reason": "quality_warning"})
        if entry["grade"] in {"D", "F"}:
            review.append({**base, "action": "review", "reason": "grade_below_threshold"})
        if page.format == "markdown_table":
            review.append(
                {**base, "action": "review", "reason": "rerun_if:arithmetic_failure_rate_above"}
            )
    return {"schema_version": "0.1", "run_id": run_id, "review_queue": review, "quarantine_queue": []}


def _validate_rerun(
    rerun: dict[str, Any],
    audit: dict[str, Any],
    route_source: Any,
    quality: list[dict[str, Any]],
    errors: list[OracleError],
) -> None:
    required = {
        "schema_version", "run_id", "parent_run_id", "parent_manifest", "rerun_depth",
        "max_rerun_depth", "created_at", "reason", "rerun_executable", "rerun_status", "items",
    }
    if set(rerun) != required:
        _add(errors, "rerun_yaml_contract_invalid", "rerun manifest fields differ")
        return
    fixed_headers = {
        "schema_version": "0.1",
        "parent_manifest": "manifest.json",
        "rerun_depth": 0,
        "max_rerun_depth": 2,
        "reason": "audit_policy",
        "rerun_executable": True,
        "rerun_status": "executable",
    }
    if any(rerun.get(key) != value for key, value in fixed_headers.items()):
        _add(errors, "rerun_header_mismatch", "fixed generation-zero rerun headers differ")
    grade_by_page = {entry["page_id"]: entry["grade"] for entry in quality}
    by_page: dict[str, dict[str, Any]] = {}
    for item in audit["review_queue"]:
        existing = by_page.get(item["page_id"])
        if existing:
            if item["reason"] not in existing["reason"].split("+"):
                existing["reason"] += "+" + item["reason"]
            continue
        by_page[item["page_id"]] = {
            "page_id": item["page_id"],
            "page_number": item["page_number"],
            "source": None,
            "action": item["action"],
            "reason": item["reason"],
            "previous_grade": grade_by_page[item["page_id"]],
        }
    expected = list(by_page.values())
    actual = copy.deepcopy(rerun.get("items", []))
    if any(item.get("source") != route_source for item in actual):
        _add(errors, "rerun_source_mismatch", "rerun sources do not link to route document")
    for item in actual:
        item["source"] = None
    if actual != expected:
        _add(errors, "rerun_policy_mismatch", "rerun items do not re-derive from audit policy")


def _validate_cost(
    cost: dict[str, Any],
    provenance: list[dict[str, Any]],
    workload: WorkloadSpec,
    errors: list[OracleError],
) -> None:
    expected_pages = len(workload.page_specs)
    expected_tokens = sum(page.tokens or 0 for page in workload.page_specs)
    expected_compute = 0.0
    expected_cost = round(sum(page.cost_usd or 0 for page in workload.page_specs), 12)
    extraction = round(sum(entry.get("extraction_seconds", 0) for entry in provenance), 3)
    expected_rollup = {
        "pages": expected_pages,
        "tokens": expected_tokens,
        "compute_seconds": expected_compute,
        "extraction_seconds": extraction,
    }
    if cost.get("usage") != expected_rollup:
        _add(errors, "cost_usage_mismatch", "cost usage does not roll up provenance")
    if cost.get("cost_usd") != expected_cost or cost.get("cost_usd") != workload.expected["cost"]["cost_usd"]:
        _add(errors, "cost_total_mismatch", "cost total does not roll up page costs")
    if (
        cost.get("cost_basis") != "adapter_reported"
        or cost.get("cost_known") is not True
        or cost.get("tokens_total") != expected_tokens
        or cost.get("pages_extracted") != expected_pages
    ):
        _add(errors, "cost_basis_mismatch", "cost basis/count fields differ")
    rollup = {
        "pages": expected_pages,
        "tokens": expected_tokens,
        "compute_seconds": expected_compute,
        "cost_usd": expected_cost,
        "cost_known": True,
    }
    if cost.get("by_adapter") != {"zero-work": rollup} or cost.get("by_page_type") != {"prose": rollup}:
        _add(errors, "cost_rollup_mismatch", "cost adapter/page-type rollups differ")
    expected_report = {
        "schema_version": "0.1",
        "run_id": cost.get("run_id"),
        "execution_mode": "execute",
        "currency": "USD",
        "canonical_unit": "pages",
        "pages_extracted": expected_pages,
        "tokens_total": expected_tokens,
        "pricing": {},
        "usage": expected_rollup,
        "cost_usd": expected_cost,
        "cost_known": True,
        "cost_basis": "adapter_reported",
        "by_adapter": {"zero-work": rollup},
        "by_page_type": {"prose": rollup},
    }
    if cost != expected_report:
        _add(errors, "cost_record_mismatch", "complete cost record differs")


def _validate_summaries(
    manifest: dict[str, Any],
    workload: WorkloadSpec,
    quality: list[dict[str, Any]],
    normalized: list[tuple[str, dict[str, Any]]],
    audit: dict[str, Any],
    cost: dict[str, Any],
    errors: list[OracleError],
) -> None:
    expected = workload.expected
    records = sum(len(entry.get("records", [])) for _, entry in normalized)
    summary = manifest.get("summary", {})
    wanted = {
        "pages_total": len(workload.page_specs),
        "pages_extracted": len(workload.page_specs),
        "pages_skipped": 0,
        "pages_routed_review": 0,
        "pages_quarantined": 0,
        "records_normalized": records,
        "estimated_cost_usd": cost.get("cost_usd"),
        "quality_warning_pages": sum(bool(entry.get("warnings")) for entry in quality),
    }
    if summary != wanted:
        _add(errors, "manifest_summary_mismatch", "manifest summary does not re-derive")
    if Counter(item["reason"] for item in audit["review_queue"]) != Counter(expected["audit"]["review_queue_by_reason"]):
        _add(errors, "audit_count_mismatch", "audit reason counts differ from frozen receipt")


def _validate_log(
    log: list[dict[str, Any]],
    provenance: list[dict[str, Any]],
    manifest: dict[str, Any],
    workload: WorkloadSpec,
    errors: list[OracleError],
) -> None:
    if [entry.get("page_id") for entry in log] != [entry.get("page_id") for entry in provenance]:
        _add(errors, "run_log_order_mismatch", "run.log extraction order differs")
    if len(log) != manifest["summary"].get("pages_extracted") or any(
        entry.get("status") != "extracted" for entry in log
    ):
        _add(errors, "run_log_summary_mismatch", "run.log extracted-event count differs")
    for index, (event, prov, _page) in enumerate(
        zip(log, provenance, workload.page_specs, strict=False), 1
    ):
        expected = {
            "schema_version": "0.1",
            "timestamp": prov.get("timestamp"),
            "level": "INFO",
            "run_id": manifest.get("run_id"),
            "page_id": _page_id(index),
            "adapter": "zero-work",
            "status": "extracted",
            "error": None,
            "budget_warning": None,
            "attempt": 1,
        }
        if event != expected:
            _add(errors, "run_log_record_mismatch", f"run.log differs for {_page_id(index)}")


def _canonical_loaded(root: Path, data: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {}
    rules = load_frozen_manifest()["allowed_nondeterminism"]
    for filename in ("audit.json", "cost.json", "manifest.json"):
        canonical[filename] = _apply_exact_paths(copy.deepcopy(data[filename]), rules[filename])
    for filename in ("provenance.jsonl", "quality.jsonl", "run.log"):
        canonical[filename] = _apply_exact_paths(copy.deepcopy(data[filename]), rules[filename])
    for filename in ("route-map.yml", "rerun-manifest.yml"):
        canonical[filename] = _apply_exact_paths(copy.deepcopy(data[filename]), rules[filename])
    canonical["normalized"] = [
        (filename, _apply_exact_paths(copy.deepcopy(entry), rules["normalized/*.json"]))
        for filename, entry in data["normalized"]
    ]
    canonical_audit = canonical["audit.json"]
    canonical["audit.md"] = render_audit_markdown(canonical_audit)
    canonical["config-snapshot.yml"] = (root / "config-snapshot.yml").read_bytes().hex()
    canonical["raw"] = [
        (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted((root / "raw").iterdir(), key=lambda item: item.name)
    ]
    return canonical


def _apply_exact_paths(value: Any, pointers: list[str]) -> Any:
    for pointer in pointers:
        parts = pointer.split("/")[1:]
        _replace_pointer(value, parts, _sentinel_for(pointer))
    return value


def _replace_pointer(value: Any, parts: list[str], replacement: str) -> None:
    if not parts:
        raise ValueError("cannot replace document root")
    head, *tail = parts
    if head == "*":
        if not isinstance(value, list):
            raise ValueError("wildcard pointer requires a list")
        for item in value:
            _replace_pointer(item, tail, replacement)
        return
    key: str | int = int(head) if isinstance(value, list) and head.isdigit() else head
    if not tail:
        value[key] = replacement
    else:
        _replace_pointer(value[key], tail, replacement)


def _sentinel_for(pointer: str) -> str:
    if "extraction_seconds" in pointer:
        return _SENTINELS["extraction"]
    if pointer.endswith("/run_id") or pointer.endswith("/parent_run_id"):
        return _SENTINELS["run_id"]
    if pointer.endswith("/source") or pointer.endswith("/path"):
        return _SENTINELS["source"]
    if "source_paths" in pointer:
        return _SENTINELS["config"]
    return _SENTINELS["timestamp"]


def _canonical_verifier_report(report: dict[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(report)
    if "run_dir" not in copied:
        raise ValueError("verifier report is missing run_dir")
    del copied["run_dir"]
    return copied


def _normalized_hash(entries: list[tuple[str, dict[str, Any]]]) -> str:
    payload = ""
    for _, entry in entries:
        copied = dict(entry)
        copied["run_id"] = "<run-id>"
        payload += json.dumps(
            copied, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_raw_bytes(content: Any) -> bytes:
    if isinstance(content, str):
        return content.encode("utf-8")
    return json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _expected_grade(category: str, result_format: str) -> str:
    if category == "noisy":
        return "F"
    if category == "historical-multiscript":
        return "B"
    if category == "clean-control" or result_format == "json":
        return "A"
    if result_format == "csv":
        return "B"
    return "D"


def _page_id(number: int) -> str:
    return f"doc_0001_page_{number:04d}"


def _read_json(path: Path, artifact: str, errors: list[OracleError]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _add(errors, "artifact_malformed", str(exc), artifact)
        return None
    if not isinstance(value, dict):
        _add(errors, "artifact_malformed", "top level must be a mapping", artifact)
        return None
    return value


def _read_jsonl(path: Path, artifact: str, errors: list[OracleError]) -> list[dict[str, Any]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        entries = [json.loads(line) for line in lines if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _add(errors, "artifact_malformed", str(exc), artifact)
        return None
    if any(not isinstance(entry, dict) for entry in entries):
        _add(errors, "artifact_malformed", "every JSONL line must be a mapping", artifact)
        return None
    return entries


def _read_yaml(path: Path, artifact: str, errors: list[OracleError]) -> dict[str, Any] | None:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        _add(errors, "artifact_malformed", str(exc), artifact)
        return None
    if not isinstance(value, dict):
        _add(errors, "artifact_malformed", "top level must be a mapping", artifact)
        return None
    return value


def _schema_validate(
    value: Any,
    schema: dict[str, Any] | None,
    artifact: str,
    errors: list[OracleError],
) -> None:
    if schema is None:
        _add(errors, "schema_unavailable", "frozen schema unavailable", artifact)
        return
    failures = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda exc: list(exc.path))
    if failures:
        _add(errors, "schema_validation_failed", failures[0].message, artifact)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _add(
    errors: list[OracleError], code: str, message: str, artifact: str | None = None
) -> None:
    errors.append(OracleError(code, message, artifact))


def _error_text(errors: list[OracleError]) -> str:
    return "; ".join(f"{error.code}: {error.message}" for error in errors)
