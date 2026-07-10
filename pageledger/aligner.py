"""Schema alignment: map structured raw page output to a declared schema.

The aligner consumes structured extraction formats (``markdown_table``,
``csv``, ``json``) and produces one normalized record file per page. It
matches source headers against declared columns by exact normalized name or
alias — no fuzzy matching. Coercion failures and failed arithmetic checks
are recorded, never silently fixed.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Formats the aligner understands. Plain text/markdown pages are not
# aligned: without a declared structure in the payload there is nothing to
# map, and guessing would violate the record-uncertainty principle.
ALIGNABLE_FORMATS = frozenset({"markdown_table", "csv", "json"})

COLUMN_TYPES = frozenset({"string", "integer", "number"})

# GFM table separator row cell: --- with optional alignment colons.
_MD_SEPARATOR_CELL = re.compile(r"^:?-+:?$")

_SCHEMA_KEYS = frozenset({"name", "columns", "checks", "quality"})
_COLUMN_KEYS = frozenset({"name", "aliases", "type", "required"})
_CHECK_KEYS = frozenset({"name", "expression", "tolerance"})
_QUALITY_KEYS = frozenset({"minimum_required_column_coverage", "low_confidence_threshold"})


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    aliases: tuple[str, ...]
    type: str
    required: bool


@dataclass(frozen=True)
class CheckSpec:
    name: str
    expression: str
    tolerance: float


@dataclass(frozen=True)
class QualitySpec:
    minimum_required_column_coverage: float | None
    low_confidence_threshold: float | None


@dataclass(frozen=True)
class SchemaSpec:
    name: str
    columns: tuple[ColumnSpec, ...]
    checks: tuple[CheckSpec, ...]
    quality: QualitySpec


def load_schema_spec(config_data: dict[str, Any]) -> SchemaSpec | None:
    """Parse and validate the config ``schema`` section.

    Returns None when the config declares no schema. Raises ValueError with
    a key-path message on any malformed field, so a bad schema fails at
    config load like every other config error.
    """
    section = config_data.get("schema")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError("schema must be a mapping")
    _reject_unknown_keys(section, _SCHEMA_KEYS, "schema")

    name = section.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("schema.name must be a non-empty string")

    raw_columns = section.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ValueError("schema.columns must be a non-empty list")

    columns: list[ColumnSpec] = []
    seen_keys: dict[str, str] = {}
    for index, raw in enumerate(raw_columns):
        prefix = f"schema.columns[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{prefix} must be a mapping")
        _reject_unknown_keys(raw, _COLUMN_KEYS, prefix)
        column_name = raw.get("name")
        if not isinstance(column_name, str) or not column_name.strip():
            raise ValueError(f"{prefix}.name must be a non-empty string")
        raw_aliases = raw.get("aliases", [])
        if not isinstance(raw_aliases, list) or not all(
            isinstance(alias, str) for alias in raw_aliases
        ):
            raise ValueError(f"{prefix}.aliases must be a list of strings")
        column_type = raw.get("type", "string")
        if column_type not in COLUMN_TYPES:
            raise ValueError(f"{prefix}.type must be one of: {', '.join(sorted(COLUMN_TYPES))}")
        required = raw.get("required", False)
        if not isinstance(required, bool):
            raise ValueError(f"{prefix}.required must be a bool")
        for key in [column_name, *raw_aliases]:
            normalized = normalize_header(key)
            if normalized in seen_keys:
                raise ValueError(
                    f"{prefix}: '{key}' collides with column "
                    f"'{seen_keys[normalized]}' after normalization"
                )
            seen_keys[normalized] = column_name
        columns.append(
            ColumnSpec(
                name=column_name,
                aliases=tuple(raw_aliases),
                type=str(column_type),
                required=required,
            )
        )

    declared_names = {column.name for column in columns}
    numeric_names = {column.name for column in columns if column.type in {"integer", "number"}}
    raw_checks = section.get("checks", [])
    if not isinstance(raw_checks, list):
        raise ValueError("schema.checks must be a list")
    checks: list[CheckSpec] = []
    seen_check_names: set[str] = set()
    for index, raw in enumerate(raw_checks):
        prefix = f"schema.checks[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{prefix} must be a mapping")
        _reject_unknown_keys(raw, _CHECK_KEYS, prefix)
        check_name = raw.get("name")
        if not isinstance(check_name, str) or not check_name.strip():
            raise ValueError(f"{prefix}.name must be a non-empty string")
        if check_name in seen_check_names:
            raise ValueError(f"{prefix}.name duplicate check name '{check_name}'")
        seen_check_names.add(check_name)
        expression = raw.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError(f"{prefix}.expression must be a non-empty string")
        tolerance = raw.get("tolerance", 0)
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
            raise ValueError(f"{prefix}.tolerance must be a non-negative number")
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError(f"{prefix}.tolerance must be a non-negative number")
        _parse_check_expression(
            expression,
            declared_names,
            numeric_names,
            key_path=f"{prefix}.expression",
        )
        checks.append(CheckSpec(name=check_name, expression=expression, tolerance=float(tolerance)))

    raw_quality = section.get("quality", {})
    if not isinstance(raw_quality, dict):
        raise ValueError("schema.quality must be a mapping")
    _reject_unknown_keys(raw_quality, _QUALITY_KEYS, "schema.quality")
    quality = QualitySpec(
        minimum_required_column_coverage=_unit_interval(
            raw_quality.get("minimum_required_column_coverage"),
            "schema.quality.minimum_required_column_coverage",
        ),
        low_confidence_threshold=_unit_interval(
            raw_quality.get("low_confidence_threshold"),
            "schema.quality.low_confidence_threshold",
        ),
    )

    return SchemaSpec(
        name=name,
        columns=tuple(columns),
        checks=tuple(checks),
        quality=quality,
    )


def normalize_header(header: str) -> str:
    """Casefold and collapse whitespace for exact header matching."""
    return " ".join(header.casefold().split())


def _reject_unknown_keys(mapping: dict[str, Any], allowed: frozenset[str], key_path: str) -> None:
    unknown = sorted(set(mapping) - allowed, key=str)
    if unknown:
        raise ValueError(f"{key_path} has unknown key '{unknown[0]}'")


def align_page(
    content: Any,
    fmt: str,
    spec: SchemaSpec,
    *,
    page: dict[str, Any],
    run_id: str,
    schema_version: str,
    raw_artifact: str,
) -> dict[str, Any] | None:
    """Align one page's extraction output against the schema.

    Returns the normalized-page record, or None for formats the aligner
    does not handle. Unparseable structured content still yields a record
    with ``records: []`` and ``metrics.parse_error`` set — the failure is
    evidence, not an exception.
    """
    if fmt not in ALIGNABLE_FORMATS:
        return None

    headers: list[str] = []
    rows: list[list[Any]] = []
    tables_found = 0
    parse_error: str | None = None
    try:
        headers, rows, tables_found = _parse_content(content, fmt)
    except _ParseError as exc:
        parse_error = str(exc)

    alias_map = {normalize_header(column.name): column for column in spec.columns}
    for column in spec.columns:
        for alias in column.aliases:
            alias_map[normalize_header(alias)] = column

    matched: dict[str, str] = {}
    matched_columns: dict[str, int] = {}
    extra: list[str] = []
    structure_issues: list[dict[str, Any]] = []
    for index, header in enumerate(headers):
        column = alias_map.get(normalize_header(header))
        if column is not None and column.name not in matched_columns:
            matched[header] = column.name
            matched_columns[column.name] = index
        else:
            extra.append(header)
            if column is not None:
                kept_header = next(
                    source for source, target in matched.items() if target == column.name
                )
                structure_issues.append(
                    {
                        "type": "duplicate_header",
                        "header": header,
                        "column": column.name,
                        "kept_header": kept_header,
                    }
                )

    for row_number, row in enumerate(rows, start=1):
        if len(row) != len(headers):
            structure_issues.append(
                {
                    "type": "row_width_mismatch",
                    "row": row_number,
                    "expected_columns": len(headers),
                    "actual_columns": len(row),
                }
            )
    tables_ignored = max(tables_found - 1, 0)
    if tables_ignored:
        structure_issues.append({"type": "ignored_table", "tables_ignored": tables_ignored})

    missing_required = [
        column.name
        for column in spec.columns
        if column.required and column.name not in matched_columns
    ]
    missing_optional = [
        column.name
        for column in spec.columns
        if not column.required and column.name not in matched_columns
    ]

    records: list[dict[str, Any]] = []
    coercion_errors: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=1):
        record: dict[str, Any] = {}
        for column in spec.columns:
            index = matched_columns.get(column.name)
            raw_value = row[index] if index is not None and index < len(row) else None
            value, error = _coerce(raw_value, column.type)
            if error is not None:
                coercion_errors.append(
                    {
                        "row": row_number,
                        "column": column.name,
                        "raw": str(raw_value),
                        "error": error,
                    }
                )
            record[column.name] = value
        records.append(record)

    check_results = [_run_check(check, records) for check in spec.checks]

    required_total = sum(column.required for column in spec.columns)
    required_matched = required_total - len(missing_required)
    total_checked = sum(result["rows_checked"] for result in check_results)
    total_passed = sum(result["rows_passed"] for result in check_results)

    result = {
        "schema_version": schema_version,
        "run_id": run_id,
        "page_id": page["page_id"],
        "page_number": page["page_number"],
        "schema_name": spec.name,
        "source_format": fmt,
        "raw_artifact": raw_artifact,
        "columns": {
            "matched": matched,
            "missing_required": missing_required,
            "missing_optional": missing_optional,
            "extra": extra,
        },
        "records": records,
        "coercion_errors": coercion_errors,
        "checks": check_results,
        "metrics": {
            "row_count": len(records),
            "tables_found": tables_found,
            "tables_ignored": tables_ignored,
            "required_column_coverage": (
                1.0 if required_total == 0 else round(required_matched / required_total, 4)
            ),
            "column_coverage": (
                round(len(matched_columns) / len(spec.columns), 4)
            ),
            "arithmetic_pass_rate": (
                None if total_checked == 0 else round(total_passed / total_checked, 4)
            ),
            "coercion_error_count": len(coercion_errors),
            "structure_issue_count": len(structure_issues),
            "parse_error": parse_error,
        },
    }
    if structure_issues:
        result["structure_issues"] = structure_issues
    return result


def align_run(
    run_dir: Path,
    *,
    schema_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-align and regrade an existing run, optionally as a read-only preview.

    Planning derives every replacement in memory. Applying first serializes
    every artifact into a staging directory, then replaces derived artifacts
    and writes ``manifest.json`` last as the commit indicator. Raw evidence is
    never modified.
    """
    from .artifacts import build_rerun_manifest, read_jsonl, render_audit_markdown
    from .config import load_config
    from .grading import grade_distribution, grade_is_below, grade_page

    out_dir = Path(run_dir).expanduser().resolve()
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"No manifest.json found in {out_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records_before = manifest["summary"].get("records_normalized", 0)
    run_id = manifest["run_id"]
    schema_version = manifest["schema_version"]

    snapshot_path = out_dir / "config-snapshot.yml"
    if not snapshot_path.is_file():
        raise ValueError(f"No config-snapshot.yml found in {out_dir}")
    config = load_config(snapshot_path, validate_adapter=False)
    spec, schema_source, schema_sha256, schema_snapshot = _resolve_align_schema(
        out_dir, schema_path=schema_path, config_data=config.data
    )

    provenance_entries = read_jsonl(out_dir / "provenance.jsonl")
    quality_entries = read_jsonl(out_dir / "quality.jsonl")
    grades_before = grade_distribution(quality_entries)
    alignments: dict[str, dict[str, Any]] = {}
    for entry in provenance_entries:
        fmt = entry["result"]["format"]
        if fmt not in ALIGNABLE_FORMATS:
            continue
        raw_artifact = entry["result"]["raw_artifact"]
        alignment = align_page(
            (out_dir / raw_artifact).read_text(encoding="utf-8"),
            fmt,
            spec,
            page={
                "page_id": entry["page_id"],
                "page_number": entry["source"]["page_number"],
            },
            run_id=run_id,
            schema_version=schema_version,
            raw_artifact=raw_artifact,
        )
        if alignment is not None:
            alignments[entry["page_id"]] = alignment

    for entry in quality_entries:
        entry.update(
            grade_page(
                entry,
                alignments.get(entry["page_id"]),
                config.grading_thresholds,
                quality_floors=spec.quality,
            )
        )
    grades = {entry["page_id"]: entry for entry in quality_entries}

    audit_path = out_dir / "audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    review_queue_before = len(audit.get("review_queue", []))
    review_queue = [
        item
        for item in audit.get("review_queue", [])
        if item.get("reason") != "grade_below_threshold"
    ]
    for item in review_queue:
        graded = grades.get(item.get("page_id"))
        if graded is not None and "grade" in item:
            item["grade"] = graded["grade"]
            item["grade_basis"] = graded["grade_basis"]
    if config.review_below_grade is not None:
        for entry in quality_entries:
            if grade_is_below(entry["grade"], config.review_below_grade):
                review_queue.append(
                    {
                        "page_id": entry["page_id"],
                        "page_number": entry["page_number"],
                        "type": config.default_review_type,
                        "confidence": None,
                        "action": "review",
                        "reason": "grade_below_threshold",
                        "grade": entry["grade"],
                        "grade_basis": entry["grade_basis"],
                    }
                )
    audit["review_queue"] = review_queue

    rerun_path = out_dir / "rerun-manifest.yml"
    previous_rerun = yaml.safe_load(rerun_path.read_text(encoding="utf-8"))
    route_map = yaml.safe_load((out_dir / "route-map.yml").read_text(encoding="utf-8"))
    aligned_at = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    rerun_created_at = previous_rerun.get("created_at", aligned_at)
    rerun_manifest = build_rerun_manifest(
        schema_version=schema_version,
        run_id=run_id,
        parent_run_id=previous_rerun["parent_run_id"],
        created_at=rerun_created_at,
        max_rerun_depth=previous_rerun["max_rerun_depth"],
        reason=previous_rerun["reason"],
        audit=audit,
        route_map=route_map,
        run_depth=previous_rerun["rerun_depth"],
        grades={page_id: entry["grade"] for page_id, entry in grades.items()},
    )

    records_normalized = sum(len(alignment["records"]) for alignment in alignments.values())
    from pageledger import __version__

    manifest["summary"]["records_normalized"] = records_normalized
    manifest["alignment"] = {
        "aligned_at": aligned_at,
        "schema_source": schema_source,
        "schema_sha256": schema_sha256,
        "pageledger_version": __version__,
    }
    log_line = {
        "schema_version": schema_version,
        "timestamp": aligned_at,
        "level": "INFO",
        "run_id": run_id,
        "status": "aligned",
    }
    run_log = (out_dir / "run.log").read_text(encoding="utf-8")
    if run_log and not run_log.endswith("\n"):
        run_log += "\n"
    run_log += json.dumps(log_line, sort_keys=True, allow_nan=False) + "\n"

    before = {
        "grade_distribution": grades_before,
        "review_queue_count": review_queue_before,
        "records_normalized": records_before,
    }
    after = {
        "grade_distribution": grade_distribution(quality_entries),
        "review_queue_count": len(review_queue),
        "records_normalized": records_normalized,
    }
    if not dry_run:
        _apply_alignment(
            out_dir,
            alignments=alignments,
            quality_entries=quality_entries,
            audit=audit,
            audit_markdown=render_audit_markdown(audit),
            rerun_manifest=rerun_manifest,
            run_log=run_log,
            manifest=manifest,
            schema_snapshot=schema_snapshot,
        )

    return {
        "run_id": run_id,
        "run_dir": str(out_dir),
        "schema_name": spec.name,
        "schema_source": schema_source,
        "pages_aligned": len(alignments),
        "records_normalized": records_normalized,
        "grade_distribution": after["grade_distribution"],
        "review_queue_count": len(review_queue),
        "applied": not dry_run,
        "before": before,
        "after": after,
    }


