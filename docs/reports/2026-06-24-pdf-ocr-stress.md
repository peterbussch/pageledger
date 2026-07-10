# PageLedger PDF/OCR stress report

Date: 2026-06-24

Sample PDF: `~/Downloads/2023-fraud-and-financial-crime-report.pdf`

Stress artifacts: `<repo>/.stress/pdf-ocr-20260624T162100Z/`

## Executive summary

The clean-install stress run passed the critical PageLedger checks after the
patches in this branch. A wheel built successfully, installed into a fresh venv
with `[pdf]`, imported, exposed CLI help, reported doctor diagnostics, dry-ran
the 72-page PDF, extracted 72 raw text files through `pdf_text`, and rejected
the wrong `text` adapter before creating an output directory.

The sample PDF is born-digital, not a scanned-first stress case. That makes it a
good test for page accounting and provenance, but a limited test for OCR
quality. OCRmyPDF forced OCR and the custom Tesseract adapter both ran, but
their output reinforces that PageLedger needs quality/audit layers around OCR
engines rather than treating OCR text as automatically trustworthy.

## What was patched

- Fixed `pdf_text` dry-run pagination. Before this work, dry-running the
  72-page PDF with `run.adapter: pdf_text` reported one page because PDF page
  counting was disabled in dry-run mode. It now reports `pages_total: 72` and
  queues 72 review items.
- Added adapter/input preflight checks. `run.adapter: text` now rejects `.pdf`
  inputs before writing a run directory, and `run.adapter: pdf_text` rejects
  non-PDF inputs before writing artifacts.
- Added `module.path:object` custom adapter loading so user projects can wrap
  Tesseract, OCRmyPDF, Docling, Marker, Surya, Gemini/OpenRouter, or
  `soviet-corpus` engines without adding heavyweight dependencies to
  PageLedger core.
- Added `pageledger doctor --json` with PageLedger version, Python runtime,
  optional `pypdf` availability, common OCR command availability, and redacted
  cloud OCR/VLM environment checks.
- Added `<repo>/scripts/stress_pdf_ocr.py`, which
  builds PageLedger, installs it into clean venvs, runs the PDF/OCR matrix, and
  writes ignored stress artifacts under `.stress/`.

## Stress results

| Area | Result | Classification |
|---|---|---|
| Clean wheel build in stress venv | Passed | ok |
| Clean install with `[pdf]` | Passed | ok |
| `import pageledger` | Passed, version `0.1.0a1` | ok |
| `pageledger --help` / `pageledger run --help` | Passed | ok |
| `pageledger doctor --json` | Passed with redacted env values | ok |
| `pdf_text` dry-run on full PDF | Passed: `pages_total: 72` | ok |
| `pdf_text` execute on full PDF | Passed: 72 raw files, 72 provenance lines | ok |
| `text` adapter on PDF | Expected failure; no output directory written | ok |
| OCRmyPDF forced OCR pages 1-2 | Passed; PageLedger read the resulting PDF | ok |
| Custom Tesseract adapter | Passed for page 1 through `module.path:object` | ok |
| Docling | Skipped: `docling` command not found | external tool/environment |
| Marker | Skipped: `marker_single` command not found | external tool/environment |
| Surya | Skipped: `surya_ocr` command not found | external tool/environment |
| Cloud/VLM OCR | Skipped: no supported cloud OCR/VLM env var in this shell | external tool/environment |

The exact `python3 -m build` command from the plan failed because
`/opt/homebrew/opt/python@3.14/bin/python3.14` does not have the `build` module
installed. The package itself built successfully with the repo venv and again
inside the stress script's isolated builder venv. This is environment friction,
not a PageLedger package failure.

## Lessons learned

- Page accounting must be exact before extraction. A dry run that reports one
  page for a 72-page PDF destroys the value of a page-denominated ledger.
- Wrong-adapter errors should happen before artifact creation. A failed run
  directory is useful after an extraction attempt, but confusing when the input
  type was obviously incompatible before execution.
- `pdf_text` is a born-digital text extractor, not OCR. It works well on this
  PDF, but it inherits whatever text layer exists.
- OCR quality visibly differs from embedded text. OCRmyPDF's forced OCR sample
  read `Kroll` as `KRULL`, which is the right kind of evidence for an audit
  queue and quality comparison pass.
- Custom adapters work, but custom PDF adapters currently paginate as one page
  unless PageLedger recognizes the adapter as PDF-aware. The Tesseract probe
  succeeded for page 1, but its manifest reported `page_count: 1`.
- Runtime PATH matters. The clean venv doctor found `pdftoppm` and `tesseract`,
  but not `pdfinfo`, `ocrmypdf`, Docling, Marker, or Surya. OCRmyPDF was usable
  only after the stress script installed it into its own temporary venv.
- Cloud/VLM tests need explicit credential context. No supported cloud keys
  were present in this shell, and the doctor correctly reported only
  `set: false` with redacted placeholders.

## Improvements to make next

1. Add an adapter page-count hook.
   Custom adapters should be able to expose `page_count(source)` or a
   `document_kind = "pdf"` capability so PageLedger can paginate PDF-backed
   custom OCR adapters without hardcoding each adapter name.

2. Add an engine capability registry inspired by `soviet-corpus`.
   Keep it lightweight: name, version, deterministic flag, input types,
   capabilities (`text`, `ocr`, `layout`, `tables`, `cloud`, `local`), and
   whether the adapter can count pages before extraction.

3. Add quality comparison artifacts.
   For OCR runs, write a simple per-page comparison report between embedded
   text and OCR text: length delta, normalized edit distance, suspicious short
   pages, and high-signal examples such as `Kroll` versus `KRULL`.

4. Extend `pageledger doctor`.
   Include command versions when available, distinguish shell PATH from bundled
   runtime paths, and suggest install commands without trying to install
   heavyweight OCR packages automatically.

5. Add optional stress-script flags for heavy tools.
   Keep the default safe, but support `--install-heavy docling,marker` or
   explicit `--docling-bin`, `--marker-bin`, and `--surya-bin` paths for
   machines where those tools live outside PATH.

6. Add cloud/VLM credential loading by explicit path.
   Support `--env-file /path/to/.env` in the stress script, print only redacted
   presence, and keep the default one-page cap. Do not auto-read unrelated
   secret files.

7. Borrow `soviet-corpus` batch ideas selectively.
   The useful pieces are checkpoint/resume state, batch summaries, cost
   rollups, and quality grades. PageLedger should adopt those as small ledger
   artifacts, not as a full extraction pipeline.

8. Add CI-level smoke tests.
   Unit tests should keep covering runtime contracts. The full PDF/OCR stress
   script should remain manual or nightly because OCRmyPDF, Docling, Marker,
   Surya, and cloud tools are too environment-sensitive for ordinary CI.

## References

- Docling CLI: https://docling-project.github.io/docling/reference/cli/
- Marker README: https://github.com/datalab-to/marker
- Surya README: https://github.com/datalab-to/surya
- Reference design inspected locally: `the soviet-corpus repository (local checkout)`
