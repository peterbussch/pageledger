# CLI reference

Nine commands ship. `run` and `rerun` extract; `classify` produces an
inspectable route map; `align` re-derives normalized records and grades from
an existing run; the rest inspect, compare, diagnose, or scaffold.

`pageledger --version` prints the installed release.

## run

```bash
pageledger run scans/ --config pageledger.yml --out runs/run-001/
pageledger run scan.pdf --adapter pdf_ocr --out runs/run-001/
pageledger run scan.pdf --config pageledger.yml --routes reviewed-routes.yml --out runs/run-002/
```

Extracts every routed page of the inputs into a new run directory. Inputs
are files or directories (directories expand to their direct child files).
`--out` must not already exist.

Exactly one of `--config` or `--adapter` is required:

- `--config pageledger.yml` uses your YAML config.
- `--adapter text|pdf_text|pdf_ocr` runs a built-in adapter with generated
  defaults, no YAML needed. The generated config is recorded in
  `config-snapshot.yml`. PageLedger never reads a config file it was not
  explicitly given, so a stray `pageledger.yml` in the working directory
  has no effect.

Other flags:

| Flag | Effect |
|---|---|
| `--pages "1-8,81,100-110"` | Extract only these source pages (single input). Page ids keep the source numbering, so provenance stays truthful when you sample a large volume. Recorded in `manifest.inputs[].pages`. |
| `--routes FILE` | Execute a complete route map from `pageledger classify`, a human, or an external classifier. Requires `--config`; cannot be combined with `--adapter` or `--pages`. |
| `--dry-run` | Write the route map and planning artifacts without calling extractors. Inspect routing before spending money. |
| `--json` | Machine-readable result on stdout; errors as JSON too. |
| `--log-level LEVEL` | Minimum `run.log` event level: DEBUG, INFO, WARNING, ERROR. |
| `--adapter-path DIR` | Add a directory to `sys.path` so custom adapters named by `run.adapter` or `run.adapter_order` can be imported. |

An imported route map must cover every page of every supplied input exactly
once, use the configured taxonomy, and contain only actions supported by the
configured adapter. Relative source paths resolve from the route-map directory.
Hashes and page counts are checked when supplied; older maps without them are
accepted with warnings and the current values are recorded.
`--dry-run --routes` preserves the proposed decisions without calling
`extract()`.

## classify

```bash
pageledger classify scans/ --config pageledger.yml --out route-map.yml
pageledger classify report.pdf --adapter pdf_text --out route-map.yml --json
pageledger classify --from-run runs/run-001/ --config pageledger.yml --out route-map-v2.yml
```

Probes every page and writes an executable route map plus
`<out-stem>.evidence.jsonl`. The built-in rules classify structural page
shape (`blank`, `sparse`, `prose`, `table_likely`, `unknown`); a configured
hook can replace the decision for a domain taxonomy. This is a separate
stage: `run` never invokes it automatically. Review the evidence, edit the
map if needed, then pass it unchanged to `run --routes`.

Exactly one input mode is allowed:

- Positional files/directories run a cheap probe. Probe precedence is
  `--adapter`, then `classify.adapter`, then `pdf_text` for PDFs or `text` for
  other supported inputs.
- `--from-run DIR` reclassifies retained raw evidence without paying for
  extraction again. The parent must be a complete, executed, original run;
  dry runs, reruns, and `--pages` samples are rejected. It cannot be combined
  with positional inputs or `--adapter`.

Flags:

| Flag | Effect |
|---|---|
| `--config FILE` | Optional taxonomy, action mapping, thresholds, probe adapter, hook, and minimum confidence. |
| `--out FILE` | Required route-map YAML path; the evidence sidecar is written beside it. |
| `--from-run DIR` | Use a parent run's manifest, routes, quality, provenance, and raw artifacts instead of probing inputs. |
| `--adapter SPEC` | Probe adapter name or `module.path:Object`; overrides `classify.adapter`. |
| `--adapter-path DIR` | Add a directory to `sys.path` for custom probe adapters or classifier hooks. |
| `--json` | Print the classification summary as JSON. |

A configured taxonomy must contain every type the active classifier can emit;
this upfront gate is what guarantees the emitted map can round-trip through
`run --routes`. An empty taxonomy is allowed and conservatively routes every
page to review. See [classifier.md](classifier.md).

