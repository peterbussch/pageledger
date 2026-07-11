# Changelog

All notable changes to PageLedger will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to the artifact compatibility policy documented in
`docs/run-manifest-spec.md` → Compatibility Policy.

## 0.1.7 - 2026-07-11

### Fixed

- **Unicode-script quality correctness:** lexical metrics now tokenize Unicode
  letters together with their combining marks. Clean Devanagari, Bengali,
  Gujarati, Gurmukhi, Tamil, Telugu, Kannada, and Malayalam prose no longer
  looks like one-character OCR fragments or suspicious symbol noise. Arabic,
  Kazakh Cyrillic, Latin, and Russian regression coverage protects the same
  script-safe boundary while existing OCR-fragment fixtures still warn.
- Per-page `word_count` now uses the same Unicode letter-plus-mark tokens as the
  lexical quality metrics instead of disagreeing with them on combining-mark
  scripts.

### Changed

- Built wheels now include the machine-readable JSON Schema contracts under
  `share/pageledger/schemas`; CI and the publish workflow inspect the installed
  or built wheel for them.
- Multilingual documentation now names the clean scripts under regression
  coverage instead of claiming universal language neutrality. README routing,
  review, confidence, historical-model, and reproducibility language is more
  precise.
- `cost_basis: none` is documented as unknown or unreported cost evidence, not
  proof that an adapter was free.

### Compatibility

- Artifact schema version remains `0.1`; no field was added, removed, renamed,
  or retyped. Corrected `word_count`, `suspicious_symbol_count`, and lexical
  metrics can differ from 0.1.6 for scripts that use combining marks or
  non-ASCII punctuation.

## 0.1.6 - 2026-07-10

### Added

- **Executable external routing:** `pageledger run --routes route-map.yml`
  validates and executes complete per-page type, action, confidence, and prompt
  decisions from a human or external classifier. Source coverage, page IDs,
  taxonomy types, hashes, and adapter action support are checked before output
  is created; the executed map and its source hash remain in the ledger.
- **Long-run page failure policy:** `run.on_page_error: continue` finishes
  independent later pages after retry exhaustion. A configurable consecutive-
  failure breaker stops dead services, while failed and not-attempted pages
  enter the audit queue and executable rerun manifest.
- Optional manifest failure counters, route source identity, route document
  hashes/page counts, and per-page derived cost evidence.

### Fixed

- `pageledger align` now re-evaluates `rerun_if` and `quarantine_if` instead of
  leaving policy decisions stale after schema evidence changes.
- Token-priced runs no longer report a known zero cost when an adapter omits
  token usage. Configured per-page rates now appear in `inspect-run --csv`.
- `verify-run` checks route/provenance agreement and failure accounting.
- The current quality schema once again accepts original 0.1 lines that predate
  confidence details and grading; current runs continue to emit those fields.

### Changed

- Release publishing now runs tests, Ruff, and mypy before building or
  publishing. Python 3.14 joins the tested matrix.
- Artifact schema version remains `0.1`; all new artifact fields are optional.

## 0.1.5 - 2026-07-10

### Added

- **Page policies** under `run.rerun_if` and `run.quarantine_if` can act on
  grades, missing required columns, and arithmetic failure rates. Quarantine
  takes precedence over rerun while the audit queue keeps every reason for
  review.
- **Ollama cleanup adapter example** for a Tesseract-first local workflow. It
  calls Ollama over its HTTP API, disables model reasoning, strips thought
  blocks defensively, and records prompt and completion token counts.
- **Two-page spread splitting recipe** using Poppler tools, with page-mapping
  cautions for downstream provenance.
- Static type checking for the package in CI.

### Changed

- Split quality analysis, budget accounting, and read-only reports out of the
  runner module. Compatibility imports preserve the existing Python surface.
- Updated GitHub Actions to the current supported majors: checkout 7,
  setup-python 6, upload-artifact 7, and download-artifact 8.
- Reworked the documentation from top to bottom for accurate claims, clearer
  language, easier navigation, and more useful package and repository search
  metadata.

### Compatibility

- Artifact schema version remains `0.1`. Existing fields keep their names and
  types; policy-free configurations keep their previous behavior.
- `run.grading.review_below_grade` remains supported alongside the new policy
  rules. Multi-adapter `adapter_order` escalation chains remain a design
  target.

## 0.1.4 - 2026-07-10

### Added

- **`pageledger verify-run`** checks cross-artifact ledger coherence:
  declared files, run/schema identities, config and source hashes, route and
  extraction counts, raw/normalized references, audit/rerun membership, and
  cost totals. External source drift is a warning; internal inconsistency is
  an error. It uses only stdlib and PyYAML and does not claim OCR correctness.
