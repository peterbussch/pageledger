# Audit artifact specification

`audit.json` records the machine-readable audit result for a PageLedger run.
`audit.md` is a human rendering of this file, not a separate source of truth.

## Minimal shape

```json
{
  "schema_version": "0.1",
  "run_id": "run-20260619-001",
  "review_queue": [
    {
      "page_id": "doc_0001_page_0003",
      "page_number": 3,
      "type": "prose",
      "action": "review",
      "reason": "no_classifier_available"
    }
  ],
  "quarantine_queue": [
    {
      "page_id": "doc_0001_page_0008",
      "page_number": 8,
      "type": "table",
      "action": "quarantine",
      "reason": "quarantine_if:missing_required_columns",
      "grade": "F",
      "grade_basis": "schema_aware"
    }
  ]
}
```

## Design notes

- `review_queue` should contain the pages or records a human should inspect
  before trusting the run.
- `quarantine_queue` contains pages excluded from rerun because they matched
  a `quarantine_if` rule. The rerun depth cap changes the rerun manifest,
  not this queue.
- `audit.md` should be generated from `audit.json`.
- The current alpha queues dry-run pages, pages explicitly configured with
  `default_action: review`, pages with quality warnings, and optionally pages
  below `run.grading.review_below_grade`, and pages matching `run.rerun_if`.
  Queue entries carry grade evidence when grading caused or informed the
  decision.
- The JSON Schema for this artifact is at `schemas/audit.schema.json`.
- Schema validation tests are in `tests/pageledger/test_schemas.py`.

## Field table

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `schema_version` | string | ✅ | no | Audit schema version. `"0.1"`. |
| `run_id` | string | ✅ | no | Run identifier from `manifest.json`. |
| `review_queue` | array | ✅ | no | Pages a human should inspect. |
| `quarantine_queue` | array | ✅ | no | Pages excluded from rerun by `quarantine_if`. |

### review_queue / quarantine_queue item fields

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `page_id` | string | ✅ | no | Stable page identifier. |
| `page_number` | integer | ✅ | no | One-based page number. |
| `type` | string | ✅ | no | Page type from taxonomy. |
| `confidence` | number | ❌ | yes | Classifier confidence. Null in alpha. |
| `action` | string | ✅ | no | `review` for review entries; `quarantine` for policy quarantine entries. |
| `reason` | string | ✅ | no | Queue reason. Policy reasons start with `rerun_if:` or `quarantine_if:`. |
| `grade` | string | ❌ | yes | Page grade at queue time (`A`–`F`). Absent on entries queued before grading (configured-review pages). |
| `grade_basis` | string | ❌ | yes | `signals_only` or `schema_aware`. |
