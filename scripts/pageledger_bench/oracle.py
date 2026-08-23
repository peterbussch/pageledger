"""Schema-complete independent oracle for frozen PageLedger benchmark runs."""

from __future__ import annotations

import copy
import hashlib
import json
import stat
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from pageledger.artifacts import render_audit_markdown
from pageledger.verify import verify_run
from scripts.pageledger_bench.workloads import WorkloadSpec, load_frozen_manifest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS_DIR = _REPOSITORY_ROOT / "schemas"
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
    schemas = _load_frozen_schemas(manifest, errors)
    inventory = _regular_file_inventory(root, errors)
    unsafe_inventory = any(
        error.code in {"run_directory_invalid", "symlink_forbidden", "non_regular_forbidden"}
        for error in errors
    )
    loaded = None if unsafe_inventory else _load_artifacts(root, schemas, errors)

    if loaded is not None:
        _validate_inventory(inventory, workload, errors)
        try:
            _validate_contract(root, workload, loaded, errors)
        except (IndexError, KeyError, OSError, TypeError, ValueError) as exc:
            _add(errors, "contract_validation_failed", str(exc))

    report = verify_run(root)
    if report.get("status") != "pass":
        _add(errors, "verifier_failed", "pageledger.verify.verify_run did not pass")
    canonical = None
    if loaded is not None:
        try:
            canonical = _canonical_loaded(root, loaded)
        except (KeyError, TypeError, ValueError) as exc:
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
    schemas = _load_frozen_schemas(load_frozen_manifest(), errors)
    loaded = _load_artifacts(root, schemas, errors)
    if errors or loaded is None:
        detail = "; ".join(f"{item.code}: {item.message}" for item in errors)
        raise ValueError(detail or "run artifacts could not be loaded")
    return _canonical_loaded(root, loaded)


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
    _validate_config_and_source(root, workload, manifest, route, errors)
    routes = _validate_route(route, workload, errors)
    _validate_provenance(root, workload, provenance, routes, errors)
    normalized_by_page = _validate_normalized(workload, normalized, provenance, errors)
    _validate_quality(workload, quality, normalized_by_page, errors)
    expected_audit = _expected_audit(manifest["run_id"], routes, quality, normalized_by_page)
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
    _validate_rerun(rerun, audit, route_source, quality, errors)
    _validate_cost(cost, provenance, workload, errors)
    _validate_summaries(manifest, workload, quality, normalized, audit, cost, errors)
    _validate_log(data["run.log"], provenance, manifest, errors)


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
        raw_path = root / str(result.get("raw_artifact", ""))
        actual_hash = _sha256(raw_path) if raw_path.is_file() and not raw_path.is_symlink() else None
        if actual_hash != result.get("raw_sha256"):
            _add(errors, "raw_sha256_mismatch", f"raw bytes do not match provenance for {page_id}")
        expected_bytes = _expected_raw_bytes(page.content)
        try:
            actual_bytes = raw_path.read_bytes()
        except OSError:
            actual_bytes = None
        if actual_bytes != expected_bytes and actual_hash == result.get("raw_sha256"):
            _add(errors, "raw_content_mismatch", f"raw content differs from frozen page {page_id}")


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
    normalized: dict[str, dict[str, Any]],
    errors: list[OracleError],
) -> None:
    expected_ids = [_page_id(index) for index in range(1, len(workload.page_specs) + 1)]
    if [entry.get("page_id") for entry in entries] != expected_ids:
        _add(errors, "quality_order_mismatch", "quality JSONL order differs from membership")
    for index, (entry, page) in enumerate(zip(entries, workload.page_specs, strict=False), 1):
        page_id = _page_id(index)
        expected_warnings = {
            "structured": [],
            "noisy": ["adapter_low_confidence", "replacement_characters"],
            "historical-multiscript": ["historical_orthography"],
            "clean-control": [],
        }[page.category]
        if entry.get("warnings") != expected_warnings:
            _add(errors, "quality_warning_mismatch", f"quality warnings differ for {page_id}")
        expected_grade = _expected_grade(page.category, page.format)
        expected_basis = "schema_aware" if page_id in normalized else "signals_only"
        if entry.get("grade") != expected_grade or entry.get("grade_basis") != expected_basis:
            _add(errors, "quality_grade_mismatch", f"grade differs for {page_id}")
        if entry.get("grade_detail", {}).get("warning_count") != len(entry.get("warnings", [])):
            _add(errors, "quality_grade_mismatch", f"grade warning count differs for {page_id}")


def _expected_audit(
    run_id: str,
    routes: list[dict[str, Any]],
    quality: list[dict[str, Any]],
    normalized: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    review: list[dict[str, Any]] = []
    for route, entry in zip(routes, quality, strict=False):
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
        alignment = normalized.get(entry["page_id"])
        pass_rate = alignment.get("metrics", {}).get("arithmetic_pass_rate") if alignment else None
        if isinstance(pass_rate, (int, float)) and pass_rate < 1:
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
    if set(rerun) != required or rerun.get("schema_version") != "0.1":
        _add(errors, "rerun_yaml_contract_invalid", "rerun manifest fields differ")
        return
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
    if actual != expected or rerun.get("rerun_status") != "executable" or rerun.get("rerun_executable") is not True:
        _add(errors, "rerun_policy_mismatch", "rerun items do not re-derive from audit policy")


def _validate_cost(
    cost: dict[str, Any],
    provenance: list[dict[str, Any]],
    workload: WorkloadSpec,
    errors: list[OracleError],
) -> None:
    usage = [entry.get("usage", {}) for entry in provenance]
    expected_pages = sum(item.get("pages", 0) for item in usage)
    expected_tokens = sum(item.get("tokens", 0) for item in usage)
    expected_compute = sum(item.get("compute_seconds", 0) for item in usage)
    expected_cost = round(sum(entry.get("cost", {}).get("usd", 0) for entry in provenance), 12)
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
    errors: list[OracleError],
) -> None:
    if [entry.get("page_id") for entry in log] != [entry.get("page_id") for entry in provenance]:
        _add(errors, "run_log_order_mismatch", "run.log extraction order differs")
    if len(log) != manifest["summary"].get("pages_extracted") or any(
        entry.get("status") != "extracted" for entry in log
    ):
        _add(errors, "run_log_summary_mismatch", "run.log extracted-event count differs")


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
