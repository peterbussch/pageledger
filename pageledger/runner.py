"""Filesystem-native alpha runner for PageLedger."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from shutil import copyfile
from typing import Any

import yaml

from .adapters import (
    PDF_ADAPTER_NAMES,
    PDF_ONLY_ADAPTER_NAMES,
    adapter_page_count,
    load_adapter,
    ocr_pdf_page_count,
    paginate,
    pdf_page_count,
)
from .artifacts import (
    build_audit,
    build_manifest,
    build_rerun_manifest,
    build_route_map,
    render_audit_markdown,
    write_json,
    write_jsonl,
    write_yaml,
)
from .config import load_config

LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


class BudgetExceededError(RuntimeError):
    """Raised when a configured budget cap is crossed."""


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
    run_depth: int = 0,
    adapter_path: Path | None = None,
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
    config = load_config(config_path, validate_adapter=not dry_run)
    adapter = None
    if not dry_run and _requires_adapter(config.default_action):
        if config.adapter_name is None:
            raise ValueError("No configured adapter; set run.adapter in the config")
        adapter = load_adapter(config.adapter_name, config.adapter_options)
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
    _validate_adapter_inputs(input_paths, adapter_name=config.adapter_name)
    _validate_out_dir(out_dir)

    documents: list[dict[str, Any]] = []
    input_entries: list[dict[str, Any]] = []
    source_sha256_map: dict[Path, str] = {}
    review_queue: list[dict[str, Any]] = []
    provenance_entries: list[dict[str, Any]] = []
    extractor_entries: list[dict[str, Any]] = []
    log_entries: list[dict[str, Any]] = []
    usage_entries: list[dict[str, Any]] = []
    quality_entries: list[dict[str, Any]] = []
    pages_total = 0
    pages_extracted = 0
    pages_skipped = 0
    tokens_total = 0
    estimated_cost_usd = 0.0
    cost_is_partial = False
    cost_bases: set[str] = set()
    extraction_seconds_values: list[float] = []
    failure_error: RuntimeError | None = None

    prompt = config.default_prompt
    prompt_hash = _sha256_text(prompt or "")
    planned_pages: list[tuple[Path, dict[str, Any]]] = []
    for document_index, source in enumerate(input_paths, start=1):
        resolved_source = source.resolve()
        if selection_by_source is None:
            page_count = _planned_page_count(
                source, adapter=adapter, adapter_name=config.adapter_name
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
        routed_pages: list[dict[str, Any]] = []
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
            if action == "review":
                review_queue.append(page)
            if action == "skip":
                pages_skipped += 1
            routed_pages.append(page)
            planned_pages.append((source, page))
            pages_total += 1
        documents.append({"source": str(resolved_source), "pages": routed_pages})
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
        action = page["action"]
        if action in {"review", "skip"}:
            continue
        page_id = page["page_id"]

        if adapter is not None:
            result = None
            extraction_seconds: float | None = None
            for attempt in range(1, config.max_retries + 2):
                extraction_started_at = _utc_now()
                attempt_started = time.perf_counter()
                try:
                    result = adapter.extract(
                        source,
                        page_id=page_id,
                        page_number=page["page_number"],
                        action=action,
                        prompt=prompt,
                    )
                    extraction_seconds = round(time.perf_counter() - attempt_started, 3)
                    break
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    final_attempt = attempt > config.max_retries
                    adapter_error = AdapterExecutionError(
                        adapter=adapter.name,
                        page_id=page_id,
                        status="failed" if final_attempt else "retry",
                        message=error,
                        stdout=getattr(exc, "stdout", None),
                        stderr=getattr(exc, "stderr", None),
                    )
                    log_entries.append(_make_log_entry(
                        schema_version=config.schema_version,
                        run_id=run_id,
                        page_id=page_id,
                        adapter_name=adapter.name,
                        level="ERROR" if final_attempt else "WARNING",
                        status="failed" if final_attempt else "retry",
                        error=adapter_error.to_dict(),
                        attempt=attempt,
                        max_retries=config.max_retries,
                    ))
                    if final_attempt:
                        failure_error = adapter_error
                        break
                    delay = _backoff_seconds(config.retry_backoff, attempt)
                    if delay > 0:
                        time.sleep(delay)
            if failure_error is not None:
                break
            try:
                _validate_extraction_result(adapter.name, result)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                adapter_error = AdapterExecutionError(
                    adapter=adapter.name,
                    page_id=page_id,
                    status="invalid_result",
                    message=error,
                    stdout=getattr(exc, "stdout", None),
                    stderr=getattr(exc, "stderr", None),
                )
                log_entries.append(_make_log_entry(
                    schema_version=config.schema_version,
                    run_id=run_id,
                    page_id=page_id,
                    adapter_name=adapter.name,
                    level="ERROR",
                    status="failed",
                    error=adapter_error.to_dict(),
                    attempt=attempt,
                    max_retries=config.max_retries,
                ))
                failure_error = adapter_error
                break
            usage = _canonical_usage(result.usage)
            raw_artifact = Path("raw") / f"{page_id}.{_artifact_extension(result.format)}"
            raw_text = (
                result.content
                if isinstance(result.content, str)
                else json.dumps(result.content, ensure_ascii=False)
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
            if config.adapter_options:
                extractor_entry["options"] = dict(config.adapter_options)
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
                )
            )
            quality_entries.append(
                _build_quality_entry(
                    schema_version=config.schema_version,
                    page=page,
                    source=source,
                    result=result,
                    adapter=adapter,
                )
            )
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
                break

    route_map = build_route_map(
        schema_version=config.schema_version,
        run_id=run_id,
        generated_at=started_at,
        documents=documents,
    )
    write_yaml(out_dir / "route-map.yml", route_map)

    quality_warning_pages = sum(1 for entry in quality_entries if entry.get("warnings"))

    # Wire quality-warning pages into the review queue so users can triage
    # flagged pages without scanning quality.jsonl by hand.
    for entry in quality_entries:
        if entry.get("warnings"):
            review_queue.append({
                "page_id": entry["page_id"],
                "page_number": entry["page_number"],
                "type": config.default_review_type,
                "confidence": None,
                "action": "review",
                "reason": "quality_warning",
            })

    status = "failed" if failure_error else "completed" if not dry_run else "partial"
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
        pages_skipped=pages_skipped,
        pages_quarantined=0,
        records_normalized=0,
        estimated_cost_usd=estimated_cost_usd,
        quality_warning_pages=quality_warning_pages,
        status=status,
        extractors=extractor_entries,
    )
    write_json(out_dir / "manifest.json", manifest)

    audit = build_audit(
        schema_version=config.schema_version,
        run_id=run_id,
        review_queue=review_queue,
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
        ),
    )

    rerun_manifest = build_rerun_manifest(
        schema_version=config.schema_version,
        run_id=run_id,
        parent_run_id=run_id,
        created_at=_utc_now(),
        max_rerun_depth=config.max_rerun_depth,
        reason="dry_run" if dry_run else "audit_policy",
        audit=audit,
        route_map=route_map,
        run_depth=run_depth,
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
    log_event = [entry for entry in log_event if _should_log(entry["level"], log_level)]
    write_jsonl(out_dir / "run.log", log_event)
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
        "config_warnings": list(config_warnings),
    }
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
    config = load_config(config_path, validate_adapter=not dry_run)
    child_depth = int(parent_rerun.get("rerun_depth", 0)) + 1
    if child_depth > config.max_rerun_depth:
        raise ValueError(
            f"Max rerun depth reached: this rerun would be generation {child_depth} "
            f"but run.max_rerun_depth is {config.max_rerun_depth}"
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
        run_depth=child_depth,
    )
    if integrity_warnings:
        result["source_integrity_warnings"] = integrity_warnings
    return result


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


def inspect_run(run_dir: Path) -> dict[str, Any]:
    """Summarize a completed or failed PageLedger run directory.

    Returns a machine-readable dictionary. Raises FileNotFoundError if the
    directory does not contain a valid manifest.json.
    """
    out_dir = run_dir.expanduser().resolve()
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"No manifest.json found in {out_dir}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = manifest.get("summary", {})

    # Derive from manifest (canonical source — no need to re-scan artifacts)
    provenance_count = summary.get("pages_extracted", 0)
    quality_warning_pages = summary.get("quality_warning_pages", 0)

    # Count failed pages from run.log (not in manifest summary)
    failed_page_count = 0
    run_log_path = out_dir / "run.log"
    if run_log_path.is_file():
        for line in run_log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("status") in ("failed", "budget_exceeded"):
                failed_page_count += 1

    # Determine which artifacts are present
    expected_artifacts = [
        "manifest.json", "config-snapshot.yml", "route-map.yml",
        "audit.json", "audit.md", "provenance.jsonl", "quality.jsonl",
        "cost.json", "run.log", "rerun-manifest.yml",
    ]
    artifacts_present: list[str] = []
    artifacts_missing: list[str] = []
    for name in expected_artifacts:
        if (out_dir / name).exists():
            artifacts_present.append(name)
        else:
            artifacts_missing.append(name)

    # Cost info
    cost_known = False
    estimated_cost_usd = None
    cost_path = out_dir / "cost.json"
    if cost_path.is_file():
        cost = json.loads(cost_path.read_text(encoding="utf-8"))
        cost_known = cost.get("cost_known", False)
        estimated_cost_usd = cost.get("cost_usd")

    # Review queue count
    review_queue_count = 0
    audit_path = out_dir / "audit.json"
    if audit_path.is_file():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        review_queue_count = len(audit.get("review_queue", []))

    return {
        "run_id": manifest["run_id"],
        "run_dir": str(out_dir),
        "status": manifest.get("status", "unknown"),
        "execution_mode": manifest.get("execution_mode", "unknown"),
        "pages_total": summary.get("pages_total", 0),
        "pages_extracted": summary.get("pages_extracted", 0),
        "pages_skipped": summary.get("pages_skipped", 0),
        "provenance_count": provenance_count,
        "quality_warning_pages": quality_warning_pages,
        "failed_page_count": failed_page_count,
        "review_queue_count": review_queue_count,
        "cost_known": cost_known,
        "estimated_cost_usd": estimated_cost_usd,
        "artifacts_present": artifacts_present,
        "artifacts_missing": artifacts_missing,
    }


def run_pages_csv(run_dir: Path) -> str:
    """Render a run's per-page evidence as CSV for spreadsheet triage.

    One row per extracted page, joining quality.jsonl (counts, confidence,
    warnings) with provenance.jsonl (cost, timing) on page_id.
    """
    out_dir = run_dir.expanduser().resolve()
    quality_path = out_dir / "quality.jsonl"
    if not quality_path.is_file():
        raise FileNotFoundError(f"No quality.jsonl found in {out_dir}")

    provenance: dict[str, dict[str, Any]] = {}
    provenance_path = out_dir / "provenance.jsonl"
    if provenance_path.is_file():
        for line in provenance_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                provenance[entry["page_id"]] = entry

    columns = [
        "page_id", "page_number", "adapter", "character_count", "word_count",
        "confidence", "warnings", "cost_usd", "extraction_seconds",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for line in quality_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        quality = json.loads(line)
        page_provenance = provenance.get(quality["page_id"], {})
        writer.writerow({
            "page_id": quality["page_id"],
            "page_number": quality["page_number"],
            "adapter": quality["adapter"],
            "character_count": quality["character_count"],
            "word_count": quality["word_count"],
            "confidence": quality.get("confidence"),
            "warnings": ";".join(quality["warnings"]),
            "cost_usd": page_provenance.get("usage", {}).get("cost_usd"),
            "extraction_seconds": page_provenance.get("extraction_seconds"),
        })
    return buffer.getvalue()


def _route_reason(*, action: str, dry_run: bool) -> str:
    if dry_run:
        return "no_classifier_available"
    if action == "skip":
        return "configured_skip"
    if action == "review":
        return "configured_review"
    return "configured_adapter"


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


def _derive_cost(
    usage: dict[str, Any],
    *,
    cost_per_page: float | None,
    cost_per_1k_tokens: float | None,
) -> tuple[float | None, str | None]:
    """Resolve a page's cost and where the number came from, in priority order.

    1. adapter-reported ``cost_usd`` (passthrough) — basis ``adapter_reported``
    2. config unit rates — basis ``configured_rate``
    3. ``(None, None)`` — cost is unknown; the run still reports raw units.
    """
    adapter_cost = usage.get("cost_usd")
    if adapter_cost is not None:
        return float(adapter_cost), "adapter_reported"
    if cost_per_page is None and cost_per_1k_tokens is None:
        return None, None
    cost = 0.0
    if cost_per_page is not None:
        cost += cost_per_page * int(usage.get("pages") or 0)
    if cost_per_1k_tokens is not None:
        tokens = usage.get("tokens")
        cost += cost_per_1k_tokens * ((tokens or 0) / 1000)
    return cost, "configured_rate"


def _backoff_seconds(backoff: str, attempt: int) -> float:
    """Delay before the next retry attempt. Exponential: 0.5s, 1s, 2s… capped at 8s."""
    if backoff != "exponential":
        return 0.0
    return min(0.5 * (2 ** (attempt - 1)), 8.0)


def _cost_basis(cost_bases: set[str]) -> str:
    """Summarize where the run's dollar figure came from.

    ``adapter_reported`` is real provider-reported spend; ``configured_rate``
    is the user's own accounting rate applied to usage; ``mixed`` combines
    both; ``none`` means no dollar figure exists (e.g. a free local engine
    with no pricing configured).
    """
    if not cost_bases:
        return "none"
    if len(cost_bases) > 1:
        return "mixed"
    return next(iter(cost_bases))


def _round_cost(value: float) -> float:
    return round(value, 12)


def _usage_rollup(
    usage_entries: list[dict[str, Any]],
    extraction_seconds_values: list[float],
) -> dict[str, Any]:
    return {
        "pages": _sum_usage_field(usage_entries, "pages"),
        "tokens": _sum_usage_field(usage_entries, "tokens"),
        "compute_seconds": _sum_usage_field(usage_entries, "compute_seconds"),
        "extraction_seconds": (
            round(sum(extraction_seconds_values), 3) if extraction_seconds_values else None
        ),
    }


def _build_cost_report(
    *,
    schema_version: str,
    run_id: str,
    execution_mode: str,
    config: Any,
    pages_extracted: int,
    tokens_total: int,
    usage_entries: list[dict[str, Any]],
    estimated_cost_usd: float,
    cost_is_partial: bool,
    cost_bases: set[str],
    extraction_seconds_values: list[float],
) -> dict[str, Any]:
    report = {
        "schema_version": schema_version,
        "run_id": run_id,
        "execution_mode": execution_mode,
        "currency": "USD",
        "canonical_unit": "pages",
        "pages_extracted": pages_extracted,
        "tokens_total": tokens_total,
        "pricing": config.pricing,
        "usage": _usage_rollup(usage_entries, extraction_seconds_values),
        "cost_usd": None if cost_is_partial and estimated_cost_usd == 0.0 else estimated_cost_usd,
        "cost_known": not cost_is_partial,
        "cost_basis": _cost_basis(cost_bases),
    }
    budget = _budget_report(
        config=config,
        pages_total=pages_extracted,
        tokens_total=tokens_total,
        estimated_cost_usd=estimated_cost_usd,
    )
    if budget:
        report["budget"] = budget
    return report


# Budget caps, in (config attr, label, current-value) form. Pages is the
# canonical unit; tokens and dollars are enforced when the config sets them.
def _budget_caps(
    config: Any, *, pages_total: int, tokens_total: int, estimated_cost_usd: float
) -> list[tuple[str, float, float]]:
    caps: list[tuple[str, float, float]] = []
    if config.budget_max_pages is not None:
        caps.append(("pages", config.budget_max_pages, pages_total))
    if config.budget_max_tokens is not None:
        caps.append(("tokens", config.budget_max_tokens, tokens_total))
    if config.budget_max_usd is not None:
        caps.append(("usd", config.budget_max_usd, estimated_cost_usd))
    return caps


def _preflight_budget_error(*, config: Any, pages_total: int) -> str | None:
    if config.budget_max_pages is None or pages_total <= config.budget_max_pages:
        return None
    return (
        "Budget exceeded before extraction: "
        f"pages={pages_total} max_pages={config.budget_max_pages}"
    )


def _budget_report(
    *, config: Any, pages_total: int, tokens_total: int, estimated_cost_usd: float
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    warn_at_percent = config.budget_warn_at_percent
    for unit, cap, current in _budget_caps(
        config,
        pages_total=pages_total,
        tokens_total=tokens_total,
        estimated_cost_usd=estimated_cost_usd,
    ):
        entry: dict[str, Any] = {"max": cap, "current": current, "exceeded": current > cap}
        if warn_at_percent is not None:
            warn_at = cap * (warn_at_percent / 100)
            entry.update({"warn_at": warn_at, "warning": current >= warn_at})
        report[unit] = entry
    return report


def _budget_error(
    *,
    config: Any,
    page_id: str,
    pages_total: int,
    tokens_total: int,
    estimated_cost_usd: float,
) -> str | None:
    for unit, cap, current in _budget_caps(
        config,
        pages_total=pages_total,
        tokens_total=tokens_total,
        estimated_cost_usd=estimated_cost_usd,
    ):
        if current > cap:
            return f"Budget exceeded after {page_id}: {unit}={current} max_{unit}={cap}"
    return None


def _budget_warning(
    *,
    config: Any,
    pages_total: int,
    tokens_total: int,
    estimated_cost_usd: float,
) -> str | None:
    warn_at_percent = config.budget_warn_at_percent
    if warn_at_percent is None:
        return None
    for unit, cap, current in _budget_caps(
        config,
        pages_total=pages_total,
        tokens_total=tokens_total,
        estimated_cost_usd=estimated_cost_usd,
    ):
        warn_at = cap * (warn_at_percent / 100)
        if current >= warn_at:
            return f"{unit}={current} warn_at_{unit}={warn_at}"
    return None


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
    if result.confidence is not None and not isinstance(result.confidence, (int, float)):
        raise ValueError(f"Adapter '{adapter_name}' confidence must be a number or null")
    if not isinstance(result.warnings, list):
        raise ValueError(f"Adapter '{adapter_name}' warnings must be a list")
    if not isinstance(result.usage, dict):
        raise ValueError(f"Adapter '{adapter_name}' usage must be a mapping")
    _require_json_serializable(
        f"Adapter '{adapter_name}' usage must be JSON-serializable",
        result.usage,
    )
    pages = result.usage.get("pages")
    if pages != 1:
        raise ValueError(
            f"Adapter '{adapter_name}' usage.pages must be exactly 1 "
            f"(the page is the canonical unit; each extract() call handles one page). "
            f"Got: {pages!r}"
        )
    tokens = result.usage.get("tokens")
    if tokens is not None and (not isinstance(tokens, int) or isinstance(tokens, bool)):
        raise ValueError(f"Adapter '{adapter_name}' usage.tokens must be an integer or null")
    compute_seconds = result.usage.get("compute_seconds")
    if compute_seconds is not None and not isinstance(compute_seconds, (int, float)):
        raise ValueError(
            f"Adapter '{adapter_name}' usage.compute_seconds must be a number or null"
        )
    cost_usd = result.usage.get("cost_usd")
    if cost_usd is not None and not isinstance(cost_usd, (int, float)):
        raise ValueError(f"Adapter '{adapter_name}' usage.cost_usd must be a number or null")


def _canonical_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "pages": usage["pages"],
        "tokens": usage.get("tokens"),
        "compute_seconds": usage.get("compute_seconds"),
        "cost_usd": usage.get("cost_usd"),
    }


def _require_json_serializable(message: str, value: Any) -> None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc


def _sum_usage_field(usage_entries: list[dict[str, Any]], field: str) -> int | float | None:
    values: list[int | float] = []
    for usage in usage_entries:
        value = usage.get(field)
        if isinstance(value, (int, float)):
            values.append(value)
    if not values:
        return None
    return sum(values)


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
        "extraction_seconds": extraction_seconds,
        "timestamp": timestamp,
    }


def _build_quality_entry(
    *,
    schema_version: str,
    page: dict[str, Any],
    source: Path,
    result: Any,
    adapter: Any,
) -> dict[str, Any]:
    text = _quality_text(result.content)
    character_count = len(text)
    word_count = len(re.findall(r"\w+", text))
    warnings: list[str] = []
    if character_count == 0:
        warnings.append("empty_text")
    elif character_count < 10:
        warnings.append("short_text")
    text_quality = _text_quality_metrics(text, character_count=character_count)
    warnings.extend(_text_quality_warnings(text_quality))
    confidence_detail = getattr(result, "confidence_detail", None)
    if _has_low_confidence_tail(confidence_detail):
        warnings.append("low_confidence")
    embedded = _embedded_text_quality(source, page["page_number"], adapter)
    delta: dict[str, Any] | None = None
    if embedded is not None:
        embedded_chars = len(embedded)
        char_delta = character_count - embedded_chars
        ratio = None if embedded_chars == 0 else round(character_count / embedded_chars, 4)
        delta = {
            "embedded_character_count": embedded_chars,
            "character_delta": char_delta,
            "character_ratio": ratio,
        }
        if embedded_chars > 0 and (ratio is not None and (ratio < 0.5 or ratio > 1.8)):
            warnings.append("suspicious_embedded_text_delta")
    return {
        "schema_version": schema_version,
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "adapter": adapter.name,
        "character_count": character_count,
        "word_count": word_count,
        "confidence": result.confidence,
        "confidence_detail": confidence_detail,
        "warnings": warnings,
        "text_quality": text_quality,
        "embedded_text_comparison": delta,
    }


def _has_low_confidence_tail(detail: Any) -> bool:
    """True when engine-native word confidences show a weak tail.

    A quarter of the words under confidence 60 flags the page; a mean can
    hide one illegible paragraph on an otherwise clean page. Requires 10+
    words — less is not enough evidence to warn on.
    """
    if not isinstance(detail, dict):
        return False
    ratio = detail.get("below_60_ratio")
    word_count = detail.get("word_count")
    return (
        isinstance(ratio, (int, float))
        and isinstance(word_count, int)
        and word_count >= 10
        and ratio >= 0.25
    )


def _quality_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


# Letters abolished by the 1918 Russian orthographic reform. \u0456 is deliberately
# absent: it is standard modern Ukrainian and Belarusian.
_PREREFORM_LETTERS = frozenset("\u0463\u0462\u0473\u0472\u0475\u0474")
# Word-final hard sign \u2014 mandatory before 1918, absent from modern Russian.
# OCR models trained on modern text destroy the abolished letters but keep \u044a,
# so this is the pre-reform signal that survives extraction.
_TERMINAL_HARD_SIGN = re.compile(r"[\u044a\u042a](?![^\W\d_])")


def _text_quality_metrics(text: str, *, character_count: int) -> dict[str, Any]:
    replacement_character_count = text.count("\ufffd")
    control_character_count = sum(
        1 for char in text if ord(char) < 32 and char not in {"\n", "\r", "\t", "\f"}
    )
    suspicious_symbol_count = sum(1 for char in text if _is_suspicious_symbol(char))
    token_lengths = [len(token) for token in re.findall(r"[^\W\d_]+", text)]
    alpha_token_count = len(token_lengths)
    return {
        "replacement_character_count": replacement_character_count,
        "control_character_count": control_character_count,
        "suspicious_symbol_count": suspicious_symbol_count,
        "suspicious_symbol_ratio": (
            0.0
            if character_count == 0
            else round(suspicious_symbol_count / character_count, 4)
        ),
        # Lexical shape of the output. Language-neutral evidence: sort pages
        # by mean_token_length to find fragment noise. These metrics cannot
        # detect word-level misrecognition ("matericl" for "material") \u2014
        # that needs a dictionary or model, which PageLedger does not ship.
        "alpha_token_count": alpha_token_count,
        "mean_token_length": (
            None
            if alpha_token_count == 0
            else round(sum(token_lengths) / alpha_token_count, 2)
        ),
        "short_token_ratio": (
            None
            if alpha_token_count == 0
            else round(
                sum(1 for length in token_lengths if length <= 2) / alpha_token_count, 4
            )
        ),
        "prereform_letter_count": sum(1 for char in text if char in _PREREFORM_LETTERS),
        "terminal_hard_sign_count": len(_TERMINAL_HARD_SIGN.findall(text)),
    }


def _text_quality_warnings(metrics: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if metrics["replacement_character_count"] > 0:
        warnings.append("replacement_characters")
    if metrics["control_character_count"] > 0:
        warnings.append("control_characters")
    if metrics["suspicious_symbol_ratio"] >= 0.03 and metrics["suspicious_symbol_count"] >= 5:
        warnings.append("suspicious_symbol_density")
    mean_token_length = metrics["mean_token_length"]
    if (
        mean_token_length is not None
        and mean_token_length < 3.0
        and metrics["alpha_token_count"] >= 20
    ):
        # Real prose in tested corpora sits above 4; OCR fragment noise
        # ("l| ||| l|l ll") collapses toward 1.
        warnings.append("fragmented_text")
    if metrics["prereform_letter_count"] >= 2 or (
        metrics["alpha_token_count"] >= 20
        and metrics["terminal_hard_sign_count"] >= 2
        and metrics["terminal_hard_sign_count"] >= metrics["alpha_token_count"] / 100
    ):
        # Pre-1918 Russian orthography: the configured OCR model is probably
        # mismatched with the page. Measured on an 1850 gubernia review:
        # 21 terminal hard signs per 100 tokens vs 0.00 in modern text.
        warnings.append("historical_orthography")
    return warnings


def _is_suspicious_symbol(char: str) -> bool:
    if char in {"_", "|", "\\", "/", "{", "}", "[", "]", "•"}:
        return True
    if char.isalnum() or char.isspace():
        return False
    if char in ".,;:!?()'\"-$%&+=*#@<>":
        return False
    if char in "«»„“”‘’‚—–…·§№°":
        # Standard European/Cyrillic typography, not extraction garble.
        return False
    return not char.isascii()


def _embedded_text_quality(source: Path, page_number: int, adapter: Any) -> str | None:
    if source.suffix.lower() != ".pdf":
        return None
    if "embedded_text" in getattr(adapter, "capabilities", ()):
        return None
    try:
        from .adapters import _pdf_page_text

        return _pdf_page_text(source, page_number)
    except Exception:
        return None


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
