# PageLedger PDF/OCR Readiness Final Report

Date: 2026-06-24

Sample PDF: `~/Downloads/2023-fraud-and-financial-crime-report.pdf`

Latest stress artifacts:
`<repo>/.stress/pdf-ocr-20260624T164549Z/`

## Capability Summary

- Born-digital PDF text extraction: working end-to-end. 72-page test PDF passes
  dry-run, execute, provenance, quality diagnostics, and cost rollup.
- Adapter protocol: built-in `text` and `pdf_text` adapters with capability
  metadata, plus custom-adapter support via import strings.
- `pageledger doctor`: reports optional deps, command versions, PATH state, and
  redacted cloud environment status.
- Quality diagnostics: `quality.jsonl` with character/word counts,
  empty/short/replacement-character warnings, and embedded-text comparison.
- Preflight validation: adapter-action compatibility, non-empty output
  directory, adapter-input type checks.
- Stress test: 72-page PDF completes with 72 raw files, 72 provenance lines,
  matching counts. Tesseract custom adapter works (2-page capped probe).
  OCRmyPDF preprocessing roundtrip passes. Cloud/heavy probes skipped when tools
  absent.

## What Changed

- Added generic adapter capability metadata: `input_types`, `output_types`,
  `capabilities`, `deterministic`, and optional `page_count(source)`.
- Built-in `text` and `pdf_text` adapters now expose capability metadata and
  page-count hooks.
- Custom PDF-backed adapters can now paginate correctly by exposing
  `page_count(source)`. Older simple adapters remain backward-compatible and
  fall back to one opaque page.
- Provenance and manifest extractor entries now include adapter capabilities.
- Added `quality.jsonl` per-page audit artifacts for adapter runs:
  character count, word count, empty/short warnings, and optional embedded-PDF
  text comparison when available.
- Extended `pageledger doctor --json` and human output with command versions,
  missing-command explanations, install hints, PATH/runtime clarity, and
  redacted cloud environment status.
- Added copy-paste examples under `examples/` for Tesseract via `pdftoppm`,
  OCRmyPDF preprocessing, and a redacted cloud/VLM adapter skeleton.
- Added `docs/ocr-options.md`, a provider-agnostic tier guide covering
  born-digital PDFs, free/local OCR, open-source document conversion, cloud
  OCR/VLM adapters, and hybrid escalation.
- Added `docs/pdf-ocr-first-run.md`, a first-run tutorial covering clean
  install, doctor, dry-run, execution, external OCR preprocessing, custom
  adapters, provenance, quality, and failure interpretation.
- Polished `scripts/stress_pdf_ocr.py` with explicit external-tool path flags,
  optional heavy-probe skipping, redacted cloud status, and capped custom
  Tesseract probing.

## Why The Package Is Still Lean

Runtime dependencies are unchanged in spirit: core still depends on PyYAML, and
PDF support remains optional through `pageledger[pdf]`. No OCR engines, cloud
SDKs, pricing tables, Docling, Marker, Surya, or Tesseract bindings were added
as runtime dependencies. PageLedger records and audits those tools when users
choose them.

## Human Workflow Readiness

A technical user can now:

- Install PageLedger and optional PDF support.
- Run doctor and understand which PDF/OCR/cloud tools are available.
- Choose `pdf_text`, external OCR preprocessing, or a custom adapter path.
- See the correct planned page count before extraction.
- Run extraction and inspect raw output, provenance, cost, route map, and
  quality diagnostics.
- Tell whether a failure is a package bug, UX gap, external environment issue,
  or backlog item.

## Verification

Commands run from `<repo>`:

```bash
.venv/bin/python -m pytest -q
```

Result: `60 passed in 0.58s`.

```bash
.venv/bin/python -m build
```

Result: built `pageledger-0.1.0a1.tar.gz` and
`pageledger-0.1.0a1-py3-none-any.whl`.

```bash
rm -rf .stress/verify-wheel-20260624
python3 -m venv .stress/verify-wheel-20260624
.stress/verify-wheel-20260624/bin/python -m pip install --quiet --upgrade pip
.stress/verify-wheel-20260624/bin/python -m pip install --quiet 'dist/pageledger-0.1.0a1-py3-none-any.whl[pdf]'
.stress/verify-wheel-20260624/bin/pageledger doctor --json
```

Result: clean wheel install passed. Doctor reported `pypdf` available,
`pdftoppm` and `tesseract` available with versions, optional heavy/cloud tools
missing as redacted environment metadata, and no secret values.

Package polish checks also verified that the source distribution includes
`docs/ocr-options.md`, `docs/pdf-ocr-first-run.md`, and the example adapters,
and that wheel metadata exposes direct URLs for the PDF/OCR first-run and OCR
options guides.

```bash
.venv/bin/python scripts/stress_pdf_ocr.py \
  --pdf ~/Downloads/2023-fraud-and-financial-crime-report.pdf \
  --max-cloud-pages 1 \
  --max-heavy-pages 2
```

Result: no critical failures.

Key stress checks:

- `pdf_text` dry-run: 72 pages planned.
- `pdf_text` execute: 72 pages extracted, 72 raw text files, 72 provenance lines.
- Wrong `text` adapter on PDF: expected preflight failure, no output directory.
- OCRmyPDF preprocessing roundtrip: passed and PageLedger read the resulting PDF.
- Custom Tesseract adapter: passed with capped 2-page run and custom
  `page_count(source)` pagination.
- Docling, Marker, Surya: skipped because commands were not found.
- Cloud/VLM OCR: skipped because no supported cloud OCR/VLM env var was set.

## Remaining Weaknesses

| Item | Classification | Reason |
|---|---|---|
| No built-in OCR engine | Backlog / design choice | Keeps PageLedger lean; users bring OCR engines through external preprocessing or adapters. |
| OCR quality is basic | Backlog | Counts and suspicious deltas are useful first-pass diagnostics, not calibrated OCR accuracy. |
| No schema alignment runtime yet | Backlog | Existing alpha still focuses on run evidence and adapter contracts. |
| Docling/Marker/Surya unavailable in this shell | External environment | Optional tools are not required for PageLedger core readiness. |
| No cloud/VLM probe in this shell | External environment | No supported cloud keys were present; doctor and stress output stayed redacted. |

## Readiness Judgment

PageLedger's `0.1.0a1` alpha meets the release-readiness criteria defined in
the backlog: a technical user can install the package, run it on local
text/PDF/custom-adapter workflows, inspect stable artifacts, understand
failures, and trust that PageLedger records what happened without overstating
OCR accuracy or model correctness. It does not pretend to solve OCR itself; it
makes the user's OCR environment diagnosable, adapter choices explicit, page
accounting correct, outputs auditable, and failures classifiable.
