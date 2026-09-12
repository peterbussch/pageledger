# PageLedger: auditable OCR and document extraction runs

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/peterbussch/pageledger/main/assets/pageledger-lockup-horizontal-reversed.png">
    <img src="https://raw.githubusercontent.com/peterbussch/pageledger/main/assets/pageledger-lockup-horizontal.png" alt="PageLedger: the tallied page" width="440">
  </picture>
</p>

<p align="center">
  <a href="https://pypi.org/project/pageledger/"><img src="https://img.shields.io/pypi/v/pageledger" alt="PyPI"></a>
  <a href="https://github.com/peterbussch/pageledger/actions/workflows/ci.yml"><img src="https://github.com/peterbussch/pageledger/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/pageledger/"><img src="https://img.shields.io/pypi/pyversions/pageledger" alt="Python versions"></a>
  <a href="https://github.com/peterbussch/pageledger/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/pageledger" alt="License"></a>
  <a href="https://doi.org/10.5281/zenodo.21340651"><img src="https://zenodo.org/badge/DOI/10.5281/zenodo.21340651.svg" alt="DOI"></a>
</p>

PageLedger records document extraction one page at a time. It calls your
chosen engine, tracks source files and adapter settings, checks budgets, and
writes the text with its provenance and review queue. Runs are directories of
JSON, YAML, Markdown, and raw output that you can inspect without a service or
database.

It grew out of a Soviet census digitization project. The same records help
archives, historians, and research teams trace an extracted page back to its
source and reconstruct how it was produced.

## Install

PageLedger 0.5.1 requires Python 3.10 or later:

```bash
pip install pageledger==0.5.1
pageledger --version
```

For PDFs, install `"pageledger[pdf]==0.5.1"`. Scanned PDFs also need Poppler and
Tesseract installed separately. `pageledger doctor` checks the available tools.

## First run

The built-in text adapter needs no OCR engine or provider. This example creates
two source pages; the replacement character on page 2 deliberately triggers a
review warning.

```bash
printf 'first page\fsecond page with a replacement character �\n' > sample.txt
pageledger init-config --out pageledger.yml
pageledger run sample.txt --config pageledger.yml --out runs/first
pageledger inspect-run runs/first
sed -n '1,20p' runs/first/raw/doc_0001_page_0002.txt
sed -n '1,80p' runs/first/audit.md
pageledger verify-run runs/first
```

`runs/first/` contains the extracted text, per-page provenance, quality signals,
cost evidence, and an executable rerun plan. Unknown cost stays unknown.
Verification checks the ledger's integrity; review the output against the
source to judge transcription accuracy.

Rerun the flagged pages into a new directory and compare the results:

```bash
pageledger rerun runs/first --config pageledger.yml --out runs/second
pageledger compare-runs runs/first runs/second
```

This example uses the same adapter, so its output stays the same. Supply a
stronger adapter config when needed. A rerun retains the parent linkage and
source page numbers; you decide which outputs to use.

The [text tutorial](docs/first-run.md) continues through CSV export, review
notes, and bundle replay. For scans, follow the
[PDF/OCR tutorial](docs/pdf-ocr-first-run.md).

## Process a document

For a small offline example, follow the [document-job tutorial](docs/document-first-run.md).
It covers processing, recovery, and review with a synthetic text file.

Document processing and resumable runs require a POSIX system such as macOS or
Linux. Use `process` when you want one job to manage local text extraction, OCR for
pages with defect evidence, and optional image-model attempts. The job retains
every attempt and publishes a transcript with links to source pages and a
report of unresolved work.

Create `processing.yml`:

```yaml
schema_version: "0.1"
run:
  adapter: pdf_text
processing:
  local_ocr:
    adapter: pdf_ocr
    adapter_options:
      lang: eng
      dpi: 300
  limits:
    max_attempt_pages: 100
    max_image_pages: 0
```

With the PDF extra, Poppler, and Tesseract installed, sample a document:

```bash
pageledger process book.pdf --config processing.yml --pages "1-10" --out jobs/book
pageledger inspect-job jobs/book
pageledger verify-job jobs/book
```

Open `jobs/book/transcript.md` for the selected text and `jobs/book/report.md`
for the source links, attempts, and review reasons. Image stages require an
explicit adapter and a positive page limit. A completed job can still have
pages awaiting human review; `review-job` records decisions bound to the source
and selected output. See the [document processing guide](docs/processing-spec.md)
for configuration, budgets, and review receipts.

The [60-page validation report](docs/validation/0.5.1/README.md) shows what
these checks caught and where source review is still needed.

## Recover interrupted work

Document jobs retain recovery evidence automatically. For an individual run,
opt in when starting it:

```bash
pageledger run book.pdf --adapter pdf_ocr --resumable --out runs/book
# After an interruption:
pageledger resume runs/book
```

Use `pageledger resume jobs/book` for a document job. Keep the source files,
output directory, and adapter environment in place. Resume verifies saved
responses and reuses them without another extraction call. A request started
without a saved outcome stops recovery because it may already have been
processed. See the [checkpoint specification](docs/checkpoint-spec.md) for
supported states and recovery limits.

## What the ledger records

| Record | Use |
|---|---|
| Source hashes and page IDs | Trace output to the original document and page, including samples and reruns. |
| Adapter, model, options, and prompt | Identify how each page was extracted. |
| Page, token, time, and cost totals | Check budgets and distinguish reported charges from configured estimates. |
| Quality signals and grades | Find pages to inspect, with the evidence behind each warning. Grades are not accuracy scores. |
| Normalized records | Align structured tables, JSON, or CSV to declared columns and arithmetic checks; retain failed checks and coercions. |
| Review and rerun queues | Re-extract selected pages or hold them for human review. |
| Verified bundles and replay results | Transport a completed generation-zero run and its source bytes; compare output under the recorded adapter identity. |

Built-in adapters cover text files, PDF text layers, and Tesseract OCR. The
[adapter protocol](docs/adapter-protocol.md) supports other engines; the
[Docling example](examples/docling_adapter.py) supplies local layout and VLM
conversion. Core depends on PyYAML, with pypdf in the PDF extra.

`classify` produces a separate route map for review before `run --routes`.
`align` can revise structured records without re-extracting. Bundle replay
requires a locally available compatible adapter; whole document jobs and runs
with image-input evidence cannot yet be bundled. The
[capabilities and limits](docs/capabilities-and-limits.md) describe these
contracts in full.

## Documentation

| Start here | Reference |
|---|---|
| [Documentation index](docs/README.md) | All guides and artifact specifications |
| [Text tutorial](docs/first-run.md) · [PDF/OCR tutorial](docs/pdf-ocr-first-run.md) | Run, inspect, review, and rerun |
| [Document processing](docs/processing-spec.md) | Stages, shared budgets, reports, and human review |
| [Checkpoint recovery](docs/checkpoint-spec.md) | Resume rules and uncertain requests |
| [CLI and configuration](docs/cli.md) | Commands, flags, and settings |
| [Run artifacts](docs/artifacts.md) | Files, identities, and verification |
| [OCR options](docs/ocr-options.md) · [Multilingual OCR](docs/multilingual-ocr.md) | Engines, languages, and historical documents |
| [Scanned archive example](docs/examples/jfk-scanned-archive.md) | Recorded OCR and escalation on a declassified document |

## Contributing

Testing a collection we haven't seen? [Open a corpus
report](https://github.com/peterbussch/pageledger/issues/new?template=corpus-report.yml)
with the script, adapter, page count, and a redacted sample. New
collections are how the quality signals improve. Development setup and
guidelines are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Citing

Software citation lives in [`CITATION.cff`](CITATION.cff). PageLedger
keeps software and source-data citations separate; `dataset_citation` in
the config records the latter into every manifest.

MIT license.