def _apply_alignment(
    out_dir: Path,
    *,
    alignments: dict[str, dict[str, Any]],
    quality_entries: list[dict[str, Any]],
    audit: dict[str, Any],
    audit_markdown: str,
    rerun_manifest: dict[str, Any],
    run_log: str,
    manifest: dict[str, Any],
    schema_snapshot: str | None,
) -> None:
    """Serialize the complete plan before replacing any derived artifact."""
    from .artifacts import write_json, write_jsonl, write_yaml

    with tempfile.TemporaryDirectory(prefix=".align-", dir=out_dir) as temp_name:
        stage = Path(temp_name)
        staged_normalized = stage / "normalized"
        staged_normalized.mkdir()
        for page_id, alignment in alignments.items():
            write_json(staged_normalized / f"{page_id}.json", alignment)
        write_jsonl(stage / "quality.jsonl", quality_entries)
        write_json(stage / "audit.json", audit)
        (stage / "audit.md").write_text(audit_markdown, encoding="utf-8")
        write_yaml(stage / "rerun-manifest.yml", rerun_manifest)
        (stage / "run.log").write_text(run_log, encoding="utf-8")
        if schema_snapshot is not None:
            (stage / "align-schema-snapshot.yml").write_text(schema_snapshot, encoding="utf-8")
        write_json(stage / "manifest.json", manifest)

        normalized_dir = out_dir / "normalized"
        normalized_dir.mkdir(exist_ok=True)
        staged_names = {path.name for path in staged_normalized.glob("*.json")}
        for path in staged_normalized.glob("*.json"):
            os.replace(path, normalized_dir / path.name)
        for stale in normalized_dir.glob("*.json"):
            if stale.name not in staged_names:
                stale.unlink()

        for name in (
            "quality.jsonl",
            "audit.json",
            "audit.md",
            "rerun-manifest.yml",
            "run.log",
        ):
            os.replace(stage / name, out_dir / name)
        if schema_snapshot is not None:
            os.replace(
                stage / "align-schema-snapshot.yml",
                out_dir / "align-schema-snapshot.yml",
            )
        else:
            stale_snapshot = out_dir / "align-schema-snapshot.yml"
            if stale_snapshot.is_file():
                stale_snapshot.unlink()
        os.replace(stage / "manifest.json", out_dir / "manifest.json")


