"""Artifact builders for filesystem-native PageLedger runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ._version import __version__
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


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write a list of dicts as JSONL (one JSON object per line)."""
    path.write_text(
        "".join(json.dumps(e, sort_keys=True, allow_nan=False) + "\n" for e in entries),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSONL objects, ignoring blank lines."""
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def build_route_map(
    *,
    schema_version: str,
    run_id: str,
    generated_at: str,
    documents: list[dict[str, Any]],
    classifier: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "pageledger_version": __version__,
        "run_id": run_id,
        "generated_at": generated_at,
        "classifier": classifier or {
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
    pages_failed: int = 0,
    pages_not_attempted: int = 0,
    pages_skipped: int = 0,
    pages_routed_review: int = 0,
    pages_quarantined: int = 0,
    records_normalized: int = 0,
    estimated_cost_usd: float = 0.0,
    quality_warning_pages: int = 0,
    status: str = "partial",
    extractors: list[dict[str, Any]] | None = None,
    routing: dict[str, Any] | None = None,
    escalation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": schema_version,
        "pageledger_version": __version__,
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
            "pages_routed_review": pages_routed_review,
            "pages_quarantined": pages_quarantined,
            "records_normalized": records_normalized,
            "estimated_cost_usd": estimated_cost_usd,
            "quality_warning_pages": quality_warning_pages,
        },
    }
    if pages_failed:
        manifest["summary"]["pages_failed"] = pages_failed
    if pages_not_attempted:
        manifest["summary"]["pages_not_attempted"] = pages_not_attempted
    if routing is not None:
        manifest["routing"] = routing
    if escalation is not None:
        manifest["escalation"] = escalation
    return manifest


def build_audit(
    *,
    schema_version: str,
    run_id: str,
    review_queue: list[dict[str, Any]],
    quarantine_queue: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "run_id": run_id,
        "review_queue": review_queue,
        "quarantine_queue": quarantine_queue,
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
    quarantined_page_ids: set[str] | None = None,
    run_depth: int = 0,
    grades: dict[str, str] | None = None,
    escalation: dict[str, Any] | None = None,
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
    - ``chain_exhausted`` — candidates remain, but no later adapter is
      configured; the review queue remains the terminal human state.
    - ``no_further_generations`` — the configured depth cap forbids another
      generation.
    """
    sources_by_page = {
        page["page_id"]: document["source"]
        for document in route_map["documents"]
        for page in document["pages"]
    }
    quarantined = set(quarantined_page_ids or ())
    quarantined.update(
        item["page_id"] for item in audit.get("quarantine_queue", [])
    )
    # A page can sit in the review queue once per reason (e.g. both
    # quality_warning and grade_below_threshold); candidate construction is
    # independent of the depth/chain gates so exhaustion remains observable.
    by_page: dict[str, dict[str, Any]] = {}
    for page in audit["review_queue"]:
        page_id = page["page_id"]
        if page_id in quarantined:
            continue
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
    candidates = list(by_page.values())
    if run_depth >= max_rerun_depth:
        rerun_status = "no_further_generations"
    elif not candidates:
        rerun_status = "empty_queue"
    elif escalation is not None and escalation.get("next_adapter") is None:
        rerun_status = "chain_exhausted"
    else:
        rerun_status = "executable"
    items = candidates if rerun_status == "executable" else []
    manifest = {
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
    if escalation is not None:
        manifest["escalation"] = escalation
    return manifest


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
