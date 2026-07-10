# Normalized page specification

`normalized/{page_id}.json` is the schema aligner's output for one
structured page: extractor output mapped to the columns declared in the
config `schema` section. One file per aligned page; plain-text pages
produce no file. Written by `pageledger run` when a schema is configured,
and rewritten by `pageledger align`.

## Minimal shape

```json
{
  "schema_version": "0.1",
  "run_id": "run-20260708T120000000000Z",
  "page_id": "doc_0001_page_0002",
  "page_number": 2,
  "schema_name": "demographic_table",
  "source_format": "markdown_table",
  "raw_artifact": "raw/doc_0001_page_0002.markdown_table",
  "columns": {
    "matched": {"Место": "place_name", "Всего": "population_total"},
    "missing_required": [],
    "missing_optional": ["population_female"],
    "extra": ["Примечание"]
  },
  "records": [
    {"place_name": "Москва", "population_total": 4137000, "population_female": null}
  ],
  "coercion_errors": [
    {"row": 3, "column": "population_total", "raw": "84б000", "error": "not_integer"}
  ],
  "checks": [
    {
      "name": "population_sum",
      "rows_checked": 12, "rows_passed": 11, "rows_failed": 1,
      "rows_unchecked": 2, "pass_rate": 0.9167,
      "failures": [{"row": 7, "delta": 14}]
    }
  ],
  "structure_issues": [
    {
      "type": "row_width_mismatch",
      "row": 3,
      "expected_columns": 4,
      "actual_columns": 5
    }
  ],
  "metrics": {
    "row_count": 14,
    "tables_found": 1,
    "tables_ignored": 0,
    "required_column_coverage": 1.0,
    "column_coverage": 0.75,
    "arithmetic_pass_rate": 0.9167,
    "coercion_error_count": 1,
    "structure_issue_count": 1,
    "parse_error": null
  }
}
```

## Design notes

- Header matching is exact after normalization (casefold, collapsed
  whitespace) against declared column names and aliases. There is no
  fuzzy matching: an unmatched header is recorded in `columns.extra`, not
  guessed at.
- Every declared column appears in every record. `null` means the column
  was unmatched, the cell was empty, or coercion failed. The distinction
  is recoverable from `columns` and `coercion_errors`.
- Coercion (`integer`/`number`) tolerates thousand separators (`,`, space,
  NBSP). A non-empty cell that fails to parse becomes `null` and the raw
  string is preserved in `coercion_errors`. Nothing is silently fixed.
- Check rows with a `null` operand are counted as `rows_unchecked` -
  missing evidence, neither pass nor fail.
- Unparseable structured content (e.g. an adapter declared
  `markdown_table` but no table was found) still writes the file, with
  `records: []` and `metrics.parse_error` set. The failure is evidence and
  grades the page's schema axis F.
- `metrics.required_column_coverage` and `metrics.arithmetic_pass_rate`
  feed the schema axis of the page grade (see `quality.jsonl`).
- `structure_issues` records duplicate mapped headers, rows whose cell count
  differs from the header, and Markdown tables ignored after the first. The
  first parsed table still wins for compatibility; PageLedger records the
  discarded structure instead of guessing how to merge it. Any structural
  issue caps the schema grade at B without lifting an already-worse grade.
- The JSON Schema for this artifact is at `schemas/normalized-page.schema.json`.
- Schema validation tests are in `tests/pageledger/test_schemas.py`.

## Field table

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `schema_version` | string | ✅ | no | Artifact schema version. `"0.1"`. |
| `run_id` | string | ✅ | no | Run identifier from `manifest.json`. |
| `page_id` | string | ✅ | no | Page identifier, matches `raw/` and `provenance.jsonl`. |
| `page_number` | integer | ✅ | no | Source page number. |
| `schema_name` | string | ✅ | no | `schema.name` from the config or `--schema` file. |
| `source_format` | string | ✅ | no | `markdown_table`, `csv`, or `json`. |
| `raw_artifact` | string | ✅ | no | Relative path of the raw page this was derived from. |
| `columns.matched` | object | ✅ | no | Source header → declared column name. |
| `columns.missing_required` | array | ✅ | no | Declared required columns with no matching header. |
| `columns.missing_optional` | array | ✅ | no | Declared optional columns with no matching header. |
| `columns.extra` | array | ✅ | no | Source headers not retained as a unique declared mapping, including unmatched and duplicate-mapped headers. |
| `records` | array | ✅ | no | One object per source row, keyed by declared columns. |
| `coercion_errors` | array | ✅ | no | Cells that failed type coercion, with raw strings. |
| `checks` | array | ✅ | no | Per-check row accounting and failures with deltas. |
| `structure_issues` | array | ❌ | no | Duplicate-header, row-width, and ignored-table evidence. Absent when no structural loss was observed. |
| `metrics.row_count` | integer | ✅ | no | Records extracted. |
| `metrics.tables_found` | integer | ✅ | no | Tables detected in the raw payload. |
| `metrics.tables_ignored` | integer | ❌ | no | Markdown tables after the retained first table. |
| `metrics.required_column_coverage` | number | ✅ | no | Matched required / declared required (1.0 when none declared). |
| `metrics.column_coverage` | number | ✅ | no | Matched declared / declared total. |
| `metrics.arithmetic_pass_rate` | number | ✅ | yes | Passed / checked across all checks. Null when nothing was checkable. |
| `metrics.coercion_error_count` | integer | ✅ | no | Total coercion failures. |
| `metrics.structure_issue_count` | integer | ❌ | no | Number of recorded structural issues. |
| `metrics.parse_error` | string | ✅ | yes | Why the payload could not be parsed; null on success. |
