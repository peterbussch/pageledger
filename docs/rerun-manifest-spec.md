# Rerun Manifest Specification

`rerun-manifest.yml` records the pages that are candidates for re-extraction
after a PageLedger run. It is a queue derived from
`audit.json` → `review_queue`, not a second copy of the full run manifest.

`pageledger rerun RUN_DIR --config CONFIG --out NEW_DIR` consumes this
manifest: it re-extracts exactly the listed pages into a new run directory,
preserving their `page_id` values, recording `parent_run_id` in the new
manifest, and enforcing `run.max_rerun_depth` from the supplied config. The
config may name a different (typically stronger) adapter — that is the point.
If a listed source file's checksum no longer matches the parent manifest,
the rerun proceeds but reports a source-integrity warning.

## Executability

- `rerun_executable` — `true` iff `items` is non-empty and another rerun
  generation is allowed by the depth cap.
- `rerun_status` — one of:
  - `"executable"` — items present, next generation allowed.
  - `"empty_queue"` — nothing needed review; nothing to rerun.
  - `"no_further_generations"` — the depth cap forbids another generation;
    `items` is empty regardless of the review queue.

## Minimal Shape

```yaml
schema_version: "0.1"
run_id: run-20260619T193000000000Z-rerun
parent_run_id: run-20260619T193000000000Z
parent_manifest: manifest.json
rerun_depth: 0
max_rerun_depth: 2
created_at: "2026-06-19T20:10:00Z"
reason: audit_policy
rerun_executable: true
rerun_status: executable
items:
  - page_id: doc_0001_page_0003
    page_number: 3
    source: scans/volume_01.pdf
    action: review
    reason: quality_warning
    previous_grade: null
  - page_id: doc_0001_page_0042
    page_number: 42
    source: scans/volume_01.pdf
    action: review
    reason: configured_review
    previous_grade: null
```

## Top-Level Fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `schema_version` | string | ✅ | Rerun manifest schema version. `"0.1"`. |
| `run_id` | string | ✅ | Identifier for the proposed rerun (`<parent>-rerun`). |
| `parent_run_id` | string | ✅ | Run that produced this queue. |
| `parent_manifest` | string | ✅ | Relative path to the parent `manifest.json`. |
| `rerun_depth` | integer | ✅ | Depth of the generating run: 0 for an original run, N for its Nth rerun generation. |
| `max_rerun_depth` | integer | ✅ | Maximum rerun generations allowed by policy. |
| `created_at` | ISO timestamp | ✅ | Queue creation time in UTC. |
| `reason` | string | ✅ | `dry_run` or `audit_policy`. |
| `rerun_executable` | boolean | ✅ | `true` iff items exist and the depth cap allows another generation. |
| `rerun_status` | string | ✅ | `"executable"`, `"empty_queue"`, or `"no_further_generations"`. |
| `items` | array | ✅ | Pages or records to review or extract again. |

## Per-Item Fields

Each item must include `page_id`, `page_number`, `source`, `action`, `reason`,
and `previous_grade`. `previous_grade` carries the page's grade (`A`–`F`)
from `quality.jsonl` at manifest generation. A page flagged for more than
one reason appears once, with the reasons joined
(`quality_warning+grade_below_threshold`).

## Review Queue Semantics

The `items` list in `rerun-manifest.yml` is populated from
`audit.json` → `review_queue`. The contents of the review queue depend on
execution mode:

**Dry-run (`reason: dry_run`):**
- Every page routes to `review` with `reason: no_classifier_available`
  because no page classifier ships.
- Pages with `default_action: review` also appear (with `reason: configured_review`).

**Execute (`reason: audit_policy`):**
- Pages with one or more quality warnings appear with `reason: quality_warning`.
- Pages with `default_action: review` appear with `reason: configured_review`.
- Pages with `default_action: skip` do NOT appear (they are skipped entirely).
- Clean pages with no warnings and a non-review action do NOT appear.

## max_rerun_depth Enforcement

A run at depth D produces a manifest with `rerun_depth: D`. When
`D >= max_rerun_depth`, the manifest emits `items: []` with
`rerun_status: "no_further_generations"`, and `pageledger rerun` refuses to
create generation D+1. With `max_rerun_depth: 0`, no rerun is ever allowed.

`pageledger rerun` also re-checks the cap at execution time against the
config it is given, so tightening the policy later still blocks stale
manifests.

## Design Notes

- The rerun manifest should be generated from audit policy, not edited into
  the parent manifest.
- Top-level `reason` values: `dry_run` or `audit_policy`.
- Item-level `reason` values: `no_classifier_available`, `configured_review`,
  `quality_warning`, `grade_below_threshold` (joined with `+` when a page
  matches several). Future audit failures can add
  `confidence_below_threshold`, `missing_required_columns`, etc.
- `previous_grade` is the page grade at generation time; `null` only for
  pages without a quality entry (e.g. dry-run manifests).
- Quarantine takes precedence over rerun (when implemented).
- Each item carries enough information to rerun without reclassifying the
  whole corpus.
- Reruns must preserve the parent manifest link so reviewers can compare
  before and after states.
- Since rerun-manifest.yml is YAML, its field contract is documented here
  rather than a JSON Schema file.
- Schema validation tests are in `tests/pageledger/test_schemas.py` and
  `tests/pageledger/test_rerun.py`.
