"""Filesystem-native alpha runner for PageLedger."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from shutil import copyfile
from typing import Any, cast

import yaml

from . import budget as _budget
from . import quality as _quality
from . import reports as _reports
from .adapters import (
    PDF_ADAPTER_NAMES,
    PDF_ONLY_ADAPTER_NAMES,
    adapter_page_count,
    load_adapter,
    ocr_pdf_page_count,
    paginate,
    pdf_page_count,
)
from .aligner import ALIGNABLE_FORMATS, align_page, load_schema_spec
from .artifacts import (
    build_audit,
    build_manifest,
    build_rerun_manifest,
    build_route_map,
    read_jsonl,
    render_audit_markdown,
    write_json,
    write_jsonl,
    write_yaml,
)
from .config import load_config
from .grading import grade_page
from .policy import rebuild_policy_queues
from .routing import load_route_map

LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


# Compatibility aliases: these helpers lived in runner.py before the
# behavior-preserving module split.
BudgetExceededError = _budget.BudgetExceededError
_budget_caps = _budget._budget_caps
_budget_error = _budget._budget_error
_budget_report = _budget._budget_report
_budget_warning = _budget._budget_warning
_new_budget_alerts = _budget._new_budget_alerts
_build_cost_report = _budget._build_cost_report
_cost_basis = _budget._cost_basis
_derive_cost = _budget._derive_cost
_preflight_budget_error = _budget._preflight_budget_error
_round_cost = _budget._round_cost
_sum_usage_field = _budget._sum_usage_field
_usage_rollup = _budget._usage_rollup
_build_quality_entry = _quality._build_quality_entry
_embedded_text_quality = _quality._embedded_text_quality
_has_low_confidence_tail = _quality._has_low_confidence_tail
_is_suspicious_symbol = _quality._is_suspicious_symbol
_output_integrity = _quality._output_integrity
_quality_text = _quality._quality_text
_text_quality_metrics = _quality._text_quality_metrics
_text_quality_warnings = _quality._text_quality_warnings
inspect_run = _reports.inspect_run
run_pages_csv = _reports.run_pages_csv


class AdapterExecutionError(RuntimeError):
    """Serializable adapter failure envelope for callers and run logs."""

    def __init__(
        self,
        *,
        adapter: str,
        page_id: str,
        status: str,
        message: str,
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(f"Adapter '{adapter}' {status} for {page_id}: {message}")
        self.adapter = adapter
        self.page_id = page_id
        self.status = status
        self.message = message
        self.stdout = _snippet(stdout)
        self.stderr = _snippet(stderr)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "page_id": self.page_id,
            "status": self.status,
            "message": self.message,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def _make_log_entry(
    *,
    schema_version: str,
    run_id: str,
    page_id: str,
    adapter_name: str,
    level: str,
    status: str,
    error: dict[str, Any],
    attempt: int,
    max_retries: int,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "timestamp": _utc_now(),
        "level": level,
        "run_id": run_id,
        "page_id": page_id,
        "adapter": adapter_name,
        "status": status,
        "error": error,
        "attempt": attempt,
        "max_retries": max_retries,
    }


def _extract_adapter_page(
    *,
    adapter: Any,
    source: Path,
    page: dict[str, Any],
    prompt: str | None,
    config: Any,
    run_id: str,
    log_entries: list[dict[str, Any]],
) -> tuple[Any | None, float | None, str, int, AdapterExecutionError | None]:
    """Extract and validate one page, preserving retry evidence."""
    page_id = page["page_id"]
    extraction_started_at = _utc_now()
    attempt = 1
    result: Any | None = None
    extraction_seconds: float | None = None
    for attempt in range(1, config.max_retries + 2):
        extraction_started_at = _utc_now()
        attempt_started = time.perf_counter()
        try:
            result = adapter.extract(
                source,
                page_id=page_id,
                page_number=page["page_number"],
                action=page["action"],
                prompt=prompt,
            )
            extraction_seconds = round(time.perf_counter() - attempt_started, 3)
            break
        except Exception as exc:
            final_attempt = attempt > config.max_retries
            adapter_error = AdapterExecutionError(
                adapter=adapter.name,
                page_id=page_id,
                status="failed" if final_attempt else "retry",
                message=f"{type(exc).__name__}: {exc}",
                stdout=getattr(exc, "stdout", None),
                stderr=getattr(exc, "stderr", None),
            )
            log_entries.append(
                _make_log_entry(
                    schema_version=config.schema_version,
                    run_id=run_id,
                    page_id=page_id,
                    adapter_name=adapter.name,
                    level="ERROR" if final_attempt else "WARNING",
                    status="failed" if final_attempt else "retry",
                    error=adapter_error.to_dict(),
                    attempt=attempt,
                    max_retries=config.max_retries,
                )
            )
            if final_attempt:
                return None, None, extraction_started_at, attempt, adapter_error
            delay = _backoff_seconds(config.retry_backoff, attempt)
            if delay > 0:
                time.sleep(delay)

    try:
        _validate_extraction_result(adapter.name, result)
    except Exception as exc:
        adapter_error = AdapterExecutionError(
            adapter=adapter.name,
            page_id=page_id,
            status="invalid_result",
            message=f"{type(exc).__name__}: {exc}",
            stdout=getattr(exc, "stdout", None),
            stderr=getattr(exc, "stderr", None),
        )
        log_entries.append(
            _make_log_entry(
                schema_version=config.schema_version,
                run_id=run_id,
                page_id=page_id,
                adapter_name=adapter.name,
                level="ERROR",
                status="failed",
                error=adapter_error.to_dict(),
                attempt=attempt,
                max_retries=config.max_retries,
            )
        )
        return None, None, extraction_started_at, attempt, adapter_error

    return result, extraction_seconds, extraction_started_at, attempt, None


def run(
    *,
    inputs: list[Path],
    config_path: Path,
    out_dir: Path,
    dry_run: bool,
    log_level: str = "INFO",
    pages: str | None = None,
    page_selection: list[dict[str, Any]] | None = None,
    parent_run_id: str | None = None,
    parent_quality_by_page: dict[str, dict[str, Any]] | None = None,
    run_depth: int = 0,
    adapter_path: Path | None = None,
    routes_path: Path | None = None,
) -> dict[str, Any]:
    """Run the alpha PageLedger loop.

    ``pages`` limits a single-input run to the listed source pages
    (e.g. ``"1-8,81,100-110"``). Page ids keep the source numbering, so the
    ledger stays truthful about which physical pages were extracted —
    unlike splitting the PDF beforehand, which renumbers them.

    When ``page_selection`` is given (by ``rerun``), only the listed
    ``(source, page_number)`` pairs are planned and extracted, keeping their
    original ``page_id`` values for cross-run traceability. ``parent_run_id``
    and ``run_depth`` record rerun lineage in the manifest and the next
    rerun manifest. ``adapter_path`` is a directory prepended to ``sys.path``
    so custom adapters can be loaded without setting PYTHONPATH.
    """

    log_level = _normalize_log_level(log_level)
    execution_mode = "dry_run" if dry_run else "execute"
    _apply_adapter_path(adapter_path)
    # Construct adapters once in the execution path. Direct callers of
    # load_config retain its validation behavior.
    config = load_config(config_path, validate_adapter=False)
    effective_adapter_name, effective_adapter_options = _effective_adapter(
        config, run_depth
    )
    adapter_order = config.adapter_order
    adapter_order_names = (
        [str(entry["adapter"]) for entry in adapter_order]
        if adapter_order is not None
        else None
    )
    escalation: dict[str, Any] | None = (
        {
            "adapter_order": adapter_order_names,
            "step": run_depth,
        }
        if adapter_order_names is not None
        else None
    )
    if routes_path is not None and (pages is not None or page_selection is not None):
        raise ValueError("--routes cannot be combined with --pages or rerun page selection")
    adapter = None
    if (not dry_run or routes_path is not None) and effective_adapter_name is not None:
        adapter = load_adapter(effective_adapter_name, effective_adapter_options)
    if not dry_run and routes_path is None and _requires_adapter(config.default_action):
        if effective_adapter_name is None:
            raise ValueError(
                "No configured adapter; set run.adapter or run.adapter_order in the config"
            )
        assert adapter is not None
        action = config.default_action
        if _requires_adapter(action) and not adapter.supports(action):
            raise ValueError(f"Adapter '{adapter.name}' does not support action '{action}'")

    started_at = _utc_now()
    run_id = f"run-{_utc_now_compact()}"
    selection_by_source: dict[Path, list[dict[str, Any]]] | None = None
    if page_selection is None:
        input_paths = _expand_inputs(inputs)
    else:
        selection_by_source = _group_selection(page_selection)
        input_paths = list(selection_by_source)
    selected_page_numbers: list[int] | None = None
    if pages is not None:
        if len(input_paths) != 1:
            raise ValueError("--pages requires a single input file")
        selected_page_numbers = _parse_pages_expression(pages)
    _validate_adapter_inputs(input_paths, adapter_name=effective_adapter_name)
    _validate_out_dir(out_dir)

    imported_routes: dict[str, Any] | None = None
    route_warnings: list[str] = []
    page_counts: dict[Path, int] = {}
    if routes_path is not None:
        for source in input_paths:
            page_counts[source.resolve()] = _planned_page_count(
                source, adapter=adapter, adapter_name=effective_adapter_name
            )
        taxonomy = config.data.get("taxonomy") or {}
        page_types = set((taxonomy.get("page_types") or {}).keys())
        imported_routes, route_warnings = load_route_map(
            routes_path,
            inputs=input_paths,
            page_counts=page_counts,
            page_types=page_types,
        )
        actions = {
            page["action"]
            for document in imported_routes["documents"]
            for page in document["pages"]
            if _requires_adapter(page["action"])
        }
        if actions and adapter is None:
            raise ValueError(
                "No configured adapter; set run.adapter or run.adapter_order in the config"
            )
        for action in sorted(actions):
            assert adapter is not None
            if not adapter.supports(action):
                raise ValueError(f"Adapter '{adapter.name}' does not support action '{action}'")

    documents: list[dict[str, Any]] = []
    input_entries: list[dict[str, Any]] = []
    source_sha256_map: dict[Path, str] = {}
    review_queue: list[dict[str, Any]] = []
    quarantine_queue: list[dict[str, Any]] = []
    provenance_entries: list[dict[str, Any]] = []
    extractor_entries: list[dict[str, Any]] = []
    log_entries: list[dict[str, Any]] = []
    usage_entries: list[dict[str, Any]] = []
    quality_entries: list[dict[str, Any]] = []
    schema_spec = load_schema_spec(config.data)
    alignments: dict[str, dict[str, Any]] = {}
    pages_total = 0
    pages_extracted = 0
    pages_failed = 0
    pages_not_attempted = 0
    pages_skipped = 0
    tokens_total = 0
    estimated_cost_usd = 0.0
    cost_is_partial = False
    cost_bases: set[str] = set()
    extraction_seconds_values: list[float] = []
    budget_alerts: list[dict[str, Any]] = []
    alerted_budget_units: set[str] = set()
    failure_error: RuntimeError | None = None
    halt_reason: str | None = None
    attempted_page_ids: set[str] = set()
    consecutive_failures = 0

    prompt = config.default_prompt
    planned_pages: list[tuple[Path, dict[str, Any]]] = []
    imported_by_source = {
        Path(document["source"]): document
        for document in (imported_routes or {}).get("documents", [])
    }
    for document_index, source in enumerate(input_paths, start=1):
        resolved_source = source.resolve()
        if imported_routes is not None:
            imported_document = imported_by_source[resolved_source]
            page_count = page_counts[resolved_source]
            routed_pages = [dict(page) for page in imported_document["pages"]]
        elif selection_by_source is None:
            page_count = _planned_page_count(
                source, adapter=adapter, adapter_name=effective_adapter_name
            )
            page_numbers: Sequence[int] = range(1, page_count + 1)
            if selected_page_numbers is not None:
                highest = selected_page_numbers[-1]
                if highest > page_count:
                    raise ValueError(
                        f"--pages selects page {highest} but {source} has "
                        f"{page_count} pages"
                    )
                page_numbers = selected_page_numbers
            planned = [
                (f"doc_{document_index:04d}_page_{page_number:04d}", page_number)
                for page_number in page_numbers
            ]
        else:
            selected = selection_by_source[source]
            page_count = len(selected)
            planned = [(item["page_id"], int(item["page_number"])) for item in selected]
        if imported_routes is None:
            routed_pages = []
            for page_id, page_number in planned:
                action = "review" if dry_run else config.default_action
                reason = _route_reason(action=action, dry_run=dry_run)
                page = {
                    "page_id": page_id,
                    "page_number": page_number,
                    "type": config.default_review_type,
                    "confidence": None,
                    "action": action,
                    "reason": reason,
                }
                if prompt is not None:
                    page["prompt"] = prompt
                routed_pages.append(page)
        for page in routed_pages:
            action = cast(str, page["action"])
            if action == "review":
                review_queue.append(_review_queue_entry(page))
            if action == "skip":
                pages_skipped += 1
            planned_pages.append((source, page))
            pages_total += 1
        try:
            source_sha256 = _sha256_path(source)
            source_sha256_map[source] = source_sha256
        except OSError as exc:
            raise RuntimeError(f"Cannot read input file '{source}': {exc}") from exc
        input_entry = {
            "path": str(source),
            "sha256": source_sha256,
            "page_count": page_count,
        }
        if selected_page_numbers is not None:
            input_entry["pages"] = pages
        input_entries.append(input_entry)
        declared_sha256 = (
            imported_by_source[resolved_source].get("source_sha256")
            if imported_routes is not None
            else None
        )
        if declared_sha256 is not None and declared_sha256 != source_sha256:
            raise ValueError(f"Route map source_sha256 does not match input: {source}")
        documents.append({
            "source": str(resolved_source),
            "source_sha256": source_sha256,
            "page_count": page_count,
            "pages": routed_pages,
        })

    preflight_error = _preflight_budget_error(config=config, pages_total=pages_total)
    if preflight_error is not None:
        raise BudgetExceededError(preflight_error)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw").mkdir(exist_ok=True)
    (out_dir / "normalized").mkdir(exist_ok=True)

    config_snapshot = out_dir / "config-snapshot.yml"
    copyfile(config_path, config_snapshot)

    # Cache static adapter metadata (unchanged per run)
    adapter_input_types: list[str] = []
    adapter_output_types: list[str] = []
    adapter_capabilities: list[str] = []
    if adapter is not None:
        adapter_input_types = list(getattr(adapter, "input_types", ()))
        adapter_output_types = list(getattr(adapter, "output_types", ()))
        adapter_capabilities = list(getattr(adapter, "capabilities", ()))

    for source, page in planned_pages:
        action = cast(str, page["action"])
        if action in {"review", "skip"}:
            continue
        if dry_run:
            continue
        page_id = cast(str, page["page_id"])
        attempted_page_ids.add(page_id)
        page_prompt = cast(str | None, page.get("prompt"))
        prompt_hash = _sha256_text(page_prompt or "")

        if adapter is not None:
            result, extraction_seconds, extraction_started_at, attempt, adapter_error = (
                _extract_adapter_page(
                    adapter=adapter,
                    source=source,
                    page=page,
                    prompt=page_prompt,
                    config=config,
                    run_id=run_id,
                    log_entries=log_entries,
                )
            )
            if adapter_error is not None:
                pages_failed += 1
                consecutive_failures += 1
                review_queue.append(_review_queue_entry(page, "extraction_failed"))
                if config.on_page_error == "stop":
                    failure_error = adapter_error
                    halt_reason = "failure"
                    break
                if (
                    config.max_consecutive_failures > 0
                    and consecutive_failures >= config.max_consecutive_failures
                ):
                    failure_error = RuntimeError(
                        "Circuit breaker opened after "
                        f"{consecutive_failures} consecutive page failures"
                    )
                    halt_reason = "failure"
                    break
                continue
            assert result is not None
            consecutive_failures = 0
            usage = _canonical_usage(result.usage)
            raw_artifact = Path("raw") / f"{page_id}.{_artifact_extension(result.format)}"
            raw_text = (
                result.content
                if isinstance(result.content, str)
                else json.dumps(result.content, ensure_ascii=False, allow_nan=False)
            )
            (out_dir / raw_artifact).write_text(raw_text, encoding="utf-8")
            page_tokens = usage.get("tokens")
            page_cost, page_cost_basis = _derive_cost(
                usage,
                cost_per_page=config.cost_per_page,
                cost_per_1k_tokens=config.cost_per_1k_tokens,
            )
            pages_extracted += int(usage["pages"])
            if isinstance(page_tokens, int):
                tokens_total += page_tokens
            if extraction_seconds is not None:
                extraction_seconds_values.append(extraction_seconds)
            if page_cost is None:
                cost_is_partial = True
            else:
                assert page_cost_basis is not None
                cost_bases.add(page_cost_basis)
                estimated_cost_usd = _round_cost(estimated_cost_usd + page_cost)
            usage_entries.append(usage)
            budget_error = _budget_error(
                config=config,
                page_id=page_id,
                pages_total=pages_extracted,
                tokens_total=tokens_total,
                estimated_cost_usd=estimated_cost_usd,
            )
            new_budget_alerts = _new_budget_alerts(
                config=config,
                page_id=page_id,
                timestamp=extraction_started_at,
                pages_total=pages_extracted,
                tokens_total=tokens_total,
                estimated_cost_usd=estimated_cost_usd,
                alerted_units=alerted_budget_units,
            )
            budget_alerts.extend(new_budget_alerts)
            alerted_budget_units.update(
                str(alert["unit"]) for alert in new_budget_alerts
            )
            # Preserve the v0.1 per-page log contract: once a threshold is
            # reached, later pages retain the scalar warning. The structured
            # alert list above separately records one first crossing per unit.
            budget_warning = _budget_warning(
                config=config,
                pages_total=pages_extracted,
                tokens_total=tokens_total,
                estimated_cost_usd=estimated_cost_usd,
            )
            extractor_entry = {
                "name": adapter.name,
                "adapter": adapter.name,
                "model": result.model,
                "version": adapter.version,
                "prompt_hash": prompt_hash,
                "deterministic": adapter.deterministic,
                "input_types": adapter_input_types,
                "output_types": adapter_output_types,
                "capabilities": adapter_capabilities,
            }
            if effective_adapter_options:
                extractor_entry["options"] = dict(effective_adapter_options)
            if extractor_entry not in extractor_entries:
                extractor_entries.append(extractor_entry)
            provenance_entries.append(
                _build_provenance_entry(
                    schema_version=config.schema_version,
                    run_id=run_id,
                    page=page,
                    source=source,
                    source_sha256=source_sha256_map[source],
                    adapter=adapter,
                    result=result,
                    usage=usage,
                    raw_artifact=raw_artifact,
                    prompt_hash=prompt_hash,
                    timestamp=extraction_started_at,
                    extraction_seconds=extraction_seconds,
                    adapter_input_types=adapter_input_types,
                    adapter_output_types=adapter_output_types,
                    adapter_capabilities=adapter_capabilities,
                    page_cost=page_cost,
                    page_cost_basis=page_cost_basis,
                )
            )
            quality_entries.append(
                _build_quality_entry(
                    schema_version=config.schema_version,
                    page=page,
                    source=source,
                    result=result,
                    adapter=adapter,
                    parent_quality=(parent_quality_by_page or {}).get(page_id),
                )
            )
            if schema_spec is not None and result.format in ALIGNABLE_FORMATS:
                alignment = align_page(
                    result.content,
                    result.format,
                    schema_spec,
                    page=page,
                    run_id=run_id,
                    schema_version=config.schema_version,
                    raw_artifact=raw_artifact.as_posix(),
                )
                if alignment is not None:
                    write_json(out_dir / "normalized" / f"{page_id}.json", alignment)
                    alignments[page_id] = alignment
            log_entries.append(
                {
                    "schema_version": config.schema_version,
                    "timestamp": extraction_started_at,
                    "level": "ERROR" if budget_error else "INFO",
                    "run_id": run_id,
                    "page_id": page_id,
                    "adapter": adapter.name,
                    "status": "budget_exceeded" if budget_error else "extracted",
                    "error": budget_error,
                    "budget_warning": None if budget_error else budget_warning,
                    "attempt": attempt,
                }
            )
            if budget_error is not None:
                failure_error = BudgetExceededError(budget_error)
                halt_reason = "budget"
                break

    if halt_reason is not None:
        reason = (
            "not_attempted_after_budget"
            if halt_reason == "budget"
            else "not_attempted_after_failure"
        )
        for _source, page in planned_pages:
            if (
                _requires_adapter(cast(str, page["action"]))
                and page["page_id"] not in attempted_page_ids
            ):
                pages_not_attempted += 1
                review_queue.append(_review_queue_entry(page, reason))

    route_map = build_route_map(
        schema_version=config.schema_version,
        run_id=run_id,
        generated_at=(imported_routes or {}).get("generated_at", started_at),
        documents=documents,
        classifier=(imported_routes or {}).get("classifier"),
    )
    write_yaml(out_dir / "route-map.yml", route_map)

    for entry in quality_entries:
        entry.update(
            grade_page(
                entry,
                alignments.get(entry["page_id"]),
                config.grading_thresholds,
                quality_floors=schema_spec.quality if schema_spec else None,
            )
        )

    quality_warning_pages = sum(1 for entry in quality_entries if entry.get("warnings"))

    routes = {
        page["page_id"]: page
        for document in documents
        for page in document["pages"]
    }
    review_queue, quarantine_queue = rebuild_policy_queues(
        config=config,
        quality_entries=quality_entries,
        alignments=alignments,
        routes=routes,
        review_queue=review_queue,
        quarantine_queue=quarantine_queue,
    )

    if failure_error:
        status = "failed"
    elif dry_run or pages_failed:
        status = "partial"
    else:
        status = "completed"
    quarantined_page_ids = {
        item["page_id"] for item in quarantine_queue
    }
    manifest = build_manifest(
        schema_version=config.schema_version,
        run_id=run_id,
        execution_mode=execution_mode,
        started_at=started_at,
        completed_at=_utc_now(),
        inputs=input_entries,
        parent_run_id=parent_run_id,
        config_sha256=_sha256_path(config_snapshot),
        config_source_paths=[str(config_path.resolve())],
        dataset_citation=config.dataset_citation,
        pages_total=pages_total,
        pages_extracted=pages_extracted,
        pages_failed=pages_failed,
        pages_not_attempted=pages_not_attempted,
        pages_skipped=pages_skipped,
        pages_quarantined=len(quarantined_page_ids),
        records_normalized=sum(
            len(alignment["records"]) for alignment in alignments.values()
        ),
        estimated_cost_usd=estimated_cost_usd,
        quality_warning_pages=quality_warning_pages,
        status=status,
        extractors=extractor_entries,
        routing=(
            {
                "source_path": str(routes_path.expanduser().resolve()),
                "sha256": _sha256_path(routes_path),
                "source_run_id": imported_routes["run_id"],
            }
            if routes_path is not None and imported_routes is not None
            else None
        ),
        escalation=escalation,
    )
    audit = build_audit(
        schema_version=config.schema_version,
        run_id=run_id,
        review_queue=review_queue,
        quarantine_queue=quarantine_queue,
    )
    write_json(out_dir / "audit.json", audit)

    (out_dir / "audit.md").write_text(
        render_audit_markdown(audit),
        encoding="utf-8",
    )
    write_jsonl(out_dir / "provenance.jsonl", provenance_entries)
    write_jsonl(out_dir / "quality.jsonl", quality_entries)
    write_json(
        out_dir / "cost.json",
        _build_cost_report(
            schema_version=config.schema_version,
            run_id=run_id,
            execution_mode=execution_mode,
            config=config,
            pages_extracted=pages_extracted,
            tokens_total=tokens_total,
            usage_entries=usage_entries,
            estimated_cost_usd=estimated_cost_usd,
            cost_is_partial=cost_is_partial,
            cost_bases=cost_bases,
            extraction_seconds_values=extraction_seconds_values,
            provenance_entries=provenance_entries,
            budget_alerts=budget_alerts,
        ),
    )

    rerun_escalation: dict[str, Any] | None = None
    if escalation is not None:
        assert adapter_order_names is not None
        rerun_escalation = {
            **escalation,
            "next_adapter": (
                adapter_order_names[run_depth + 1]
                if run_depth + 1 < len(adapter_order_names)
                else None
            ),
        }
    rerun_manifest = build_rerun_manifest(
        schema_version=config.schema_version,
        run_id=run_id,
        parent_run_id=run_id,
        created_at=_utc_now(),
        max_rerun_depth=config.max_rerun_depth,
        reason="dry_run" if dry_run else "audit_policy",
        audit=audit,
        route_map=route_map,
        quarantined_page_ids=quarantined_page_ids,
        run_depth=run_depth,
        grades={entry["page_id"]: entry["grade"] for entry in quality_entries},
        escalation=rerun_escalation,
    )
    write_yaml(out_dir / "rerun-manifest.yml", rerun_manifest)

    log_event = (
        log_entries
        if log_entries
        else [
            {
                "schema_version": config.schema_version,
                "timestamp": _utc_now(),
                "level": "INFO",
                "run_id": run_id,
                "execution_mode": execution_mode,
                "status": "dry_run_complete" if dry_run else "run_complete",
                "pages_total": pages_total,
                "pages_extracted": pages_extracted,
                "pages_skipped": pages_skipped,
            }
        ]
    )
    log_event = [
        entry
        for entry in log_event
        if _should_log(cast(str, entry["level"]), log_level)
    ]
    write_jsonl(out_dir / "run.log", log_event)
    # The manifest is the commit indicator for a fully written run directory.
    # Write it only after every artifact it points to exists.
    write_json(out_dir / "manifest.json", manifest)
    if failure_error is not None:
        raise failure_error

    config_warnings = getattr(config, "warnings", [])
    result = {
        "run_id": run_id,
        "out_dir": str(out_dir),
        "dry_run": dry_run,
        "execution_mode": execution_mode,
        "status": status,
        "summary": manifest["summary"],
        "quality_warning_pages": quality_warning_pages,
        "raw_artifact_count": len(provenance_entries),
        "config_warnings": [*config_warnings, *route_warnings],
    }
    if escalation is not None:
        result["escalation"] = escalation
    if budget_alerts:
        result["budget_alerts"] = budget_alerts
    if parent_run_id is not None:
        result["parent_run_id"] = parent_run_id
        result["rerun_depth"] = run_depth
    return result


def rerun(
    *,
    parent_dir: Path,
    config_path: Path,
    out_dir: Path,
    dry_run: bool = False,
    log_level: str = "INFO",
    adapter_path: Path | None = None,
) -> dict[str, Any]:
    """Execute the rerun manifest of a previous run.

    Reads ``rerun-manifest.yml`` from ``parent_dir`` and re-extracts exactly
    the listed pages into a new run directory, preserving their original
    page ids. The config supplied here decides the adapter — pass a config
    with a stronger engine to re-extract weak pages.

    Enforces ``run.max_rerun_depth`` from the supplied config against the
    parent's recorded ``rerun_depth``. Warns (without failing) when a source
    file's checksum no longer matches the parent run's manifest.
    """
    parent = parent_dir.expanduser().resolve()
    manifest_path = parent / "manifest.json"
    rerun_path = parent / "rerun-manifest.yml"
    if not manifest_path.is_file():
        raise ValueError(f"No manifest.json found in {parent}; not a run directory")
    if not rerun_path.is_file():
        raise ValueError(f"No rerun-manifest.yml found in {parent}")
    parent_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parent_rerun = yaml.safe_load(rerun_path.read_text(encoding="utf-8"))
    if not isinstance(parent_rerun, dict):
        raise ValueError(f"Invalid rerun manifest: {rerun_path}")

    _apply_adapter_path(adapter_path)
    config = load_config(config_path, validate_adapter=False)
    child_depth = int(parent_rerun.get("rerun_depth", 0)) + 1
    if child_depth > config.max_rerun_depth:
        raise ValueError(
            f"Max rerun depth reached: this rerun would be generation {child_depth} "
            f"but run.max_rerun_depth is {config.max_rerun_depth}"
        )
    if config.adapter_order is not None and child_depth >= len(config.adapter_order):
        raise ValueError(
            "Adapter chain exhausted: this rerun would be generation "
            f"{child_depth} but run.adapter_order has {len(config.adapter_order)} step(s)"
        )

    effective_adapter_name, _effective_options = _effective_adapter(config, child_depth)
    escalation_warnings: list[str] = []
    parent_escalation = parent_rerun.get("escalation")
    if isinstance(parent_escalation, dict):
        recorded_next = parent_escalation.get("next_adapter")
        if config.adapter_order is None:
            escalation_warnings.append(
                "Supplied run.adapter overrides the parent adapter chain"
                + (
                    f" planned next adapter '{recorded_next}'"
                    if isinstance(recorded_next, str)
                    else ""
                )
            )
        elif not _adapter_names_match(recorded_next, effective_adapter_name):
            escalation_warnings.append(
                "Supplied config selects adapter "
                f"'{effective_adapter_name}' for generation {child_depth}; "
                f"parent planned '{recorded_next}' (config wins)"
            )

    items = parent_rerun.get("items") or []
    if not items:
        raise ValueError(
            "Rerun manifest has no items to rerun "
            f"(rerun_status: {parent_rerun.get('rerun_status', 'unknown')})"
        )

    recorded_hashes = {
        str(Path(entry["path"]).resolve()): entry.get("sha256")
        for entry in parent_manifest.get("inputs", [])
        if isinstance(entry, dict) and "path" in entry
    }
    integrity_warnings: list[str] = []
    for source_str in sorted({str(item["source"]) for item in items}):
        source = Path(source_str)
        if not source.exists():
            raise ValueError(f"Rerun source no longer exists: {source}")
        recorded = recorded_hashes.get(str(source.resolve()))
        if recorded is not None and _sha256_path(source) != recorded:
            integrity_warnings.append(
                f"Source changed since parent run: {source} "
                "(sha256 no longer matches parent manifest)"
            )

    result = run(
        inputs=[],
        config_path=config_path,
        out_dir=out_dir,
        dry_run=dry_run,
        log_level=log_level,
        page_selection=items,
        parent_run_id=parent_manifest["run_id"],
        parent_quality_by_page={
            entry["page_id"]: entry for entry in read_jsonl(parent / "quality.jsonl")
        },
        run_depth=child_depth,
        adapter_path=adapter_path,
    )
    if integrity_warnings:
        result["source_integrity_warnings"] = integrity_warnings
    if escalation_warnings:
        result["escalation_warnings"] = escalation_warnings
    return result


def _effective_adapter(config: Any, run_depth: int) -> tuple[str | None, dict[str, Any]]:
    order = config.adapter_order
    if order is None:
        return config.adapter_name, config.adapter_options
    if run_depth >= len(order):
        raise ValueError(
            f"Adapter chain exhausted at generation {run_depth}: "
            f"run.adapter_order has {len(order)} step(s)"
        )
    entry = order[run_depth]
    return str(entry["adapter"]), dict(entry["adapter_options"])


def _adapter_names_match(recorded: Any, configured: str | None) -> bool:
    if not isinstance(recorded, str) or configured is None:
        return recorded is None and configured is None
    aliases = {"pdf": "pdf_text", "pdf_text": "pdf_text"}
    return aliases.get(recorded, recorded) == aliases.get(configured, configured)


def _parse_pages_expression(expression: str) -> list[int]:
    """Parse a page selection like ``1-8,81,100-110`` into sorted numbers."""
    numbers: set[int] = set()
    for part in expression.split(","):
        part = part.strip()
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if match is None:
            raise ValueError(
                f"--pages: cannot parse '{part}'; use forms like '1-8,81,100-110'"
            )
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if start < 1 or end < start:
            raise ValueError(f"--pages: invalid range '{part}'")
        numbers.update(range(start, end + 1))
    return sorted(numbers)


def _group_selection(page_selection: list[dict[str, Any]]) -> dict[Path, list[dict[str, Any]]]:
    """Group rerun items by source path, preserving first-seen source order."""
    grouped: dict[Path, list[dict[str, Any]]] = {}
    for item in page_selection:
        for field in ("page_id", "page_number", "source"):
            if field not in item:
                raise ValueError(f"Rerun item missing required field '{field}': {item}")
        source = Path(str(item["source"]))
        if not source.exists():
            raise ValueError(f"Rerun source does not exist: {source}")
        grouped.setdefault(source, []).append(item)
    if not grouped:
        raise ValueError("Page selection is empty")
    return grouped


def _route_reason(*, action: str, dry_run: bool) -> str:
    if dry_run:
        return "no_classifier_available"
    if action == "skip":
        return "configured_skip"
    if action == "review":
        return "configured_review"
    return "configured_adapter"


def _review_queue_entry(
    page: dict[str, Any], reason: str | None = None
) -> dict[str, Any]:
    return {
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "type": page["type"],
        "confidence": page.get("confidence"),
        "action": "review",
        "reason": reason or page["reason"],
    }


def _requires_adapter(action: str) -> bool:
    return action not in {"review", "skip"}


def _planned_page_count(source: Path, *, adapter: Any, adapter_name: str | None) -> int:
    if adapter is not None:
        return adapter_page_count(adapter, source)
    if source.suffix.lower() == ".pdf":
        if adapter_name == "pdf_ocr":
            return ocr_pdf_page_count(source)
        if adapter_name in PDF_ADAPTER_NAMES or adapter_name is None:
            return pdf_page_count(source)
        return 1
    return paginate(source, allow_pdf=False)


def _apply_adapter_path(adapter_path: Path | None) -> None:
    """Prepend a directory to sys.path so custom adapter modules resolve."""
    if adapter_path is None:
        return
    resolved = adapter_path.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"--adapter-path is not a directory: {adapter_path}")
    entry = str(resolved)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _normalize_log_level(log_level: str) -> str:
    normalized = log_level.upper()
    if normalized not in LOG_LEVELS:
        valid = ", ".join(LOG_LEVELS)
        raise ValueError(f"log_level must be one of: {valid}")
    return normalized


def _should_log(event_level: str, configured_level: str) -> bool:
    return LOG_LEVELS[event_level] >= LOG_LEVELS[configured_level]


def _expand_inputs(inputs: list[Path]) -> list[Path]:
    expanded: list[Path] = []
    for path in inputs:
        if not path.exists():
            raise ValueError(f"Input path does not exist: {path}")
        if path.is_dir():
            expanded.extend(sorted(child for child in path.iterdir() if child.is_file()))
        else:
            expanded.append(path)
    if not expanded:
        raise ValueError("No input files found")
    return expanded


def _validate_adapter_inputs(inputs: list[Path], *, adapter_name: str | None) -> None:
    if adapter_name == "text":
        pdf_inputs = [path for path in inputs if path.suffix.lower() == ".pdf"]
        if pdf_inputs:
            first = pdf_inputs[0]
            raise ValueError(
                f"Adapter 'text' cannot read PDF input: {first}. "
                "Use run.adapter: pdf_text for born-digital PDFs or "
                "run.adapter: pdf_ocr for scanned PDFs."
            )
    if adapter_name in PDF_ONLY_ADAPTER_NAMES:
        non_pdf_inputs = [path for path in inputs if path.suffix.lower() != ".pdf"]
        if non_pdf_inputs:
            first = non_pdf_inputs[0]
            raise ValueError(
                f"Adapter '{adapter_name}' only reads PDF inputs: {first}. "
                "Use run.adapter: text for UTF-8 text fixtures or provide a custom adapter."
            )


def _validate_out_dir(out_dir: Path) -> None:
    if not out_dir.exists():
        return
    if not out_dir.is_dir():
        raise ValueError(f"Output path exists and is not a directory: {out_dir}")
    if any(out_dir.iterdir()):
        raise ValueError(f"Output directory is not empty: {out_dir}")


def _backoff_seconds(backoff: str, attempt: int) -> float:
    """Delay before the next retry attempt. Exponential: 0.5s, 1s, 2s… capped at 8s."""
    if backoff != "exponential":
        return 0.0
    return min(0.5 * (2 ** (attempt - 1)), 8.0)


def _validate_extraction_result(adapter_name: str, result: Any) -> None:
    if not isinstance(result.content, (str, dict, list)):
        raise ValueError(f"Adapter '{adapter_name}' content must be string, object, or list")
    _require_json_serializable(
        f"Adapter '{adapter_name}' content must be JSON-serializable",
        result.content,
    )
    if not isinstance(result.format, str) or not result.format:
        raise ValueError(f"Adapter '{adapter_name}' format must be a non-empty string")
    if re.fullmatch(r"[A-Za-z0-9_]+", result.format) is None:
        raise ValueError(
            f"Adapter '{adapter_name}' format must contain only letters, numbers, and underscores"
        )
    _validate_optional_finite_number(adapter_name, "confidence", result.confidence)
    if result.confidence is not None and not 0 <= result.confidence <= 1:
        raise ValueError(f"Adapter '{adapter_name}' confidence must be between 0 and 1")
    if result.model is not None and not isinstance(result.model, str):
        raise ValueError(f"Adapter '{adapter_name}' model must be a string or null")
    if not isinstance(result.warnings, list):
        raise ValueError(f"Adapter '{adapter_name}' warnings must be a list")
    if not all(isinstance(warning, str) for warning in result.warnings):
        raise ValueError(f"Adapter '{adapter_name}' warnings must contain only strings")
    confidence_detail = getattr(result, "confidence_detail", None)
    if confidence_detail is not None and not isinstance(confidence_detail, dict):
        raise ValueError(
            f"Adapter '{adapter_name}' confidence_detail must be a mapping or null"
        )
    _require_json_serializable(
        f"Adapter '{adapter_name}' confidence_detail must be JSON-serializable",
        confidence_detail,
    )
    if not isinstance(result.usage, dict):
        raise ValueError(f"Adapter '{adapter_name}' usage must be a mapping")
    _require_json_serializable(
        f"Adapter '{adapter_name}' usage must be JSON-serializable",
        result.usage,
    )
    pages = result.usage.get("pages")
    if not isinstance(pages, int) or isinstance(pages, bool) or pages != 1:
        raise ValueError(
            f"Adapter '{adapter_name}' usage.pages must be exactly 1 "
            f"(the page is the canonical unit; each extract() call handles one page). "
            f"Got: {pages!r}"
        )
    tokens = result.usage.get("tokens")
    if tokens is not None and (not isinstance(tokens, int) or isinstance(tokens, bool)):
        raise ValueError(f"Adapter '{adapter_name}' usage.tokens must be an integer or null")
    compute_seconds = result.usage.get("compute_seconds")
    _validate_optional_finite_number(
        adapter_name, "usage.compute_seconds", compute_seconds
    )
    cost_usd = result.usage.get("cost_usd")
    _validate_optional_finite_number(adapter_name, "usage.cost_usd", cost_usd)


def _validate_optional_finite_number(
    adapter_name: str, field: str, value: Any
) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(
            f"Adapter '{adapter_name}' {field} must be a finite number or null"
        )


def _canonical_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "pages": usage["pages"],
        "tokens": usage.get("tokens"),
        "compute_seconds": usage.get("compute_seconds"),
        "cost_usd": usage.get("cost_usd"),
    }


def _require_json_serializable(message: str, value: Any) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc


def _build_provenance_entry(
    *,
    schema_version: str,
    run_id: str,
    page: dict[str, Any],
    source: Path,
    source_sha256: str,
    adapter: Any,
    result: Any,
    usage: dict[str, Any],
    raw_artifact: Path,
    prompt_hash: str,
    timestamp: str,
    extraction_seconds: float | None,
    adapter_input_types: list[str],
    adapter_output_types: list[str],
    adapter_capabilities: list[str],
    page_cost: float | None,
    page_cost_basis: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "run_id": run_id,
        "page_id": page["page_id"],
        "source": {
            "path": str(source),
            "page_number": page["page_number"],
            "sha256": source_sha256,
        },
        "route": {
            "type": page["type"],
            "action": page["action"],
            "route_confidence": page["confidence"],
        },
        "extractor": {
            "adapter": adapter.name,
            "adapter_version": adapter.version,
            "model": result.model,
            "prompt_hash": prompt_hash,
            "deterministic": adapter.deterministic,
            "input_types": adapter_input_types,
            "output_types": adapter_output_types,
            "capabilities": adapter_capabilities,
        },
        "result": {
            "format": result.format,
            "confidence": result.confidence,
            "warnings": result.warnings,
            "raw_artifact": raw_artifact.as_posix(),
        },
        "usage": usage,
        "metrics": usage,
        "cost": {"usd": page_cost, "basis": page_cost_basis},
        "extraction_seconds": extraction_seconds,
        "timestamp": timestamp,
    }


def _artifact_extension(result_format: str) -> str:
    return "txt" if result_format == "text" else result_format


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snippet(value: Any, *, limit: int = 1000) -> str | None:
    if value is None:
        return None
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    return text[:limit]
