"""Relational integrity verification for PageLedger run directories.

This checks agreement between artifacts. It deliberately does not claim to
validate extraction accuracy or replace the JSON Schemas in ``schemas/``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import yaml

from .artifacts import build_rerun_manifest, render_audit_markdown
from .config import PageLedgerConfig
from .replay import ReplayError, validate_reproducibility_profile

REQUIRED_ARTIFACTS = {
    "config_snapshot",
    "route_map",
    "raw_dir",
    "normalized_dir",
    "audit",
    "audit_md",
    "provenance",
    "quality",
    "cost",
    "run_log",
    "rerun_manifest",
}


def verify_run(
    run_dir: Path,
    *,
    check_external_sources: bool = True,
    check_rerun_manifest: bool = True,
) -> dict[str, Any]:
    """Return a structured coherence report for a run directory.

    Portable transported baselines can disable checks that would dereference
    historical external sources or treat a copied rerun plan as executable
    evidence. Ordinary verification keeps both checks enabled by default.
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    counts = {
        "routed_pages": 0,
        "extracted_pages": 0,
        "quality_pages": 0,
        "raw_artifacts": 0,
        "normalized_pages": 0,
        "normalized_records": 0,
        "quality_warning_pages": 0,
        "audit_references": 0,
        "rerun_references": 0,
    }
    try:
        declared_root = run_dir.expanduser()
    except (OSError, RuntimeError, ValueError):
        declared_root = run_dir
    root = _safe_resolve(declared_root)
    if root is None:
        _add(
            errors,
            "run_path_invalid",
            "Run directory cannot be resolved safely",
            artifact=str(declared_root),
        )
        return _report(declared_root.absolute(), errors, warnings, counts)
    declared_manifest_path = root / "manifest.json"
    manifest_path = _safe_resolve(declared_manifest_path)
    if (
        declared_manifest_path.is_symlink()
        or manifest_path is None
        or manifest_path.parent != root
    ):
        _add(
            errors,
            "manifest_path_invalid",
            "manifest.json must be a regular file contained in the run directory",
            artifact="manifest.json",
        )
        return _report(root, errors, warnings, counts)
    if not manifest_path.is_file():
        _add(errors, "manifest_missing", "No manifest.json found", artifact="manifest.json")
        return _report(root, errors, warnings, counts)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("top level must be a mapping")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        _add(
            errors,
            "manifest_malformed",
            f"manifest.json cannot be parsed: {exc}",
            artifact="manifest.json",
        )
        return _report(root, errors, warnings, counts)

    run_id = manifest.get("run_id")
    schema_version = manifest.get("schema_version")
    if not isinstance(run_id, str) or not run_id:
        _add(errors, "manifest_identity_missing", "Manifest run_id is missing or invalid")
    if not isinstance(schema_version, str) or not schema_version:
        _add(
            errors,
            "manifest_identity_missing",
            "Manifest schema_version is missing or invalid",
        )
    if not isinstance(manifest.get("pageledger_version"), str):
        _add(
            warnings,
            "legacy_evidence_incomplete",
            "Manifest predates PageLedger generator-version recording",
            artifact="manifest.json",
        )

    manifest_identities = (
        _manifest_extractor_identities(manifest, errors)
        if manifest.get("execution_mode") != "dry_run"
        else []
    )

    declarations = manifest.get("artifacts")
    if not isinstance(declarations, dict):
        _add(errors, "artifact_declarations_missing", "Manifest artifacts must be a mapping")
        return _report(root, errors, warnings, counts)
    for key in sorted(REQUIRED_ARTIFACTS - declarations.keys()):
        _add(
            errors,
            "artifact_declaration_missing",
            f"Manifest does not declare {key}",
            artifact=key,
        )

    loaded: dict[str, Any] = {}
    paths: dict[str, Path] = {}
    for key, relative in declarations.items():
        if not isinstance(relative, str) or not relative:
            _add(
                errors,
                "artifact_path_invalid",
                f"Artifact path for {key} is not a non-empty string",
                artifact=key,
            )
            continue
        path = _safe_resolve(root / relative)
        if path is None:
            _add(
                errors,
                "artifact_path_invalid",
                f"Artifact path cannot be resolved safely: {relative}",
                artifact=relative,
            )
            continue
        if path != root and root not in path.parents:
            _add(
                errors,
                "artifact_path_invalid",
                f"Artifact path escapes the run directory: {relative}",
                artifact=relative,
            )
            continue
        paths[key] = path
        expects_directory = key in {"raw_dir", "normalized_dir"}
        exists = path.is_dir() if expects_directory else path.is_file()
        if not exists:
            _add(
                errors,
                "replay_artifact_missing" if key == "replay" else "artifact_missing",
                (
                    f"Manifest-declared replay artifact is missing: {relative}"
                    if key == "replay"
                    else f"Manifest-declared artifact is missing: {relative}"
                ),
                artifact=relative,
            )
            continue
        if key == "rerun_manifest" and not check_rerun_manifest:
            continue
        try:
            loaded[key] = _load_artifact(key, path)
        except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError, ValueError) as exc:
            _add(
                errors,
                "replay_artifact_malformed" if key == "replay" else "artifact_malformed",
                (
                    f"Replay artifact cannot be parsed: {exc}"
                    if key == "replay"
                    else f"Artifact cannot be parsed: {exc}"
                ),
                artifact=relative,
            )
        except Exception as exc:
            if key != "replay":
                raise
            _add(
                errors,
                "replay_artifact_malformed",
                f"Replay artifact cannot be loaded: {exc}",
                artifact=relative,
            )

    manifest_config = manifest.get("config")
    if not isinstance(manifest_config, dict):
        manifest_config = {}
        _add(errors, "manifest_structure_invalid", "Manifest config must be a mapping")
    manifest_inputs = manifest.get("inputs")
    if not isinstance(manifest_inputs, list):
        manifest_inputs = []
        _add(errors, "manifest_structure_invalid", "Manifest inputs must be a list")
    summary = manifest.get("summary")
    if not isinstance(summary, dict):
        summary = {}
        _add(errors, "manifest_structure_invalid", "Manifest summary must be a mapping")

    config = loaded.get("config_snapshot")
    config_path = paths.get("config_snapshot")
    if config_path is not None and config_path.is_file():
        expected_hash = manifest_config.get("sha256")
        actual_hash = _sha256(config_path)
        if not isinstance(expected_hash, str) or expected_hash != actual_hash:
            _add(
                errors,
                "config_hash_mismatch",
                "Config snapshot SHA-256 does not match the manifest",
                artifact=config_path.name,
                expected=expected_hash,
                actual=actual_hash,
            )
    if isinstance(config, dict) and config.get("schema_version") not in {
        None,
        schema_version,
    }:
        _add(
            errors,
            "schema_version_mismatch",
            "Config snapshot schema_version does not match the manifest",
            artifact=paths["config_snapshot"].name,
        )

    alignment = manifest.get("alignment")
    if alignment is not None:
        if not isinstance(alignment, dict):
            _add(
                errors,
                "manifest_structure_invalid",
                "Manifest alignment must be a mapping",
                artifact="manifest.json",
            )
        else:
            schema_source = alignment.get("schema_source")
            expected_schema_hash = alignment.get("schema_sha256")
            required_alignment_fields = (
                "aligned_at",
                "schema_source",
                "schema_sha256",
                "pageledger_version",
            )
            if any(
                not isinstance(alignment.get(field), str)
                or not alignment.get(field)
                for field in required_alignment_fields
            ):
                _add(
                    errors,
                    "manifest_structure_invalid",
                    "Manifest alignment fields must be non-empty strings",
                    artifact="manifest.json",
                )
            else:
                assert isinstance(schema_source, str)
                assert isinstance(expected_schema_hash, str)
                if schema_source == "config_snapshot":
                    if config_path is not None and config_path.is_file():
                        actual_schema_hash = _sha256(config_path)
                        if actual_schema_hash != expected_schema_hash:
                            _add(
                                errors,
                                "alignment_schema_hash_mismatch",
                                "Alignment schema hash differs from config-snapshot.yml",
                                artifact=config_path.name,
                                expected=expected_schema_hash,
                                actual=actual_schema_hash,
                            )
                else:
                    declared_snapshot = root / "align-schema-snapshot.yml"
                    alignment_snapshot = _safe_resolve(declared_snapshot)
                    if alignment_snapshot is None:
                        _add(
                            errors,
                            "alignment_schema_snapshot_invalid",
                            "External alignment schema snapshot cannot be resolved safely",
                            artifact=declared_snapshot.name,
                        )
                    elif alignment_snapshot != root and root not in alignment_snapshot.parents:
                        _add(
                            errors,
                            "alignment_schema_snapshot_invalid",
                            "External alignment schema snapshot resolves outside the run directory",
                            artifact=declared_snapshot.name,
                        )
                    elif not alignment_snapshot.is_file():
                        _add(
                            errors,
                            "alignment_schema_snapshot_missing",
                            "External alignment schema snapshot is missing",
                            artifact=declared_snapshot.name,
                        )
                    else:
                        actual_schema_hash = _sha256(alignment_snapshot)
                        if actual_schema_hash != expected_schema_hash:
                            _add(
                                errors,
                                "alignment_schema_hash_mismatch",
                                "Alignment schema hash differs from align-schema-snapshot.yml",
                                artifact=declared_snapshot.name,
                                expected=expected_schema_hash,
                                actual=actual_schema_hash,
                            )

    route = loaded.get("route_map")
    route_pages: dict[str, dict[str, Any]] = {}
    route_sources: list[Any] = []
    if isinstance(route, dict):
        _check_identity(route, run_id, schema_version, errors, paths["route_map"].name)
        documents = route.get("documents")
        if not isinstance(documents, list):
            _add(errors, "artifact_structure_invalid", "Route documents must be a list")
        else:
            for document_index, document in enumerate(documents):
                route_sources.append(
                    document.get("source") if isinstance(document, dict) else None
                )
                if not isinstance(document, dict) or not isinstance(document.get("pages"), list):
                    _add(errors, "artifact_structure_invalid", "Route document is malformed")
                    continue
                source = document.get("source")
                manifest_input = (
                    manifest_inputs[document_index]
                    if document_index < len(manifest_inputs)
                    else None
                )
                source_sha256 = (
                    manifest_input.get("sha256")
                    if isinstance(manifest_input, dict)
                    else None
                )
                if (
                    document.get("source_sha256") is not None
                    and document.get("source_sha256") != source_sha256
                ):
                    _add(
                        errors,
                        "source_identity_mismatch",
                        "Route document source hash differs from the manifest input",
                        artifact=paths["route_map"].name,
                    )
                if (
                    document.get("page_count") is not None
                    and isinstance(manifest_input, dict)
                    and document.get("page_count") != manifest_input.get("page_count")
                ):
                    _add(
                        errors,
                        "route_page_count_mismatch",
                        "Route document page_count differs from the manifest input",
                        artifact=paths["route_map"].name,
                    )
                for page in document["pages"]:
                    counts["routed_pages"] += 1
                    if not isinstance(page, dict) or not isinstance(page.get("page_id"), str):
                        _add(errors, "artifact_structure_invalid", "Route page is malformed")
                        continue
                    page_id = page["page_id"]
                    if page_id in route_pages:
                        _add(
                            errors,
                            "duplicate_route_page_id",
                            f"Route page_id appears more than once: {page_id}",
                            page_id=page_id,
                        )
                        continue
                    route_pages[page_id] = {
                        "page_number": page.get("page_number"),
                        "source": source,
                        "source_sha256": source_sha256,
                        "type": page.get("type"),
                        "action": page.get("action"),
                        "confidence": page.get("confidence"),
                    }
    if summary.get("pages_total") != counts["routed_pages"]:
        _add(
            errors,
            "route_page_count_mismatch",
            "Route page count does not match manifest.summary.pages_total",
            expected=summary.get("pages_total"),
            actual=counts["routed_pages"],
        )

    routed_review_count = sum(
        page.get("action") == "review" for page in route_pages.values()
    )
    skipped_page_count = sum(
        page.get("action") == "skip" for page in route_pages.values()
    )
    if "pages_routed_review" not in summary:
        _add(
            warnings,
            "legacy_evidence_incomplete",
            "Manifest predates pages_routed_review accounting",
        )
    elif summary.get("pages_routed_review") != routed_review_count:
        _add(
            errors,
            "routed_review_count_mismatch",
            "Review-route count does not match manifest.summary.pages_routed_review",
            expected=summary.get("pages_routed_review"),
            actual=routed_review_count,
        )
    if summary.get("pages_skipped") != skipped_page_count:
        _add(
            errors,
            "skipped_page_count_mismatch",
            "Skip-route count does not match manifest.summary.pages_skipped",
            expected=summary.get("pages_skipped"),
            actual=skipped_page_count,
        )
    if "pages_routed_review" in summary:
        accounting_values = {
            "pages_total": summary.get("pages_total"),
            "pages_extracted": summary.get("pages_extracted"),
            "pages_failed": summary.get("pages_failed", 0),
            "pages_not_attempted": summary.get("pages_not_attempted", 0),
            "pages_skipped": summary.get("pages_skipped"),
            "pages_routed_review": summary.get("pages_routed_review"),
        }
        if all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in accounting_values.values()
        ):
            accounted = sum(
                value
                for key, value in accounting_values.items()
                if key != "pages_total"
            )
            if accounting_values["pages_total"] != accounted:
                _add(
                    errors,
                    "page_accounting_mismatch",
                    "Manifest page buckets do not sum to pages_total",
                    expected=accounting_values["pages_total"],
                    actual=accounted,
                )
        if routed_review_count and manifest.get("status") == "completed":
            _add(
                errors,
                "run_status_mismatch",
                "A run with review-only routes cannot have completed status",
                expected="partial",
                actual="completed",
            )

    if check_external_sources:
        _check_external_sources(manifest_inputs, route_sources, warnings)

    provenance_entries = loaded.get("provenance")
    quality_entries = loaded.get("quality")
    provenance = _index_pages(provenance_entries, "provenance.jsonl", errors)
    quality = _index_pages(quality_entries, "quality.jsonl", errors)
    _check_provenance_extractor_membership(provenance, manifest_identities, errors)
    counts["extracted_pages"] = len(provenance)
    counts["quality_pages"] = len(quality)
    if set(provenance) != set(quality):
        _add(
            errors,
            "page_identity_mismatch",
            "Provenance and quality page identities differ",
            provenance_only=sorted(set(provenance) - set(quality)),
            quality_only=sorted(set(quality) - set(provenance)),
        )

    raw_references: set[Path] = set()
    for page_id, entry in provenance.items():
        _check_identity(entry, run_id, schema_version, errors, "provenance.jsonl", page_id)
        route_page = route_pages.get(page_id)
        if route_page is None:
            _add(
                errors,
                "unknown_page_reference",
                f"Provenance references unrouted page {page_id}",
                page_id=page_id,
            )
        elif route_page.get("action") in {"review", "skip"}:
            _add(
                errors,
                "route_evidence_mismatch",
                f"Provenance exists for non-extraction route {page_id}",
                page_id=page_id,
            )
        source = entry.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("sha256"), str):
            _add(
                warnings,
                "legacy_evidence_incomplete",
                f"Provenance lacks source identity evidence for {page_id}",
                page_id=page_id,
            )
        else:
            expected_source_hash = (
                route_page.get("source_sha256") if route_page is not None else None
            )
            if isinstance(expected_source_hash, str) and source["sha256"] != expected_source_hash:
                _add(
                    errors,
                    "source_identity_mismatch",
                    f"Provenance source hash differs from the routed source for {page_id}",
                    page_id=page_id,
                )
            if route_page is not None:
                _check_page_number(
                    page_id,
                    source.get("page_number"),
                    route_page["page_number"],
                    errors,
                    "provenance.jsonl",
                )
        result = entry.get("result")
        recorded_route = entry.get("route")
        if isinstance(recorded_route, dict) and route_page is not None:
            expected_route = {
                "type": route_page["type"],
                "action": route_page["action"],
                "route_confidence": route_page["confidence"],
            }
            if recorded_route != expected_route:
                _add(
                    errors,
                    "route_evidence_mismatch",
                    f"Provenance route differs from route-map.yml for {page_id}",
                    page_id=page_id,
                )
        raw_name = result.get("raw_artifact") if isinstance(result, dict) else None
        if not isinstance(raw_name, str):
            _add(
                errors,
                "raw_artifact_reference_missing",
                f"Provenance lacks a raw artifact reference for {page_id}",
                page_id=page_id,
            )
            continue
        assert isinstance(result, dict)
        raw_path = _safe_resolve(root / raw_name)
        if raw_path is None:
            _add(
                errors,
                "raw_artifact_path_invalid",
                f"Raw artifact path cannot be resolved safely for {page_id}",
                page_id=page_id,
                artifact=raw_name,
            )
            continue
        if raw_path.stem != page_id:
            _add(
                errors,
                "page_identity_mismatch",
                f"Raw artifact filename does not match page_id {page_id}",
                page_id=page_id,
                artifact=raw_name,
            )
        raw_dir = paths.get("raw_dir")
        if raw_dir is None or not raw_dir.is_dir() or raw_path.parent != raw_dir:
            _add(
                errors,
                "raw_artifact_path_invalid",
                f"Raw artifact lacks a valid contained raw directory for {page_id}",
                page_id=page_id,
                artifact=raw_name,
            )
            continue
        raw_references.add(raw_path)
        if not raw_path.is_file():
            _add(
                errors,
                "raw_artifact_missing",
                f"Referenced raw artifact is missing for {page_id}",
                page_id=page_id,
                artifact=raw_name,
            )
        else:
            expected_raw_hash = result.get("raw_sha256")
            if expected_raw_hash is None:
                _add(
                    errors,
                    "raw_artifact_hash_missing",
                    f"Provenance lacks a raw artifact hash for {page_id}",
                    page_id=page_id,
                )
            elif (
                not isinstance(expected_raw_hash, str)
                or _sha256(raw_path) != expected_raw_hash
            ):
                _add(
                    errors,
                    "raw_artifact_hash_mismatch",
                    f"Raw artifact SHA-256 differs from provenance for {page_id}",
                    page_id=page_id,
                    artifact=raw_name,
                )

    for page_id, entry in quality.items():
        _check_schema(entry, schema_version, errors, "quality.jsonl", page_id)
        route_page = route_pages.get(page_id)
        if route_page is None:
            _add(
                errors,
                "unknown_page_reference",
                f"Quality references unrouted page {page_id}",
                page_id=page_id,
            )
        else:
            _check_page_number(
                page_id,
                entry.get("page_number"),
                route_page["page_number"],
                errors,
                "quality.jsonl",
            )
        prov = provenance.get(page_id)
        extractor = prov.get("extractor") if isinstance(prov, dict) else None
        prov_adapter = extractor.get("adapter") if isinstance(extractor, dict) else None
        if prov is not None and entry.get("adapter") != prov_adapter:
            _add(
                errors,
                "adapter_identity_mismatch",
                f"Quality and provenance adapters differ for {page_id}",
                page_id=page_id,
            )
    counts["quality_warning_pages"] = sum(
        1 for entry in quality.values() if entry.get("warnings")
    )
    if summary.get("quality_warning_pages") != counts["quality_warning_pages"]:
        _add(
            errors,
            "quality_warning_count_mismatch",
            "Quality warning page count does not match the manifest",
            expected=summary.get("quality_warning_pages"),
            actual=counts["quality_warning_pages"],
        )

    raw_dir = paths.get("raw_dir")
    if raw_dir is not None and raw_dir.is_dir():
        raw_files: set[Path] = set()
        for candidate in raw_dir.rglob("*"):
            if candidate.is_symlink():
                _add(
                    errors,
                    "raw_artifact_path_invalid",
                    "Raw artifact inventory contains a symbolic link",
                    artifact=str(candidate.relative_to(root)),
                )
                continue
            if not candidate.is_file():
                continue
            resolved = _safe_resolve(candidate)
            if resolved is None:
                _add(
                    errors,
                    "raw_artifact_path_invalid",
                    "Raw artifact inventory path cannot be resolved safely",
                    artifact=str(candidate.relative_to(root)),
                )
                continue
            if resolved != raw_dir and raw_dir not in resolved.parents:
                _add(
                    errors,
                    "raw_artifact_path_invalid",
                    "Raw artifact inventory resolves outside the declared raw directory",
                    artifact=str(candidate.relative_to(root)),
                )
                continue
            raw_files.add(resolved)
        counts["raw_artifacts"] = len(raw_files)
        for extra in sorted(raw_files - raw_references):
            _add(
                errors,
                "raw_artifact_unreferenced",
                f"Raw artifact has no provenance entry: {extra.name}",
                artifact=str(extra.relative_to(root)),
            )

    _check_normalized(
        paths.get("normalized_dir"),
        root,
        run_id,
        schema_version,
        summary,
        route_pages,
        provenance,
        errors,
        counts,
    )

    audit = loaded.get("audit")
    if isinstance(audit, dict):
        _check_identity(audit, run_id, schema_version, errors, paths["audit"].name)
        for queue_name in ("review_queue", "quarantine_queue"):
            queue = audit.get(queue_name)
            if not isinstance(queue, list):
                _add(errors, "artifact_structure_invalid", f"Audit {queue_name} must be a list")
                continue
            counts["audit_references"] += len(queue)
            _check_references(queue, route_pages, errors, f"audit.json:{queue_name}")
        quarantine_queue = audit.get("quarantine_queue")
        if isinstance(quarantine_queue, list):
            quarantined = {
                item.get("page_id")
                for item in quarantine_queue
                if isinstance(item, dict) and isinstance(item.get("page_id"), str)
            }
            if summary.get("pages_quarantined") != len(quarantined):
                _add(
                    errors,
                    "quarantine_count_mismatch",
                    "Manifest quarantine count differs from audit.json",
                    expected=summary.get("pages_quarantined"),
                    actual=len(quarantined),
                )
        audit_markdown = loaded.get("audit_md")
        if isinstance(audit_markdown, str):
            try:
                expected_audit_markdown = render_audit_markdown(audit)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                _add(
                    errors,
                    "artifact_structure_invalid",
                    f"audit.json cannot be rendered safely: {exc}",
                    artifact=paths["audit"].name,
                )
            else:
                if audit_markdown != expected_audit_markdown:
                    _add(
                        errors,
                        "audit_render_mismatch",
                        "audit.md is not the current rendering of audit.json",
                        artifact=paths["audit_md"].name,
                    )

    rerun = loaded.get("rerun_manifest")
    if isinstance(rerun, dict):
        _check_schema(rerun, schema_version, errors, paths["rerun_manifest"].name)
        if rerun.get("parent_run_id") != run_id:
            _add(
                errors,
                "run_id_mismatch",
                "Rerun manifest parent_run_id does not match the run manifest",
                artifact=paths["rerun_manifest"].name,
            )
        items = rerun.get("items")
        if not isinstance(items, list):
            _add(errors, "artifact_structure_invalid", "Rerun items must be a list")
        else:
            counts["rerun_references"] = len(items)
            _check_references(items, route_pages, errors, paths["rerun_manifest"].name)
        if (
            isinstance(config, dict)
            and isinstance(route, dict)
            and isinstance(audit, dict)
        ):
            _check_rerun_plan(
                rerun,
                manifest,
                config,
                route,
                audit,
                quality,
                errors,
                warnings,
            )

    cost = loaded.get("cost")
    if isinstance(cost, dict):
        _check_identity(cost, run_id, schema_version, errors, paths["cost"].name)
        extracted = sum(
            entry.get("usage", {}).get("pages", 0)
            for entry in provenance.values()
            if isinstance(entry.get("usage"), dict)
            and isinstance(entry["usage"].get("pages"), int)
        )
        expected_pages = summary.get("pages_extracted")
        cost_pages = cost.get("pages_extracted")
        usage = cost.get("usage")
        usage_pages = usage.get("pages") if isinstance(usage, dict) else None
        if (
            expected_pages != extracted
            or cost_pages != extracted
            or (usage_pages is not None and usage_pages != extracted)
        ):
            _add(
                errors,
                "cost_page_count_mismatch",
                "Cost, provenance, and manifest extraction page totals differ",
                manifest=expected_pages,
                provenance=extracted,
                cost=cost_pages,
                usage=usage_pages,
            )

    log_entries = loaded.get("run_log")
    if isinstance(log_entries, list):
        failed_page_ids = {
            entry.get("page_id")
            for entry in log_entries
            if entry.get("status") in {"failed", "invalid_result"}
            and isinstance(entry.get("page_id"), str)
        }
        if (
            "pages_failed" in summary
            and summary["pages_failed"] != len(failed_page_ids)
        ):
            _add(
                errors,
                "failed_page_count_mismatch",
                "Manifest failed-page count differs from run.log",
                expected=summary["pages_failed"],
                actual=len(failed_page_ids),
            )
        extraction_routes = {
            page_id
            for page_id, page in route_pages.items()
            if page.get("action") not in {"review", "skip"}
        }
        not_attempted = extraction_routes - set(provenance) - failed_page_ids
        if (
            manifest.get("execution_mode") != "dry_run"
            and "pages_not_attempted" in summary
            and summary["pages_not_attempted"] != len(not_attempted)
        ):
            _add(
                errors,
                "not_attempted_count_mismatch",
                "Manifest not-attempted count differs from route and log evidence",
                expected=summary["pages_not_attempted"],
                actual=len(not_attempted),
            )
        for line_number, entry in enumerate(log_entries, start=1):
            _check_identity(
                entry,
                run_id,
                schema_version,
                errors,
                paths["run_log"].name,
                line_number=line_number,
            )

    _check_replay_linkage(
        manifest, declarations, loaded, provenance, manifest_identities, errors
    )

    return _report(root, errors, warnings, counts)


