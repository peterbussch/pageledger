# CLI reference

Use `process` to manage a document job, or `run` for an extraction run with one
adapter. `rerun` extracts flagged pages into a new run; `resume` recovers pending
work in place. `classify` prepares routes, and `align` revises structured records
from retained output.

`pageledger --version` prints the installed release. Execution errors return
exit code 1 with a diagnostic on stderr. Commands with `--json` also emit the
error as JSON on stdout. Invalid command-line syntax returns exit code 2.

For a complete text-only example, follow [First run](first-run.md).

## process

```bash
pageledger process book.pdf --config processing.yml --pages "1-10" --out jobs/book
```

Processes one source document through local text, OCR, and optional image
stages. `--config` and `--out` are required, and the output directory must be
new. Use the [processing guide](processing-spec.md) to create the config.
Document jobs retain checkpoints automatically. Stages use `processing`
profiles; `run.adapter_order` is reserved for explicit rerun generations and
is rejected here. So are `run` budget, pricing, grading, rerun and retry
settings, which a job would otherwise ignore; set limits under
`processing.limits`. A leftover `run.adapter` or `taxonomy` produces a
warning.

| Flag | Effect |
|---|---|
| `--pages "1-10,25"` | Select source pages while preserving original numbering. |
| `--adapter-path DIR` | Add a trusted import directory for custom adapters. |
| `--review FILE` | Apply source-bound human review receipts before extraction. |
| `--json` | Print the job result as JSON. |

A job can finish with unresolved pages. `completed` means processing finished,
`paused_budget` means a processing limit was reached (continue with
`resume --raise-limit`), and `halted` records a stop that will not be retried
automatically. `process` and `resume` exit 0 for a paused job, which is an
expected outcome of a configured limit, and 1 for a halted or failed one.

## inspect-job, verify-job, review-sheet and review-job

```bash
pageledger inspect-job jobs/book
pageledger inspect-job jobs/book --json
pageledger verify-job jobs/book
pageledger review-sheet jobs/book --out review.csv
pageledger review-job jobs/book --review review.csv --reviewer "Name"
pageledger review-job jobs/book --review review.csv --dry-run
pageledger review-job jobs/book --review reviewed-pages.json
```

