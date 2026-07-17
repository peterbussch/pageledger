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
      "confidence": 0.7,
      "action": "review",
      "reason": "prose_text"
    }
  ],
  "quarantine_queue": [
    {
      "page_id": "doc_0001_page_0008",
      "page_number": 8,
      "type": "table_likely",
      "confidence": 0.75,
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
  not this queue. Adapter-chain exhaustion behaves the same way: candidates
  remain here for human review even when the rerun manifest reports
  `chain_exhausted` and clears its executable items.
- `audit.md` should be generated from `audit.json`.
- Current runs queue dry-run pages, pages explicitly configured with
  `default_action: review`, pages with quality warnings, and optionally pages
  below `run.grading.review_below_grade`, and pages matching `run.rerun_if`.
  Queue entries carry grade evidence when grading caused or informed the
  decision.
- Routes produced by `pageledger classify` carry their page type and real
  classifier confidence into this queue. These fixed heuristic confidences
  are uncalibrated evidence, not probabilities. `null` remains valid when a
  decision is unknown or no classifier ran.
- Exhausted extraction failures use `extraction_failed`. If a global failure,
  circuit breaker, or budget cap halts the loop, remaining extraction pages use
  `not_attempted_after_failure` or `not_attempted_after_budget`. These entries
  flow into `rerun-manifest.yml` unless quarantined or depth-capped.
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
| `confidence` | number | ❌ | yes | Route classifier confidence from `route-map.yml`, between 0 and 1 when available. Emitted by current runs; optional for older 0.1 artifacts and null for unknown/unclassified routes. |
| `action` | string | ✅ | no | `review` for review entries; `quarantine` for policy quarantine entries. |
| `reason` | string | ✅ | no | Queue reason. A route sent directly to review can retain a classifier reason such as `prose_text`; policy reasons include `quality_warning`, `grade_below_threshold`, `rerun_if:*`, and `quarantine_if:*`. |
| `grade` | string | ❌ | yes | Page grade at queue time (`A`–`F`). Absent on entries queued before grading (configured-review pages). |
| `grade_basis` | string | ❌ | yes | `signals_only` or `schema_aware`. |

The queue `confidence` is route evidence and is distinct from extractor
confidence in `quality.jsonl`. For an extracted page, the same route value is
also written to `provenance.jsonl` as `route.route_confidence`; later quality
or policy processing copies it into any audit entries it creates.

The classifier and confidence fields are additive within the 0.1 artifact
contract. PageLedger 0.2.0 therefore continues to write
`schema_version: "0.1"`.
