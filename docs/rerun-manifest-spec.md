# Rerun manifest specification

`rerun-manifest.yml` records the pages that are candidates for re-extraction
after a PageLedger run. It is a queue derived from
`audit.json` → `review_queue` after removing page ids found in
`quarantine_queue`. It is not a second copy of the full run manifest.

`pageledger rerun RUN_DIR --config CONFIG --out NEW_DIR` consumes this
manifest: it re-extracts exactly the listed pages into a new run directory,
preserving their `page_id` values, recording `parent_run_id` in the new
manifest, and enforcing `run.max_rerun_depth` from the supplied config. The
config may name a different, typically stronger, adapter, or an
`adapter_order` chain whose entry at the new generation is selected. That is
the point.
Before extraction, PageLedger verifies the parent ledger and re-derives the
rerun queue from its audit, route, config, grade, quarantine, depth, and adapter
chain evidence. An edited or inconsistent plan is rejected. If a listed source
file's checksum no longer matches the parent manifest, the rerun fails before
creating the child directory; changed bytes are a new input, not the same
lineage.

The supplied config is the execution authority. The optional `escalation`
block records what the producing run planned; it does not pin a future rerun
to that plan. If the supplied chain selects a different next adapter,
PageLedger warns and uses the config. Supplying a single `run.adapter` in
place of the parent chain likewise warns and overrides the recorded plan.

## Executability

- `rerun_executable`: `true` iff `rerun_status` is `"executable"`.
- `rerun_status`: one of:
  - `"executable"`: items are present and both the depth cap and adapter
    chain allow the next generation.
  - `"empty_queue"`: nothing needed review; nothing to rerun.
  - `"chain_exhausted"`: review candidates remain, but `adapter_order` has no
    later entry. `items` is empty and the audit review queue remains the
    terminal human state.
  - `"no_further_generations"`: the depth cap forbids another generation;
    `items` is empty regardless of the review queue.

## Minimal shape

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
escalation:
  adapter_order:
    - pdf_text
    - pdf_ocr
  step: 0
  next_adapter: pdf_ocr
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

## Top-level fields

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
| `rerun_executable` | boolean | ✅ | `true` iff `rerun_status` is `"executable"`. |
| `rerun_status` | string | ✅ | `"executable"`, `"empty_queue"`, `"chain_exhausted"`, or `"no_further_generations"`. |
| `escalation` | object | ❌ | Adapter-chain evidence for this generation: `adapter_order`, `step`, and `next_adapter`. Present only when `run.adapter_order` produced the run. |
| `items` | array | ✅ | Pages or records to review or extract again. |

### escalation fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `adapter_order` | array of strings | ✅ | Adapter import strings or built-in names, in generation order. Options are retained in the config snapshot, not duplicated here. |
| `step` | integer | ✅ | Zero-based generation that produced this manifest. |
| `next_adapter` | string or null | ✅ | Recorded adapter name for generation `step + 1`, or `null` when the chain is exhausted. Evidence only; the config passed to `rerun` wins. |

## Per-item fields

Each item must include `page_id`, `page_number`, `source`, `action`, `reason`,
and `previous_grade`. `previous_grade` carries the page's grade (`A`–`F`)
from `quality.jsonl` at manifest generation. A page flagged for more than
one reason appears once, with the reasons joined
(`quality_warning+grade_below_threshold`).

## Review queue semantics

The `items` list in `rerun-manifest.yml` is populated from
`audit.json` → `review_queue`. The contents of the review queue depend on
execution mode:

**Dry-run (`reason: dry_run`):**
- Built-in dry-runs route every page to `review` with
  `reason: no_classifier_available` because `run` does not invoke the
  classifier implicitly. Run `pageledger classify` first and pass its route
  map with `run --routes` to preserve classified types and confidences.
- `--routes` dry-runs preserve imported actions; only pages explicitly routed
  to review enter the queue.
- Pages with `default_action: review` also appear (with `reason: configured_review`).

**Execute (`reason: audit_policy`):**
- Pages with one or more quality warnings appear with `reason: quality_warning`.
- Pages with `default_action: review` appear with `reason: configured_review`.
- Pages matching `run.grading.review_below_grade` appear with
  `reason: grade_below_threshold`.
- Pages matching `run.rerun_if` appear with reasons such as
  `rerun_if:grade_below`.
- Any page in `quarantine_queue` is excluded from `items`, even when the
  review queue keeps other reasons for that page.
- Pages with `default_action: skip` do NOT appear (they are skipped entirely).
- Exhausted page failures use `extraction_failed`; remaining pages after a
  failure or budget halt use `not_attempted_after_failure` or
  `not_attempted_after_budget`.
- Clean pages with no warnings and a non-review action do NOT appear.

## max_rerun_depth enforcement

A run at depth D produces a manifest with `rerun_depth: D`. When
`D >= max_rerun_depth`, the manifest emits `items: []` with
`rerun_status: "no_further_generations"`, and `pageledger rerun` refuses to
create generation D+1. With `max_rerun_depth: 0`, no rerun is ever allowed.

`pageledger rerun` also re-checks the cap at execution time against the
config it is given, so tightening the policy later still blocks stale
manifests.

## Adapter-chain enforcement

With `run.adapter_order`, generation D uses entry D for every extraction in
that generation. PageLedger constructs one effective adapter instance; the
chain is escalation across rerun generations, not per-page fallback within a
run. The run manifest records `{adapter_order, step}` and the rerun manifest
adds `next_adapter`.

When review candidates remain but no entry exists for D+1, the rerun manifest
uses `rerun_status: "chain_exhausted"`, `rerun_executable: false`, and
`items: []`. `pageledger rerun` also rejects an attempted generation beyond
the supplied config's chain. `run.max_rerun_depth` is an independent gate: if
the depth cap is reached first, `no_further_generations` takes precedence even
when the chain records another adapter.

At execution time PageLedger compares the parent rerun manifest's recorded
`next_adapter` with the effective adapter selected by the supplied config.
A mismatch is returned and printed as an `escalation_warnings` entry; the
config wins. This makes the artifact an auditable plan without turning stale
configuration into hidden authority.

## Design notes

- The rerun manifest should be generated from audit policy, not edited into
  the parent manifest. `pageledger rerun` rejects edited executable plans.
- Top-level `reason` values: `dry_run` or `audit_policy`.
- Item-level `reason` values: `no_classifier_available`, `configured_review`,
  `quality_warning`, `grade_below_threshold`, `extraction_failed`,
  `not_attempted_after_failure`, `not_attempted_after_budget`, and
  `rerun_if:<predicate>`
  (joined with `+` when a page matches several).
- `previous_grade` is the page grade at generation time; `null` only for
  pages without a quality entry (e.g. dry-run manifests).
- Quarantine takes precedence over rerun.
- Depth and chain exhaustion change only this executable plan; candidates
  remain visible in `audit.json` for human review.
- Each item carries enough information to rerun without reclassifying the
  whole corpus.
- Reruns must preserve the parent manifest link so reviewers can compare
  before and after states.
- The optional `escalation` block and `chain_exhausted` status shipped in
  PageLedger 0.2.0 as additive 0.1 contract extensions; `schema_version`
  remains `"0.1"`.
- Since rerun-manifest.yml is YAML, its field contract is documented here
  rather than a JSON Schema file.
- Schema validation tests are in `tests/pageledger/test_schemas.py` and
  `tests/pageledger/test_rerun.py`.