## rerun

```bash
pageledger rerun runs/run-001/ --config stronger.yml --out runs/run-002/
```

Re-extracts exactly the pages listed in the parent run's
`rerun-manifest.yml` (the pages that were flagged for review), preserving
their page ids and recording parent lineage. A plain `run.adapter` config can
select a stronger engine, or one `run.adapter_order` can define the whole
generation-indexed chain: entry 0 is the original run, entry 1 the first
rerun, and so on. Each entry can carry its own options.

Chain exhaustion with pending pages produces `rerun_status:
chain_exhausted`; those pages stay in human review and no items are marked
executable. `run.max_rerun_depth` is an independent cap and takes precedence
when both limits are reached. The supplied config
is authoritative: if it disagrees with the parent's recorded next adapter,
PageLedger prints an escalation warning and uses the config. Source-integrity
changes fail closed before the child directory is created. The parent ledger
must pass `verify-run`, and its executable queue must still match the audit,
routes, config, grades, quarantine, and lineage evidence. `rerun` takes the
same `--dry-run`, `--json`, `--log-level`, and `--adapter-path` flags as `run`.

## align

```bash
pageledger align runs/run-001/
pageledger align runs/run-001/ --schema table-v2.yml
pageledger align runs/run-001/ --schema table-v2.yml --dry-run
```

Re-aligns an existing run's structured raw pages against a schema and
regrades every page without re-extracting, so iterating on column
aliases costs nothing even when the extraction was paid OCR or a VLM.
Without `--schema` the run's own `config-snapshot.yml` schema is used;
with it, the file (a bare schema mapping or any config with a `schema:`
section) is snapshotted into the run directory as
`align-schema-snapshot.yml`. Rewrites `normalized/`, the grade fields in
`quality.jsonl`, the grade-threshold audit entries, and the rerun
manifest; records the mutation in `run.log` and a `manifest.json`
`alignment` block. `--json` for machine-readable output.

Human output prints separate `Grades (signals)` and `Grades (schema)`
distributions. JSON retains `grade_distribution` for compatibility and adds
`grade_distribution_by_basis` at the top level and in both `before` and
`after`.

`--dry-run` computes the complete replacement in memory and reports
before/after grades, review-queue size, and normalized-record count without
writing any artifact or schema snapshot. Applied alignment stages every
derived file first and writes `manifest.json` last as the commit indicator;
the multi-file update is deliberately not described as a transaction.

## compare-runs

```bash
pageledger compare-runs runs/run-001/ runs/run-002/
```

Page-by-page diff of two runs: character, word, and extraction-time deltas;
warning and grade transitions; adapters; provenance identity; and cost.
Directional totals such as “improved” and “resolved” are counted only when
source bytes, source page, and the effective extractor identity match. That
identity includes the adapter and version, model, prompt hash, determinism,
input/output types, capabilities, and a SHA-256 identity of the recorded
adapter options (the comparison report does not copy their values). Grade
direction has a second gate: both grades must come from the
same PageLedger version and grading configuration, have the same evidence
basis, and (for schema-aware grades) have the same recorded schema identity.
Changed-source, cross-adapter,
same-adapter/different-extractor, and legacy-unknown transitions are shown but
unranked. `--json` exposes extraction and grade comparability separately for
every shared page id.

## verify-run

```bash
pageledger verify-run runs/run-001/
pageledger verify-run runs/run-001/ --json
```

Checks that the files in a run directory agree with one another: manifest
declarations, identifiers, hashes, page counts, raw and normalized references,
quality totals, audit/rerun items, alignment-schema snapshots, and cost totals.
Missing or changed external source files are warnings; malformed or
inconsistent ledger artifacts are errors and produce exit code 1. Verification
checks coherence, not extraction accuracy, and is not a replacement for the
JSON Schema test suite.

## inspect-run

```bash
pageledger inspect-run runs/run-001/
pageledger inspect-run runs/run-001/ --csv > pages.csv
```