def _check_rerun_plan(
    rerun: dict[str, Any],
    manifest: dict[str, Any],
    config: dict[str, Any],
    route: dict[str, Any],
    audit: dict[str, Any],
    quality: dict[str, dict[str, Any]],
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    """Re-derive the executable queue from parent evidence and compare it."""
    schema_version = manifest.get("schema_version")
    run_id = manifest.get("run_id")
    if not isinstance(schema_version, str) or not isinstance(run_id, str):
        return
    raw_depth = manifest.get("run_depth")
    if isinstance(raw_depth, int) and not isinstance(raw_depth, bool) and raw_depth >= 0:
        run_depth = raw_depth
    elif manifest.get("parent_run_id") is None:
        run_depth = 0
        _add(
            warnings,
            "legacy_evidence_incomplete",
            "Manifest predates durable run_depth recording; inferred generation 0",
            artifact="manifest.json",
        )
    else:
        raw_rerun_depth = rerun.get("rerun_depth")
        if not isinstance(raw_rerun_depth, int) or isinstance(raw_rerun_depth, bool):
            return
        run_depth = raw_rerun_depth
        _add(
            warnings,
            "legacy_evidence_incomplete",
            "Rerun lineage predates durable manifest run_depth recording",
            artifact="manifest.json",
        )

    run_config = config.get("run")
    if not isinstance(run_config, dict):
        run_config = {}
    max_rerun_depth = run_config.get("max_rerun_depth", 2)
    if (
        not isinstance(max_rerun_depth, int)
        or isinstance(max_rerun_depth, bool)
        or max_rerun_depth < 0
    ):
        return

    try:
        configured_order = PageLedgerConfig(
            schema_version=schema_version,
            data=config,
        ).adapter_order
    except ValueError as exc:
        _add(
            errors,
            "config_adapter_order_invalid",
            f"Config snapshot adapter chain is invalid: {exc}",
            artifact="config-snapshot.yml",
        )
        return

    adapter_order = (
        [str(entry["adapter"]) for entry in configured_order]
        if configured_order is not None
        else None
    )
    expected_manifest_escalation = (
        {"adapter_order": adapter_order, "step": run_depth}
        if adapter_order is not None
        else None
    )
    if manifest.get("escalation") != expected_manifest_escalation:
        _add(
            errors,
            "manifest_escalation_mismatch",
            "Manifest adapter escalation does not match config-snapshot.yml",
            artifact="manifest.json",
            expected=expected_manifest_escalation,
            actual=manifest.get("escalation"),
        )

    rerun_escalation: dict[str, Any] | None = None
    if adapter_order is not None:
        rerun_escalation = {
            "adapter_order": adapter_order,
            "step": run_depth,
            "next_adapter": (
                adapter_order[run_depth + 1]
                if run_depth + 1 < len(adapter_order)
                else None
            ),
        }

    try:
        expected = build_rerun_manifest(
            schema_version=schema_version,
            run_id=run_id,
            parent_run_id=run_id,
            created_at=str(rerun.get("created_at", "")),
            max_rerun_depth=max_rerun_depth,
            reason=(
                "dry_run"
                if manifest.get("execution_mode") == "dry_run"
                else "audit_policy"
            ),
            audit=audit,
            route_map=route,
            run_depth=run_depth,
            grades={
                page_id: entry["grade"]
                for page_id, entry in quality.items()
                if isinstance(entry.get("grade"), str)
            },
            escalation=rerun_escalation,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        # Malformed audit/route evidence is already reported by the structural
        # and reference checks; re-derivation must not turn it into a crash.
        return
    fields = (
        "schema_version",
        "run_id",
        "parent_run_id",
        "parent_manifest",
        "rerun_depth",
        "max_rerun_depth",
        "reason",
        "rerun_executable",
        "rerun_status",
        "items",
        "escalation",
    )
    differing = [field for field in fields if rerun.get(field) != expected.get(field)]
    if differing:
        _add(
            errors,
            "rerun_plan_mismatch",
            "Rerun manifest is not the queue derived from audit, route, config, and grade evidence",
            artifact="rerun-manifest.yml",
            differing_fields=differing,
        )


def render_verification(report: dict[str, Any]) -> str:
    """Render a compact verifier result for the CLI."""
    lines = [
        f"Run verification: {report['status'].upper()}",
        f"Run directory: {report['run_dir']}",
        f"Errors: {len(report['errors'])}  Warnings: {len(report['warnings'])}",
    ]
    for heading, key in (("Errors", "errors"), ("Warnings", "warnings")):
        if report[key]:
            lines.extend(["", f"{heading}:"])
            lines.extend(f"- [{issue['code']}] {issue['message']}" for issue in report[key])
    return "\n".join(lines) + "\n"


def _load_artifact(key: str, path: Path) -> Any:
    if key in {"raw_dir", "normalized_dir"}:
        return path
    if key == "audit_md":
        return path.read_text(encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    if key in {"config_snapshot", "route_map", "rerun_manifest"}:
        value = yaml.safe_load(text)
        if not isinstance(value, dict):
            raise ValueError("top level must be a mapping")
        return value
    if key in {"provenance", "quality", "run_log"}:
        entries = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise ValueError(f"line {line_number} must be a mapping")
            entries.append(entry)
        return entries
    if key in {"audit", "cost", "replay"}:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("top level must be a mapping")
        return value
    return text


def _check_replay_linkage(
    manifest: dict[str, Any],
    declarations: dict[str, Any],
    loaded: dict[str, Any],
    provenance: dict[str, dict[str, Any]],
    manifest_identities: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    """Check optional replay evidence and its additive manifest linkage."""
    linkage_keys = {
        "replay_schema_version",
        "baseline_run_id",
        "bundle_manifest_sha256",
        "outcome",
    }
    has_linkage = any(key in manifest for key in linkage_keys)
    has_artifact = "replay" in declarations
    if not has_linkage and not has_artifact:
        return
    present_linkage = linkage_keys & manifest.keys()
    if has_linkage and present_linkage != linkage_keys:
        _add(
            errors,
            "replay_artifact_malformed",
            "Replay linkage must declare all replay metadata fields",
            artifact="manifest.json",
        )
        return
    if not has_artifact:
        _add(
            errors,
            "replay_artifact_missing",
            "Replay linkage exists but manifest does not declare replay.json",
            artifact="manifest.json",
        )
        return
    if not has_linkage:
        _add(
            errors,
            "replay_artifact_missing",
            "Manifest declares replay.json without replay linkage",
            artifact="manifest.json",
        )
        return
    replay = loaded.get("replay")
    if not isinstance(replay, dict):
        # The loader reports malformed/missing replay artifacts. Keep this
        # helper fail-closed when called with an incomplete loaded mapping.
        if "replay" not in loaded:
            return
        _add(
            errors,
            "replay_artifact_malformed",
            "Replay artifact must be a mapping",
            artifact="replay.json",
        )
        return

    required = {
        "replay_schema_version",
        "bundle_manifest_sha256",
        "baseline_run_id",
        "replay_run_id",
        "baseline_extractor",
        "local_extractor",
        "profile_match",
        "outcome",
        "raw",
        "comparison",
    }
    if not required <= replay.keys():
        _add(
            errors,
            "replay_artifact_malformed",
            "Replay artifact is missing required fields",
            artifact="replay.json",
        )
        return

    if replay.get("replay_schema_version") != "0.1":
        _add(
            errors,
            "replay_artifact_malformed",
            "Replay schema version must be 0.1",
            artifact="replay.json",
        )
    if not isinstance(replay.get("baseline_run_id"), str) or not replay["baseline_run_id"]:
        _add(errors, "replay_artifact_malformed", "Replay baseline_run_id is invalid", artifact="replay.json")
    if not isinstance(replay.get("replay_run_id"), str) or not replay["replay_run_id"]:
        _add(errors, "replay_artifact_malformed", "Replay replay_run_id is invalid", artifact="replay.json")
    if not _is_sha256(replay.get("bundle_manifest_sha256")):
        _add(errors, "replay_artifact_malformed", "Replay bundle manifest hash is invalid", artifact="replay.json")
    if replay.get("outcome") not in {"exact", "evidence_compared", "deterministic_mismatch"}:
        _add(errors, "replay_artifact_malformed", "Replay outcome is invalid", artifact="replay.json")
    if replay.get("profile_match") is not None and not isinstance(replay.get("profile_match"), bool):
        _add(errors, "replay_artifact_malformed", "Replay profile_match is invalid", artifact="replay.json")

    if (
        replay.get("replay_schema_version") != manifest.get("replay_schema_version")
        or replay.get("baseline_run_id") != manifest.get("baseline_run_id")
        or replay.get("bundle_manifest_sha256") != manifest.get("bundle_manifest_sha256")
        or replay.get("outcome") != manifest.get("outcome")
        or replay.get("replay_run_id") != manifest.get("run_id")
    ):
        _add(
            errors,
            "replay_linkage_mismatch",
            "Replay artifact does not agree with manifest replay linkage",
            artifact="replay.json",
        )

    raw = replay.get("raw")
    comparison = replay.get("comparison")
    baseline_extractor = replay.get("baseline_extractor")
    local_extractor = replay.get("local_extractor")
    if not isinstance(raw, dict) or not isinstance(comparison, dict):
        _add(errors, "replay_artifact_malformed", "Replay raw/comparison evidence is malformed", artifact="replay.json")
        return
    if not isinstance(baseline_extractor, dict) or not isinstance(local_extractor, dict):
        _add(errors, "replay_artifact_malformed", "Replay extractor evidence is malformed", artifact="replay.json")
        return
    baseline_identity = _validate_replay_extractor_identity(
        baseline_extractor, "baseline_extractor", errors
    )
    local_identity = _validate_replay_extractor_identity(
        local_extractor, "local_extractor", errors
    )
    expected_local = _manifest_replay_extractor_identity(manifest_identities, errors)
    extractor_evidence_valid = baseline_identity is not None and local_identity is not None
    if baseline_identity is not None and local_identity is not None:
        base_keys = (
            "adapter",
            "version",
            "deterministic",
            "input_types",
            "output_types",
            "capabilities",
            "options",
        )
        if any(baseline_identity[key] != local_identity[key] for key in base_keys):
            _add(
                errors,
                "replay_linkage_mismatch",
                "Replay baseline and local extractor identities differ",
                artifact="replay.json",
            )
        if expected_local is not None and local_identity != expected_local:
            _add(
                errors,
                "replay_linkage_mismatch",
                "Replay local extractor does not match the run manifest",
                artifact="replay.json",
            )

    counts: dict[str, int] = {}
    for name in ("equal", "different", "missing"):
        value = raw.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _add(errors, "replay_artifact_malformed", f"Replay raw.{name} must be a nonnegative integer", artifact="replay.json")
        else:
            counts[name] = value
    page_lists: dict[str, list[str]] = {}
    for name in ("different_page_ids", "missing_page_ids"):
        values = raw.get(name)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))
        ):
            _add(errors, "replay_artifact_malformed", f"Replay raw.{name} must be unique page-id strings", artifact="replay.json")
        else:
            page_lists[name] = values
            count_name = "different" if name.startswith("different") else "missing"
            if count_name in counts and len(values) != counts[count_name]:
                _add(errors, "replay_linkage_mismatch", f"Replay raw.{name} length does not match its count", artifact="replay.json")

    run_a = comparison.get("run_a")
    run_b = comparison.get("run_b")
    if not isinstance(run_a, dict) or not isinstance(run_b, dict):
        _add(errors, "replay_artifact_malformed", "Replay comparison run summaries are malformed", artifact="replay.json")
        return
    baseline_id = replay.get("baseline_run_id")
    replay_id = replay.get("replay_run_id")
    if run_a.get("run_id") != baseline_id or run_b.get("run_id") != replay_id:
        _add(errors, "replay_linkage_mismatch", "Replay comparison run IDs do not match replay IDs", artifact="replay.json")

    pages_only_a = comparison.get("pages_only_in_a", [])
    pages_only_b = comparison.get("pages_only_in_b", [])
    if (
        not isinstance(pages_only_a, list)
        or not isinstance(pages_only_b, list)
        or any(not isinstance(value, str) or not value for value in pages_only_a + pages_only_b)
        or len(pages_only_a) != len(set(pages_only_a))
        or len(pages_only_b) != len(set(pages_only_b))
    ):
        _add(errors, "replay_artifact_malformed", "Replay comparison page sets are malformed", artifact="replay.json")
        pages_only_a = []
        pages_only_b = []
    pages = comparison.get("pages", [])
    if not isinstance(pages, list):
        _add(errors, "replay_artifact_malformed", "Replay comparison pages must be a list", artifact="replay.json")
        pages = []
    comparison_different: list[str] = []
    comparison_missing: list[str] = list(pages_only_a) + list(pages_only_b)
    comparison_equal = 0
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("page_id"), str) or not page["page_id"]:
            _add(errors, "replay_artifact_malformed", "Replay comparison page is malformed", artifact="replay.json")
            continue
        page_id = page["page_id"]
        raw_a = page.get("raw_sha256_a")
        raw_b = page.get("raw_sha256_b")
        raw_a_valid = raw_a is None or _is_sha256(raw_a)
        raw_b_valid = raw_b is None or _is_sha256(raw_b)
        if not raw_a_valid:
            _add(
                errors,
                "replay_artifact_malformed",
                "Replay comparison raw_sha256_a must be a SHA-256 string or null",
                artifact="replay.json",
            )
        if not raw_b_valid:
            _add(
                errors,
                "replay_artifact_malformed",
                "Replay comparison raw_sha256_b must be a SHA-256 string or null",
                artifact="replay.json",
            )
        expected_raw_equal = raw_a == raw_b if _is_sha256(raw_a) and _is_sha256(raw_b) else None
        raw_equal = page.get("raw_equal")
        if raw_equal is not expected_raw_equal:
            _add(
                errors,
                "replay_linkage_mismatch",
                "Replay comparison raw_equal contradicts raw SHA-256 evidence",
                artifact="replay.json",
                page_id=page_id,
            )
        current_entry = provenance.get(page_id)
        current_result = current_entry.get("result") if isinstance(current_entry, dict) else None
        current_raw = current_result.get("raw_sha256") if isinstance(current_result, dict) else None
        if raw_b != current_raw:
            _add(
                errors,
                "replay_linkage_mismatch",
                "Replay raw_sha256_b differs from current provenance",
                artifact="replay.json",
                page_id=page_id,
            )
        if raw_equal is True:
            comparison_equal += 1
        elif raw_equal is False:
            comparison_different.append(page_id)
        elif raw_equal is None:
            comparison_missing.append(page_id)
        else:
            _add(errors, "replay_artifact_malformed", "Replay comparison raw_equal is invalid", artifact="replay.json")
    common_page_ids = [
        page["page_id"]
        for page in pages
        if isinstance(page, dict) and isinstance(page.get("page_id"), str) and page["page_id"]
    ]
    common_page_set = set(common_page_ids)
    if len(common_page_ids) != len(common_page_set):
        _add(errors, "replay_linkage_mismatch", "Replay comparison contains duplicate common page IDs", artifact="replay.json")
    if set(pages_only_a) & set(pages_only_b) or (
        common_page_set & (set(pages_only_a) | set(pages_only_b))
    ):
        _add(errors, "replay_linkage_mismatch", "Replay comparison page identity sets overlap", artifact="replay.json")
    expected_missing = sorted(set(comparison_missing))
    if (
        counts.get("equal") != comparison_equal
        or counts.get("different") != len(comparison_different)
        or counts.get("missing") != len(expected_missing)
        or set(page_lists.get("different_page_ids", [])) != set(comparison_different)
        or set(page_lists.get("missing_page_ids", [])) != set(expected_missing)
    ):
        _add(errors, "replay_linkage_mismatch", "Replay raw evidence does not match comparison evidence", artifact="replay.json")

    if not extractor_evidence_valid:
        return
    assert baseline_identity is not None
    deterministic = baseline_identity["deterministic"]
    capabilities = cast(list[str], baseline_identity["capabilities"])
    cloud = any(value.casefold() == "cloud" for value in capabilities)
    if deterministic and not cloud:
        if (
            baseline_identity["reproducibility_profile_sha256"] is None
            or local_identity is None
            or local_identity["reproducibility_profile_sha256"] is None
            or baseline_identity["reproducibility_profile_sha256"]
            != local_identity["reproducibility_profile_sha256"]
            or replay.get("profile_match") is not True
        ):
            _add(
                errors,
                "replay_linkage_mismatch",
                "Deterministic replay profile evidence does not agree",
                artifact="replay.json",
            )
    elif replay.get("profile_match") is not None:
        _add(
            errors,
            "replay_linkage_mismatch",
            "Nondeterministic or cloud replay must not claim a profile match",
            artifact="replay.json",
        )
    outcome = replay.get("outcome")
    if outcome == "exact" and (
        not deterministic
        or cloud
        or replay.get("profile_match") is not True
        or counts.get("different") != 0
        or counts.get("missing") != 0
        or pages_only_a
        or pages_only_b
    ):
        _add(errors, "replay_linkage_mismatch", "Exact replay invariants are not satisfied", artifact="replay.json")
    elif outcome == "evidence_compared" and deterministic and not cloud:
        _add(errors, "replay_linkage_mismatch", "Evidence-compared outcome requires a nondeterministic or cloud extractor", artifact="replay.json")
    elif outcome == "deterministic_mismatch" and (
        not deterministic
        or cloud
        or (counts.get("different", 0) == 0 and counts.get("missing", 0) == 0 and not pages_only_a and not pages_only_b)
    ):
        _add(errors, "replay_linkage_mismatch", "Deterministic mismatch outcome has no differing or missing pages", artifact="replay.json")


