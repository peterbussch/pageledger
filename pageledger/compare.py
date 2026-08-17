"""Cross-run comparison for PageLedger run directories."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .artifacts import read_jsonl
from .grading import format_grade, grade_is_below, merge_thresholds, validate_thresholds


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
    grade_pages_comparable = 0
    grade_pages_incomparable = 0
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
        effective_extractor_a = _effective_extractor_identity(
            adapter_a,
            extractor_a,
            _manifest_extractor_options_hash(a["manifest"], extractor_a),
        )
        effective_extractor_b = _effective_extractor_identity(
            adapter_b,
            extractor_b,
            _manifest_extractor_options_hash(b["manifest"], extractor_b),
        )
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
        grade_basis_a = qa.get("grade_basis")
        grade_basis_b = qb.get("grade_basis")
        grade_generator_a = _grade_generator_identity(a["manifest"])
        grade_generator_b = _grade_generator_identity(b["manifest"])
        grade_config_identity_a = a["grade_config_identity"]
        grade_config_identity_b = b["grade_config_identity"]
        grade_schema_identity_a = _grade_schema_identity(
            a["schema_identity"], grade_basis_a
        )
        grade_schema_identity_b = _grade_schema_identity(
            b["schema_identity"], grade_basis_b
        )
        grade_comparability = _grade_comparability(
            comparability,
            grade_basis_a,
            grade_basis_b,
            grade_generator_a,
            grade_generator_b,
            grade_config_identity_a,
            grade_config_identity_b,
            grade_schema_identity_a,
            grade_schema_identity_b,
        )
        if grade_a is not None and grade_b is not None:
            if grade_comparability == "comparable":
                grade_pages_comparable += 1
            else:
                grade_pages_incomparable += 1
        if (
            grade_comparability == "comparable"
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
                "grade_basis_a": grade_basis_a,
                "grade_basis_b": grade_basis_b,
                "grade_generator_a": grade_generator_a,
                "grade_generator_b": grade_generator_b,
                "grade_config_identity_a": grade_config_identity_a,
                "grade_config_identity_b": grade_config_identity_b,
                "grade_schema_identity_a": grade_schema_identity_a,
                "grade_schema_identity_b": grade_schema_identity_b,
                "grade_comparability": grade_comparability,
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
        "grade_pages_comparable_total": grade_pages_comparable,
        "grade_pages_incomparable_total": grade_pages_incomparable,
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
        f"Grade-comparable pages: {report.get('grade_pages_comparable_total', 0)}"
        f" / incomparable {report.get('grade_pages_incomparable_total', 0)}",
        f"Warnings resolved in B: {report['warnings_resolved_total']}",
        f"Warnings introduced in B: {report['warnings_introduced_total']}",
        f"Grades (matching basis/schema only): improved {report['grades_improved_total']}"
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
                "| page_id | extraction | grade | chars A→B | grade A→B | resolved | introduced |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for page in changed[:50]:
            lines.append(
                f"| {page['page_id']}"
                f" | {page.get('comparability', 'comparable')}"
                f" | {page.get('grade_comparability', 'incomparable_unknown')}"
                f" | {page['character_count_a']}→{page['character_count_b']}"
                f" | {_grade_transition(page)}"
                f" | {', '.join(page['warnings_resolved']) or '-'}"
                f" | {', '.join(page['warnings_introduced']) or '-'} |"
            )
        if len(changed) > 50:
            lines.append(f"... and {len(changed) - 50} more (use --json for all pages)")
    return "\n".join(lines) + "\n"


def _load_run(run_dir: Path, *, label: str) -> dict[str, Any]:
    try:
        declared_dir = run_dir.expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Run {label}: run directory cannot be resolved safely") from exc
    out_dir = _safe_resolve(declared_dir)
    if out_dir is None:
        raise ValueError(f"Run {label}: run directory cannot be resolved safely")

    manifest_path = _contained_regular_file(out_dir, "manifest.json", label=label)
    assert manifest_path is not None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    quality_path = _contained_regular_file(out_dir, "quality.jsonl", label=label)
    assert quality_path is not None
    quality = {
        entry["page_id"]: entry for entry in read_jsonl(quality_path)
    }

    cost: dict[str, Any] = {}
    cost_path = _contained_regular_file(
        out_dir, "cost.json", label=label, required=False
    )
    if cost_path is not None:
        cost = json.loads(cost_path.read_text(encoding="utf-8"))

    provenance_path = _contained_regular_file(
        out_dir, "provenance.jsonl", label=label
    )
    assert provenance_path is not None
    provenance = {
        entry["page_id"]: entry for entry in read_jsonl(provenance_path)
    }
    config = _load_config_snapshot(out_dir, manifest)
    effective_schema, schema_known = _load_effective_grade_schema(
        out_dir, manifest, config
    )
    schema_identity = (
        _canonical_mapping_hash(effective_schema)
        if schema_known and effective_schema is not None
        else None
    )
    grade_config_identity = _grade_config_identity(
        config,
        effective_schema=effective_schema,
        schema_known=schema_known,
    )

    return {
        "dir": out_dir,
        "manifest": manifest,
        "quality": quality,
        "provenance": provenance,
        "cost": cost,
        "config": config,
        "schema_identity": schema_identity,
        "grade_config_identity": grade_config_identity,
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
    adapter: Any, extractor: Any, options_sha256: str | None
) -> dict[str, Any] | None:
    """Return normalized evidence that two outputs used the same extractor."""
    if (
        not isinstance(adapter, str)
        or not adapter
        or not isinstance(extractor, dict)
        or options_sha256 is None
    ):
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
        "options_sha256": options_sha256,
        **normalized_lists,
    }


def _manifest_extractor_options_hash(
    manifest: Any, page_extractor: Any
) -> str | None:
    """Resolve page provenance to one non-secret manifest options identity."""
    if not isinstance(manifest, dict) or not isinstance(page_extractor, dict):
        return None
    manifest_extractors = manifest.get("extractors")
    if not isinstance(manifest_extractors, list):
        return None

    option_hashes: set[str] = set()
    for candidate in manifest_extractors:
        if not _manifest_extractor_matches(candidate, page_extractor):
            continue
        assert isinstance(candidate, dict)
        options = candidate.get("options", {})
        if not isinstance(options, dict):
            return None
        try:
            canonical = json.dumps(
                options,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError):
            return None
        option_hashes.add(hashlib.sha256(canonical).hexdigest())
    if len(option_hashes) != 1:
        return None
    return next(iter(option_hashes))


def _manifest_extractor_matches(candidate: Any, page_extractor: dict[str, Any]) -> bool:
    if not isinstance(candidate, dict):
        return False
    scalar_fields = (
        ("adapter", "adapter"),
        ("version", "adapter_version"),
        ("model", "model"),
        ("prompt_hash", "prompt_hash"),
        ("deterministic", "deterministic"),
    )
    if any(candidate.get(left) != page_extractor.get(right) for left, right in scalar_fields):
        return False
    for field in ("input_types", "output_types", "capabilities"):
        left = candidate.get(field)
        right = page_extractor.get(field)
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        if sorted(left) != sorted(right):
            return False
    return True


def _grade_schema_identity(identity: Any, grade_basis: Any) -> str | None:
    if grade_basis != "schema_aware":
        return None
    return identity if isinstance(identity, str) and identity else None


def _grade_generator_identity(manifest: Any) -> str | None:
    if not isinstance(manifest, dict):
        return None
    alignment = manifest.get("alignment")
    if isinstance(alignment, dict):
        aligned_version = alignment.get("pageledger_version")
        if isinstance(aligned_version, str) and aligned_version:
            return aligned_version
    version = manifest.get("pageledger_version")
    return version if isinstance(version, str) and version else None


def _grade_config_identity(
    config: Any,
    *,
    effective_schema: dict[str, Any] | None = None,
    schema_known: bool = True,
) -> str | None:
    if not isinstance(config, dict):
        return None
    run_config = config.get("run")
    if not isinstance(run_config, dict):
        return None
    grading = run_config.get("grading", {})
    if not isinstance(grading, dict):
        return None
    threshold_overrides = grading.get("thresholds")
    try:
        validate_thresholds(threshold_overrides)
        thresholds = {
            axis: {grade: float(value) for grade, value in bands.items()}
            for axis, bands in merge_thresholds(threshold_overrides).items()
        }
    except (AttributeError, TypeError, ValueError):
        return None

    if not schema_known:
        return None
    schema = effective_schema or {}
    quality = schema.get("quality", {})
    if quality is None:
        quality = {}
    if not isinstance(quality, dict):
        return None
    low_confidence_floor = quality.get("low_confidence_threshold")
    if low_confidence_floor is not None and (
        isinstance(low_confidence_floor, bool)
        or not isinstance(low_confidence_floor, (int, float))
        or not 0 <= low_confidence_floor <= 1
    ):
        return None
    if low_confidence_floor is not None:
        low_confidence_floor = float(low_confidence_floor)
    return _canonical_mapping_hash(
        {
            "thresholds": thresholds,
            "low_confidence_threshold": low_confidence_floor,
        }
    )


def _canonical_mapping_hash(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(canonical).hexdigest()


def _load_config_snapshot(out_dir: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    manifest_config = manifest.get("config")
    if not isinstance(manifest_config, dict):
        return None
    relative = manifest_config.get("path")
    if not isinstance(relative, str) or not relative:
        return None
    path = _safe_resolve(out_dir / relative)
    if path is None:
        return None
    if path != out_dir and out_dir not in path.parents:
        return None
    expected_hash = manifest_config.get("sha256")
    if (
        not isinstance(expected_hash, str)
        or not _file_hash_matches(path, expected_hash)
    ):
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _load_effective_grade_schema(
    out_dir: Path,
    manifest: dict[str, Any],
    config: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, bool]:
    if not isinstance(config, dict):
        return None, False
    config_schema = config.get("schema")
    if config_schema is not None and not isinstance(config_schema, dict):
        return None, False
    alignment = manifest.get("alignment")
    if alignment is None:
        return config_schema, True
    if not isinstance(alignment, dict):
        return None, False
    source = alignment.get("schema_source")
    expected_hash = alignment.get("schema_sha256")
    if not isinstance(source, str) or not isinstance(expected_hash, str):
        return None, False
    if source == "config_snapshot":
        manifest_config = manifest.get("config")
        relative = (
            manifest_config.get("path")
            if isinstance(manifest_config, dict)
            else None
        )
        if not isinstance(relative, str):
            return None, False
        config_path = _safe_resolve(out_dir / relative)
        if config_path is None:
            return None, False
        if not _file_hash_matches(config_path, expected_hash):
            return None, False
        return config_schema, config_schema is not None

    schema_path = _safe_resolve(out_dir / "align-schema-snapshot.yml")
    if schema_path is None:
        return None, False
    if schema_path != out_dir and out_dir not in schema_path.parents:
        return None, False
    if not _file_hash_matches(schema_path, expected_hash):
        return None, False
    try:
        loaded = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None, False
    if not isinstance(loaded, dict):
        return None, False
    schema = loaded.get("schema", loaded)
    return (schema, True) if isinstance(schema, dict) else (None, False)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_hash_matches(path: Path, expected: str) -> bool:
    try:
        return path.is_file() and _file_sha256(path) == expected
    except OSError:
        return False


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


def _contained_regular_file(
    out_dir: Path,
    name: str,
    *,
    label: str,
    required: bool = True,
) -> Path | None:
    """Return a fixed run artifact only when it is regular and contained."""
    declared = out_dir / name
    if declared.is_symlink():
        raise ValueError(f"Run {label}: {name} must be a contained regular file")
    resolved = _safe_resolve(declared)
    if resolved is None or resolved.parent != out_dir:
        raise ValueError(f"Run {label}: {name} must be a contained regular file")
    if not resolved.is_file():
        if not required and not resolved.exists():
            return None
        raise ValueError(f"Run {label}: {name} must be a contained regular file")
    return resolved


def _grade_comparability(
    extraction_comparability: str,
    basis_a: Any,
    basis_b: Any,
    generator_a: str | None,
    generator_b: str | None,
    config_identity_a: str | None,
    config_identity_b: str | None,
    schema_identity_a: str | None,
    schema_identity_b: str | None,
) -> str:
    if extraction_comparability != "comparable":
        return "incomparable_extraction"
    valid_bases = {"signals_only", "schema_aware"}
    if basis_a not in valid_bases or basis_b not in valid_bases:
        return "incomparable_unknown"
    if basis_a != basis_b:
        return "incomparable_basis"
    if generator_a is None or generator_b is None:
        return "incomparable_unknown"
    if generator_a != generator_b:
        return "incomparable_generator"
    if config_identity_a is None or config_identity_b is None:
        return "incomparable_unknown"
    if config_identity_a != config_identity_b:
        return "incomparable_grade_config"
    if basis_a == "schema_aware":
        if schema_identity_a is None or schema_identity_b is None:
            return "incomparable_unknown"
        if schema_identity_a != schema_identity_b:
            return "incomparable_schema"
    return "comparable"


def _cost_line(summary: dict[str, Any]) -> str:
    if summary.get("cost_usd") is None:
        return "unknown"
    basis = summary.get("cost_basis")
    suffix = f" ({basis})" if basis else ""
    return f"${summary['cost_usd']}{suffix}"
