# CLI reference

Six commands ship in the alpha. `run` and `rerun` are the ones that
extract; the rest inspect, compare, or scaffold.

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

## compare-runs

```bash
pageledger compare-runs runs/run-001/ runs/run-002/
```

Page-by-page diff of two runs: character and word deltas, warnings
resolved or introduced, adapters, and cost. This is how you decide whether
an escalation was worth it. `--json` for machine-readable output.

## inspect-run

```bash
pageledger inspect-run runs/run-001/
pageledger inspect-run runs/run-001/ --csv > pages.csv
```

Summarizes a run directory: status, page counts, warnings, failures,
review-queue size, cost, artifact presence. `--csv` writes one row per
page (page id, counts, confidence, warnings, cost, timing) for triage in a
spreadsheet. `--json` for the summary as JSON.

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
  max_rerun_depth: 2
```

Split config examples (`page-taxonomy.yml`, `table-schema.yml`,
`run-policy.yml`) live in [`examples/`](examples/) for larger projects.
