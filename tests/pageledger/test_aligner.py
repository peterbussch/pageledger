"""Schema aligner tests: spec loading, parsing, matching, coercion, checks.

Covers:
  - load_schema_spec validation (good specs, every rejection path)
  - markdown_table / csv / json parsing including all three JSON shapes
  - alias matching is casefold + whitespace-collapsed, never fuzzy
  - missing required/optional columns and extra source headers
  - integer coercion tolerating thousand separators; failures recorded
  - arithmetic checks: pass, tolerance boundary, fail, null-operand rows
  - AST whitelist rejects hostile or out-of-shape expressions
  - parse_error path still produces a normalized record
  - non-alignable formats return None
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pageledger.aligner import align_page, align_run, load_schema_spec

PAGE = {"page_id": "doc_0001_page_0001", "page_number": 1}


def _spec(**overrides):
    data = {
        "schema": {
            "name": "demographic_table",
            "columns": [
                {
                    "name": "place_name",
                    "aliases": ["place", "settlement"],
                    "type": "string",
                    "required": True,
                },
                {
                    "name": "population_total",
                    "aliases": ["total", "всего"],
                    "type": "integer",
                    "required": True,
                },
                {"name": "population_male", "aliases": ["male"], "type": "integer"},
                {"name": "population_female", "aliases": ["female"], "type": "integer"},
            ],
            "checks": [
                {
                    "name": "population_sum",
                    "expression": "population_total == population_male + population_female",
                    "tolerance": 2,
                }
            ],
            **overrides,
        }
    }
    return load_schema_spec(data)


def _align(content, fmt, spec=None):
    return align_page(
        content,
        fmt,
        spec or _spec(),
        page=PAGE,
        run_id="run-test",
        schema_version="0.1",
        raw_artifact=f"raw/doc_0001_page_0001.{fmt}",
    )


# =========================================================================
# Spec loading
# =========================================================================


def test_no_schema_section_returns_none():
    assert load_schema_spec({}) is None
    assert load_schema_spec({"run": {}}) is None


def test_spec_parses_columns_checks_quality():
    spec = _spec(quality={"minimum_required_column_coverage": 1.0, "low_confidence_threshold": 0.7})
    assert spec.name == "demographic_table"
    assert [c.name for c in spec.columns if c.required] == [
        "place_name",
        "population_total",
    ]
    assert spec.columns[2].type == "integer"
    assert spec.columns[2].required is False
    assert spec.checks[0].tolerance == 2.0
    assert spec.quality.minimum_required_column_coverage == 1.0
    assert spec.quality.low_confidence_threshold == 0.7


@pytest.mark.parametrize(
    "schema, message",
    [
        ("not a mapping", "schema must be a mapping"),
        ({"name": "x"}, "schema.columns must be a non-empty list"),
        ({"name": "x", "columns": []}, "schema.columns must be a non-empty list"),
        ({"name": "", "columns": [{"name": "a"}]}, "schema.name"),
        ({"name": "x", "columns": [{"name": "a", "type": "float"}]}, r"columns\[0\].type"),
        ({"name": "x", "columns": [{"name": "a", "required": "yes"}]}, r"columns\[0\].required"),
        ({"name": "x", "columns": [{"name": "a", "aliases": [1]}]}, r"columns\[0\].aliases"),
        (
            {"name": "x", "columns": [{"name": "a"}, {"name": "b", "aliases": ["A"]}]},
            "collides",
        ),
        (
            {
                "name": "x",
                "columns": [{"name": "a"}],
                "checks": [{"name": "c", "expression": "a == b"}],
            },
            "undeclared column 'b'",
        ),
        (
            {
                "name": "x",
                "columns": [{"name": "a"}],
                "checks": [{"name": "c", "expression": "a == 1", "tolerance": -1}],
            },
            "tolerance",
        ),
        (
            {"name": "x", "columns": [{"name": "a"}], "quality": {"low_confidence_threshold": 1.5}},
            "between 0 and 1",
        ),
    ],
)
def test_spec_rejections(schema, message):
    with pytest.raises(ValueError, match=message):
        load_schema_spec({"schema": schema})


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('true')",
        "a < 1",
        "a == 1 == 1",
        "a == b if True else 0",
        "a == max(1, 2)",
        "a == 'text'",
        "a.__class__ == 1",
    ],
)
def test_hostile_or_out_of_shape_expressions_rejected(expression):
    schema = {
        "name": "x",
        "columns": [{"name": "a"}, {"name": "b"}],
        "checks": [{"name": "c", "expression": expression}],
    }
    with pytest.raises(ValueError):
        load_schema_spec({"schema": schema})


def test_negative_constant_and_subtraction_allowed():
    schema = {
        "name": "x",
        "columns": [{"name": "a", "type": "integer"}, {"name": "b", "type": "integer"}],
        "checks": [{"name": "c", "expression": "a - b == -1"}],
    }
    assert load_schema_spec({"schema": schema}) is not None


@pytest.mark.parametrize(
    "schema, message",
    [
        (
            {"name": "x", "columns": [{"name": "a"}], "surprise": True},
            "schema has unknown key 'surprise'",
        ),
        (
            {"name": "x", "columns": [{"name": "a", "surprise": True}]},
            r"schema.columns\[0\] has unknown key 'surprise'",
        ),
        (
            {
                "name": "x",
                "columns": [{"name": "a", "type": "number"}],
                "checks": [{"name": "c", "expression": "a == 1", "surprise": True}],
            },
            r"schema.checks\[0\] has unknown key 'surprise'",
        ),
        (
            {
                "name": "x",
                "columns": [{"name": "a"}],
                "quality": {"surprise": True},
            },
            "schema.quality has unknown key 'surprise'",
        ),
    ],
)
def test_owned_schema_mappings_reject_unknown_keys(schema, message):
    with pytest.raises(ValueError, match=message):
        load_schema_spec({"schema": schema})


def test_duplicate_check_names_are_rejected():
    schema = {
        "name": "x",
        "columns": [{"name": "a", "type": "number"}],
        "checks": [
            {"name": "same", "expression": "a == 1"},
            {"name": "same", "expression": "a == 2"},
        ],
    }
    with pytest.raises(ValueError, match="duplicate check name 'same'"):
        load_schema_spec({"schema": schema})


def test_arithmetic_checks_reject_string_columns():
    schema = {
        "name": "x",
        "columns": [
            {"name": "label", "type": "string"},
            {"name": "amount", "type": "number"},
        ],
        "checks": [{"name": "bad", "expression": "amount == label + 1"}],
    }
    with pytest.raises(ValueError, match="non-numeric column 'label'"):
        load_schema_spec({"schema": schema})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_schema_numbers_are_rejected(value):
    with pytest.raises(ValueError, match="tolerance"):
        _spec(checks=[{"name": "c", "expression": "population_total == 1", "tolerance": value}])
    with pytest.raises(ValueError, match="between 0 and 1"):
        _spec(quality={"minimum_required_column_coverage": value})


def test_nonfinite_expression_constants_are_rejected():
    schema = {
        "name": "x",
        "columns": [{"name": "a", "type": "number"}],
        "checks": [{"name": "c", "expression": "a == 1e309"}],
    }
    with pytest.raises(ValueError, match="finite"):
        load_schema_spec({"schema": schema})


# =========================================================================
# Parsing and alignment
# =========================================================================

MD_TABLE = """\
Some preamble text.

