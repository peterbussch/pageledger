# Run manifest specification

Every PageLedger run should produce a canonical `manifest.json`. The manifest
is the durable pointer to every other artifact in the run directory.

## Minimal shape

```json
{
  "schema_version": "0.1",
  "pageledger_version": "0.4.1",
  "run_id": "run-20260619T193000000000Z",
  "parent_run_id": null,
  "run_depth": 0,
  "execution_mode": "execute",
  "started_at": "2026-06-19T19:30:00Z",
  "completed_at": "2026-06-19T19:42:00Z",
  "status": "completed",
  "inputs": [
    {
      "path": "scans/volume_01.pdf",
      "sha256": "abc123",
      "page_count": 240
    }
  ],
  "config": {
    "path": "config-snapshot.yml",
    "sha256": "def456",
    "source_paths": ["/absolute/path/to/pageledger.yml"]
  },
  "extractors": [
    {
      "name": "qwen-local",
      "adapter": "pageledger.adapters.qwen",
      "model": "Qwen3.5-VL",
      "version": "recorded-by-adapter",
      "prompt_hash": "ghi789",
      "deterministic": false,
      "input_types": ["pdf"],
      "output_types": ["markdown_table"],
      "capabilities": ["ocr", "tables", "cloud"]
    }
  ],
  "escalation": {
    "adapter_order": [
      "my_project.adapters:QwenLocalAdapter",
      "my_project.adapters:QwenCloudAdapter"
    ],
    "step": 0
  },
  "dataset_citation": {
    "label": "User-provided source citation",
    "text": "Cite the source images or archival collection separately from PageLedger."
  },
  "artifacts": {
    "config_snapshot": "config-snapshot.yml",
    "route_map": "route-map.yml",
    "raw_dir": "raw/",
    "normalized_dir": "normalized/",
    "audit": "audit.json",
    "audit_md": "audit.md",
    "provenance": "provenance.jsonl",
    "quality": "quality.jsonl",
    "cost": "cost.json",
    "run_log": "run.log",
    "rerun_manifest": "rerun-manifest.yml",
    "replay": "replay.json"
  },
  "summary": {
    "pages_total": 240,
    "pages_extracted": 212,
    "pages_skipped": 21,
    "pages_routed_review": 7,
    "pages_quarantined": 0,
    "records_normalized": 0,
    "estimated_cost_usd": 12.44
  }
}
```

