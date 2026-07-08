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
import io
import json
import re
from dataclasses import dataclass
from typing import Any

# Formats the aligner understands. Plain text/markdown pages are not
# aligned: without a declared structure in the payload there is nothing to
# map, and guessing would violate the record-uncertainty principle.
ALIGNABLE_FORMATS = frozenset({"markdown_table", "csv", "json"})

COLUMN_TYPES = frozenset({"string", "integer", "number"})

# GFM table separator row cell: --- with optional alignment colons.
_MD_SEPARATOR_CELL = re.compile(r"^:?-+:?$")


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

    @property
    def required_columns(self) -> tuple[ColumnSpec, ...]:
        return tuple(column for column in self.columns if column.required)


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
            raise ValueError(
                f"{prefix}.type must be one of: {', '.join(sorted(COLUMN_TYPES))}"
            )
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
    raw_checks = section.get("checks", [])
    if not isinstance(raw_checks, list):
        raise ValueError("schema.checks must be a list")
    checks: list[CheckSpec] = []
    for index, raw in enumerate(raw_checks):
        prefix = f"schema.checks[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{prefix} must be a mapping")
        check_name = raw.get("name")
        if not isinstance(check_name, str) or not check_name.strip():
            raise ValueError(f"{prefix}.name must be a non-empty string")
        expression = raw.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError(f"{prefix}.expression must be a non-empty string")
        _parse_check_expression(expression, declared_names, key_path=f"{prefix}.expression")
        tolerance = raw.get("tolerance", 0)
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
            raise ValueError(f"{prefix}.tolerance must be a non-negative number")
        if tolerance < 0:
            raise ValueError(f"{prefix}.tolerance must be a non-negative number")
        checks.append(
            CheckSpec(name=check_name, expression=expression, tolerance=float(tolerance))
        )

    raw_quality = section.get("quality", {})
    if not isinstance(raw_quality, dict):
        raise ValueError("schema.quality must be a mapping")
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
    for index, header in enumerate(headers):
        column = alias_map.get(normalize_header(header))
        if column is not None and column.name not in matched_columns:
            matched[header] = column.name
            matched_columns[column.name] = index
        else:
            extra.append(header)

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

    required_total = len(spec.required_columns)
    required_matched = required_total - len(missing_required)
    total_checked = sum(result["rows_checked"] for result in check_results)
    total_passed = sum(result["rows_passed"] for result in check_results)

    return {
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
            "required_column_coverage": (
                1.0 if required_total == 0 else round(required_matched / required_total, 4)
            ),
            "column_coverage": (
                0.0
                if not spec.columns
                else round(len(matched_columns) / len(spec.columns), 4)
            ),
            "arithmetic_pass_rate": (
                None if total_checked == 0 else round(total_passed / total_checked, 4)
            ),
            "coercion_error_count": len(coercion_errors),
            "parse_error": parse_error,
        },
    }


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
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace(" ", "").replace("\xa0", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Arithmetic checks
# ---------------------------------------------------------------------------

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult)


def _parse_check_expression(
    expression: str, declared: set[str], *, key_path: str
) -> ast.Expression:
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
    for node in (root.left, root.comparators[0]):
        _validate_operand(node, declared, key_path=key_path)
    return tree


def _validate_operand(node: ast.AST, declared: set[str], *, key_path: str) -> None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        _validate_operand(node.left, declared, key_path=key_path)
        _validate_operand(node.right, declared, key_path=key_path)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        _validate_operand(node.operand, declared, key_path=key_path)
        return
    if isinstance(node, ast.Name):
        if node.id not in declared:
            raise ValueError(
                f"{key_path} references undeclared column '{node.id}'"
            )
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return
    raise ValueError(
        f"{key_path} may only use declared columns, numbers, and + - * operators"
    )


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
        rows_checked += 1
        delta = lhs - rhs
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
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        return left * right
    if isinstance(node, ast.UnaryOp):
        operand = _eval_operand(node.operand, record)
        return None if operand is None else -operand
    if isinstance(node, ast.Name):
        value = record.get(node.id)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    assert isinstance(node, ast.Constant)
    return float(node.value)


def _unit_interval(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number between 0 and 1")
    if value < 0 or value > 1:
        raise ValueError(f"{name} must be a number between 0 and 1")
    return float(value)
