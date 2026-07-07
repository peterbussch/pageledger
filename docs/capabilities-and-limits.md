# Capabilities and limits

What the 0.1.x alpha does, what it leaves to you, and what is documented
design rather than working code. This is the honest-scope page; the README
stays short because this exists.

## Built in and tested

- `pageledger run` for text fixtures (form-feed pagination), born-digital
  PDF text layers (`pdf_text`, via `pageledger[pdf]`), and scanned PDFs
  (`pdf_ocr`, using locally installed `pdftoppm` + `tesseract`).
- `pageledger run --adapter text|pdf_text|pdf_ocr` runs a built-in adapter
  without a YAML config. The generated defaults are recorded in
  `config-snapshot.yml`; PageLedger never reads a config file it was not
  explicitly given.
- `--pages "1-8,81,100-110"` extracts a page selection from one source
  while keeping the source page numbering in every artifact. Sampling a
  large volume no longer means splitting the PDF and losing page identity.
- Adapter options (`run.adapter_options`) passed to built-in and custom
  adapter constructors, and `--adapter-path` for loading custom adapter
  modules without touching PYTHONPATH.
- Dry-run mode that writes full planning artifacts without calling
  extractors.
- Per-page provenance (`provenance.jsonl`), quality diagnostics
  (`quality.jsonl`), cost rollups (`cost.json`), structured run logs
  (`run.log`), and review queues (`audit.json`, `audit.md`).
- Word-level OCR confidence: `pdf_ocr` reads Tesseract's per-word
  confidences and records mean, minimum, and the low-confidence tail in
  `quality.jsonl`. Pages where a quarter of the words fall under engine
  confidence 60 get a `low_confidence` warning.
- Historical-orthography detection for pre-1918 Russian: counts of
  abolished letters (ѣ, ѳ, ѵ) and word-final hard signs, with a
  `historical_orthography` warning when a page is orthographically
  mismatched with a modern OCR model. See
  [`multilingual-ocr.md`](multilingual-ocr.md).
- Page-denominated budget enforcement (pages, tokens, dollars) with
  preflight refusal and per-page caps.
- Retry with configurable `max_retries` and optional exponential backoff.
- `pageledger rerun`: re-extracts exactly the pages listed in a previous
  run's rerun manifest (typically with a stronger adapter), preserving page
  ids, recording parent lineage, enforcing `max_rerun_depth`, and warning
  if a source file changed since the parent run.
- `pageledger compare-runs`: page-by-page diff of two runs. Character and
  word deltas, warnings resolved or introduced, adapters, cost.
- `pageledger inspect-run --csv`: one row per page (counts, confidence,
  warnings, cost, timing) for spreadsheet triage.
- Cost provenance: `cost.json` records `cost_basis` (`adapter_reported`,
  `configured_rate`, `mixed`, or `none`) so derived accounting rates are
  never mistaken for provider-billed spend, plus measured
  `extraction_seconds` per page and in total.
- `pageledger doctor`: optional-dependency diagnostics, installed Tesseract
  language packs, and redacted cloud-key status.
- Custom adapters via `module.path:object` import strings.

## Adapter-supported, user-supplied

- OCRmyPDF preprocessing, or any external engine wrapped as a custom
  adapter.
- Cloud OCR/VLM adapters. You provide API keys, adapter code, and pricing.
- Local document-conversion engines (Docling, Marker, Surya) through custom
  adapters.
- Local-LLM cleanup of OCR output (see
  [`examples/local_llm_cleanup_adapter.py`](../examples/local_llm_cleanup_adapter.py)).
- PDF page counting for custom adapters that expose `page_count(source)`.

## Documented design, not yet implemented

- Automatic page classification. The alpha routes every page to the
  configured `default_action` (`review` in dry-run mode); no classifier
  ships.
- Schema alignment. The schema config section is parsed and preserved but
  the runner does not yet produce normalized records; the `normalized/`
  directory stays empty.
- Audit grading. Review queues are populated but no grades are computed.
- Multi-adapter routing chains, `rerun_if`/`quarantine_if` policies, and
  staged CLI commands (`classify`, `extract`, `align`, `audit`).

Details and examples live in [`design.md`](design.md).

## Known limits

- `pdf_text` reads existing text layers. It does not OCR. For scanned PDFs
  use `pdf_ocr` or wrap a stronger engine as a custom adapter.
- `pdf_ocr` needs poppler and Tesseract installed. PageLedger never
  installs OCR engines; it fails with an install hint when they are
  missing, and refuses to start when `run.adapter_options.lang` names a
  language pack that is not installed. OCR quality is Tesseract's, at the
  DPI and language you configure.
- Quality signals are diagnostic, not calibrated. `quality.jsonl` records
  per-page evidence a human should weigh, not accuracy scores. Shape-based
  heuristics cannot detect word-level misrecognition ("matericl" for
  "material"); Tesseract's own word confidence (`low_confidence`) is the
  closest signal the alpha ships, and it reflects the engine's opinion of
  itself, not ground truth.
- Born-digital text layers carry their own defects. Mid-word space
  artifacts («С анкционная» for «Санкционная») pass every shape heuristic;
  they come from the source PDF, not from extraction.
- Quality-warning pages land in `audit.json → review_queue` with reason
  `quality_warning`. Dry-run review entries use route-based reasons.
- No automatic page routing. Every page follows the configured
  `default_action`. Projects that need page-type-aware routing must
  classify pages outside PageLedger for now.
- Reruns re-extract listed pages; they do not merge results. Combining
  parent and rerun outputs into one corpus is the project's decision, and
  `pageledger compare-runs` shows the per-page evidence for making it.
- PageLedger does not make OCR or VLM output correct. It cannot calibrate
  confidence across unrelated extractors, guarantee accuracy, or make a
  right-to-left, mixed-script, tabular, or handwritten collection work
  without explicit adapter and schema configuration. Its job is narrower:
  preserve enough evidence that a researcher can see what ran, what
  failed, what is uncertain, and what should be reviewed or rerun.

## Tested scale and documents

The 0.1.x alpha has been exercised locally on:

- 5,000 synthetic text pages (2.4 s, ~2,100 pages/sec, artifact counts
  verified: raw files = provenance lines = quality lines = pages).
- A 107-page declassified government scan (JFK Assassination Records
  Collection), OCR'd at 1.25 s/page, with a three-tier escalation
  validated on top: free local Tesseract, free local-LLM cleanup, paid
  cloud VLM. Walkthrough:
  [`examples/jfk-scanned-archive.md`](examples/jfk-scanned-archive.md).
- A modern 259-page born-digital Russian report and an 1850 Russian
  military-statistical review (178-page image-only scan, pre-reform
  orthography). Walkthrough: [`multilingual-ocr.md`](multilingual-ocr.md).
- A 72-page born-digital PDF via `pageledger[pdf]`.

This is evidence of the envelope, not a performance guarantee. Stress
tests are marked `@pytest.mark.stress` and skipped in default CI:

```bash
python -m pytest tests/pageledger/ -m stress
```
