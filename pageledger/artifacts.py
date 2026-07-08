"""Artifact builders for filesystem-native PageLedger runs."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .grading import format_grade

ARTIFACT_PATHS = {
    "config_snapshot": "config-snapshot.yml",
    "route_map": "route-map.yml",
    "raw_dir": "raw/",
    "normalized_dir": "normalized/",
    "audit": "audit.json",
    "audit_md": "audit.md",
    "provenance": "provenance.jsonl",
    "quality": "quality.jsonl",
    "cost": "cost.json",
    "run_log": "run.log",
    "rerun_manifest": "rerun-manifest.yml",
}


def write_json(
    path: Path, data: dict[str, Any], *, sort_keys: bool = True, atomic: bool = False
) -> None:
    _write_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n",
        atomic=atomic,
    )


def write_jsonl(path: Path, entries: list[dict[str, Any]], *, atomic: bool = False) -> None:
    """Write a list of dicts as JSONL (one JSON object per line)."""
    _write_text(
        path,
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries),
        atomic=atomic,
    )


def write_yaml(path: Path, data: dict[str, Any], *, atomic: bool = False) -> None:
    _write_text(
        path,
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        atomic=atomic,
    )


def _write_text(path: Path, text: str, *, atomic: bool) -> None:
    """Write text, optionally via temp-file-and-replace.

    Atomic mode is for `pageledger align`, which rewrites artifacts inside
    an existing run directory: a crash mid-write must never leave a
    half-written artifact behind.
    """
    if not atomic:
        path.write_text(text, encoding="utf-8")
        return
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def build_route_map(
    *,
    schema_version: str,
    run_id: str,
    generated_at: str,
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "run_id": run_id,
        "generated_at": generated_at,
        "classifier": {
            "adapter": None,
            "model": None,
            "prompt_hash": None,
        },
        "documents": documents,
    }


def build_manifest(
    *,
    schema_version: str,
    run_id: str,
    execution_mode: str,
    started_at: str,
    completed_at: str,
    inputs: list[dict[str, Any]],
    config_sha256: str,
    config_source_paths: list[str],
    dataset_citation: dict[str, str] | None,
    pages_total: int,
    parent_run_id: str | None = None,
    pages_extracted: int = 0,
    pages_skipped: int = 0,
    pages_quarantined: int = 0,
    records_normalized: int = 0,
    estimated_cost_usd: float = 0.0,
    quality_warning_pages: int = 0,
    status: str = "partial",
    extractors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "execution_mode": execution_mode,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "inputs": inputs,
        "config": {
            "path": ARTIFACT_PATHS["config_snapshot"],
            "sha256": config_sha256,
            "source_paths": config_source_paths,
        },
        "extractors": extractors or [],
        "dataset_citation": dataset_citation,
        "artifacts": ARTIFACT_PATHS,
        "summary": {
            "pages_total": pages_total,
            "pages_extracted": pages_extracted,
            "pages_skipped": pages_skipped,
            "pages_quarantined": pages_quarantined,
            "records_normalized": records_normalized,
            "estimated_cost_usd": estimated_cost_usd,
            "quality_warning_pages": quality_warning_pages,
        },
    }


def build_audit(
    *,
    schema_version: str,
    run_id: str,
    review_queue: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "run_id": run_id,
        "review_queue": review_queue,
        "quarantine_queue": [],
    }


def build_rerun_manifest(
    *,
    schema_version: str,
    run_id: str,
    parent_run_id: str,
    created_at: str,
    max_rerun_depth: int,
    reason: str,
    audit: dict[str, Any],
    route_map: dict[str, Any],
    run_depth: int = 0,
    grades: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Generate a rerun manifest consumable by ``pageledger rerun``.

    The manifest lists pages from ``audit.review_queue`` that are candidates
    for re-extraction. ``rerun_depth`` records the depth of the generating
    run (0 for an original run, N for its Nth rerun generation). When the
    next generation would exceed ``max_rerun_depth``, items are omitted and
    the status is ``no_further_generations``.

    ``rerun_status`` is one of:

    - ``executable`` — items present and a further generation is allowed;
      ``pageledger rerun`` will consume this manifest.
    - ``empty_queue`` — nothing needed review, so there is nothing to rerun.
    - ``no_further_generations`` — the configured depth cap forbids another
      generation.
    """
    sources_by_page = {
        page["page_id"]: document["source"]
        for document in route_map["documents"]
        for page in document["pages"]
    }
    allowed = run_depth < max_rerun_depth
    items: list[dict[str, Any]] = []
    if allowed:
        # A page can sit in the review queue once per reason (e.g. both
        # quality_warning and grade_below_threshold); the rerun manifest
        # lists it once, with the reasons joined.
        by_page: dict[str, dict[str, Any]] = {}
        for page in audit["review_queue"]:
            page_id = page["page_id"]
            existing = by_page.get(page_id)
            if existing is not None:
                if page["reason"] not in existing["reason"].split("+"):
                    existing["reason"] += f"+{page['reason']}"
                continue
            by_page[page_id] = {
                "page_id": page_id,
                "page_number": page["page_number"],
                "source": sources_by_page[page_id],
                "action": page["action"],
                "reason": page["reason"],
                "previous_grade": (grades or {}).get(page_id),
            }
        items = list(by_page.values())
    if not allowed:
        rerun_status = "no_further_generations"
    elif items:
        rerun_status = "executable"
    else:
        rerun_status = "empty_queue"
    return {
        "schema_version": schema_version,
        "run_id": f"{run_id}-rerun",
        "parent_run_id": parent_run_id,
        "parent_manifest": "manifest.json",
        "rerun_depth": run_depth,
        "max_rerun_depth": max_rerun_depth,
        "created_at": created_at,
        "reason": reason,
        "rerun_executable": rerun_status == "executable",
        "rerun_status": rerun_status,
        "items": items,
    }


def render_audit_markdown(audit: dict[str, Any]) -> str:
    review_queue = audit.get("review_queue", [])
    quarantine_queue = audit.get("quarantine_queue", [])
    lines = [
        "# PageLedger Audit",
        "",
        f"Run `{audit['run_id']}`",
        "",
        f"- Review queue: {len(review_queue)}",
        f"- Quarantine queue: {len(quarantine_queue)}",
    ]
    lines.extend(_render_queue("Review Queue", review_queue))
    lines.extend(_render_queue("Quarantine Queue", quarantine_queue))
    return "\n".join(lines) + "\n"


def _render_queue(title: str, queue: list[dict[str, Any]]) -> list[str]:
    if not queue:
        return ["", f"## {title}", "", "None."]
    lines = [
        "",
        f"## {title}",
        "",
        "| page_id | page_number | type | action | reason | grade |",
        "|---|---:|---|---|---|---|",
    ]
    for item in queue:
        lines.append(
            f"| {item.get('page_id', '')} | {item.get('page_number', '')}"
            f" | {item.get('type', '')} | {item.get('action', '')}"
            f" | {item.get('reason', '')}"
            f" | {format_grade(item.get('grade'), item.get('grade_basis'))} |"
        )
    return lines
