"""Cross-run comparison for PageLedger run directories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .grading import format_grade, grade_is_below


def compare_runs(run_dir_a: Path, run_dir_b: Path) -> dict[str, Any]:
    """Compare two run directories page-by-page.

    Pages are matched on ``page_id``, which reruns preserve, so an original
    run and its rerun (or two runs of the same inputs with different
    adapters) line up directly. Returns a machine-readable report; raises
    ValueError when either directory is not a run directory.
    """
    a = _load_run(run_dir_a, label="A")
    b = _load_run(run_dir_b, label="B")

    ids_a = set(a["quality"])
    ids_b = set(b["quality"])
    common = sorted(ids_a & ids_b)

    pages: list[dict[str, Any]] = []
    warnings_resolved = 0
    warnings_introduced = 0
    grades_improved = 0
    grades_regressed = 0
    for page_id in common:
        qa = a["quality"][page_id]
        qb = b["quality"][page_id]
        set_a = set(qa.get("warnings", []))
        set_b = set(qb.get("warnings", []))
        resolved = sorted(set_a - set_b)
        introduced = sorted(set_b - set_a)
        warnings_resolved += len(resolved)
        warnings_introduced += len(introduced)
        grade_a = qa.get("grade")
        grade_b = qb.get("grade")
        if grade_a is not None and grade_b is not None and grade_a != grade_b:
            if grade_is_below(grade_a, grade_b):
                grades_improved += 1
            else:
                grades_regressed += 1
        pages.append(
            {
                "page_id": page_id,
                "character_count_a": qa.get("character_count"),
                "character_count_b": qb.get("character_count"),
                "character_delta": _delta(
                    qa.get("character_count"), qb.get("character_count")
                ),
                "word_count_a": qa.get("word_count"),
                "word_count_b": qb.get("word_count"),
                "warnings_a": sorted(set_a),
                "warnings_b": sorted(set_b),
                "warnings_resolved": resolved,
                "warnings_introduced": introduced,
                "grade_a": grade_a,
                "grade_b": grade_b,
                "grade_basis_a": qa.get("grade_basis"),
                "grade_basis_b": qb.get("grade_basis"),
            }
        )

    return {
        "run_a": _run_summary(a),
        "run_b": _run_summary(b),
        "pages_compared": len(common),
        "pages_only_in_a": sorted(ids_a - ids_b),
        "pages_only_in_b": sorted(ids_b - ids_a),
        "warning_pages_a": sum(1 for q in a["quality"].values() if q.get("warnings")),
        "warning_pages_b": sum(1 for q in b["quality"].values() if q.get("warnings")),
        "warnings_resolved_total": warnings_resolved,
        "warnings_introduced_total": warnings_introduced,
        "grades_improved_total": grades_improved,
        "grades_regressed_total": grades_regressed,
        "pages": pages,
    }


def render_comparison(report: dict[str, Any]) -> str:
    """Render a comparison report as human-readable text."""
    a = report["run_a"]
    b = report["run_b"]
    lines = [
        f"Run A: {a['run_id']}  [{', '.join(a['adapters']) or 'no adapter'}]"
        f"  ({a['run_dir']})",
        f"Run B: {b['run_id']}  [{', '.join(b['adapters']) or 'no adapter'}]"
        f"  ({b['run_dir']})",
        "",
        f"Pages compared: {report['pages_compared']}"
        + (
            f" (only in A: {len(report['pages_only_in_a'])},"
            f" only in B: {len(report['pages_only_in_b'])})"
            if report["pages_only_in_a"] or report["pages_only_in_b"]
            else ""
        ),
        f"Warning pages: A={report['warning_pages_a']} B={report['warning_pages_b']}",
        f"Warnings resolved in B: {report['warnings_resolved_total']}",
        f"Warnings introduced in B: {report['warnings_introduced_total']}",
        f"Grades: improved {report['grades_improved_total']}"
        f" / regressed {report['grades_regressed_total']}",
        f"Cost: A={_cost_line(a)} B={_cost_line(b)}",
    ]
    changed = [
        page
        for page in report["pages"]
        if page["warnings_resolved"]
        or page["warnings_introduced"]
        or (
            page["grade_a"] is not None
            and page["grade_b"] is not None
            and page["grade_a"] != page["grade_b"]
        )
    ]
    if changed:
        lines.extend(
            [
                "",
                "Pages with warning or grade changes:",
                "| page_id | chars A→B | grade A→B | resolved | introduced |",
                "|---|---|---|---|---|",
            ]
        )
        for page in changed[:50]:
            lines.append(
                f"| {page['page_id']}"
                f" | {page['character_count_a']}→{page['character_count_b']}"
                f" | {_grade_transition(page)}"
                f" | {', '.join(page['warnings_resolved']) or '-'}"
                f" | {', '.join(page['warnings_introduced']) or '-'} |"
            )
        if len(changed) > 50:
            lines.append(f"... and {len(changed) - 50} more (use --json for all pages)")
    return "\n".join(lines) + "\n"


def _load_run(run_dir: Path, *, label: str) -> dict[str, Any]:
    out_dir = run_dir.expanduser().resolve()
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Run {label}: no manifest.json found in {out_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    quality: dict[str, dict[str, Any]] = {}
    quality_path = out_dir / "quality.jsonl"
    if quality_path.is_file():
        for line in quality_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                quality[entry["page_id"]] = entry

    cost: dict[str, Any] = {}
    cost_path = out_dir / "cost.json"
    if cost_path.is_file():
        cost = json.loads(cost_path.read_text(encoding="utf-8"))

    return {"dir": out_dir, "manifest": manifest, "quality": quality, "cost": cost}


def _run_summary(loaded: dict[str, Any]) -> dict[str, Any]:
    manifest = loaded["manifest"]
    cost = loaded["cost"]
    return {
        "run_dir": str(loaded["dir"]),
        "run_id": manifest["run_id"],
        "parent_run_id": manifest.get("parent_run_id"),
        "status": manifest.get("status"),
        "execution_mode": manifest.get("execution_mode"),
        "adapters": sorted(
            {extractor.get("adapter", "?") for extractor in manifest.get("extractors", [])}
        ),
        "pages_extracted": manifest.get("summary", {}).get("pages_extracted"),
        "cost_usd": cost.get("cost_usd"),
        "cost_known": cost.get("cost_known"),
        "cost_basis": cost.get("cost_basis"),
    }


def _grade_transition(page: dict[str, Any]) -> str:
    if page["grade_a"] is None and page["grade_b"] is None:
        return "-"
    left = format_grade(page["grade_a"], page["grade_basis_a"]) or "?"
    right = format_grade(page["grade_b"], page["grade_basis_b"]) or "?"
    return f"{left}→{right}"


def _delta(value_a: Any, value_b: Any) -> int | None:
    if isinstance(value_a, int) and isinstance(value_b, int):
        return value_b - value_a
    return None


def _cost_line(summary: dict[str, Any]) -> str:
    if summary.get("cost_usd") is None:
        return "unknown"
    basis = summary.get("cost_basis")
    suffix = f" ({basis})" if basis else ""
    return f"${summary['cost_usd']}{suffix}"
