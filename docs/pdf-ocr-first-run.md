# First PageLedger OCR run

This tutorial walks a PDF (replace the path with your own file) through
extraction: born-digital first, then scanned via the built-in `pdf_ocr`
adapter.

PageLedger stays lean: it counts pages, runs configured adapters, writes
provenance, and audits output. It does not install OCR engines for you.

For choosing between local OCR, open-source document conversion, cloud OCR, VLM
adapters, and hybrid workflows, see `docs/ocr-options.md`.

## 1. Clean install

For the current package-index release:

```bash
python3 -m venv /tmp/pageledger-first-run
/tmp/pageledger-first-run/bin/python -m pip install "pageledger[pdf]"
```

If 0.4.1 is not yet on the package index, verify its exact candidate wheel by
absolute path in a fresh environment rather than importing a checkout:

```bash
/tmp/pageledger-first-run/bin/python -m pip install \
  "/absolute/path/to/pageledger-0.4.1-py3-none-any.whl[pdf]"
```

Contributors working from a source checkout use `python -m pip install -e
".[dev,pdf]"`; that is development setup, not the package-install path above.

## 2. Doctor

```bash
/tmp/pageledger-first-run/bin/pageledger doctor
/tmp/pageledger-first-run/bin/pageledger doctor --json
```

Doctor is read-only. It reports Python runtime, PATH, optional packages,
external command availability, command versions when available, install hints,
and redacted cloud environment status. It never installs tools or prints secret
values.

## 3. Born-digital PDF dry run

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

## 4. Born-digital PDF run

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

## 5. Scanned PDF with `pdf_ocr`

If the PDF is scanned (or the text layer is junk), use the built-in OCR
adapter. It needs poppler and Tesseract installed. Step 2's doctor output
tells you if they're missing.

```bash
cat > pageledger-ocr.yml <<'YAML'
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: pdf_ocr
YAML

/tmp/pageledger-first-run/bin/pageledger run \
  /path/to/your/document.pdf \
  --config pageledger-ocr.yml \
  --out runs/document-ocr \
  --json
```

Tune DPI and language in the config:

```yaml
run:
  adapter: pdf_ocr
  adapter_options:
    dpi: 400
    lang: eng+deu
```

For a full worked example on a real scanned document, see
`docs/examples/jfk-scanned-archive.md`. To produce a searchable PDF for other
tools as a side effect, preprocess with `examples/ocrmypdf_preprocess.sh`
instead and run `pdf_text` on the output.

## 6. Custom OCR adapter

Wrap any engine as a custom adapter and name it with an import string:

```yaml
schema_version: "0.1"
taxonomy:
  page_types:
    prose:
      default_action: transcribe_text
run:
  adapter: tesseract_pdftoppm_adapter:TesseractPdftoppmAdapter
```

Point `--adapter-path` at the directory containing the module:

```bash
/tmp/pageledger-first-run/bin/pageledger run \
  /path/to/your/document.pdf \
  --config pageledger-tesseract.yml \
  --out runs/document-tesseract \
  --adapter-path examples \
  --json
```

Custom PDF adapters should expose `page_count(source)`. That lets PageLedger
paginate correctly without knowing about the OCR engine. Constructor options
can come from `run.adapter_options`. See
[the custom adapter protocol](adapter-protocol.md).

## 7. Rerun flagged pages with a stronger engine

If the first run flagged pages in `quality.jsonl` (they also land in
`audit.json` → `review_queue` and `rerun-manifest.yml`), re-extract only
those pages with a different adapter if you want, then compare:

```bash
/tmp/pageledger-first-run/bin/pageledger rerun \
  runs/document-pdf-text \
  --config pageledger-ocr.yml \
  --out runs/document-rerun

/tmp/pageledger-first-run/bin/pageledger compare-runs \
  runs/document-pdf-text runs/document-rerun
```

The rerun keeps the original page ids, records the parent run id, and
enforces `run.max_rerun_depth`. `compare-runs` shows which warnings the
stronger engine resolved or introduced. An empty LLM response on a rerun
is caught as `empty_text` and re-queued for review. Cross-adapter changes are
shown but deliberately unranked, and PageLedger does not automatically select
or assemble a corrected corpus.

## 8. Interpreting failures

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