`inspect-job` displays `report.md`, or `document.json` with `--json`.
`verify-job` checks the source, retained attempts, selections, and report.
`review-sheet` writes a CSV of the job's pages to fill in with a spreadsheet.
`review-job` records human decisions from that sheet, or from a JSON file
following the [review receipt contract](document-report-spec.md#human-review-receipt),
without extracting anything and keeping earlier receipts. Decisions from a sheet
need `--reviewer NAME` or the `PAGELEDGER_REVIEWER` variable; `--dry-run` checks
and counts them without recording any. See
[Review in a spreadsheet](processing-spec.md#review-in-a-spreadsheet).
All four commands accept `--json`.

## export

```bash
pageledger export jobs/book --format txt --out book.txt
pageledger export jobs/book --format jsonl --out book.jsonl --reviewed-only
```

Writes a job's selected text page by page as `txt`, `md`, `jsonl` or `tei`,
with each page's review state and the attempt that produced it. A job that does
not verify is refused. `--reviewed-only` keeps only pages with a human review
receipt. See [Export document text](export.md) for the formats and for citing an
export.

## run

```bash
pageledger run scans/ --config pageledger.yml --out runs/run-001/
pageledger run scan.pdf --adapter pdf_ocr --out runs/run-001/
pageledger run scan.pdf --config pageledger.yml --routes reviewed-routes.yml --out runs/run-002/
```

Extracts every routed page of the inputs into a new run directory. Inputs
are files or directories. A directory expands to its direct child files in
name order; subfolders are not searched. Hidden files, whose names start with
`.` (such as macOS `.DS_Store` files and `._*` sidecars), are skipped and
listed as `skipped_inputs` in the run result and manifest. A hidden file named
explicitly on the command line is still read. `--out` must not already exist.

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
| `--resumable` | Retain durable page attempts so an interrupted generation-zero execution can resume in the same directory. Requires zero automatic retries and stop-on-error policy; cannot be combined with `--dry-run`. |
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

Human run summaries read the persisted `cost.json` evidence. `Cost USD:
unknown` means no complete dollar total was established; known zero remains
`0.0`, and a known subtotal with unknown pages is explicitly labeled partial.
Adapter-reported totals are labeled as such; configured-rate totals are called
estimates and explicitly distinguished from provider charges; mixed-basis
totals name both evidence sources.
Dry-run output says that no extraction was performed, so its zero is not
presented as a provider charge or projected bill. `--json` result mappings are
unchanged; use `cost.json` for the authoritative `cost_known`, `cost_usd`, and
`cost_basis` fields.

## resume

```bash
pageledger run book.pdf --adapter pdf_ocr --resumable --out runs/book/
# After interruption, with the same source files and adapter environment:
pageledger resume runs/book/
pageledger inspect-run runs/book/
pageledger verify-run runs/book/
```

Continues a run created with `run --resumable`, or a document job created with
`process`, in its existing directory. For a job, use `pageledger resume jobs/book`;
see [job recovery](processing-spec.md#resume-an-interrupted-job). A job paused
by a processing limit continues only with `--raise-limit LIMIT=VALUE`
(repeatable), for example `--raise-limit max_attempt_pages=200`; see
[one budget for the job](processing-spec.md#one-budget-for-the-job). The flag is
refused for runs and for jobs that are not paused.

For an individual run, resume retains the run id. The retained config snapshot, source identities, page
selection, routes and adapter identity are the execution authority. There are
no replacement-input, config, adapter, budget or page-selection flags.
`--adapter-path DIR` loads a trusted custom adapter; `--json` emits a
machine-readable result.

Before further extraction, resume verifies saved page evidence and source
bytes. Verified completed pages and durably saved responses are reused without
another adapter call. A raw text file alone is insufficient. Costs and budgets
include the retained successful pages; unknown dollar costs remain unknown.
Source hashes are checked again before final publication.

An interrupted request with no durable outcome is `outcome_unknown`. Resume
refuses to retry it or start queued calls: the provider might already have
processed and charged for the request. A recorded adapter failure also stops
queued work. Retain the evidence and resolve the request outcome with the
provider or adapter operator before deliberately starting new work. Resume
does not promise exactly-once remote execution.

Run recovery supports generation-zero execute runs. Ordinary
runs without a recovery journal, dry runs and interrupted rerun generations
cannot be resumed. `run.adapter_order` retains its generation semantics;
resume does not advance it. Resume refuses finalized failed runs. For other
finalized runs, it verifies and returns the existing result without reconstructing
it or loading the extraction adapter. Valid later alignment changes are preserved. Use the
audit queue and separate `rerun` command for quality-driven re-extraction.
See the [checkpoint contract](checkpoint-spec.md).

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
It writes a separate selected-page run and does not choose or assemble a final
corrected corpus for you.

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
same PageLedger version and effective grading policy (merged thresholds plus
the low-confidence floor from the retained config or external alignment
schema), have the same evidence basis, and (for schema-aware grades) have the
same recorded schema identity.
Changed-source, cross-adapter,
same-adapter/different-extractor, and legacy-unknown transitions are shown but
unranked. When no pages clear a comparability gate, the human report labels
warning or grade changes `not assessed` instead of presenting zero as an
improvement result. `--json` exposes extraction and grade comparability
separately for every shared page id. Comparison reads its manifest, quality, provenance, and
optional cost evidence only from contained regular files; symlinks are rejected
instead of followed.

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

## bundle

```bash
pageledger bundle RUN_DIR --out BUNDLE_DIR
pageledger bundle RUN_DIR --out BUNDLE_DIR --json
```

Creates a new directory bundle only after `RUN_DIR` passes `verify-run` and is
an ordinary, generation-zero execute run. The bundle contains an unchanged
`baseline/` run (manifest, config, and all declared artifacts), copied source
files under `sources/`, an inventory with SHA-256 hashes, and
`replay-route-map.yml`. `bundle.json` is the index and records the baseline
manifest hash, extractor identity/profile, source identities, and portable
paths; replay evidence records the exact `bundle.json` index hash. The output
directory must not already exist; no archive is created.

The only accepted flags are `--out DIR` (required) and `--json`. There are no
config, adapter, or source override flags. Credential protection is limited to
the normalized exact denylist keys in mappings under `adapter_options` or
`hook_options` in the config snapshot and in persisted manifest extractor
options. Values, arbitrary fields or text, sources, raw artifacts, and logs are
not scanned, so this cannot prove that sensitive data is absent. A missing
optional reproducibility profile does not block ordinary runs, but it means
deterministic replay cannot claim exactness.

## replay

```bash
pageledger replay BUNDLE_DIR --out RUN_DIR
pageledger replay BUNDLE_DIR --out RUN_DIR --adapter-path TRUSTED_DIR --json
```

Validates the untrusted directory bundle, checks its baseline and inventory,
loads the locally available adapter named by the bundle, and runs the ordinary
PageLedger extraction path against the bundled sources. `--adapter-path DIR`
is the only optional override and is a locally trusted import path; a trusted
path must not be equal to, inside, or above the bundle. `--out DIR` is required and must not already
exist. `--json` emits the result and `replay.json` records baseline/local
extractor linkage, profile match, raw equal/different/missing counts, and the
comparison object.
Human output also prints `Raw comparison: N equal / N different / N missing`;
these counts are evidence, not an authenticity claim. See the [replay
boundary](capabilities-and-limits.md#verified-replay-boundary).

Exit codes are consistent across these commands: 0 means bundle creation or a
successful replay (`exact` or `evidence_compared`); 1 means a verified-run,
bundle, adapter, source, or replay-integrity failure (including
`deterministic_mismatch`); 2 is argparse usage failure such as a missing
required flag or an unapproved flag. Human replay output names the outcome;
`--json` includes `outcome` and structured `error`/`code` fields on failure.

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

Human cost wording uses the same evidence distinctions as `run` and `rerun`:
unknown, known zero/nonzero, or a qualified partial subtotal. The JSON summary
retains its existing field names for machine consumers.

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

`run` needs an extraction route. Without `taxonomy.page_types`, `--routes` or
`--adapter`, every page would go to review and nothing would be extracted, so
the command stops with "No extraction route" and names the fixes. A dry run
needs no route. For a deliberate review-only run, map a page type to
`default_action: review`.

Common `run` settings:

```yaml
taxonomy:
  page_types:
    blank: {default_action: skip}
    sparse: {default_action: transcribe_text, review: true}
    prose: {default_action: transcribe_text}
    table_likely: {default_action: transcribe_text, review: true}
    unknown: {default_action: transcribe_text, review: true}

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
  matches aligned pages with an arithmetic pass rate. The comparison is strict:
  a recorded 70% failure rate does not exceed a threshold of `0.7`.

The older `run.grading.review_below_grade` setting still works and can be
used with `rerun_if`. Overlapping reasons are kept in `audit.json` and
joined on the single rerun item.

Split config examples (`page-taxonomy.yml`, `table-schema.yml`,
`run-policy.yml`) live in [`examples/`](examples/) for larger projects.
