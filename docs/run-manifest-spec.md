# Run Manifest Specification

Every PageLedger run should produce a canonical `manifest.json`. The manifest
is the durable pointer to every other artifact in the run directory.

## Minimal Shape

```json
{
  "schema_version": "0.1",
  "run_id": "run-20260619T193000000000Z",
  "parent_run_id": null,
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
    "rerun_manifest": "rerun-manifest.yml"
  },
  "summary": {
    "pages_total": 240,
    "pages_extracted": 212,
    "pages_skipped": 21,
    "pages_quarantined": 0,
    "records_normalized": 0,
    "estimated_cost_usd": 12.44
  }
}
```

## Required Fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | Manifest schema version. |
| `run_id` | string | Stable identifier for this run. |
| `parent_run_id` | string or null | Previous run if this is a rerun. |
| `execution_mode` | string | `dry_run` or `execute`. |
| `started_at` | ISO timestamp | Run start time in UTC. |
| `completed_at` | ISO timestamp or null | Run completion time in UTC. |
| `status` | string | `running`, `completed`, `failed`, or `partial`. |
| `inputs` | array | Source files and checksums. |
| `config` | object | Run-directory config snapshot path, checksum, and source config paths. |
| `extractors` | array | Extractor adapters and model/version metadata. |
| `dataset_citation` | object or null | Optional user-provided source citation for the input collection. |
| `artifacts` | object | Relative paths to route, raw, normalized, audit, and provenance artifacts. |
| `summary` | object | Counts and cost summary for quick inspection. |

Required `summary` keys are `pages_total`, `pages_extracted`,
`pages_skipped`, `pages_quarantined`, `records_normalized`,
`estimated_cost_usd`, and `quality_warning_pages`.

Use `partial` for dry runs and other runs that intentionally produce only part
of the extraction lifecycle.

## Design Notes

- Keep the v0.1 manifest JSON-native and tool-friendly. DDI, DataCite, TEI,
  PAGE, and ALTO integrations can be exporters later.
- Reruns must set `parent_run_id` and should preserve a link to the parent
  manifest.
- Hashes should cover source inputs and configs, not credential files.
- `config-snapshot.yml` should copy the resolved user config used for the run
  so the manifest's config hash is inspectable later.
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
  `compute_seconds`. Per-page `cost_usd` values remain in provenance usage;
  aggregate dollar cost is an estimate, not a provider invoice.

## Compatibility Policy

PageLedger artifacts carry `schema_version: "0.1"` as their release contract.

- **Patch releases** (e.g. 0.1.0 → 0.1.1) may add new nullable or optional
  fields to any artifact. Existing fields must not be renamed, removed, or
  have their type or nullability changed.
- **Minor releases** (e.g. 0.1 → 0.2) may add required fields, remove fields,
  or change field types after a `schema_version` bump. The previous schema
  version's artifacts remain readable but are treated as legacy.
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
- New items in the `warnings` taxonomy for `quality.jsonl`.
- New keys in the `budget` object of `cost.json` when no cap is configured.
- New `reason` values in the `rerun-manifest.yml` or route map.

## Failure Recovery and Partial-Run Guarantees

PageLedger is designed to produce inspectable artifacts even when a run fails.
The following guarantees hold for all failure paths:

### Artifact write order

Artifacts are written in this order, which matters for interrupted-run recovery:

1. `config-snapshot.yml` — copied before any extraction
2. `route-map.yml` — written immediately after extraction loop exits (even on failure)
3. `manifest.json` — always written, `status: "failed"` when appropriate
4. `audit.json` + `audit.md` — written from the review queue as it stood at failure
5. `provenance.jsonl` — only successful pages; empty on pre-extraction failure
6. `quality.jsonl` — only successful pages; empty on pre-extraction failure
7. `cost.json` — partial data when only some pages succeeded
8. `run.log` — all logged events including the failure entry
9. `rerun-manifest.yml` — review queue items at the point of failure
10. Raw artifacts (`raw/*.txt`) — written per-page as extraction succeeds

If the process is killed during artifact writing, artifacts written earlier
may survive while later ones do not. The presence of `manifest.json` with
`"status": "failed"` is the canonical signal that writing completed.

### Failure scenarios

| Scenario | manifest.status | pages_extracted | provenance | raw/*.txt | run.log |
|---|---|---|---|---|---|
| Preflight budget exceeded | *no run dir created* | — | — | — | — |
| Pre-extraction config error | *no run dir created* | — | — | — | — |
| Adapter fails on page 1 | `"failed"` | 0 | empty | none | error entry |
| Adapter fails on page N (>1) | `"failed"` | N−1 | N−1 lines | N−1 files | error entry |
| Invalid adapter result | `"failed"` | prior pages | prior pages only | prior pages only | error entry |
| Budget mid-run | `"failed"` | pages up to cap | pages up to cap | pages up to cap | budget entry |
| Retry exhausted, adapter still fails | `"failed"` | prior pages | prior pages only | prior pages only | retry+error entries |
| Dry-run (always succeeds) | `"partial"` | 0 | empty | none | summary entry |

### Key invariants

- **No `"completed"` status on failure.** A run that raised an exception or hit
  a budget cap always has `"failed"` or `"partial"` in `manifest.status`.
- **Provenance is written only for successfully extracted pages.** Failed,
  skipped, and review-only pages do not appear in `provenance.jsonl`.
- **Raw artifacts exist only for successful pages.** The `raw/` directory
  contains output for pages that completed extraction without error.
- **`run.log` always has the failure entry.** Even if only one page failed, the
  log entry carries the adapter name, page ID, error envelope, attempt count,
  and retry status. The `error` field is a serializable dict (not a traceback).
- **No secrets in logs or exceptions.** `AdapterExecutionError` captures
  stdout/stderr snippets (max 1000 chars each) but the runner never prints
  environment variables or credential values.
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
| `Adapter 'X' failed for ...` | Adapter exception | Check `run.log` for error envelope; inspect adapter |
| `usage must be JSON-serializable` | Adapter returned non-serializable usage | Fix adapter `usage` dict |
| `usage.pages must be exactly 1` | Adapter misreported page count | Adapter must set `usage.pages = 1` |