Summarizes a run directory: status, page counts, warnings, failures,
review-queue size, records normalized, grade distributions grouped by evidence
basis, cost, and artifact presence. Human output labels each distribution as
`Grades (signals)`, `Grades (schema)`, or `Grades (unknown)` for legacy graded
entries; it never merges these into an unlabeled headline. JSON retains the
aggregate `grade_distribution` for compatibility and adds
`grade_distribution_by_basis`. `--csv` writes one row per page (page id,
counts, confidence, warnings, grade, grade basis, cost, timing) for triage in a
spreadsheet. `--json` emits the summary as JSON.

## init-config

```bash
pageledger init-config --out pageledger.yml
pageledger init-config --adapter pdf_ocr --out pageledger-ocr.yml
```

Writes a minimal valid config. With `--adapter pdf_ocr` the config
includes `adapter_options` (`dpi: 300`, `lang: eng`) so the knobs
non-English collections need are visible from the start.

## doctor

```bash
pageledger doctor
pageledger doctor --json
```

Read-only diagnostics: Python runtime, optional packages, external
commands with versions and install hints, installed Tesseract language
packs (the valid values for `run.adapter_options.lang`), and whether cloud
OCR/VLM keys are present, without printing their values.

## Configuration

The recommended starting point is one `pageledger.yml` with optional
`classify`, plus `taxonomy`, `schema`, and `run` sections; `init-config` writes
the minimal form and
[`examples/pageledger.yml`](examples/pageledger.yml) is a commented copy.
Common `run` settings:

```yaml
taxonomy:
  page_types:
    blank: {default_action: skip}
    sparse: {default_action: review}
    prose: {default_action: transcribe_text}
    table_likely: {default_action: review}
    unknown: {default_action: review}

classify:
  min_confidence: 0.5
  # adapter: pdf_ocr             # omit for per-suffix probe defaults
  # hook: my_project.routes:Classifier
  thresholds:
    table_column_line_ratio: 0.015

run:
  # adapter_order is mutually exclusive with run.adapter/adapter_options.
  adapter_order:
    - adapter: pdf_ocr
      adapter_options: {dpi: 400, lang: rus}
    - adapter: my_project.adapters:StrongerAdapter
      adapter_options: {model: stronger-model}
  budget:
    max_pages: 500              # refuse before extraction if the plan is larger
    max_usd: 5.00
    warn_pages: 400             # alerts do not require a matching cap
    warn_tokens: 500000
    warn_usd: 4.00
    warn_at_percent: 80         # cap-relative warning remains supported
  retry:
    max_retries: 2
    backoff: exponential
  on_page_error: continue         # stop (default) | continue
  max_consecutive_failures: 3     # circuit breaker; 0 disables
  pricing:
    cost_per_page: 0.0015       # only if you want derived cost estimates
  grading:
    review_below_grade: C       # queue pages graded below C (off by default)
  rerun_if:
    - grade_below: C
    - missing_required_columns: true
    - arithmetic_failure_rate_above: 0.05
  quarantine_if:
    - grade_below: D
  max_rerun_depth: 2
```

Each budget unit records one structured first crossing. If absolute and
cap-relative thresholds coexist, the lower effective value wins; an exact tie
is labeled `absolute`. `cost.json` also groups extracted-page usage and
resolved cost under `by_adapter` and `by_page_type`; skipped/review-only pages
do not appear in those rollups.

With `on_page_error: continue`, exhausted page failures are recorded and the
run continues. Any failed page makes the final run `partial` and the CLI exits
nonzero after writing complete artifacts. The circuit breaker halts after the
configured number of consecutive failed pages. Failed pages and pages not
attempted after a halt are written to the rerun manifest.

`rerun_if` adds matching pages to the review queue and rerun manifest.
`quarantine_if` records matching pages in the quarantine queue and excludes
them from rerun items. Rules are evaluated after grading. Each list item must
be a single-key mapping. The predicates are:

- `grade_below` with one of `A, B, C, D, F`. The comparison is strict.
- `missing_required_columns: true`. It only matches aligned pages.
- `arithmetic_failure_rate_above` with a number from 0 to 1. It only
  matches aligned pages with an arithmetic pass rate.

The older `run.grading.review_below_grade` setting still works and can be
used with `rerun_if`. Overlapping reasons are kept in `audit.json` and
joined on the single rerun item.

Split config examples (`page-taxonomy.yml`, `table-schema.yml`,
`run-policy.yml`) live in [`examples/`](examples/) for larger projects.
