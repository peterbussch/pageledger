# Capabilities and limits

What PageLedger 0.3.0a1 does, what it leaves to you, and what is documented
design rather than working code. This is the honest-scope page; the README
stays short because this exists.

## Built in and tested

- `pageledger run` for text fixtures (form-feed pagination), born-digital
  PDF text layers (`pdf_text`, via `pageledger[pdf]`), and scanned PDFs
  (`pdf_ocr`, using locally installed `pdftoppm` + `tesseract`).
  Per-page provenance identifies the installed pypdf backend for `pdf_text`,
  or the Tesseract and Poppler versions plus DPI/language for `pdf_ocr`.
- `pageledger run --adapter text|pdf_text|pdf_ocr` runs a built-in adapter
  without a YAML config. The generated defaults are recorded in
  `config-snapshot.yml`; PageLedger never reads a config file it was not
  explicitly given.
- `--pages "1-8,81,100-110"` extracts a page selection from one source
  while keeping the source page numbering in every artifact. Sampling a
  large volume no longer means splitting the PDF and losing page identity.
- `--routes route-map.yml` executes complete, reviewed per-page decisions from
  `pageledger classify`, a human, or an external classifier. It validates
  source coverage, page identity, taxonomy types, confidence, prompts, and
  adapter action support before extraction, then records the source route-map
  hash.
- `pageledger classify` probes source pages or reuses retained evidence with
  `--from-run`, assigns the structural types `blank`, `sparse`, `prose`,
  `table_likely`, and `unknown`, maps types to configured actions, and emits an
  executable schema-0.1 route map plus a text-free evidence sidecar. The tested
  workflow is `classify` followed by `run --routes`; see
  [`classifier.md`](classifier.md).
- User-supplied classifier hooks can replace the built-in type decision through
  a `module.path:Object` import string. Hooks declare their page types and
  return a type, confidence, reason, and optional action or prompt. Hook output
  is validated before either artifact is written.
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
- Conservative output-integrity signals: `instruction_echo` detects leaked
  chat-template markers, and reruns record parent character evidence with an
  `output_inflation` warning at the fixed 4× / 1,000-character boundary.
- Unicode-category lexical metrics keep combining marks attached to their
  base-letter tokens. Clean-prose regression fixtures cover Latin, Cyrillic,
  Arabic, Devanagari, Bengali, Gujarati, Gurmukhi, Tamil, Telugu, Kannada, and
  Malayalam scripts; this guards against known shape-warning false positives,
  not OCR errors or language-specific accuracy.
- Page-denominated budget enforcement (pages, tokens, dollars) with preflight
  refusal and per-page caps. Absolute `warn_pages`, `warn_tokens`, and
  `warn_usd` thresholds can alert without a cap; `cost.json` records each
  unit's first crossing and provenance-derived rollups by adapter and page
  type.
- Retry with configurable `max_retries` and optional exponential backoff.
- Optional continuation after exhausted page failures, with a consecutive-
  failure circuit breaker and failed/not-attempted pages added to rerun work.
- `pageledger rerun`: re-extracts exactly the pages listed in a previous
  run's rerun manifest (typically with a stronger adapter), preserving page
  ids, recording parent lineage, enforcing `max_rerun_depth`, re-deriving the
  queue from parent evidence, and refusing changed source bytes or edited
  executable plans before creating the child run.
- Multi-adapter escalation chains through `run.adapter_order`. Generation zero
  uses the first adapter, each explicit `pageledger rerun` advances to the next,
  and manifests record the selected step and planned next adapter. Exhausting
  the chain leaves unresolved pages in review and makes the rerun plan
  non-executable; it does not silently quarantine them.
- Schema alignment: the config `schema` section (columns, aliases, types,
  required fields, arithmetic `checks` with tolerance) maps structured
  extraction output (`markdown_table`, `json`, `csv`) to normalized
  records in `normalized/{page_id}.json`. Header matching is exact
  (casefold + collapsed whitespace) against declared names and aliases.
  It is never fuzzy. Coercion failures and failed checks are recorded evidence,
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
- `run.rerun_if` and `run.quarantine_if` evaluate page grades and
  schema-alignment evidence after grading. Rerun rules add review reasons.
  Quarantine rules keep their audit evidence and exclude matching pages from
  `rerun-manifest.yml`.
- `pageledger align <run-dir> [--schema file.yml]`: re-align and regrade
  an existing run from its raw pages without re-extracting. Iterate on a
  schema without paying for OCR/VLM again. The manifest records the
  re-alignment (`alignment` block, schema hash); external schemas are
  snapshotted into the run directory. `--dry-run` previews the complete
  grade/audit/normalized change without mutating the ledger.
- `pageledger compare-runs`: page-by-page diff of two runs. Character and
  word deltas, warning and grade transitions, adapters, and cost. Directional
  improvement/resolution totals are reported only when source identity and
  the complete effective extractor identity (including a hash of adapter
  options) match. Grade direction additionally requires the same PageLedger
  version, effective grading policy, evidence basis, and (for schema-aware
  grades) the same schema identity. Changed-source,
  cross-adapter, and same-adapter/different-extractor transitions are unranked.