## Required fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | Manifest schema version. |
| `pageledger_version` | string | PageLedger package version that generated the manifest. Current writers always emit it; it is optional only when reading older schema-0.1 artifacts. |
| `run_id` | string | Stable identifier for this run. |
| `parent_run_id` | string or null | Previous run if this is a rerun. |
| `run_depth` | integer | Zero-based rerun generation: `0` for an original run. Current writers always emit it; it is optional only for older schema-0.1 artifacts. |
| `execution_mode` | string | `dry_run` or `execute`. |
| `started_at` | ISO timestamp | Run start time in UTC. |
| `completed_at` | ISO timestamp or null | Run completion time in UTC. |
| `status` | string | `completed`, `failed`, or `partial`. |
| `inputs` | array | Source files and checksums. Each entry carries `path`, `sha256`, and `page_count` (the source's full size). When `--pages` or a rerun limits the run, the entry also carries the selection expression as `pages` (e.g. `"1-8,81"`). |
| `config` | object | Run-directory config snapshot path, checksum, and source config paths. |
| `extractors` | array | Extractor adapters and model/version metadata. Current writers may add an optional `reproducibility_profile` containing the profile envelope and `profile_sha256`; it is path-free and omitted when an adapter has no hook. |
| `dataset_citation` | object or null | Optional user-provided source citation for the input collection. |
| `artifacts` | object | Relative paths to route, raw, normalized, audit, and provenance artifacts. |
| `summary` | object | Counts and cost summary for quick inspection. |
| `alignment` | object | Optional. Present only after `pageledger align` re-derived the run's normalized/grade artifacts: `aligned_at`, `schema_source` (`config_snapshot` or the external schema path, snapshotted as `align-schema-snapshot.yml`), `schema_sha256`, `pageledger_version`. |
| `routing` | object | Optional. Present when `--routes` supplied a reviewed map: `source_path`, `sha256`, and the source map's `source_run_id`. |
| `escalation` | object | Optional. Present when `run.adapter_order` selected the effective adapter: `adapter_order` is the ordered list of adapter names/import strings and `step` is this run's zero-based rerun generation. |
| `replay_schema_version` | string | Optional. Present on a replay run; currently `"0.1"`. |
| `baseline_run_id` | string | Optional. Replay linkage to the transported baseline run. |
| `bundle_manifest_sha256` | string | Optional. SHA-256 of the exact `bundle.json` bytes (the bundle index), recorded on a replay run. |
| `outcome` | string | Optional. Replay result: `exact`, `evidence_compared`, or `deterministic_mismatch`. |

Current writers emit `pages_total`, `pages_extracted`, `pages_skipped`,
`pages_routed_review`, `pages_quarantined`, `records_normalized` (rows written
to `normalized/` by the schema aligner), `estimated_cost_usd`, and
`quality_warning_pages`. `pages_routed_review` remains optional in the schema
so pre-hardening schema-0.1 manifests can still be read; `verify-run` warns
when that legacy evidence is absent.

Current writers also emit top-level `pageledger_version`. It remains optional
in the schema solely so pre-hardening schema-0.1 manifests remain readable;
`verify-run` warns when that generator identity is absent.

Current writers emit `run_depth` on every manifest. For original legacy runs
without it, generation 0 can be inferred from `parent_run_id: null`. A legacy
rerun whose manifest lacks `run_depth` remains readable, but PageLedger will
not execute another generation because its lineage depth is ambiguous.

Runs with failures add `pages_failed`; runs halted before every extraction
action was attempted add `pages_not_attempted`. They are omitted when zero so
older successful 0.1 artifacts keep their compact shape.

`pages_quarantined` counts distinct pages that matched at least one
`quarantine_if` rule.

`pages_routed_review` counts pages whose route action was `review`, so no
extractor ran. It is disjoint from `pages_extracted`, `pages_skipped`,
`pages_failed`, and `pages_not_attempted`. New manifests obey:

```text
pages_total = pages_extracted + pages_failed + pages_not_attempted
            + pages_skipped + pages_routed_review
```

The audit review queue can be larger because post-extraction quality warnings
also enter review; those pages remain part of `pages_extracted` and are not
double-counted in `pages_routed_review`.

Use `partial` for dry runs, runs with failures, and execute-mode runs that
intentionally defer one or more pages through a `review` route. `completed`
means every routed extraction/skip decision finished without a review-only gap;
it does not claim extraction accuracy.

## Adapter escalation

`run.adapter_order` is a generation-indexed escalation chain. Generation D
selects `adapter_order[D]`, constructs one effective adapter instance, and
uses that adapter for every extraction in that generation. It does not try
the whole chain per page. The original run records `step: 0`; its first rerun
records `step: 1`, and so on.

The manifest's optional `escalation` block contains names only:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `adapter_order` | array of strings | ✅ | Built-in names or custom import strings in generation order. Per-step `adapter_options` remain in `config-snapshot.yml`. |
| `step` | integer | ✅ | Zero-based generation whose effective adapter produced this run. |

The current generation's effective adapter is also the extractor recorded in
`extractors` and per-page provenance. The companion `rerun-manifest.yml` adds
`next_adapter`, if any, so the planned next generation is inspectable. See
[`rerun-manifest-spec.md`](rerun-manifest-spec.md).

## Design notes

- Keep the v0.1 manifest JSON-native and tool-friendly. DDI, DataCite, TEI,
  PAGE, and ALTO integrations can be exporters later.
- Reruns must set `parent_run_id` and should preserve a link to the parent
  manifest.
- Hashes should cover source inputs and configs, not credential files.
- `config-snapshot.yml` should copy the resolved user config used for the run
  so the manifest's config hash is inspectable later.
- `run.adapter_options` are retained verbatim in `config-snapshot.yml` and,
  when non-empty, in `manifest.extractors[].options`. Never put API keys,
  passwords, tokens, or other credentials in adapter options; adapters should
  resolve credentials from their normal external credential mechanism.
- The current alpha records the copied config snapshot at `config.path` and the
  absolute source config path in `config.source_paths`. If a future project uses
  split config files, record all source inputs there.
- Extractor entries should include `prompt_hash` whenever prompts influence
  output, and `deterministic` should be false unless the adapter can guarantee
  stable output for the same input and config.
- Pricing should be read from user config and recorded in `cost.json`; the
  manifest summary should report the resulting estimate, not hardcode provider
  rates.
- `estimated_cost_usd` is computed from user-configured prices and recorded
  usage. It is an estimate, not a provider invoice.
- `run.log` should record one JSON line per extractor call with timestamp,
  `page_id`, adapter, status, and any error. It is an operational log, not a
  second audit source.
- `cost.json` reports generated usage rollups with `pages`, `tokens`, and
  `compute_seconds`. Provenance `usage.cost_usd` remains adapter-reported;
  the separate per-page `cost` object records the resolved accounting value.
  Aggregate dollar cost is an estimate, not a provider invoice. Its optional
  `alerts`, `by_adapter`, and `by_page_type` fields are additive 0.1 evidence;
  the rollups contain extracted pages only because they are derived from
  `provenance.jsonl`.

## Compatibility policy

PageLedger artifacts carry `schema_version: "0.1"` as their release contract.

The package release and artifact schema are versioned independently.
PageLedger 0.4.1 keeps artifact `schema_version: "0.1"`: its newer classifier,
escalation, and cost fields are additive and optional, so existing 0.1
artifacts remain readable. A package minor release does not by itself require
an artifact schema bump.

- Package releases may add new nullable or optional fields to an existing
  artifact schema. Existing fields must not be renamed, removed, or have
  their type or nullability changed without a schema-version bump.
- Compatibility is reader-forward: current schemas and commands accept older
  0.1 artifacts. An older schema file with `additionalProperties: false`
  cannot know fields introduced by a later release, so consumers should validate
  with the PageLedger version that reads the artifact.
- A package release may add optional fields while retaining the artifact
  schema version. Adding required fields, removing fields, or changing field
  types requires a `schema_version` bump; the previous schema version's
  artifacts remain readable but are treated as legacy.
- **Breaking changes** require a `schema_version` increment and a changelog
  entry explaining the migration. Consumers that pin to a specific
  `schema_version` can detect breaking changes by inspecting the field.
- **JSON Schema files** under `schemas/` are the machine-readable authority
  for JSON and JSONL artifacts. YAML artifacts (route-map.yml,
  rerun-manifest.yml, config-snapshot.yml) rely on documented field tables in
  their respective spec documents.
- **CI must fail** if a core artifact field is accidentally renamed or removed
  without a schema-version bump. The `tests/pageledger/test_schemas.py` module
  includes field-presence assertions that serve as a first line of defense.

Additions that do NOT require a schema-version bump:
- New optional/nulled-by-default fields in any artifact.
- Optional evidence blocks such as manifest/rerun `escalation`, cost alerts
  and rollups, and classifier identity/evidence fields shipped in 0.2.0.
- New items in the `warnings` taxonomy for `quality.jsonl`.
- New keys in the `budget` object of `cost.json` when no cap is configured.
- New `reason` values in the `rerun-manifest.yml` or route map.

## Failure recovery and partial-run guarantees

PageLedger is designed to produce inspectable artifacts even when a run fails.
The following guarantees hold for all failure paths:

### Artifact write order

Artifacts are written in this order, which matters for interrupted-run recovery:

1. `config-snapshot.yml` is copied before extraction.
2. Raw and normalized page artifacts are written as each page succeeds.
3. `route-map.yml` is written after the extraction loop exits, including on
   failure.
4. `audit.json` and `audit.md` are written from the queues at that point.
5. `provenance.jsonl` and `quality.jsonl` contain successful pages only.
6. `cost.json` records partial totals when only some pages succeeded.
7. `rerun-manifest.yml` records the eligible review pages.
8. `run.log` includes the failure entry.
9. `manifest.json` is written last, with `status: "failed"` when needed.

If the process is killed during artifact writing, artifacts written earlier
may survive while later ones do not. The presence of `manifest.json` is the
canonical signal that PageLedger finished writing every artifact it points to;
`pageledger verify-run` diagnoses an interrupted or manually altered directory.

### Failure scenarios

| Scenario | manifest.status | pages_extracted | provenance | raw/*.txt | run.log |
|---|---|---|---|---|---|
| Preflight budget exceeded | *no run dir created* | n/a | n/a | n/a | n/a |
| Pre-extraction config error | *no run dir created* | n/a | n/a | n/a | n/a |
| Adapter fails on page 1 | `"failed"` | 0 | empty | none | error entry |
| Adapter fails on page N (>1) | `"failed"` | N−1 | N−1 lines | N−1 files | error entry |
| Invalid adapter result | `"failed"` | prior pages | prior pages only | prior pages only | error entry |
| Budget mid-run | `"failed"` | pages up to cap | pages up to cap | pages up to cap | budget entry |
| Retry exhausted, adapter still fails | `"failed"` | prior pages | prior pages only | prior pages only | retry+error entries |
| Page fails with `on_page_error: continue` | `"partial"` | all other successful pages | successful pages only | successful pages only | error + later entries |
| Consecutive-failure breaker opens | `"failed"` | successes before halt | successful pages only | successful pages only | page errors |
| Dry-run (always succeeds) | `"partial"` | 0 | empty | none | summary entry |

### Key invariants

- **No `"completed"` status on failure.** A run that raised an exception or hit
  a budget cap always has `"failed"` or `"partial"` in `manifest.status`.
- **Provenance is written only for successfully extracted pages.** Failed,
  skipped, and review-only pages do not appear in `provenance.jsonl`.
- **Failures remain executable work.** Failed pages enter the audit and rerun
  manifests as `extraction_failed`. Pages left after an adapter or budget halt
  use `not_attempted_after_failure` or `not_attempted_after_budget`.
- **Raw artifacts exist only for successful pages.** The `raw/` directory
  contains output for pages that completed extraction without error.
- **`run.log` always has the failure entry.** Even if only one page failed, the
  log entry carries the adapter name, page ID, error envelope, attempt count,
  and retry status. The `error` field is a serializable dict (not a traceback).
- **Adapter diagnostics fail closed.** `AdapterExecutionError` retains the
  exception class, page, adapter, attempt, and whether stdout/stderr existed.
  Adapter-controlled messages and stdout/stderr contents are replaced with
  `<redacted>` in both `run.log` and terminal output so provider errors cannot
  leak credentials.
- **Config snapshot is always written before extraction.** If a failure happens
  during extraction, `config-snapshot.yml` exists and can be audited.
- **Output directory is empty-or-new before extraction starts.** The runner
  rejects non-empty directories at preflight, so a partial run never contaminates
  a prior run's artifacts.

### Common errors and user action

| Error | Likely cause | Action |
|---|---|---|
| `No configured adapter` | Missing `run.adapter` in config | Add `run.adapter: text` or `run.adapter: pdf_text` |
| `Output directory is not empty` | Reusing a previous run dir | Use a new empty directory |
| `PDF support requires optional dependency` | `pageledger[pdf]` not installed | `pip install "pageledger[pdf]"` |
| `Adapter 'X' does not support action 'Y'` | Adapter/action mismatch | Check `adapter.supports(action)` |
| `Cannot read input file` | Permission error or missing file | `chmod +r` or verify path |
| `Budget exceeded after ...` | Page/token/dollar cap hit | Increase budget caps or reduce input |
| `Adapter 'X' failed for ...` | Adapter exception | Check the redacted `run.log` envelope, then inspect or debug the adapter locally |
| `usage must be JSON-serializable` | Adapter returned non-serializable usage | Fix adapter `usage` dict |
| `usage.pages must be exactly 1` | Adapter misreported page count | Adapter must set `usage.pages = 1` |
