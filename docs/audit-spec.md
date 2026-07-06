# Audit Artifact Specification

`audit.json` records the machine-readable audit result for a PageLedger run.
`audit.md` is a human rendering of this file, not a separate source of truth.

## Minimal Shape

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
  "quarantine_queue": []
}
```

## Design Notes

- `review_queue` should contain the pages or records a human should inspect
  before trusting the run.
- `quarantine_queue` should contain pages or records excluded from rerun
  because they matched quarantine policy or exceeded `max_rerun_depth`.
- `audit.md` should be generated from `audit.json`.
- The current alpha queues dry-run pages and pages explicitly configured with
  `default_action: review`. Future versions can add summary counts, quality
  grades, and uncertainty bands, but the v0.1 alpha keeps the shape small.
- The JSON Schema for this artifact is at `schemas/audit.schema.json`.
- Schema validation tests are in `tests/pageledger/test_schemas.py`.

## Field Table

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `schema_version` | string | ✅ | no | Audit schema version. `"0.1"`. |
| `run_id` | string | ✅ | no | Run identifier from `manifest.json`. |
| `review_queue` | array | ✅ | no | Pages a human should inspect. |
| `quarantine_queue` | array | ✅ | no | Pages excluded from rerun. Empty in alpha. |

### review_queue / quarantine_queue Item Fields

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `page_id` | string | ✅ | no | Stable page identifier. |
| `page_number` | integer | ✅ | no | One-based page number. |
| `type` | string | ✅ | no | Page type from taxonomy. |
| `confidence` | number | ❌ | yes | Classifier confidence. Null in alpha. |
| `action` | string | ✅ | no | Extraction action. |
| `reason` | string | ✅ | no | Reason for queue placement. |