def _resolve_align_schema(
    out_dir: Path, *, schema_path: Path | None, config_data: dict[str, Any]
) -> tuple[SchemaSpec, str, str, str | None]:
    """Resolve a schema without writing its eventual external snapshot."""
    if schema_path is not None:
        schema_file = Path(schema_path).expanduser().resolve()
        if not schema_file.is_file():
            raise ValueError(f"Schema file does not exist: {schema_file}")
        text = schema_file.read_text(encoding="utf-8")
        loaded = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"Schema file must be a YAML mapping: {schema_file}")
        # Accept either a bare schema mapping or a document with a
        # top-level `schema:` key (a full pageledger config also works).
        section = loaded.get("schema", loaded)
        spec = load_schema_spec({"schema": section})
        if spec is None:
            raise ValueError(f"Schema file declares no columns: {schema_file}")
        return spec, str(schema_file), _sha256_text(text), text

    spec = load_schema_spec(config_data)
    if spec is None:
        raise ValueError("Run config snapshot has no schema section; pass --schema <file.yml>")
    snapshot_text = (out_dir / "config-snapshot.yml").read_text(encoding="utf-8")
    return spec, "config_snapshot", _sha256_text(snapshot_text), None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Content parsing
# ---------------------------------------------------------------------------


class _ParseError(Exception):
    pass


