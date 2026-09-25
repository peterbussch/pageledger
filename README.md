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

PageLedger requires Python 3.10 or later:

```bash
pip install pageledger
pageledger --version
```

For PDFs, install `"pageledger[pdf]"`. Scanned PDFs also need Poppler and
Tesseract installed separately. `pageledger doctor` checks the available tools.

## First run

The built-in text adapter needs no OCR engine or provider. This example creates
two source pages; the replacement character on page 2 deliberately triggers a
review warning.

```bash pageledger-tutorial
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

```bash pageledger-tutorial
pageledger rerun runs/first --config pageledger.yml --out runs/second
pageledger compare-runs runs/first runs/second
```

This example uses the same adapter, so its output stays the same. Supply a
stronger adapter config when needed. A rerun retains the parent linkage and
source page numbers; you decide which outputs to use.

The [text tutorial](https://github.com/peterbussch/pageledger/blob/main/docs/first-run.md) continues through CSV export, review
notes and bundle replay. For scans, follow the
[PDF/OCR tutorial](https://github.com/peterbussch/pageledger/blob/main/docs/pdf-ocr-first-run.md).

## Choose an engine

Built-in adapters read plain text files (`text`), PDF text layers
(`pdf_text`), scans with Tesseract (`pdf_ocr`), and page images with a vision
model behind an OpenAI-compatible endpoint, on your machine or hosted
(`vision`). The [engine recipes](https://github.com/peterbussch/pageledger/blob/main/docs/engine-recipes.md) add RapidOCR,
Tesseract's model for pre-1918 Russian print, Apple Vision and local vision
models, with what each did well and badly on 24 transcribed historical pages.
[OCR options](https://github.com/peterbussch/pageledger/blob/main/docs/ocr-options.md)
compares the approaches, and the
[adapter protocol](https://github.com/peterbussch/pageledger/blob/main/docs/adapter-protocol.md)
connects any other engine.

## Process a document

Use `process` when one job should manage a whole document: it reads the text
layer, sends pages with defects to OCR and, if you allow it, to a vision model,
and keeps every attempt. Create `processing.yml`:

```yaml
schema_version: "0.1"
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

With the PDF extra, Poppler and Tesseract installed, sample a document:

```bash
pageledger process book.pdf --config processing.yml --pages "1-10" --out jobs/book
pageledger inspect-job jobs/book
pageledger verify-job jobs/book
```

`jobs/book/report.md` lists first the pages that need a person, with the text
the job selected, whether engines agree and any review so far. Review those
pages in a spreadsheet with `pageledger review-sheet`, record the decisions with
`review-job`, and write the text out as plain text, Markdown, JSONL or TEI with
`pageledger export`. See [document processing](https://github.com/peterbussch/pageledger/blob/main/docs/processing-spec.md),
[warnings and holds](https://github.com/peterbussch/pageledger/blob/main/docs/warnings.md) and
[export](https://github.com/peterbussch/pageledger/blob/main/docs/export.md). The
[document-job tutorial](https://github.com/peterbussch/pageledger/blob/main/docs/document-first-run.md) walks through a job from
a source checkout, and [process a collection](https://github.com/peterbussch/pageledger/blob/main/docs/process-a-collection.md)
through a folder of real documents.

Document jobs and `resume` need POSIX file locks: use macOS, Linux or, on
Windows, WSL. Plain `run` uses no such locks but is not tested on Windows. The
tutorials use bash.

## Recover interrupted work

Document jobs retain recovery evidence automatically. For an individual run,
opt in when starting it:

```bash
pageledger run book.pdf --adapter pdf_ocr --resumable --out runs/book
# After an interruption:
pageledger resume runs/book
```

Use `pageledger resume jobs/book` for a document job. Keep the source files,
output directory and adapter environment in place. Resume verifies saved
responses and reuses them without another extraction call. A request started
without a saved outcome stops recovery because it may already have been
processed. See the [checkpoint specification](https://github.com/peterbussch/pageledger/blob/main/docs/checkpoint-spec.md) for
supported states and recovery limits.

## What the ledger records

| Record | Use |
|---|---|
| Source hashes and page IDs | Trace output to the original document and page, including samples and reruns. |
| Adapter, model, options and prompt | Identify how each page was extracted. |
| Page, token, time and cost totals | Check budgets and distinguish reported charges from configured estimates. |
| Quality warnings | Find pages to inspect: hollow text layers, model loops, text in the wrong script, lost historical letters. Each warning keeps its evidence; none is an accuracy score. |
| Normalized records | Align structured tables, JSON or CSV to declared columns and arithmetic checks; retain failed checks and coercions. |
| Review and rerun queues | Re-extract selected pages or hold them for human review. |
| Verified bundles and replay results | Carry a completed run and its source bytes elsewhere and compare output under the recorded adapter identity. |

Core depends only on PyYAML; the PDF extra adds pypdf with AES support. The
[capabilities and limits](https://github.com/peterbussch/pageledger/blob/main/docs/capabilities-and-limits.md) say what is and
is not supported, and the [glossary](https://github.com/peterbussch/pageledger/blob/main/docs/glossary.md) defines the terms.

## Documentation

| Start here | Then |
|---|---|
| [Documentation index](https://github.com/peterbussch/pageledger/blob/main/docs/README.md) | Tutorials, how-to guides, reference and explanations |
| [Text tutorial](https://github.com/peterbussch/pageledger/blob/main/docs/first-run.md) and [PDF/OCR tutorial](https://github.com/peterbussch/pageledger/blob/main/docs/pdf-ocr-first-run.md) | Run, inspect, review and rerun |
| [Process a collection](https://github.com/peterbussch/pageledger/blob/main/docs/process-a-collection.md) | A folder of real documents, from sample to export |
| [Troubleshooting](https://github.com/peterbussch/pageledger/blob/main/docs/troubleshooting.md) | What an error or odd result means and what to do |
| [CLI reference](https://github.com/peterbussch/pageledger/blob/main/docs/cli.md) | Commands, options, exit codes and settings |
| [Share and cite results](https://github.com/peterbussch/pageledger/blob/main/docs/share-and-cite.md) | Identifiers, deposits and absolute paths |

## Contributing

Testing a collection we haven't seen? [Open a corpus
report](https://github.com/peterbussch/pageledger/issues/new?template=corpus-report.yml)
with the script, adapter, page count and a redacted sample. New collections
are how the quality signals improve. Development setup and guidelines are in
[CONTRIBUTING.md](https://github.com/peterbussch/pageledger/blob/main/CONTRIBUTING.md).

## Citing

Cite the software with [CITATION.cff](https://github.com/peterbussch/pageledger/blob/main/CITATION.cff). PageLedger keeps
software and source-data citations separate: `dataset_citation` in the config
records the latter in every manifest.

MIT license.
