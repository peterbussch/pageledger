# CLI reference

Use `process` to manage a document job, or `run` for an extraction run with one
adapter. `rerun` extracts flagged pages into a new run; `resume` recovers pending
work in place. `classify` prepares routes, and `align` revises structured records
from retained output.

`pageledger --version` prints the installed release. Invalid command-line
syntax returns exit code 2. Runtime errors return 1; commands can also return 1
for a failed result as shown below. Commands with `--json` emit runtime errors
as JSON on stdout and a diagnostic on stderr.

For a complete text-only example, follow [First run](first-run.md).

## Command index

Each command below accepts `-h`/`--help`. Required arguments and options are
shown in the synopsis; optional path flags have no implicit path default. Terms
such as stage, route and disposition are defined in the
[glossary](glossary.md).

| Command | Synopsis and options | Exit code 0 | Exit code 1 |
|---|---|---|---|
| `process` | `process SOURCE --config FILE --out DIR [--pages RANGE] [--adapter-path DIR] [--review FILE] [--json]` | Job completed or paused by a limit. | Job halted or failed, or runtime error. |
| `inspect-job` | `inspect-job JOB_DIR [--json]` | Report read. | Runtime error, or with `--json`, the job is halted or failed. |
| `verify-job` | `verify-job JOB_DIR [--json]` | Job verifies. | Verification fails or runtime error. |
| `review-sheet` | `review-sheet JOB_DIR --out FILE [--json]` | Sheet written. | Runtime error. |
| `review-job` | `review-job JOB_DIR --review FILE [--reviewer NAME] [--dry-run] [--json]` | Decisions checked or recorded. | Job halted/failed or runtime error. |
| `export` | `export JOB_DIR --format txt|md|jsonl|tei --out FILE [--reviewed-only]` | Verified export written. | Runtime error or refused export. |
| `run` | `run INPUT... (--config FILE | --adapter text|pdf_text|pdf_ocr) --out DIR [--pages RANGE] [--routes FILE] [--resumable] [--dry-run] [--json] [--log-level LEVEL] [--adapter-path DIR]` | Run completed, including partial results without failed or unattempted pages. | Partial run with failed or unattempted pages, or runtime error. |
| `resume` | `resume RUN_DIR [--adapter-path DIR] [--raise-limit LIMIT=VALUE] [--json]` | Work resumed, paused, or already finalized. | Halted/failed result or runtime error. |
| `rerun` | `rerun PARENT_DIR --config FILE --out DIR [--dry-run] [--json] [--log-level LEVEL] [--adapter-path DIR]` | Rerun completed. | Any partial execution result or runtime error. |
| `classify` | `classify [INPUT...] --out FILE [--config FILE] [--from-run DIR] [--adapter SPEC] [--adapter-path DIR] [--json]` | Route map written. | Runtime error. |
| `doctor` | `doctor [--json]` | Diagnostics reported; findings do not change the exit code. | Runtime error. |
| `init-config` | `init-config [--out FILE] [--adapter text|pdf_text|pdf_ocr]` | Config written or printed. | Runtime error. |
| `inspect-run` | `inspect-run RUN_DIR [--json | --csv]` | Summary written. | Runtime error. `--json` and `--csv` are mutually exclusive (usage error 2). |
| `align` | `align RUN_DIR [--schema FILE] [--json] [--dry-run]` | Alignment applied or previewed. | Runtime error. |
| `compare-runs` | `compare-runs RUN_A RUN_B [--json]` | Comparison written, including when changes are unranked. | Runtime error. |
| `verify-run` | `verify-run RUN_DIR [--json]` | Verification passes. | Verification fails or runtime error. |
| `bundle` | `bundle RUN_DIR --out DIR [--json]` | Bundle written. | Runtime error. |
| `replay` | `replay BUNDLE_DIR --out DIR [--adapter-path DIR] [--json]` | Outcome is `exact` or `evidence_compared`. | `deterministic_mismatch` or runtime error. |

Any command-line syntax error, including a missing required argument or an
unrecognized option, exits 2. `--version` exits 0.

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

Extracts every routed page of the inputs into a new run directory. Inputs are
files or directories. Each input directory expands to its direct child files
in name order; subfolders are not searched. Hidden children, whose
names start with `.` (such as macOS `.DS_Store` files and `._*` sidecars), are
skipped and listed as `skipped_inputs` in the run result and manifest. An
explicitly named hidden file is still read. Document numbers follow the sorted
expanded input set, so skipped hidden files do not shift numbering. `--out`
must not already exist.

To process a collection as separate document jobs, run once per file. This
shell loop uses direct child files and skips hidden names:

```bash
mkdir -p jobs
for source in collection/*; do
  [ -f "$source" ] || continue
  case "$(basename "$source")" in .*) continue ;; esac
  name=$(basename "$source")
  pageledger process "$source" --config processing.yml --out "jobs/$name"
done
```

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
| `--resumable` | Keep page attempts so an interrupted run can be resumed in place. Needs zero automatic retries and `on_page_error: stop`; cannot be combined with `--dry-run`. See the [checkpoint contract](checkpoint-spec.md). |
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

Human run summaries read `cost.json`. `Cost USD:
unknown` means no complete dollar total was established; known zero remains
`0.0`, and a known subtotal with unknown pages is explicitly labeled partial.
Adapter-reported totals are labeled as such; configured-rate totals are called
estimates and explicitly distinguished from provider charges; mixed-basis
totals name both evidence sources.
Dry-run output says that no extraction was performed, so its zero is not
presented as a provider charge or projected bill. `--json` result mappings are
unchanged; use `cost.json` for the `cost_known`, `cost_usd`, and
`cost_basis` fields.

## resume

```bash
pageledger run book.pdf --adapter pdf_ocr --resumable --out runs/book/
# After interruption, with the same source files and adapter environment:
pageledger resume runs/book/
pageledger inspect-run runs/book/
pageledger verify-run runs/book/
```