def _validate_replay_extractor_identity(
    identity: dict[str, Any], label: str, errors: list[dict[str, Any]]
) -> dict[str, Any] | None:
    expected = {
        "adapter",
        "version",
        "deterministic",
        "input_types",
        "output_types",
        "capabilities",
        "options",
        "reproducibility_profile_sha256",
    }
    if set(identity) != expected:
        _add(errors, "replay_artifact_malformed", f"Replay {label} has invalid fields", artifact="replay.json")
        return None
    if (
        not isinstance(identity["adapter"], str)
        or not identity["adapter"]
        or not isinstance(identity["version"], str)
        or not identity["version"]
        or not isinstance(identity["deterministic"], bool)
    ):
        _add(errors, "replay_artifact_malformed", f"Replay {label} has invalid scalar identity fields", artifact="replay.json")
        return None
    for field in ("input_types", "output_types", "capabilities"):
        values = identity[field]
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            _add(errors, "replay_artifact_malformed", f"Replay {label}.{field} must be a list of strings", artifact="replay.json")
            return None
        if values != sorted(set(values)):
            _add(errors, "replay_artifact_malformed", f"Replay {label}.{field} must be sorted and unique", artifact="replay.json")
            return None
    options = identity["options"]
    if not isinstance(options, dict) or any(not isinstance(key, str) for key in options):
        _add(errors, "replay_artifact_malformed", f"Replay {label}.options must be a mapping", artifact="replay.json")
        return None
    try:
        json.dumps(options, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        _add(errors, "replay_artifact_malformed", f"Replay {label}.options must contain finite JSON: {exc}", artifact="replay.json")
        return None
    profile_hash = identity["reproducibility_profile_sha256"]
    if profile_hash is not None and not _is_sha256(profile_hash):
        _add(errors, "replay_artifact_malformed", f"Replay {label} profile hash is invalid", artifact="replay.json")
        return None
    return identity


def _manifest_extractor_identities(
    manifest: dict[str, Any], errors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    entries = manifest.get("extractors")
    if not isinstance(entries, list) or not entries:
        _add(errors, "extractor_identity_mismatch", "Run manifest has no extractor entries")
        return []
    identities: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            _add(errors, "extractor_identity_mismatch", "Run manifest extractor is malformed")
            continue
        if (
            not isinstance(entry.get("adapter"), str)
            or not entry["adapter"]
            or not isinstance(entry.get("version"), str)
            or not entry["version"]
        ):
            _add(
                errors,
                "extractor_identity_mismatch",
                "Run manifest extractor adapter and version are invalid",
                artifact="manifest.json",
            )
            continue
        profile = entry.get("reproducibility_profile")
        profile_hash: str | None = None
        if profile is not None:
            try:
                validated_profile = validate_reproducibility_profile(profile, entry)
            except ReplayError as exc:
                _add(errors, exc.code, str(exc), artifact="manifest.json")
                continue
            profile_hash = validated_profile["profile_sha256"]
        candidate = {
            "adapter": entry.get("adapter"),
            "version": entry.get("version"),
            "deterministic": entry.get("deterministic"),
            "input_types": entry.get("input_types"),
            "output_types": entry.get("output_types"),
            "capabilities": entry.get("capabilities"),
            "options": entry.get("options", {}),
            "reproducibility_profile_sha256": profile_hash,
        }
        validated = _validate_replay_extractor_identity(candidate, "manifest extractor", errors)
        if validated is not None:
            identities.append(validated)
    return identities


def _manifest_replay_extractor_identity(
    identities: list[dict[str, Any]], errors: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not identities:
        return None
    first = identities[0]
    if any(identity != first for identity in identities[1:]):
        _add(
            errors,
            "replay_linkage_mismatch",
            "Run manifest extractor entries disagree",
            artifact="manifest.json",
        )
    return first


def _canonical_extractor_core(
    extractor: object, *, provenance: bool
) -> tuple | None:
    if not isinstance(extractor, dict):
        return None
    version_key = "adapter_version" if provenance else "version"
    adapter = extractor.get("adapter")
    version = extractor.get(version_key)
    deterministic = extractor.get("deterministic")
    if (
        not isinstance(adapter, str)
        or not adapter
        or not isinstance(version, str)
        or not version
        or not isinstance(deterministic, bool)
    ):
        return None
    lists: list[tuple] = []
    for field in ("input_types", "output_types", "capabilities"):
        values = extractor.get(field)
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
            return None
        lists.append(tuple(sorted(values)))
    return (adapter, version, deterministic, *lists)


def _check_provenance_extractor_membership(
    provenance: dict[str, dict[str, Any]],
    manifest_identities: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    manifest_cores = {
        _canonical_extractor_core(identity, provenance=False)
        for identity in manifest_identities
    }
    for page_id, entry in provenance.items():
        extractor = entry.get("extractor")
        core = _canonical_extractor_core(extractor, provenance=True)
        if core is None or core not in manifest_cores:
            _add(
                errors,
                "extractor_identity_mismatch",
                f"Provenance extractor is not declared by the manifest for {page_id}",
                artifact="provenance.jsonl",
                page_id=page_id,
            )


def _index_pages(
    entries: Any, artifact: str, errors: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    if not isinstance(entries, list):
        return indexed
    for entry in entries:
        page_id = entry.get("page_id") if isinstance(entry, dict) else None
        if not isinstance(page_id, str) or not page_id:
            _add(errors, "page_identity_missing", f"{artifact} entry lacks page_id")
        elif page_id in indexed:
            _add(
                errors,
                "duplicate_page_id",
                f"{artifact} contains duplicate page_id {page_id}",
                artifact=artifact,
                page_id=page_id,
            )
        else:
            indexed[page_id] = entry
    return indexed


def _check_normalized(
    normalized_dir: Path | None,
    root: Path,
    run_id: Any,
    schema_version: Any,
    summary: dict[str, Any],
    route_pages: dict[str, dict[str, Any]],
    provenance: dict[str, dict[str, Any]],
    errors: list[dict[str, Any]],
    counts: dict[str, int],
) -> None:
    if normalized_dir is None or not normalized_dir.is_dir():
        return
    for declared_path in sorted(normalized_dir.rglob("*")):
        if declared_path.is_symlink():
            _add(
                errors,
                "normalized_artifact_path_invalid",
                "Normalized artifact must not be a symbolic link",
                artifact=str(declared_path.relative_to(root)),
            )
            continue
        if not declared_path.is_file():
            continue
        path = _safe_resolve(declared_path)
        if path is None or (path != normalized_dir and normalized_dir not in path.parents):
            _add(
                errors,
                "normalized_artifact_path_invalid",
                "Normalized artifact resolves outside the declared directory",
                artifact=str(declared_path.relative_to(root)),
            )
            continue
        counts["normalized_pages"] += 1
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entry, dict):
                raise ValueError("top level must be a mapping")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            _add(
                errors,
                "artifact_malformed",
                f"Normalized artifact cannot be parsed: {exc}",
                artifact=str(path.relative_to(root)),
            )
            continue
        page_id = entry.get("page_id")
        _check_identity(
            entry,
            run_id,
            schema_version,
            errors,
            str(path.relative_to(root)),
            page_id,
        )
        if page_id not in route_pages or page_id not in provenance:
            _add(
                errors,
                "unknown_page_reference",
                f"Normalized artifact references unknown page {page_id}",
                page_id=page_id,
            )
        if path.stem != page_id:
            _add(
                errors,
                "page_identity_mismatch",
                f"Normalized filename does not match page_id {page_id}",
                page_id=page_id,
                artifact=str(path.relative_to(root)),
            )
        records = entry.get("records")
        if not isinstance(records, list):
            _add(
                errors,
                "artifact_structure_invalid",
                f"Normalized records must be a list for {page_id}",
                page_id=page_id,
            )
        else:
            counts["normalized_records"] += len(records)
        prov = provenance.get(page_id) if isinstance(page_id, str) else None
        result = prov.get("result") if isinstance(prov, dict) else None
        raw_artifact = result.get("raw_artifact") if isinstance(result, dict) else None
        if prov is not None and entry.get("raw_artifact") != raw_artifact:
            _add(
                errors,
                "raw_artifact_reference_mismatch",
                f"Normalized and provenance raw artifacts differ for {page_id}",
                page_id=page_id,
            )
    if summary.get("records_normalized") != counts["normalized_records"]:
        _add(
            errors,
            "normalized_record_count_mismatch",
            "Normalized record total does not match the manifest",
            expected=summary.get("records_normalized"),
            actual=counts["normalized_records"],
        )


def _check_references(
    entries: list[Any],
    route_pages: dict[str, dict[str, Any]],
    errors: list[dict[str, Any]],
    artifact: str,
) -> None:
    for entry in entries:
        if not isinstance(entry, dict):
            _add(
                errors,
                "artifact_structure_invalid",
                f"{artifact} queue item must be a mapping",
                artifact=artifact,
            )
            continue
        page_id = entry.get("page_id")
        route_page = route_pages.get(page_id) if isinstance(page_id, str) else None
        if route_page is None:
            _add(
                errors,
                "unknown_page_reference",
                f"{artifact} references unrouted page {page_id}",
                artifact=artifact,
                page_id=page_id,
            )
            continue
        _check_page_number(
            page_id,
            entry.get("page_number"),
            route_page["page_number"],
            errors,
            artifact,
        )
        if "source" in entry and entry.get("source") != route_page["source"]:
            _add(
                errors,
                "source_identity_mismatch",
                f"{artifact} source differs from the route for {page_id}",
                artifact=artifact,
                page_id=page_id,
            )


def _check_page_number(
    page_id: Any,
    actual: Any,
    expected: Any,
    errors: list[dict[str, Any]],
    artifact: str,
) -> None:
    if actual != expected:
        _add(
            errors,
            "page_number_mismatch",
            f"{artifact} page number differs from the route for {page_id}",
            artifact=artifact,
            page_id=page_id,
            expected=expected,
            actual=actual,
        )


def _check_identity(
    entry: Any,
    run_id: Any,
    schema_version: Any,
    errors: list[dict[str, Any]],
    artifact: str,
    page_id: Any = None,
    **details: Any,
) -> None:
    if not isinstance(entry, dict):
        _add(errors, "artifact_structure_invalid", f"{artifact} entry must be a mapping")
        return
    _check_schema(entry, schema_version, errors, artifact, page_id)
    if entry.get("run_id") != run_id:
        _add(
            errors,
            "run_id_mismatch",
            f"{artifact} run_id does not match the manifest",
            artifact=artifact,
            page_id=page_id,
            **details,
        )


def _check_schema(
    entry: dict[str, Any],
    schema_version: Any,
    errors: list[dict[str, Any]],
    artifact: str,
    page_id: Any = None,
) -> None:
    if entry.get("schema_version") != schema_version:
        _add(
            errors,
            "schema_version_mismatch",
            f"{artifact} schema_version does not match the manifest",
            artifact=artifact,
            page_id=page_id,
        )


def _check_external_sources(
    inputs: Any, route_sources: list[Any], warnings: list[dict[str, Any]]
) -> None:
    if not isinstance(inputs, list):
        return
    for index, entry in enumerate(inputs):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            _add(
                warnings,
                "legacy_evidence_incomplete",
                "Manifest input lacks a source path",
            )
            continue
        route_source = route_sources[index] if index < len(route_sources) else None
        source_path = route_source if isinstance(route_source, str) else entry["path"]
        path = Path(source_path).expanduser()
        if not path.is_file():
            _add(
                warnings,
                "source_missing",
                f"External source file is missing: {path}",
                source=str(path),
            )
            continue
        expected = entry.get("sha256")
        if not isinstance(expected, str):
            _add(
                warnings,
                "legacy_evidence_incomplete",
                f"Manifest input lacks a source hash: {path}",
                source=str(path),
            )
        elif _sha256(path) != expected:
            _add(
                warnings,
                "source_changed",
                f"External source file no longer matches the manifest: {path}",
                source=str(path),
            )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _safe_resolve(path: Path) -> Path | None:
    """Resolve safely across Python versions, retaining only missing tail parts."""
    unresolved: list[str] = []
    candidate = path
    while True:
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            try:
                if candidate.is_symlink():
                    return None
            except (OSError, ValueError):
                return None
            parent = candidate.parent
            if parent == candidate:
                return None
            unresolved.append(candidate.name)
            candidate = parent
        except (OSError, RuntimeError, ValueError):
            return None
        else:
            return resolved.joinpath(*reversed(unresolved))


def _add(issues: list[dict[str, Any]], code: str, message: str, **details: Any) -> None:
    issue = {"code": code, "message": message}
    issue.update({key: value for key, value in details.items() if value is not None})
    issues.append(issue)


def _report(
    root: Path,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    counts: dict[str, int],
) -> dict[str, Any]:
    return {
        "status": "fail" if errors else "pass",
        "run_dir": str(root),
        "errors": errors,
        "warnings": warnings,
        "counts": counts,
    }