def _parse_content(content: Any, fmt: str) -> tuple[list[str], list[list[Any]], int]:
    if fmt == "json":
        return _parse_json(content)
    if not isinstance(content, str):
        raise _ParseError("content_not_text")
    if fmt == "markdown_table":
        return _parse_markdown_table(content)
    return _parse_csv(content)


def _parse_markdown_table(text: str) -> tuple[list[str], list[list[Any]], int]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if "|" in line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    tables: list[tuple[list[str], list[list[Any]]]] = []
    for block in blocks:
        if len(block) < 2:
            continue
        separator_cells = _split_md_row(block[1])
        if not separator_cells or not all(
            _MD_SEPARATOR_CELL.match(cell.strip()) for cell in separator_cells
        ):
            continue
        headers = _split_md_row(block[0])
        rows = [_split_md_row(line) for line in block[2:]]
        tables.append((headers, [row for row in rows if any(cell for cell in row)]))

    if not tables:
        raise _ParseError("no_markdown_table_found")
    headers, rows = tables[0]
    return headers, rows, len(tables)


def _split_md_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _parse_csv(text: str) -> tuple[list[str], list[list[Any]], int]:
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    if not rows:
        raise _ParseError("empty_csv")
    headers = [cell.strip() for cell in rows[0]]
    return headers, [[cell.strip() for cell in row] for row in rows[1:]], 1