Continues an individual run created with `run --resumable`, or a document job
created with `process`, in its existing directory. For document-job recovery
and processing limits, see [document processing](processing-spec.md). For
individual-run recovery rules, see the [checkpoint contract](checkpoint-spec.md).
`--adapter-path DIR` adds a trusted adapter import directory; `--json` emits a
machine-readable result. `--raise-limit LIMIT=VALUE` applies only to a paused
document job; see [one budget for the job](processing-spec.md#one-budget-for-the-job).

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

Compares shared pages by character and word counts, extraction time, warnings,
grades, adapters, provenance, and cost. Directional totals such as “improved”
and “resolved” are counted only when the stated comparison gates pass.
`--json` reports extraction and grade comparability separately for each shared
page id.

| Is a directional improvement counted? | Rule |
|---|---|
| Extraction and warning changes | Only when source bytes, source page, and effective extractor identity match. Identity includes adapter/version, model, prompt hash, determinism, input/output types, capabilities, and adapter-options hash. |
| Grade changes | Also requires the same PageLedger version, grading policy, grade basis, and—when schema-aware—the same schema identity. |
| Different or unknown identities | Changes are shown but unranked. If no page qualifies, the report says `not assessed`, not zero improvement. |

Grade policy includes merged grade thresholds and the low-confidence floor
from the retained config or external alignment schema.

Adapter option values are not copied into the comparison report. Inputs must
be contained regular files; symbolic links are rejected.

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
config, adapter, or source override flags. Credential filtering checks only
normalized exact denylist keys in mappings under `adapter_options` or
`hook_options` in the config snapshot and persisted extractor options. It does
not scan values, arbitrary fields or text, sources, raw artifacts, or logs.
Warning: do not treat a bundle as scrubbed of secrets or personal data. A
missing optional reproducibility profile does not block ordinary runs, but it means
deterministic replay cannot claim exactness.

## replay

```bash
pageledger replay BUNDLE_DIR --out RUN_DIR
pageledger replay BUNDLE_DIR --out RUN_DIR --adapter-path TRUSTED_DIR --json
```

Validates the untrusted directory bundle, checks its baseline and inventory,
loads the locally available adapter named by the bundle, and runs the ordinary
PageLedger extraction path against the bundled sources. `--adapter-path DIR`
is the only optional override and is a locally trusted import path; it must
not be equal to, inside, or above the bundle. `--out DIR` is required and must
not already exist. `--json` emits the result and `replay.json` records baseline/local
extractor linkage, profile match, raw equal/different/missing counts, and the
comparison object.
Human output also prints `Raw comparison: N equal / N different / N missing`;
these counts are evidence, not an authenticity claim. See the [replay
boundary](capabilities-and-limits.md#verified-replay-boundary).

See the [command index](#command-index) for exit codes. `--json` includes
replay `outcome` and structured `error`/`code` fields on failure.

## inspect-run

```bash
pageledger inspect-run runs/run-001/
pageledger inspect-run runs/run-001/ --csv > pages.csv
```

Summarizes a run directory: status, page counts, warnings, failures,
review-queue size, records normalized, grade distributions grouped by evidence
basis, cost, and artifact presence. Human output labels each distribution as
`Grades (signals)`, `Grades (schema)`, or `Grades (unknown)` for older graded
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

Configuration sections and defaults:

| Key | Type | Default / allowed values |
|---|---|---|
| `schema_version` | string | `0.1` |
| `dataset_citation.label`, `.text` | strings | Optional citation label and text. |
| `language.script` | string | Optional: `Cyrillic`, `Latin`, `Greek`, `Arabic`, `Devanagari`. |
| `language.orthography` | string | Optional: `prereform`. |
| `taxonomy.page_types` | mapping | Optional; entries may have `default_action` (`transcribe_text`, `skip`, `review`), `prompt` (string), and `review` (boolean, default false). |
| `classify.adapter` | string | No default; absent means suffix-based probe (`pdf_text` for PDF, `text` otherwise). |
| `classify.adapter_options` | mapping | Empty; passed to the probe adapter. |
| `classify.hook`, `classify.hook_options` | string, mapping | Optional hook import and options. |
| `classify.min_confidence` | number 0–1 | `0.5`. |
| `classify.thresholds` | mapping | Classifier threshold overrides; omitted values use built-in defaults. |
| `run.adapter` | string | Optional: `text`, `pdf_text`, `pdf_ocr`, `rapidocr`, `vision`, or a custom adapter's `module:object` import path. |
| `run.adapter_options` | mapping | Empty; adapter-specific. Cannot accompany `adapter_order`. |
| `run.adapter_order` | non-empty list | Optional chain; entries are adapter strings or `{adapter, adapter_options}` mappings. Mutually exclusive with `adapter` and top-level `adapter_options`. |
| `run.budget.max_pages`, `max_tokens`, `max_usd` | non-negative integer, integer, number | No cap unless set; these limits are also used by `process` only when placed under `processing.limits`. |
| `run.budget.warn_pages`, `warn_tokens`, `warn_usd` | non-negative integer, integer, number | No absolute warning unless set. |
| `run.budget.warn_at_percent` | number 0–100 | No percentage warning unless set; relative to a configured cap. |
| `run.pricing.cost_per_page`, `cost_per_1k_tokens` | non-negative numbers | No configured rate. Used only when adapter-reported cost is absent. |
| `run.retry.max_retries` | non-negative integer | `0`. |
| `run.retry.backoff` | string | `none`; `exponential` is also allowed. |
| `run.on_page_error` | string | `stop`; or `continue`. |
| `run.max_consecutive_failures` | non-negative integer | `0` disables the circuit breaker. |
| `run.grading.review_below_grade` | string | Off; one of `A`, `B`, `C`, `D`, `F`. |
| `run.grading.thresholds` | mapping | Optional overrides for `confidence` (`A`, `B`, `C`, `D`), `required_column_coverage` (`A`, `B`, `C`), and `arithmetic_pass_rate` (`A`, `B`, `C`); numeric values merge over built-in thresholds. |
| `run.rerun_if`, `run.quarantine_if` | list of single-key mappings | Empty. Rules support `grade_below`, `missing_required_columns: true`, `arithmetic_failure_rate_above` (0–1). |
| `run.max_rerun_depth` | non-negative integer | `2`. |
| `schema.name` | string | Required when `schema` exists. |
| `schema.columns[].name` | string | Required. `type` is `string` (default), `integer`, or `number`; `required` is boolean (default false); `aliases` is a list of strings (default empty). |
| `schema.checks[]` | list | Optional entries require `name` and `expression`; `tolerance` is a non-negative number (default 0). Expressions allow one `==` and `+`, `-`, `*` over declared numeric columns and numeric constants. |
| `schema.quality.minimum_required_column_coverage`, `low_confidence_threshold` | numbers 0–1 | Optional grading floors. |
| `processing.local_text`, `local_ocr`, `image`, `second_opinion` | stage mappings or null | `local_text` required; OCR and image stages default disabled. Each stage has `adapter`, `adapter_options` (mapping, empty by default), and `prompt` (non-empty string). `second_opinion` requires `image`; image requires `local_ocr` and positive `max_image_pages`. |
| `processing.limits.max_attempt_pages`, `max_tokens`, `max_cost_usd` | non-negative integer, integer, number | No limit unless set. |
| `processing.limits.max_image_pages` | non-negative integer | `0`. Must be positive to enable image stage. |
| `processing.links.article`, `.custody` | non-empty strings | Optional caller-supplied links; PageLedger does not verify them. |
| `processing.benchmark` | mapping | Optional; `{stage, every_nth_page}` requires an enabled stage and positive integer interval. |

Built-in `pdf_ocr` adapter options are `dpi` (integer 50–1200, default 300),
`lang` (Tesseract language codes separated by `+`, default `eng`) and
`max_render_pixels` (integer 1,000,000–400,000,000, default 60,000,000).
Rendering has a 120-second per-page timeout and OCR a 300-second per-page
timeout. Adapter-specific options are passed through; custom adapters define
their own options.

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
