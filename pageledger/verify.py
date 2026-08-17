"""Relational integrity verification for PageLedger run directories.

This checks agreement between artifacts. It deliberately does not claim to
validate extraction accuracy or replace the JSON Schemas in ``schemas/``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .artifacts import build_rerun_manifest, render_audit_markdown
from .config import PageLedgerConfig

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


def verify_run(run_dir: Path) -> dict[str, Any]:
    """Return a structured coherence report for a run directory."""
    root = run_dir.expanduser().resolve()
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
    manifest_path = root / "manifest.json"
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
        path = (root / relative).resolve()
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
                "artifact_missing",
                f"Manifest-declared artifact is missing: {relative}",
                artifact=relative,
            )
            continue
        try:
            loaded[key] = _load_artifact(key, path)
        except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError, ValueError) as exc:
            _add(
                errors,
                "artifact_malformed",
                f"Artifact cannot be parsed: {exc}",
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
                    alignment_snapshot = root / "align-schema-snapshot.yml"
                    if not alignment_snapshot.is_file():
                        _add(
                            errors,
                            "alignment_schema_snapshot_missing",
                            "External alignment schema snapshot is missing",
                            artifact=alignment_snapshot.name,
                        )
                    else:
                        actual_schema_hash = _sha256(alignment_snapshot)
                        if actual_schema_hash != expected_schema_hash:
                            _add(
                                errors,
                                "alignment_schema_hash_mismatch",
                                "Alignment schema hash differs from align-schema-snapshot.yml",
                                artifact=alignment_snapshot.name,
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

    _check_external_sources(manifest_inputs, route_sources, warnings)

    provenance_entries = loaded.get("provenance")
    quality_entries = loaded.get("quality")
    provenance = _index_pages(provenance_entries, "provenance.jsonl", errors)
    quality = _index_pages(quality_entries, "quality.jsonl", errors)
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
        raw_path = (root / raw_name).resolve()
        if raw_path.stem != page_id:
            _add(
                errors,
                "page_identity_mismatch",
                f"Raw artifact filename does not match page_id {page_id}",
                page_id=page_id,
                artifact=raw_name,
            )
        raw_dir = paths.get("raw_dir")
        if raw_dir is not None and raw_path.parent != raw_dir:
            _add(
                errors,
                "raw_artifact_path_invalid",
                f"Raw artifact is outside the declared raw directory for {page_id}",
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
                    warnings,
                    "legacy_evidence_incomplete",
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
        raw_files = {path.resolve() for path in raw_dir.rglob("*") if path.is_file()}
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
            except (KeyError, TypeError, ValueError) as exc:
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
    except (KeyError, TypeError, ValueError):
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
    if key in {"audit", "cost"}:
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("top level must be a mapping")
        return value
    return text


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
    for path in sorted(item for item in normalized_dir.rglob("*") if item.is_file()):
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
        page_id = entry.get("page_id") if isinstance(entry, dict) else None
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
