"""Read-only reports for completed PageLedger runs."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .grading import grade_distribution


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

    # Grade distribution ({} for pre-grading runs — entries without grades)
    quality_path = out_dir / "quality.jsonl"
    graded_entries: list[dict[str, Any]] = []
    if quality_path.is_file():
        graded_entries = [
            json.loads(line)
            for line in quality_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    distribution = grade_distribution(graded_entries)
    if not any(distribution.values()):
        distribution = {}

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
        "records_normalized": summary.get("records_normalized", 0),
        "grade_distribution": distribution,
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
        "confidence", "warnings", "grade", "grade_basis", "cost_usd",
        "extraction_seconds",
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
            "grade": quality.get("grade"),
            "grade_basis": quality.get("grade_basis"),
            "cost_usd": page_provenance.get("usage", {}).get("cost_usd"),
            "extraction_seconds": page_provenance.get("extraction_seconds"),
        })
    return buffer.getvalue()
