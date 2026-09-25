# First PageLedger OCR run

This tutorial takes a PDF through PageLedger: first its text layer, then OCR
with Tesseract through the built-in `pdf_ocr` adapter.

PageLedger counts pages, calls configured adapters, and records the output
with provenance and quality signals. Install OCR engines separately.

For choosing between local OCR, open-source document conversion, cloud OCR, VLM
adapters, and hybrid workflows, see [OCR options](ocr-options.md).

## 1. Install

Install PageLedger with the PDF extra in a virtual environment, and activate
it so the `pageledger` command is on your path:

```bash
python3 -m venv /tmp/pageledger-first-run
source /tmp/pageledger-first-run/bin/activate
python -m pip install "pageledger[pdf]"
pageledger --version
```

OCR also needs Poppler and Tesseract, installed separately: on macOS
`brew install poppler tesseract`, on Debian or Ubuntu
`sudo apt-get install poppler-utils tesseract-ocr`. Contributors can use the
[development setup](../CONTRIBUTING.md).

## 2. Doctor

```bash
pageledger doctor
pageledger doctor --json
```

Doctor is read-only. It reports Python runtime, PATH, optional packages,
external command availability, command versions when available, install hints,
and redacted cloud environment status. It never installs tools or prints secret
values.

## 3. Plan a run without extracting

The commands from here on read `document.pdf` in the current directory. Copy a
PDF of your own there under that name; a few pages are enough.

Write a config for the PDF's text layer and dry-run it:

```bash pageledger-tutorial
cat > pageledger-pdf.yml <<'YAML'
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: pdf_text
YAML
pageledger run document.pdf --config pageledger-pdf.yml --out runs/document-dry --dry-run --json
```

Inspect `runs/document-dry/manifest.json` and `route-map.yml`. A good dry run
shows the PDF's real page count before anything is extracted.

## 4. Read the text layer

```bash pageledger-tutorial
pageledger run document.pdf --config pageledger-pdf.yml --out runs/document-pdf-text --json
```

Read:

- `manifest.json` for run status and page totals.
- `raw/*.txt` for extracted text.
- `provenance.jsonl` for per-page source, adapter, prompt hash, result, and usage.
- `quality.jsonl` for character counts, word counts, short/empty/noisy-text
  warnings, and text-quality metrics.
- `cost.json` for page/token/cost rollups. PageLedger does not hard-code pricing.

A scan has no text layer, or a poor one: its pages come back empty or with
warnings such as `empty_text` in `quality.jsonl`. The
[warnings reference](warnings.md) says what each warning means.

## 5. Read a scan with `pdf_ocr`

For a scan, or a text layer that is junk, use the built-in OCR adapter. Step 2's
doctor output tells you whether Poppler and Tesseract are installed.

```bash pageledger-tutorial
cat > pageledger-ocr.yml <<'YAML'
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: pdf_ocr
YAML
pageledger run document.pdf --config pageledger-ocr.yml --out runs/document-ocr --json
```

Tune DPI and language in the `run` section of your config (keep its
`taxonomy` section, which tells PageLedger which pages to extract):

```yaml
# fragment
run:
  adapter: pdf_ocr
  adapter_options:
    dpi: 400
    lang: eng+deu
```

For a full worked example on a real scanned document, see
[the scanned archive example](examples/jfk-scanned-archive.md).

## 6. Another engine

To read pages with RapidOCR, Apple Vision, a vision model or Tesseract's model
for pre-1918 Russian print, follow the [engine recipes](engine-recipes.md):
each has a complete config. Engines PageLedger does not ship are custom
adapters, named with an import string and found through `--adapter-path`; see
the [adapter protocol](adapter-protocol.md). The example adapters live in the
[`examples` folder on GitHub](https://github.com/peterbussch/pageledger/tree/main/examples);
pip does not install them.

## 7. Rerun flagged pages with a stronger engine

Pages flagged in the text-layer run (they are listed in `audit.json` under
`review_queue` and in `rerun-manifest.yml`) can be read again with OCR and the
two runs compared:

```bash pageledger-tutorial
pageledger rerun runs/document-pdf-text --config pageledger-ocr.yml --out runs/document-rerun
pageledger compare-runs runs/document-pdf-text runs/document-rerun
```

The rerun keeps the original page IDs, records the parent run ID, and enforces
`run.max_rerun_depth`. `compare-runs` shows which warnings the stronger engine
resolved or introduced. When OCR recovers text from an empty page,
`output_inflation` can be expected: it asks you to check the source and does
not by itself show that the engine invented text. PageLedger does not select or
assemble a corrected corpus; you choose which outputs to use.

A replay bundle needs a first-generation run: bundle a fresh OCR run rather
than a rerun.

## 8. Interpreting failures

- Missing `pypdf`: install `pageledger[pdf]` for `pdf_text`, PDF page counts
  and AES-encrypted files.
- `unsupported_encryption`, `malformed_pdf` or `missing_crypto_dependency`
  before any page runs: the named PDF needs a password, uses an encryption
  scheme pypdf cannot read, is damaged, or needs the AES support that
  `pageledger[pdf]` installs. PageLedger does not take passwords or repair
  files; use an unprotected or repaired copy.
- Missing `pdftoppm` or `tesseract`, or a language pack named in `lang`: the
  run stops before any page is read and prints a message that starts with a
  code, for example `missing_binary: ... install Poppler` or
  `missing_language_pack: ... Installed: eng, osd`. No run directory is
  written. Install what the message names, or change `lang`.
- Missing cloud key: set the provider env var only in the shell that needs it.
  Doctor reports presence as redacted metadata.
- Short, empty, or noisy pages in `quality.jsonl`: inspect the raw page artifact
  and compare against the PDF. Treat OCR output as evidence, not truth.
- `render_dpi_capped` in `quality.jsonl`: the PDF declares that page so large
  that rendering it at your DPI would exceed `max_render_pixels`, so it was
  rendered at a lower DPI, which provenance records. A page that would need
  less than 72 DPI stops the run with `render_limit: ...`. See
  [page size and rendering](ocr-options.md#page-size-and-rendering) before raising the limit.
- Adapter crash, budget exceeded, or invalid result: a partial run directory is
  still written. Check `manifest.json` → `status` for `"failed"` (mid-run
  failure) vs. `"completed"` (success). Inspect `run.log` for per-page error
  envelopes including adapter name, page ID, and error message. Pages that
  succeeded before the failure have raw artifacts, provenance lines, and quality
  entries. See [failure recovery and partial-run
  guarantees](run-manifest-spec.md#failure-recovery-and-partial-run-guarantees) for the full failure scenario table and common error actions.

## Document jobs and recovery

For a document that needs both text-layer extraction and OCR, use
[`process`](processing-spec.md). It manages attempts under one budget and
produces a source-linked transcript and report. Document jobs retain
checkpoints automatically; individual `run` commands need `--resumable` at
creation. Both use [`pageledger resume DIR`](checkpoint-spec.md#get-started)
after an interruption.
