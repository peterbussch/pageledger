# CLI reference

Eight commands ship. `run` and `rerun` are the ones that extract; `align`
re-derives normalized records and grades from an existing run; the rest
inspect, compare, or scaffold.

`pageledger --version` prints the installed release.

## run

```bash
pageledger run scans/ --config pageledger.yml --out runs/run-001/
pageledger run scan.pdf --adapter pdf_ocr --out runs/run-001/
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
| `--dry-run` | Write the route map and planning artifacts without calling extractors. Inspect routing before spending money. |
| `--json` | Machine-readable result on stdout; errors as JSON too. |
| `--log-level LEVEL` | Minimum `run.log` event level: DEBUG, INFO, WARNING, ERROR. |
| `--adapter-path DIR` | Add a directory to `sys.path` so a custom `run.adapter` module can be imported. |

## rerun

```bash
pageledger rerun runs/run-001/ --config stronger.yml --out runs/run-002/
```

Re-extracts exactly the pages listed in the parent run's
`rerun-manifest.yml` (the pages that were flagged for review), preserving
their page ids and recording parent lineage. Pass a config with a stronger
adapter to escalate just the weak pages. Enforces `run.max_rerun_depth`
and warns when a source file changed since the parent run. Takes the same
`--dry-run`, `--json`, `--log-level`, and `--adapter-path` flags as `run`.

## align

```bash
pageledger align runs/run-001/
pageledger align runs/run-001/ --schema table-v2.yml
pageledger align runs/run-001/ --schema table-v2.yml --dry-run
```

Re-aligns an existing run's structured raw pages against a schema and
regrades every page — without re-extracting, so iterating on column
aliases costs nothing even when the extraction was paid OCR or a VLM.
Without `--schema` the run's own `config-snapshot.yml` schema is used;
with it, the file (a bare schema mapping or any config with a `schema:`
section) is snapshotted into the run directory as
`align-schema-snapshot.yml`. Rewrites `normalized/`, the grade fields in
`quality.jsonl`, the grade-threshold audit entries, and the rerun
manifest; records the mutation in `run.log` and a `manifest.json`
`alignment` block. `--json` for machine-readable output.

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
source bytes, source page, and adapter match. Cross-adapter, changed-source,
and legacy-unknown transitions are shown but unranked. `--json` exposes the
comparability evidence for every shared page id.

## verify-run

```bash
pageledger verify-run runs/run-001/
pageledger verify-run runs/run-001/ --json
```

Checks that the files in a run directory agree with one another: manifest
declarations, identifiers, hashes, page counts, raw and normalized references,
quality totals, audit/rerun items, and cost totals. Missing or changed external
source files are warnings; malformed or inconsistent ledger artifacts are
errors and produce exit code 1. Verification checks coherence, not extraction
accuracy, and is not a replacement for the JSON Schema test suite.

## inspect-run

```bash
pageledger inspect-run runs/run-001/
pageledger inspect-run runs/run-001/ --csv > pages.csv
```

Summarizes a run directory: status, page counts, warnings, failures,
review-queue size, records normalized, grade distribution, cost, artifact
presence. `--csv` writes one row per page (page id, counts, confidence,
warnings, grade, cost, timing) for triage in a spreadsheet. `--json` for
the summary as JSON.

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

The recommended starting point is one `pageledger.yml` with `taxonomy`,
`schema`, and `run` sections; `init-config` writes it and
[`examples/pageledger.yml`](examples/pageledger.yml) is a commented copy.
Common `run` settings:

```yaml
run:
  adapter: pdf_ocr              # text | pdf_text | pdf_ocr | module.path:Object
  adapter_options:
    dpi: 400
    lang: rus                   # pageledger doctor lists installed packs
  budget:
    max_pages: 500              # refuse before extracting page 501
    max_usd: 5.00
  retry:
    max_retries: 2
    backoff: exponential
  pricing:
    cost_per_page: 0.0015       # only if you want derived cost estimates
  grading:
    review_below_grade: C       # queue pages graded below C (off by default)
  max_rerun_depth: 2
```

Split config examples (`page-taxonomy.yml`, `table-schema.yml`,
`run-policy.yml`) live in [`examples/`](examples/) for larger projects.
