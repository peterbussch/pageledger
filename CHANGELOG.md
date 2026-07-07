# Changelog

All notable changes to PageLedger will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to the artifact compatibility policy documented in
`docs/run-manifest-spec.md` → Compatibility Policy.

## 0.1.2 — 2026-07-07

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
- `--pages` on `run` — extract only the listed source pages
  (`--pages "1-8,81,100-110"`). Page ids keep the source numbering, so
  sampling a large volume no longer means splitting the PDF and losing
  page identity in the ledger. The selection is recorded in
  `manifest.inputs[].pages`.
- `run --adapter text|pdf_text|pdf_ocr` — run a built-in adapter without
  writing a YAML config. The generated defaults are recorded in
  `config-snapshot.yml`; no implicit config file is ever read.
- `inspect-run --csv` — one row per page (counts, confidence, warnings,
  cost, timing) for spreadsheet triage.
- `pageledger doctor` lists installed Tesseract language packs, and
  `pdf_ocr` fails before extraction with the installed-language list when
  `run.adapter_options.lang` names a pack that is not installed.
- `init-config --adapter pdf_ocr` now includes `adapter_options`
  (`dpi`, `lang`) so the knobs non-English collections need are visible.
- `examples/prereform_normalizer_adapter.py` — OCR plus pre-1918 Russian
  orthography canonicalization (ѣ→е, і→и, ѳ→ф, ѵ→и, morphology-aware ъ),
  with every rewrite counted in a result warning.

### Changed

- Standard European/Cyrillic typography («guillemets», em/en dashes,
  ellipsis, №, §, °) no longer counts toward `suspicious_symbol_density`,
  which was flagging ordinary Russian bibliographies.

## 0.1.1 — 2026-07-07

### Added

- Built-in `pdf_ocr` adapter — OCRs scanned PDFs with locally installed
  Tesseract, rendering pages via `pdftoppm`. No new Python dependencies;
  missing binaries fail with a `pageledger doctor` hint. Reports
  `model: "tesseract <version>"` and measured `compute_seconds` per page.
- `run.adapter_options` — a config mapping passed to the adapter
  constructor. `pdf_ocr` takes `dpi` (default 300) and `lang` (default
  `eng`, `+`-joined for multiple languages). Custom adapter classes and
  factories receive the same options, lifting the old no-argument
  constructor limitation. Options are recorded in `manifest.extractors`
  (new optional `options` field, additive and backward compatible).
- `--adapter-path` on `run` and `rerun` — adds a directory to `sys.path`
  so custom adapter modules load without setting PYTHONPATH.
- `pageledger init-config --adapter pdf_ocr`.
- Lexical-shape quality metrics (`alpha_token_count`, `mean_token_length`,
  `short_token_ratio`) in `quality.jsonl`, and a `fragmented_text` warning
  for OCR fragment noise (mean token length < 3 across 20+ tokens). These
  are additive optional fields; the warning taxonomy grows to seven items.
- `docs/examples/jfk-scanned-archive.md` — end-to-end walkthrough OCR-ing a
  107-page declassified scanned document from the National Archives.
- Ruff linting (`[tool.ruff]` in pyproject, dev extra, CI lint job).

### Changed

- Docs rewritten around the built-in OCR path: README quickstart,
  `docs/pdf-ocr-first-run.md` (no more `PYTHONPATH=examples`),
  `docs/ocr-options.md` decision matrix, `docs/adapter-protocol.md`
  (adapter options, `--adapter-path`).

## 0.1.0 — 2026-07-06

### Added

- `pageledger run` — extraction command supporting dry-run artifact
  generation and built-in `text` and `pdf_text` adapter execution.
- `pageledger rerun` — re-extracts exactly the pages listed in a previous
  run's `rerun-manifest.yml`, preserving page ids, recording parent lineage,
  enforcing `run.max_rerun_depth`, and warning when a source checksum no
  longer matches the parent manifest.
- `pageledger compare-runs` — page-by-page diff of two run directories:
  character/word deltas, warnings resolved/introduced, adapters, cost.
- `pageledger init-config` — generates a minimal valid config to stdout or file.
- `pageledger inspect-run` — summarizes a completed or failed run directory.
- `pageledger doctor` — reports optional dependencies, external tool versions,
  and redacted cloud environment status.
- Filesystem-native run artifacts: `manifest.json`, `route-map.yml`,
  `config-snapshot.yml`, `audit.json`, `audit.md`, `provenance.jsonl`,
  `quality.jsonl`, `cost.json`, `run.log`, `rerun-manifest.yml`, and per-page
  output under `raw/`.
- Page-denominated budget enforcement — caps on pages, tokens, and dollars
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
- `usage.pages == 1` enforcement — each `extract()` call handles exactly one page.
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