| Place | Всего | Male | Female | Примечание |
| --- | ---: | --- | --- | --- |
| Moscow | 4,137,000 | 2 001 000 | 2136000 | capital |
| Leningrad | 3191304 | 1 500 000 | 1691304 |  |
"""


def test_markdown_table_alignment():
    result = _align(MD_TABLE, "markdown_table")
    assert result["schema_name"] == "demographic_table"
    assert result["columns"]["matched"] == {
        "Place": "place_name",
        "Всего": "population_total",
        "Male": "population_male",
        "Female": "population_female",
    }
    assert result["columns"]["extra"] == ["Примечание"]
    assert result["columns"]["missing_required"] == []
    assert result["records"][0] == {
        "place_name": "Moscow",
        "population_total": 4137000,
        "population_male": 2001000,
        "population_female": 2136000,
    }
    assert result["metrics"]["row_count"] == 2
    assert result["metrics"]["required_column_coverage"] == 1.0
    assert result["metrics"]["column_coverage"] == 1.0
    assert result["metrics"]["parse_error"] is None
    check = result["checks"][0]
    assert check["rows_checked"] == 2
    assert check["rows_passed"] == 2
    assert check["pass_rate"] == 1.0


def test_csv_alignment_with_coercion_error():
    csv_text = "place,total,male,female\nKiev,84б000,400000,440000\n"
    result = _align(csv_text, "csv")
    assert result["records"][0]["place_name"] == "Kiev"
    assert result["records"][0]["population_total"] is None
    assert result["coercion_errors"] == [
        {"row": 1, "column": "population_total", "raw": "84б000", "error": "not_integer"}
    ]
    assert result["metrics"]["coercion_error_count"] == 1
    # Null operand: the check row is unchecked, not passed.
    assert result["checks"][0]["rows_unchecked"] == 1
    assert result["checks"][0]["pass_rate"] is None


def test_json_records_alignment():
    content = '[{"place": "Minsk", "total": 300, "male": 150, "female": 149}]'
    result = _align(content, "json")
    assert result["records"] == [
        {
            "place_name": "Minsk",
            "population_total": 300,
            "population_male": 150,
            "population_female": 149,
        }
    ]
    # 300 != 299 exceeds nothing: tolerance 2 covers delta 1.
    assert result["checks"][0]["rows_passed"] == 1


def test_json_headers_rows_and_single_dict_shapes():
    headers_rows = '{"headers": ["place", "total"], "rows": [["Baku", "500"]]}'
    result = _align(headers_rows, "json")
    assert result["records"][0]["place_name"] == "Baku"
    assert result["records"][0]["population_total"] == 500

    single = '{"place": "Erevan", "total": 200}'
    result = _align(single, "json")
    assert result["metrics"]["row_count"] == 1
    assert result["records"][0]["place_name"] == "Erevan"


def test_dict_content_from_in_run_adapter():
    result = _align([{"place": "Tbilisi", "total": 100}], "json")
    assert result["records"][0]["place_name"] == "Tbilisi"


def test_tolerance_boundary_and_failure_delta():
    md = "| place | total | male | female |\n| - | - | - | - |\n| A | 102 | 50 | 50 |\n| B | 110 | 50 | 50 |\n"
    result = _align(md, "markdown_table")
    check = result["checks"][0]
    assert check["rows_passed"] == 1  # delta 2 == tolerance 2
    assert check["rows_failed"] == 1
    assert check["failures"] == [{"row": 2, "delta": 10.0}]
    assert check["pass_rate"] == 0.5


def test_missing_required_column():
    md = "| place | male |\n| - | - |\n| A | 5 |\n"
    result = _align(md, "markdown_table")
    assert result["columns"]["missing_required"] == ["population_total"]
    assert result["columns"]["missing_optional"] == ["population_female"]
    assert result["metrics"]["required_column_coverage"] == 0.5
    assert result["records"][0]["population_total"] is None


def test_matching_is_exact_not_fuzzy():
    md = "| places | total |\n| - | - |\n| A | 5 |\n"  # "places" ≠ alias "place"
    result = _align(md, "markdown_table")
    assert result["columns"]["extra"] == ["places"]
    assert result["columns"]["missing_required"] == ["place_name", "population_total"][:1]


def test_alias_matching_casefolds_and_collapses_whitespace():
    md = "|  PLACE  | ВСЕГО |\n| - | - |\n| A | 5 |\n"
    result = _align(md, "markdown_table")
    assert result["columns"]["matched"] == {"PLACE": "place_name", "ВСЕГО": "population_total"}


def test_parse_error_still_writes_record():
    result = _align("just prose, no table here", "markdown_table")
    assert result["records"] == []
    assert result["metrics"]["parse_error"] == "no_markdown_table_found"
    assert result["metrics"]["row_count"] == 0

    result = _align("{not json", "json")
    assert result["metrics"]["parse_error"] == "invalid_json"


def test_multiple_tables_first_wins():
    md = "| place | total |\n| - | - |\n| A | 5 |\n\n| other | headers |\n| - | - |\n| x | y |\n"
    result = _align(md, "markdown_table")
    assert result["metrics"]["tables_found"] == 2
    assert result["records"][0]["place_name"] == "A"
    assert result["metrics"]["tables_ignored"] == 1
    assert result["structure_issues"] == [{"type": "ignored_table", "tables_ignored": 1}]


def test_structure_issues_record_duplicate_headers_and_row_widths():
    md = "| place | total | total |\n| - | - | - |\n| A | 5 | 6 | extra |\n| B | 7 |\n"
    result = _align(md, "markdown_table")
    assert result["records"] == [
        {
            "place_name": "A",
            "population_total": 5,
            "population_male": None,
            "population_female": None,
        },
        {
            "place_name": "B",
            "population_total": 7,
            "population_male": None,
            "population_female": None,
        },
    ]
    assert result["structure_issues"] == [
        {
            "type": "duplicate_header",
            "header": "total",
            "column": "population_total",
            "kept_header": "total",
        },
        {
            "type": "row_width_mismatch",
            "row": 1,
            "expected_columns": 3,
            "actual_columns": 4,
        },
        {
            "type": "row_width_mismatch",
            "row": 2,
            "expected_columns": 3,
            "actual_columns": 2,
        },
    ]
    assert result["metrics"]["structure_issue_count"] == 3


def test_nonfinite_numeric_cells_are_recorded_not_emitted():
    spec = _spec(
        columns=[
            {"name": "amount", "type": "number"},
            {"name": "count", "type": "integer"},
        ],
        checks=[],
    )
    result = _align('[{"amount": NaN, "count": Infinity}]', "json", spec)
    assert result["records"] == [{"amount": None, "count": None}]
    assert [error["error"] for error in result["coercion_errors"]] == [
        "not_number",
        "not_integer",
    ]
    json.dumps(result, allow_nan=False)


def test_text_and_markdown_formats_not_aligned():
    assert _align("plain page text", "text") is None
    assert _align("# heading", "markdown") is None


def test_empty_cells_are_null_without_error():
    md = "| place | total |\n| - | - |\n| A |  |\n"
    result = _align(md, "markdown_table")
    assert result["records"][0]["population_total"] is None
    assert result["coercion_errors"] == []


def test_config_load_validates_schema_section(tmp_path):
    from pageledger.config import load_config

    config = tmp_path / "config.yml"
    config.write_text(
        "schema_version: '0.1'\n"
        "schema:\n"
        "  name: t\n"
        "  columns:\n"
        "    - {name: a}\n"
        "  checks:\n"
        "    - {name: c, expression: 'a == undeclared'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="undeclared"):
        load_config(config)


def test_period_thousands_grouping_coerces_for_integers():
    """Soviet-era numbers group thousands with periods: 1.084.598."""
    md = "| place | total | male | female |\n| - | - | - | - |\n| A | 1.084.598 | 521.790 | 562.808 |\n"
    result = _align(md, "markdown_table")
    assert result["records"][0]["population_total"] == 1084598
    assert result["records"][0]["population_male"] == 521790
    assert result["coercion_errors"] == []
    # A non-grouped decimal-looking string still fails integer coercion
    md = "| place | total |\n| - | - |\n| B | 12.5 |\n"
    result = _align(md, "markdown_table")
    assert result["records"][0]["population_total"] is None
    assert result["coercion_errors"][0]["error"] == "not_integer"


# =========================================================================
# Existing-run preview/apply
# =========================================================================


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _existing_run(tmp_path: Path) -> Path:
    out = tmp_path / "run"
    (out / "raw").mkdir(parents=True)
    (out / "normalized").mkdir()
    (out / "raw" / "page-1.md").write_text(
        "| town | total |\n| - | - |\n| Kazan | 400000 |\n", encoding="utf-8"
    )
    (out / "config-snapshot.yml").write_text(
        "schema_version: '0.1'\n"
        "schema:\n"
        "  name: old\n"
        "  columns:\n"
        "    - {name: place, type: string, required: true}\n"
        "    - {name: total, type: integer, required: true}\n",
        encoding="utf-8",
    )
    _write_json(
        out / "manifest.json",
        {
            "schema_version": "0.1",
            "run_id": "run-test",
            "summary": {"records_normalized": 0},
        },
    )
    provenance = {
        "page_id": "page-1",
        "source": {"page_number": 1},
        "result": {"format": "markdown_table", "raw_artifact": "raw/page-1.md"},
    }
    (out / "provenance.jsonl").write_text(json.dumps(provenance) + "\n", encoding="utf-8")
    quality = {
        "schema_version": "0.1",
        "run_id": "run-test",
        "page_id": "page-1",
        "page_number": 1,
        "confidence": 0.95,
        "warnings": [],
    }
    (out / "quality.jsonl").write_text(json.dumps(quality) + "\n", encoding="utf-8")
    _write_json(
        out / "audit.json",
        {
            "schema_version": "0.1",
            "run_id": "run-test",
            "review_queue": [],
            "quarantine_queue": [],
        },
    )
    (out / "audit.md").write_text("old audit\n", encoding="utf-8")
    (out / "rerun-manifest.yml").write_text(
        "schema_version: '0.1'\n"
        "run_id: run-test-rerun\n"
        "parent_run_id: run-test\n"
        "rerun_depth: 0\n"
        "max_rerun_depth: 2\n"
        "reason: audit_queue\n",
        encoding="utf-8",
    )
    (out / "route-map.yml").write_text(
        "documents:\n"
        "  - source: {path: doc.txt, sha256: abc}\n"
        "    pages:\n"
        "      - {page_id: page-1}\n",
        encoding="utf-8",
    )
    (out / "run.log").write_text("", encoding="utf-8")
    _write_json(out / "normalized" / "stale.json", {"stale": True})
    return out


def _tree_contents(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_align_run_dry_run_previews_without_writes(tmp_path):
    out = _existing_run(tmp_path)
    schema = tmp_path / "schema.yml"
    schema.write_text(
        "name: new\n"
        "columns:\n"
        "  - {name: place, aliases: [town], type: string, required: true}\n"
        "  - {name: total, type: integer, required: true}\n",
        encoding="utf-8",
    )
    before = _tree_contents(out)

    report = align_run(out, schema_path=schema, dry_run=True)

    assert report["applied"] is False
    assert report["before"]["records_normalized"] == 0
    assert report["after"]["records_normalized"] == 1
    assert report["after"]["grade_distribution"]["A"] == 1
    assert _tree_contents(out) == before
    assert not (out / "align-schema-snapshot.yml").exists()


def test_align_run_applies_staged_results_and_manifest_last(tmp_path, monkeypatch):
    out = _existing_run(tmp_path)
    manifest_before = (out / "manifest.json").read_bytes()

    import pageledger.artifacts as artifacts

    original_write_json = artifacts.write_json

    def fail_staging(path, data, **kwargs):
        if path.name == "page-1.json" and path.parent.name == "normalized":
            raise RuntimeError("forced staging failure")
        return original_write_json(path, data, **kwargs)

    monkeypatch.setattr(artifacts, "write_json", fail_staging)
    with pytest.raises(RuntimeError, match="forced staging failure"):
        align_run(out)

    assert (out / "manifest.json").read_bytes() == manifest_before
    assert json.loads((out / "normalized" / "stale.json").read_text())["stale"] is True
    assert not list(out.glob(".align-*"))

    monkeypatch.setattr(artifacts, "write_json", original_write_json)
    report = align_run(out)
    assert report["applied"] is True
    assert report["records_normalized"] == 1
    assert not (out / "normalized" / "stale.json").exists()
    assert (out / "normalized" / "page-1.json").is_file()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["summary"]["records_normalized"] == 1
    assert manifest["alignment"]["schema_source"] == "config_snapshot"


def test_align_preview_matches_apply_and_repeated_derivation_is_idempotent(tmp_path):
    out = _existing_run(tmp_path)
    schema = tmp_path / "schema.yml"
    schema.write_text(
        "name: new\n"
        "columns:\n"
        "  - {name: place, aliases: [town], type: string, required: true}\n"
        "  - {name: total, type: integer, required: true}\n",
        encoding="utf-8",
    )

    preview = align_run(out, schema_path=schema, dry_run=True)
    applied = align_run(out, schema_path=schema)

    assert preview["before"] == applied["before"]
    assert preview["after"] == applied["after"]
    stable_paths = [
        out / "normalized" / "page-1.json",
        out / "quality.jsonl",
        out / "audit.json",
        out / "rerun-manifest.yml",
    ]
    first_derivation = {path: path.read_bytes() for path in stable_paths}

    repeated = align_run(out, schema_path=schema)

    assert repeated["after"] == applied["after"]
    assert {path: path.read_bytes() for path in stable_paths} == first_derivation


def test_align_preserves_adapter_chain_escalation(tmp_path):
    out = _existing_run(tmp_path)
    rerun_path = out / "rerun-manifest.yml"
    rerun = yaml.safe_load(rerun_path.read_text(encoding="utf-8"))
    rerun["escalation"] = {
        "adapter_order": ["weak", "strong"],
        "step": 0,
        "next_adapter": "strong",
    }
    rerun_path.write_text(yaml.safe_dump(rerun, sort_keys=False), encoding="utf-8")

    align_run(out)

    aligned_rerun = yaml.safe_load(rerun_path.read_text(encoding="utf-8"))
    assert aligned_rerun["escalation"] == rerun["escalation"]


def test_align_removes_obsolete_external_schema_snapshot(tmp_path):
    out = _existing_run(tmp_path)
    schema = tmp_path / "schema.yml"
    schema.write_text(
        "name: external\ncolumns:\n  - {name: total, type: integer}\n",
        encoding="utf-8",
    )
    align_run(out, schema_path=schema)
    assert (out / "align-schema-snapshot.yml").is_file()

    align_run(out)

    assert not (out / "align-schema-snapshot.yml").exists()