- **Read-only alignment preview** with `pageledger align ... --dry-run`,
  reporting before/after grades, review counts, and normalized records without
  writing a schema snapshot or changing the run.
- **Output-integrity evidence** in `quality.jsonl`: `instruction_echo` for
  high-specificity chat-template markers and `output_inflation` when a rerun
  is at least 4× and 1,000 characters larger than its parent page.
- **Alignment structure accounting** records duplicate headers, row-width
  mismatches, and ignored Markdown tables instead of silently discarding that
  ambiguity. Structural loss caps the schema grade at B.
- Top-level `pageledger --version`.

### Changed

- `compare-runs` now ranks warning and grade changes only when provenance
  proves the source bytes, source page, and adapter match. Changed-source,
  cross-adapter, and legacy-unknown transitions remain visible but unranked.
- Configuration, schema, adapter results, and artifact writers reject
  malformed mappings, booleans/fractions used as integers, unsupported schema
  versions, protocol-incomplete adapters, and non-finite JSON values.
- Built-in adapter identity is immutable; custom adapter metadata is required
  rather than invented. Adapter classes/factories are constructed once per
  execution.
- New-run and re-alignment manifests are written last as commit indicators.
  Re-alignment stages all derived output before replacing existing files.
- The duplicate Tesseract example is now a compatibility alias for the built-in
  `PdfOcrAdapter`; the TSV and local-LLM examples gained stricter bounds and
  output validation.

### Compatibility

- Artifact schema version remains `0.1`; new artifact fields are optional and
  no existing field was removed, renamed, or retyped.
- Malformed configs and adapters that depended on undocumented metadata
  invention now fail with explicit errors.

## 0.1.3 - 2026-07-08

### Added

- **Schema aligner** (previously a design target): the config `schema`
  section now drives extraction. Structured page output (`markdown_table`,
  `json`, `csv`) is mapped to declared columns with exact alias matching
  (casefold, collapsed whitespace; never fuzzy), `integer`/`number`
  coercion tolerant of thousand separators, and arithmetic `checks` with
  tolerance, writing one `normalized/{page_id}.json` record per page
  (new JSON Schema `schemas/normalized-page.schema.json`, spec
  `docs/normalized-spec.md`). Coercion failures and failed checks are
  recorded evidence, never silent fixes; unparseable structured payloads
  produce a record with `parse_error` set. Plain-text pages are not
  aligned. Check expressions are validated against an AST whitelist at
  config load.
- **Per-page quality grades (A–F)** (previously a design target):
  `quality.jsonl` lines now carry `grade`, `grade_basis`
  (`signals_only`/`schema_aware`), and `grade_detail`. Grades take the
  worst of a signals axis (confidence bands, warning counts, hard F on
  `empty_text`) and a schema axis (required-column coverage, arithmetic
  pass rate, coercion cap at B). Rendered surfaces always show the basis.
  `A (signals)` and `A (schema)` are different claims because grades are
  evidence summaries per adapter, not calibrated accuracy. Thresholds are
  configurable under `run.grading.thresholds`; the schema `quality` keys
  act as floors.
- **`pageledger align <run-dir> [--schema file.yml]`** re-aligns and
  regrade an existing run from its raw pages without re-extracting, so
  iterating on a schema costs nothing even when extraction was paid.
  Atomic rewrites; `manifest.json` written last with a new `alignment`
  block (timestamp, schema source, hash, version); external schemas
  snapshotted as `align-schema-snapshot.yml`; the mutation logged in
  `run.log` (`status: aligned`).
- **`run.grading.review_below_grade`** sends pages graded strictly below the
  configured letter join the review queue (reason `grade_below_threshold`)
  and the rerun manifest. Off by default: grading annotates without
  changing review behavior unless you opt in. This is the shipped subset
  of the `rerun_if` design target.
- Rerun manifests now fill `previous_grade` (previously always null) and
  list a multi-reason page once with reasons joined
  (`quality_warning+grade_below_threshold`).
- `inspect-run` reports `records_normalized` and a grade distribution;
  `inspect-run --csv` gains `grade` and `grade_basis` columns;
  `compare-runs` counts grades improved/regressed and renders transitions
  (`C (signals)→A (schema)`); audit.md queues gain a grade column.
- `examples/tesseract_tsv_table_adapter.py` clusters Tesseract TSV words
  clustered into a `markdown_table` by pixel coordinates. Deliberately
  naive column clustering, shipped as the structured-output adapter
  example the aligner needs.

### Changed

- The `schema` config section is now strictly validated at load
  (previously parsed and ignored). A schema without `columns`, which was legal
  as an inert stub in 0.1.2, is now a config error.