- `pageledger verify-run`: checks cross-artifact ledger coherence, identifiers,
  route-action/page-bucket counts, hashes, and references without claiming OCR
  correctness or requiring a runtime JSON Schema dependency. Current manifests
  count review-only routes separately so incomplete extraction coverage cannot
  hide behind extracted/skipped totals. Current provenance also hashes exact
  raw-output bytes, and the verifier checks that `audit.md` is the deterministic
  rendering of `audit.json`. Removing a raw hash from a current run is an error;
  only legacy manifests that predate generator-version and raw-hash recording
  receive an incomplete-evidence warning.
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
  [`examples/local_llm_cleanup_adapter.py`](../examples/local_llm_cleanup_adapter.py)
  and
  [`examples/ollama_cleanup_adapter.py`](../examples/ollama_cleanup_adapter.py)).
- PDF page counting for custom adapters that expose `page_count(source)`.
- Domain-specific page taxonomies through a classifier hook. Core supplies the
  structural signals and route-map process, while the project supplies the
  semantic decision.

## Documented design, not yet implemented

- Separate staged `extract` and `audit` commands. Extraction and audit behavior
  already exists under `run`; no independent commands ship yet. `classify` and
  `align` do ship.

Details and examples live in [`design.md`](design.md).

## Known limits

- `pdf_text` reads existing text layers. It does not OCR. For scanned PDFs
  use `pdf_ocr` or wrap a stronger engine as a custom adapter.
- `pdf_ocr` needs poppler and Tesseract installed. PageLedger never
  installs OCR engines; it fails with an install hint when they are
  missing, and refuses to start when `run.adapter_options.lang` names a
  language pack that is not installed. OCR quality is Tesseract's, at the
  DPI and language you configure.
- The built-in classifier is structural, not semantic. It has no image model,
  language model, document-domain labels, or region-level routing. Domain types
  require a project hook.
- Classifier confidences are fixed, uncalibrated evidence scores. They rank the
  built-in rule outcomes but are not probabilities, and they are not directly
  comparable with a hook's confidence scale.
- Table classification uses structured-result format, pipe density, or the
  combination of column spacing and digit density. The tuned
  `table_column_line_ratio: 0.015` default recovered six known OCR table
  spreads in a small census dogfood while its digit guard kept the sampled
  prose pages out. This is not broad accuracy calibration; layout loss and
  digit-heavy prose can still produce false negatives or positives.
- An empty `pdf_text` probe cannot distinguish a visually blank PDF page from
  an image-only page. It emits `unknown` with null confidence and routes to
  review. An OCR or text probe can emit `blank`, but that remains a text-output
  judgment rather than proof about page pixels.
- `classify` probe mode has no retry, budget enforcement, cost ledger, or run
  directory. A `pdf_ocr` probe still spends OCR time per page. Probe failures
  become `unknown`/`review`; classifier-hook failures abort the command.
- `classify --from-run` accepts only a full-coverage, non-dry-run
  generation-zero parent. Missing retained evidence for an individual page
  becomes `unknown`/`review`; reruns and `--pages` partial runs are rejected
  rather than emitting an incomplete map.
- A custom classification probe and the later run adapter may count pages
  differently. `run --routes` fails its complete-coverage check in that case;
  PageLedger does not renumber or reconcile the map silently.
- Quality signals are diagnostic, not calibrated. `quality.jsonl` records
  per-page evidence a human should weigh, not accuracy scores. Shape-based
  heuristics cannot detect word-level misrecognition ("matericl" for
  "material"); Tesseract's own word confidence (`low_confidence`) is the
  closest signal the alpha ships, and it reflects the engine's opinion of
  itself, not ground truth.
- Output-integrity signals are deliberately conservative heuristics. A marker
  or large rerun expansion queues review; it does not prove that an adapter
  hallucinated, and absence of a warning does not prove faithful output.
- Grades are deterministic summaries of that same evidence, not accuracy.
  A grade is only comparable when the effective extractor identity, PageLedger
  version, effective grading policy, evidence basis, and any schema-aware
  schema identity match:
  confidence is uncalibrated across engines, versions, models, and prompts, so
  an `A` from one extractor and a `B` from another are not orderable claims. A
  `signals_only` grade from an
  adapter that reports no confidence rests on warning counts alone. The
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
- `run` without `--routes` still sends every page to the configured
  `default_action` (`review` in dry-run mode). Classification is an explicit
  `classify` then `run --routes` workflow so the map remains reviewable evidence.
- Adapter escalation chains advance only when the user runs `pageledger rerun`.
  They do not automatically call every adapter in one run, merge parent and
  child output, or remove unresolved pages from human review.
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

PageLedger has been exercised locally on:

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
- The 0.2.0 structural classifier was checked against retained OCR from five
  sampled pages of a 1916 Bessarabia address-calendar and seven sampled 1939
  census spreads. That pass tuned the column-line threshold; it is evidence for
  the default, not a general benchmark.

This is evidence of the envelope, not a performance guarantee. Stress
tests are marked `@pytest.mark.stress` and skipped in default CI:

```bash
python -m pytest tests/pageledger/ -m stress
```
