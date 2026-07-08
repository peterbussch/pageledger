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
- Schema alignment: the config `schema` section (columns, aliases, types,
  required fields, arithmetic `checks` with tolerance) maps structured
  extraction output (`markdown_table`, `json`, `csv`) to normalized
  records in `normalized/{page_id}.json`. Header matching is exact
  (casefold + collapsed whitespace) against declared names and aliases —
  never fuzzy. Coercion failures and failed checks are recorded evidence,
  never silent fixes. Plain-text pages are not aligned.
- Per-page quality grades (A–F) in `quality.jsonl`, `audit.json`/`audit.md`,
  `inspect-run`, its CSV, and `compare-runs`. Grades combine text signals
  (confidence bands, warning counts) with schema evidence (required-column
  coverage, arithmetic pass rate) and always carry their basis:
  `A (signals)` and `A (schema)` are different claims. Thresholds are
  configurable under `run.grading.thresholds`.
- `run.grading.review_below_grade: C` adds pages graded below the
  threshold to the review queue (reason `grade_below_threshold`) and the
  rerun manifest, which now records `previous_grade`. Off by default.
- `pageledger align <run-dir> [--schema file.yml]`: re-align and regrade
  an existing run from its raw pages without re-extracting — iterate on a
  schema without paying for OCR/VLM again. The manifest records the
  re-alignment (`alignment` block, schema hash); external schemas are
  snapshotted into the run directory.
- `pageledger compare-runs`: page-by-page diff of two runs. Character and
  word deltas, warnings resolved or introduced, grades improved or
  regressed, adapters, cost.
- `pageledger inspect-run --csv`: one row per page (counts, confidence,
  warnings, grade, cost, timing) for spreadsheet triage.
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
- Multi-adapter routing chains and the full `rerun_if`/`quarantine_if`
  policy grammar (`review_below_grade` is the shipped subset), and the
  remaining staged CLI commands (`classify`, `extract`, `audit` — `align`
  ships in 0.1.3).

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
- Grades are deterministic summaries of that same evidence, not accuracy.
  A grade is only comparable within one adapter: confidence is
  uncalibrated across engines, so an `A` from one extractor and a `B`
  from another are not orderable claims. A `signals_only` grade from an
  adapter that reports no confidence rests on warning counts alone — the
  `(signals)`/`(schema)` label exists so that weaker evidence is never
  mistaken for schema-checked records.
- Schema alignment consumes structured output only. `pdf_ocr` and other
  plain-text adapters grade on signals alone; producing tables is the
  adapter's job (see
  [`examples/tesseract_tsv_table_adapter.py`](../examples/tesseract_tsv_table_adapter.py)
  for a deliberately naive demonstration).
- Born-digital text layers carry their own defects. Mid-word space
  artifacts («С анкционная» for «Санкционная») pass every shape heuristic;
  they come from the source PDF, not from extraction.
- Struck-through or overstamped text (e.g. classification portion
  markings on declassified documents) is silently dropped or garbled by
  OCR without any warning firing. Dropped text is invisible to shape
  heuristics; if portion markings are citable metadata in your workflow,
  verify them against the page images.
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