- Raw artifacts for adapters returning dict/list content are written as
  JSON (`json.dumps`, `ensure_ascii=False`), not Python `repr`. Byte-level
  change for third-party structured adapters; required for `align` to
  re-parse raw pages.
- The prose-calibrated shape warnings (`suspicious_symbol_density`,
  `fragmented_text`) no longer fire on structured formats
  (`markdown_table`/`json`/`csv`), where pipes and braces are construction,
  not garble.
- `quality.jsonl` lines now require the grade fields; external validators
  holding the 0.1 quality-line schema will reject pre-0.1.3 artifacts
  against the new schema (same precedent as `confidence_detail` in 0.1.2).

## 0.1.2 - 2026-07-07

### Added

- Word-level OCR confidence: `pdf_ocr` reads Tesseract's TSV output and
  reports mean page confidence (`ExtractionResult.confidence`, 0–1) plus
  per-word statistics in a new optional `confidence_detail` field. Both are
  recorded in `quality.jsonl`. A new `low_confidence` warning fires when a
  quarter of a page's words fall under engine confidence 60 (10+ words),
  catching weak passages a page mean would hide.
- Historical-orthography detection: `quality.jsonl` now counts letters
  abolished by the 1918 Russian reform (`prereform_letter_count`) and
  word-final hard signs (`terminal_hard_sign_count`), and a
  `historical_orthography` warning flags pre-1918 pages where an OCR model
  trained on modern text will degrade. Calibrated on an 1850 gubernia
  review: 21 terminal ъ per 100 tokens vs 0.00 in modern Russian.
- `--pages` on `run` extracts only the listed source pages
  (`--pages "1-8,81,100-110"`). Page ids keep the source numbering, so
  sampling a large volume no longer means splitting the PDF and losing
  page identity in the ledger. The selection is recorded in
  `manifest.inputs[].pages`.
- `run --adapter text|pdf_text|pdf_ocr` runs a built-in adapter without
  writing a YAML config. The generated defaults are recorded in
  `config-snapshot.yml`; no implicit config file is ever read.
- `inspect-run --csv` writes one row per page (counts, confidence, warnings,
  cost, timing) for spreadsheet triage.
- `pageledger doctor` lists installed Tesseract language packs, and
  `pdf_ocr` fails before extraction with the installed-language list when
  `run.adapter_options.lang` names a pack that is not installed.
- `init-config --adapter pdf_ocr` now includes `adapter_options`
  (`dpi`, `lang`) so the knobs non-English collections need are visible.
- `examples/prereform_normalizer_adapter.py` provides OCR plus pre-1918 Russian
  orthography canonicalization (ѣ→е, і→и, ѳ→ф, ѵ→и, morphology-aware ъ),
  with every rewrite counted in a result warning.

### Changed

- Standard European/Cyrillic typography («guillemets», em/en dashes,
  ellipsis, №, §, °) no longer counts toward `suspicious_symbol_density`,
  which was flagging ordinary Russian bibliographies.

## 0.1.1 - 2026-07-07

### Added

- Built-in `pdf_ocr` adapter OCRs scanned PDFs with locally installed
  Tesseract, rendering pages via `pdftoppm`. No new Python dependencies;
  missing binaries fail with a `pageledger doctor` hint. Reports
  `model: "tesseract <version>"` and measured `compute_seconds` per page.
- `run.adapter_options` is a config mapping passed to the adapter
  constructor. `pdf_ocr` takes `dpi` (default 300) and `lang` (default
  `eng`, `+`-joined for multiple languages). Custom adapter classes and
  factories receive the same options, lifting the old no-argument
  constructor limitation. Options are recorded in `manifest.extractors`
  (new optional `options` field, additive and backward compatible).
- `--adapter-path` on `run` and `rerun` adds a directory to `sys.path`
  so custom adapter modules load without setting PYTHONPATH.
- `pageledger init-config --adapter pdf_ocr`.
- Lexical-shape quality metrics (`alpha_token_count`, `mean_token_length`,
  `short_token_ratio`) in `quality.jsonl`, and a `fragmented_text` warning
  for OCR fragment noise (mean token length < 3 across 20+ tokens). These
  are additive optional fields; the warning taxonomy grows to seven items.
- `docs/examples/jfk-scanned-archive.md` is an end-to-end walkthrough for a
  107-page declassified scanned document from the National Archives.
- Ruff linting (`[tool.ruff]` in pyproject, dev extra, CI lint job).

### Changed