def _parse_json(content: Any) -> tuple[list[str], list[list[Any]], int]:
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            raise _ParseError("invalid_json") from None
    else:
        payload = content

    if isinstance(payload, list):
        if not payload:
            raise _ParseError("empty_json_records")
        if not all(isinstance(item, dict) for item in payload):
            raise _ParseError("unrecognized_json_shape")
        headers = list(dict.fromkeys(key for item in payload for key in item))
        rows = [[item.get(key) for key in headers] for item in payload]
        return headers, rows, 1
    if isinstance(payload, dict):
        if "headers" in payload and "rows" in payload:
            headers = payload["headers"]
            rows = payload["rows"]
            if not isinstance(headers, list) or not isinstance(rows, list):
                raise _ParseError("unrecognized_json_shape")
            if not all(isinstance(row, list) for row in rows):
                raise _ParseError("unrecognized_json_shape")
            return [str(header) for header in headers], rows, 1
        headers = list(payload)
        return headers, [[payload[key] for key in headers]], 1
    raise _ParseError("unrecognized_json_shape")


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


def _coerce(value: Any, column_type: str) -> tuple[Any, str | None]:
    """Coerce a cell to the declared type.

    Empty cells are missing data, not garble: they become null with no
    error. A non-empty cell that fails to parse becomes null with a
    recorded error — the raw string survives in coercion_errors.
    """
    if value is None:
        return None, None
    if isinstance(value, str) and not value.strip():
        return None, None

    if column_type == "string":
        return str(value).strip(), None

    if isinstance(value, str) and column_type == "integer":
        # European/Russian thousands grouping uses periods (161.168,
        # 1.084.598). Only the full grouped pattern is rewritten — a lone
        # "161.168" declared as `number` stays a decimal.
        grouped = value.strip()
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", grouped):
            value = grouped.replace(".", "")

    number = _parse_number(value)
    if number is None:
        return None, "not_integer" if column_type == "integer" else "not_number"
    if column_type == "integer":
        if number != int(number):
            return None, "not_integer"
        return int(number), None
    return number, None


