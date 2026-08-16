"""Cross-run comparison for PageLedger run directories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifacts import read_jsonl
from .grading import format_grade, grade_is_below


def compare_runs(run_dir_a: Path, run_dir_b: Path) -> dict[str, Any]:
    """Compare two run directories page-by-page.

    Pages line up on ``page_id``, but changes are ranked only when provenance
    proves that both entries refer to the same source bytes, source page, and
    effective extractor identity. Reused identifiers and legacy runs with
    incomplete evidence stay visible as incomparable pages.
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
    pages_comparable = 0
    identity_mismatches: list[str] = []
    for page_id in common:
        qa = a["quality"][page_id]
        qb = b["quality"][page_id]
        provenance_a = a["provenance"].get(page_id, {})
        provenance_b = b["provenance"].get(page_id, {})
        source_a = provenance_a.get("source")
        source_b = provenance_b.get("source")
        source_status = _source_status(source_a, source_b)
        if source_status in {"changed", "different"}:
            identity_mismatches.append(page_id)
        extractor_a = provenance_a.get("extractor", {})
        extractor_b = provenance_b.get("extractor", {})
        adapter_a = qa.get("adapter") or extractor_a.get("adapter")
        adapter_b = qb.get("adapter") or extractor_b.get("adapter")
        effective_extractor_a = _effective_extractor_identity(adapter_a, extractor_a)
        effective_extractor_b = _effective_extractor_identity(adapter_b, extractor_b)
        if source_status in {"changed", "different"}:
            comparability = "incomparable_source"
        elif source_status == "unknown" or not adapter_a or not adapter_b:
            comparability = "incomparable_unknown"
        elif adapter_a != adapter_b:
            comparability = "incomparable_adapter"
        elif effective_extractor_a is None or effective_extractor_b is None:
            comparability = "incomparable_unknown"
        elif effective_extractor_a != effective_extractor_b:
            comparability = "incomparable_extractor"
        else:
            comparability = "comparable"
            pages_comparable += 1
        set_a = set(qa.get("warnings", []))
        set_b = set(qb.get("warnings", []))
        resolved = sorted(set_a - set_b)
        introduced = sorted(set_b - set_a)
        if comparability == "comparable":
            warnings_resolved += len(resolved)
            warnings_introduced += len(introduced)
        grade_a = qa.get("grade")
        grade_b = qb.get("grade")
        if (
            comparability == "comparable"
            and grade_a is not None
            and grade_b is not None
            and grade_a != grade_b
        ):
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
                "word_delta": _delta(qa.get("word_count"), qb.get("word_count")),
                "extraction_seconds_a": provenance_a.get("extraction_seconds"),
                "extraction_seconds_b": provenance_b.get("extraction_seconds"),
                "extraction_seconds_delta": _delta(
                    provenance_a.get("extraction_seconds"),
                    provenance_b.get("extraction_seconds"),
                ),
                "warnings_a": sorted(set_a),
                "warnings_b": sorted(set_b),
                "warnings_resolved": resolved,
                "warnings_introduced": introduced,
                "grade_a": grade_a,
                "grade_b": grade_b,
                "grade_basis_a": qa.get("grade_basis"),
                "grade_basis_b": qb.get("grade_basis"),
                "source_a": source_a,
                "source_b": source_b,
                "source_status": source_status,
                "adapter_a": adapter_a,
                "adapter_b": adapter_b,
                "effective_extractor_a": effective_extractor_a,
                "effective_extractor_b": effective_extractor_b,
                "comparability": comparability,
            }
        )

    return {
        "run_a": _run_summary(a),
        "run_b": _run_summary(b),
        "pages_compared": len(common),
        "pages_comparable_total": pages_comparable,
        "pages_incomparable_total": len(common) - pages_comparable,
        "page_identity_mismatches": identity_mismatches,
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
        f"Comparable pages: {report.get('pages_comparable_total', report['pages_compared'])}"
        f" / incomparable {report.get('pages_incomparable_total', 0)}",
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
                "| page_id | comparable | chars A→B | grade A→B | resolved | introduced |",
                "|---|---|---|---|---|---|",
            ]
        )
        for page in changed[:50]:
            lines.append(
                f"| {page['page_id']}"
                f" | {page.get('comparability', 'comparable')}"
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

    quality = {
        entry["page_id"]: entry for entry in read_jsonl(out_dir / "quality.jsonl")
    }

    cost: dict[str, Any] = {}
    cost_path = out_dir / "cost.json"
    if cost_path.is_file():
        cost = json.loads(cost_path.read_text(encoding="utf-8"))

    provenance = {
        entry["page_id"]: entry for entry in read_jsonl(out_dir / "provenance.jsonl")
    }

    return {
        "dir": out_dir,
        "manifest": manifest,
        "quality": quality,
        "provenance": provenance,
        "cost": cost,
    }


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


def _delta(value_a: Any, value_b: Any) -> int | float | None:
    if (
        isinstance(value_a, (int, float))
        and not isinstance(value_a, bool)
        and isinstance(value_b, (int, float))
        and not isinstance(value_b, bool)
    ):
        return value_b - value_a
    return None


def _source_identity(source: Any) -> tuple[str, int] | None:
    if not isinstance(source, dict):
        return None
    sha256 = source.get("sha256")
    page_number = source.get("page_number")
    if (
        not isinstance(sha256, str)
        or not sha256
        or not isinstance(page_number, int)
        or isinstance(page_number, bool)
    ):
        return None
    return sha256, page_number


def _source_status(source_a: Any, source_b: Any) -> str:
    identity_a = _source_identity(source_a)
    identity_b = _source_identity(source_b)
    if identity_a is None or identity_b is None:
        return "unknown"
    if identity_a == identity_b:
        return "same"
    if identity_a[1] != identity_b[1]:
        return "different"
    path_a = source_a.get("path")
    path_b = source_b.get("path")
    if isinstance(path_a, str) and isinstance(path_b, str) and path_a == path_b:
        return "changed"
    return "different"


def _effective_extractor_identity(
    adapter: Any, extractor: Any
) -> dict[str, Any] | None:
    """Return normalized evidence that two outputs used the same extractor."""
    if not isinstance(adapter, str) or not adapter or not isinstance(extractor, dict):
        return None

    required = (
        "adapter_version",
        "model",
        "prompt_hash",
        "deterministic",
        "input_types",
        "output_types",
        "capabilities",
    )
    if any(field not in extractor for field in required):
        return None

    adapter_version = extractor["adapter_version"]
    model = extractor["model"]
    prompt_hash = extractor["prompt_hash"]
    deterministic = extractor["deterministic"]
    if not isinstance(adapter_version, str) or not adapter_version:
        return None
    if model is not None and not isinstance(model, str):
        return None
    if prompt_hash is not None and not isinstance(prompt_hash, str):
        return None
    if not isinstance(deterministic, bool):
        return None

    normalized_lists: dict[str, list[str]] = {}
    for field in ("input_types", "output_types", "capabilities"):
        values = extractor[field]
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
            return None
        normalized_lists[field] = sorted(values)

    return {
        "adapter": adapter,
        "adapter_version": adapter_version,
        "model": model,
        "prompt_hash": prompt_hash,
        "deterministic": deterministic,
        **normalized_lists,
    }


def _cost_line(summary: dict[str, Any]) -> str:
    if summary.get("cost_usd") is None:
        return "unknown"
    basis = summary.get("cost_basis")
    suffix = f" ({basis})" if basis else ""
    return f"${summary['cost_usd']}{suffix}"