- Docs rewritten around the built-in OCR path: README quickstart,
  `docs/pdf-ocr-first-run.md` (no more `PYTHONPATH=examples`),
  `docs/ocr-options.md` decision matrix, `docs/adapter-protocol.md`
  (adapter options, `--adapter-path`).

## 0.1.0 - 2026-07-06

### Added

- `pageledger run` is an extraction command supporting dry-run artifact
  generation and built-in `text` and `pdf_text` adapter execution.
- `pageledger rerun` re-extracts exactly the pages listed in a previous
  run's `rerun-manifest.yml`, preserving page ids, recording parent lineage,
  enforcing `run.max_rerun_depth`, and warning when a source checksum no
  longer matches the parent manifest.
- `pageledger compare-runs` creates a page-by-page diff of two run directories:
  character/word deltas, warnings resolved/introduced, adapters, cost.
- `pageledger init-config` generates a minimal valid config to stdout or file.
- `pageledger inspect-run` summarizes a completed or failed run directory.
- `pageledger doctor` reports optional dependencies, external tool versions,
  and redacted cloud environment status.
- Filesystem-native run artifacts: `manifest.json`, `route-map.yml`,
  `config-snapshot.yml`, `audit.json`, `audit.md`, `provenance.jsonl`,
  `quality.jsonl`, `cost.json`, `run.log`, `rerun-manifest.yml`, and per-page
  output under `raw/`.
- Page-denominated budget enforcement caps pages, tokens, and dollars
  with configurable warning thresholds, enforced preflight and mid-run.
- Cost provenance: `cost.json` reports `cost_basis` (`adapter_reported`,
  `configured_rate`, `mixed`, `none`) and runner-measured
  `extraction_seconds`; provenance lines carry per-page `extraction_seconds`.
- Retry with configurable `max_retries` and optional exponential backoff
  (`retry.backoff: exponential`, 0.5 s base doubling to an 8 s cap).
- Quality signal diagnostics with seven-item warning taxonomy (`empty_text`,
  `short_text`, `replacement_characters`, `control_characters`,
  `suspicious_symbol_density`, `fragmented_text`,
  `suspicious_embedded_text_delta`).
- Quality-warning pages automatically routed to audit `review_queue` with
  `reason: "quality_warning"`.
- `quality_warning_pages` rollup count in `manifest.summary`.
- Custom adapters via `module.path:object` import strings.
- Adapter conformance checker: `pageledger.adapters.adapter_conformance_check()`.
- Adapter metadata validation at load time (name, version, deterministic,
  input_types, output_types, capabilities, supports, extract).
- `usage.pages == 1` enforcement means each `extract()` call handles exactly one page.
- Executable rerun manifests: `rerun_status`
  (`executable`/`empty_queue`/`no_further_generations`), `rerun_depth`
  generation tracking, and the `max_rerun_depth == 0` guard producing empty
  items.
- Config validation with key-path error messages and suspicious-config warnings
  (empty taxonomy, unknown top-level keys, impossible budget thresholds).
- JSON Schema files for all JSON/JSONL artifacts under `schemas/`.
- Schema validation tests covering dry-run, execute, budget failure, adapter
  failure, and empty-review-queue scenarios.
- Failure recovery: partial-run guarantees documented with scenario table,
  write-order, and common error/user-action mappings.
- Comprehensive test suite: 197 tests covering all CLI commands, adapters,
  quality signals, rerun execution, cross-run comparison, cost provenance,
  schemas, failure paths, and edge-case inputs.
- CI workflow with test matrix (with/without PDF extra, Python 3.10–3.13)
  and wheel/sdist smoke tests.
- Release checklist under `.planning/release-checklist.md`.

### Changed

- "Core Modules" → "Design Architecture" with explicit implementation-status
  labels in README.
- Added "Current Runtime Capabilities" (three tiers) and "Known Limits" sections.
- Claim audit: removed optimistic score language, constrained the public promise
  to implemented behavior only.
- `adapter-protocol.md` rewritten with frozen contract, `usage.pages == 1` rule,
  and subprocess/timeout guidance.

### Removed

- Python 3.14 classifier (not yet released).

### Performance

- Tested scale: 5,000 text pages in 2.4s (~2,100 pages/sec) locally.
  Artifact counts verified: raw files == provenance lines == quality lines
  == pages extracted. Stress tests reproducible without credentials or
  private data.

### Fixed

- `--json` now emits parseable error JSON (`{"status": "error", "error": "..."}`)
  on failure instead of unstructured stderr-only errors.
- Unreadable input files now produce clean `RuntimeError` instead of raw
  `PermissionError` tracebacks.
- Rerun manifest `max_rerun_depth == 0` now correctly produces empty `items: []`
  with `rerun_status: "no_further_generations"`.
