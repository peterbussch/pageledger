# PDF/OCR First Run

This tutorial uses a born-digital PDF (replace the path with your own file):

`/path/to/your/document.pdf`

PageLedger stays lean: it counts pages, runs configured adapters, writes
provenance, and audits output. It does not install OCR engines for you.

For choosing between local OCR, open-source document conversion, cloud OCR, VLM
adapters, and hybrid workflows, see `docs/ocr-options.md`.

## 1. Clean Install

```bash
python3 -m venv /tmp/pageledger-first-run
/tmp/pageledger-first-run/bin/python -m pip install --upgrade pip
/tmp/pageledger-first-run/bin/python -m pip install ".[pdf]"
```

From a built wheel:

```bash
/tmp/pageledger-first-run/bin/python -m pip install "dist/pageledger-0.1.0-py3-none-any.whl[pdf]"
```

## 2. Doctor

```bash
/tmp/pageledger-first-run/bin/pageledger doctor
/tmp/pageledger-first-run/bin/pageledger doctor --json
```

Doctor is read-only. It reports Python runtime, PATH, optional packages,
external command availability, command versions when available, install hints,
and redacted cloud environment status. It never installs tools or prints secret
values.

## 3. Born-Digital PDF Dry Run

Create `pageledger-pdf.yml`:

```yaml
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: pdf_text
```

Dry-run the PDF:

```bash
/tmp/pageledger-first-run/bin/pageledger run \
  /path/to/your/document.pdf \
  --config pageledger-pdf.yml \
  --out runs/document-dry \
  --dry-run \
  --json
```

Inspect `runs/document-dry/manifest.json` and `route-map.yml`. A good dry
run shows the real PDF page count before extraction. If the config omits
`run.adapter`, dry runs can still count PDF pages when `pageledger[pdf]` is
installed, but an explicit `run.adapter: pdf_text` is clearer for a first run.

## 4. Born-Digital PDF Run

```bash
/tmp/pageledger-first-run/bin/pageledger run \
  /path/to/your/document.pdf \
  --config pageledger-pdf.yml \
  --out runs/document-pdf-text \
  --json
```

Read:

- `manifest.json` for run status and page totals.
- `raw/*.txt` for extracted text.
- `provenance.jsonl` for per-page source, adapter, prompt hash, result, and usage.
- `quality.jsonl` for character counts, word counts, short/empty/noisy-text
  warnings, and text-quality metrics.
- `cost.json` for page/token/cost rollups. PageLedger does not hard-code pricing.

## 5. External OCR Preprocessing

If the PDF is scanned or has a weak text layer, preprocess outside PageLedger:

```bash
examples/ocrmypdf_preprocess.sh \
  /path/to/your/document.pdf \
  /tmp/document-ocr.pdf

/tmp/pageledger-first-run/bin/pageledger run \
  /tmp/document-ocr.pdf \
  --config pageledger-pdf.yml \
  --out runs/document-ocrmypdf \
  --json
```

This keeps OCRmyPDF optional. PageLedger audits the resulting PDF text layer.

## 6. Custom OCR Adapter

Use a Python import string:

```yaml
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: tesseract_pdftoppm_adapter:TesseractPdftoppmAdapter
```

Run with the examples directory on `PYTHONPATH`:

```bash
PYTHONPATH=examples /tmp/pageledger-first-run/bin/pageledger run \
  /path/to/your/document.pdf \
  --config pageledger-tesseract.yml \
  --out runs/document-tesseract \
  --json
```

Custom PDF adapters should expose `page_count(source)`. That lets PageLedger
paginate correctly without knowing about the OCR engine.

## 7. Rerun Flagged Pages With a Stronger Engine

If the first run flagged pages in `quality.jsonl` (they also land in
`audit.json` → `review_queue` and `rerun-manifest.yml`), re-extract only
those pages — with a different adapter if you want — and compare:

```bash
PYTHONPATH=examples /tmp/pageledger-first-run/bin/pageledger rerun \
  runs/document-pdf-text \
  --config pageledger-tesseract.yml \
  --out runs/document-rerun

/tmp/pageledger-first-run/bin/pageledger compare-runs \
  runs/document-pdf-text runs/document-rerun
```

The rerun keeps the original page ids, records the parent run id, and
enforces `run.max_rerun_depth`. `compare-runs` shows which warnings the
stronger engine resolved (or introduced — an empty LLM response on a rerun
is caught as `empty_text` and re-queued for review).

## 8. Interpreting Failures

- Missing `pypdf`: install `pageledger[pdf]` for `pdf_text` and PDF page counts.
- Missing `pdftoppm` or `tesseract`: install those external commands or use a
  different adapter.
- Missing cloud key: set the provider env var only in the shell that needs it.
  Doctor reports presence as redacted metadata.
- Short, empty, or noisy pages in `quality.jsonl`: inspect the raw page artifact
  and compare against the PDF. Treat OCR output as evidence, not truth.
- Adapter crash, budget exceeded, or invalid result: a partial run directory is
  still written. Check `manifest.json` → `status` for `"failed"` (mid-run
  failure) vs. `"completed"` (success). Inspect `run.log` for per-page error
  envelopes including adapter name, page ID, and error message. Pages that
  succeeded before the failure have raw artifacts, provenance lines, and quality
  entries. See `docs/run-manifest-spec.md` → "Failure Recovery and Partial-Run
  Guarantees" for the full failure scenario table and common error actions.
