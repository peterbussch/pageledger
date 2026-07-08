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

import pytest

from pageledger.aligner import align_page, load_schema_spec

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
    assert [c.name for c in spec.required_columns] == ["place_name", "population_total"]
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
            {"name": "x", "columns": [{"name": "a"}], "checks": [{"name": "c", "expression": "a == b"}]},
            "undeclared column 'b'",
        ),
        (
            {"name": "x", "columns": [{"name": "a"}], "checks": [{"name": "c", "expression": "a == 1", "tolerance": -1}]},
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
        {"place_name": "Minsk", "population_total": 300, "population_male": 150, "population_female": 149}
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
    md = (
        "| place | total |\n| - | - |\n| A | 5 |\n\n"
        "| other | headers |\n| - | - |\n| x | y |\n"
    )
    result = _align(md, "markdown_table")
    assert result["metrics"]["tables_found"] == 2
    assert result["records"][0]["place_name"] == "A"


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