def _parse_number(value: Any) -> float | None:
    """Parse a numeric cell, tolerating thousand separators (`,`, space, NBSP)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace(" ", "").replace("\xa0", "")
        try:
            number = float(cleaned)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


# ---------------------------------------------------------------------------
# Arithmetic checks
# ---------------------------------------------------------------------------

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult)


def _parse_check_expression(
    expression: str,
    declared: set[str],
    numeric: set[str],
    *,
    key_path: str,
) -> None:
    """Validate a check expression against the AST whitelist.

    Only `column == arithmetic-of-columns-and-constants` shapes pass:
    single == comparison, +/-/* operators, declared column names, numeric
    constants. Everything else is a config error, never evaluated.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise ValueError(f"{key_path} is not a valid expression") from None

    root = tree.body
    if not (
        isinstance(root, ast.Compare)
        and len(root.ops) == 1
        and isinstance(root.ops[0], ast.Eq)
        and len(root.comparators) == 1
    ):
        raise ValueError(f"{key_path} must be a single '==' comparison")
    names = [
        node.id
        for side in (root.left, root.comparators[0])
        for node in ast.walk(side)
        if isinstance(node, ast.Name)
    ]
    for name in names:
        if name not in declared:
            raise ValueError(f"{key_path} references undeclared column '{name}'")
    for name in names:
        if name not in numeric:
            raise ValueError(f"{key_path} references non-numeric column '{name}'")
    for node in (root.left, root.comparators[0]):
        _validate_operand(node, key_path=key_path)


def _validate_operand(
    node: ast.AST,
    *,
    key_path: str,
) -> None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        _validate_operand(node.left, key_path=key_path)
        _validate_operand(node.right, key_path=key_path)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        _validate_operand(node.operand, key_path=key_path)
        return
    if isinstance(node, ast.Name):
        return
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        if not math.isfinite(node.value):
            raise ValueError(f"{key_path} numeric constants must be finite")
        return
    raise ValueError(f"{key_path} may only use declared columns, numbers, and + - * operators")


def _run_check(check: CheckSpec, records: list[dict[str, Any]]) -> dict[str, Any]:
    tree = ast.parse(check.expression, mode="eval")
    root = tree.body
    assert isinstance(root, ast.Compare)  # validated at config load

    rows_checked = 0
    rows_passed = 0
    rows_failed = 0
    rows_unchecked = 0
    failures: list[dict[str, Any]] = []
    for row_number, record in enumerate(records, start=1):
        lhs = _eval_operand(root.left, record)
        rhs = _eval_operand(root.comparators[0], record)
        if lhs is None or rhs is None:
            # A null operand is missing evidence, not a pass.
            rows_unchecked += 1
            continue
        delta = lhs - rhs
        if not math.isfinite(delta):
            rows_unchecked += 1
            continue
        rows_checked += 1
        if abs(delta) <= check.tolerance:
            rows_passed += 1
        else:
            rows_failed += 1
            failures.append({"row": row_number, "delta": round(delta, 4)})

    return {
        "name": check.name,
        "rows_checked": rows_checked,
        "rows_passed": rows_passed,
        "rows_failed": rows_failed,
        "rows_unchecked": rows_unchecked,
        "pass_rate": None if rows_checked == 0 else round(rows_passed / rows_checked, 4),
        "failures": failures,
    }


def _eval_operand(node: ast.AST, record: dict[str, Any]) -> float | None:
    if isinstance(node, ast.BinOp):
        left = _eval_operand(node.left, record)
        right = _eval_operand(node.right, record)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        else:
            result = left * right
        return result if math.isfinite(result) else None
    if isinstance(node, ast.UnaryOp):
        operand = _eval_operand(node.operand, record)
        if operand is None:
            return None
        result = -operand
        return result if math.isfinite(result) else None
    if isinstance(node, ast.Name):
        value = record.get(node.id)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    assert isinstance(node, ast.Constant)
    return float(node.value)


def _unit_interval(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number between 0 and 1")
    if not math.isfinite(value) or value < 0 or value > 1:
        raise ValueError(f"{name} must be a number between 0 and 1")
    return float(value)
