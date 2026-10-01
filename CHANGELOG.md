# Changelog

Release changes follow the [artifact compatibility policy](docs/run-manifest-spec.md#compatibility-policy).

## Unreleased

### Added

- `reading_order: columns` for the `rapidocr` adapter reads a page set in
  columns one column at a time, after cutting the page at its whitespace. The
  default, `rows`, still reads across the page, which suits tables. On a
  two-column journal article the default glued the halves of neighbouring lines
  together without any warning.

### Fixed

- A scanned page with no text layer no longer waits for a person once OCR reads
  it without concern. The empty text layer used to leave a `blank_candidate`
  hold on every such page, so a whole scanned book showed as "needing a person"
  even where OCR had read each page. New jobs record `hold_policy: "0.6.1"`;
  jobs written by 0.6.0 keep their holds and still verify.

## 0.6.0 - 2026-09-28

### Added

- Documentation for working with a real collection: a glossary; a reference of
  every warning, hold and disposition with its likely cause and what to do;
  how-to guides for processing a collection, choosing OCR settings, planning
  time and cost, troubleshooting, and sharing and citing results; and engine
  recipes. The documentation index is grouped into tutorials, how-to guides,
  reference and explanation, and maintainer records moved to
  `docs/maintainers/`. The PDF/OCR tutorial now runs in CI against a generated
  scan, and README links work on PyPI. Capabilities and limits are grouped by
  task, and each recovery rule is stated once: for runs in the checkpoint
  specification, for document jobs in the processing specification.
  `docs/examples/processing.yml` now matches the configs shown in the README
  and processing specification (a job pauses after 100 attempted pages), a
  test keeps them identical, and its optional image stage uses the built-in
  `vision` adapter.
- Warnings for text layers that exist but carry little of the page:
  `digits_only_text` (a table layer that kept its digits and lost its words),
  `mixed_script_tokens` (Latin look-alikes inside Cyrillic words, or the
  reverse), `private_use_characters` (characters lost to font-specific code
  points, such as old-style digits) and, across a run, `repeated_page_text`
  (the same short stamp on three or more pages). Quality lines gain
  `letter_count`, `digit_count`, `mixed_script_token_ratio` and
  `private_use_count`. The thresholds were measured on 5,702 sampled pages
  from a research library before they were set, and `process` treats these
  warnings as coverage defects, so such a page moves on to OCR.
- Warnings for the ways vision models fail. `repetition_loop` flags a model
  stuck in a loop: one line on much of the page, an ending repeated 20 or more
  times, or one letter repeated 40 times. On 287 outputs of 24 transcribed
  pages by 11 engines it flagged 16 pages, every one a loop, and nothing from
  classic OCR engines, hosted models or the reference. A new top-level
  `language` block enables `script_mismatch`, for a page read mostly in another
  script, and with `orthography: prereform`, `historical_letters_lost`, for
  pre-reform text returned in modern spelling; PageLedger does not guess a
  language. Quality lines gain `largest_identical_line_count`,
  `longest_repeated_tail_length` and `longest_letter_run`, and document jobs
  hold such pages as coverage defects.
- Document jobs compare the engines' readings of each page. When the selected
  reading and another clean one agree on fewer than 60% of words, the page is
  held as `engine_disagreement`; when a number differs, as
  `numeric_disagreement`. A reading from a generative adapter, such as
  `vision`, stays in review as `unconfirmed_model_output` until another engine
  agrees with it. A plain `run` holds every page a generative adapter read in
  its review queue, with the same reason. `processing.benchmark` runs a stage
  on every Nth page even when it is not needed, to compare engines on a
  sample.
- A page type can set `review: true` to be extracted and still held for
  review: after extraction the page joins the review queue with reason
  `route_review:<type>`. `classify` copies the flag into route maps; route
  maps without it behave as before.
- Engine recipes (`docs/engine-recipes.md`) for putting a stronger engine than
  Tesseract behind PageLedger: a local vision model through `mlx_vlm.server`
  or llama.cpp, RapidOCR with the PP-OCRv5 Cyrillic recognizer (the built-in
  `rapidocr` adapter below), Tesseract's community `orus` model for
  pre-reform print, Apple Vision on macOS (`examples/apple_vision_adapter.py`)
  and hosted models through a gateway, each with what it was measured to do
  well and badly on real pages.
- A built-in `rapidocr` adapter reads scans with RapidOCR and a PP-OCRv5
  recognizer you supply, such as the Cyrillic one that keeps pre-reform
  letters. Install it with `pip install 'pageledger[rapidocr]'`; it runs on
  the CPU and needs Poppler. `pageledger doctor` reports whether RapidOCR and
  ONNX Runtime are installed.
- A built-in `vision` adapter reads pages with a vision model behind an
  OpenAI-compatible endpoint, on this machine (`llama-server`,
  `mlx_vlm.server`) or hosted. It renders each page with Poppler within the
  request's size limit, sends pages to another machine only over `https://`
  with `allow_remote: true`, reads the key only from a named environment
  variable, and refuses redirects. Finish states become typed failures,
  including the new `MODEL_CONTENT_FILTERED` and `MODEL_RECITATION`. In
  document jobs' image stages it keeps the exact JPEG sent as image evidence.
  `reasoning_effort` asks a model that reasons before answering to reason
  less, so its token budget goes to the reading.
- `pageledger review-sheet JOB --out review.csv` writes a CSV for reviewing a
  job in a spreadsheet: a link to each page, its disposition, the start of its
  text and its review reasons, with empty `decision` and `note` columns.
  `review-job --review review.csv --reviewer NAME` records the decisions after
  checking the whole sheet against the evidence each row was written from,
  which a binding file beside the sheet records. A stale, duplicated or
  formula-bearing row stops the import before anything is recorded, and
  `--dry-run` checks and counts the decisions without recording them.
- `pageledger export JOB --format txt|md|jsonl|tei --out FILE` writes a
  verified job's selected text page by page, with each page's review state,
  the attempt and engine that produced its text, and the text's SHA-256.
  `--reviewed-only` keeps only reviewed pages. The TEI is a minimal TEI P5
  document with a `<pb>` for every source page and reviewed and unreviewed
  pages marked apart. Links to the source are relative to the exported file.

### Changed

- Document reports list the pages that need a person first, then the rest, and
  show separately the text the policy selected, whether engines agree on it,
  and who reviewed it. Reports written before 0.6 keep their layout.
- Document reports name the source by its path relative to the job directory,
  so a shared `report.md`, `transcript.md` or `document.json` no longer shows
  where its owner keeps files. New reports record `report_format: "0.6"`;
  reports written by 0.5.1 keep their absolute paths and still verify.
- `init-config` now extracts `sparse`, `table_likely` and `unknown` pages with
  `review: true`. They were previously routed to review without extraction,
  so a first `classify` and `run --routes` on a statistical volume extracted
  none of its tables.
- `run` now stops with "No extraction route" when a config has no
  `taxonomy.page_types` and neither `--routes` nor `--adapter` is given.
  Such a run previously sent every page to review, extracted nothing and
  exited successfully. Dry runs and deliberate review-only taxonomies still
  work. Documentation and example configs that omitted the taxonomy now
  include it or say where to paste them.
- `process` rejects `run` settings that a document job would ignore: budget,
  pricing, grading, `rerun_if`, `quarantine_if`, adapter options, rerun depth
  and consecutive-failure limits. A `run.budget.max_pages: 1` previously let a
  job process every page without a warning. A leftover `run.adapter` or
  `taxonomy` now produces a warning, and the processing examples drop both.
- The `pdf` extra now installs `pypdf[crypto]`, which adds the `cryptography`
  package. AES-encrypted PDFs that open without a password, typically files
  that only restrict printing or copying, previously failed in `pdf_text`, in
  PDF page counting and in `process`; 20 of the library PDFs in our test
  collection were such files. `process` now accepts them too.
- In document jobs, a page with the `low_confidence` warning now gets its own
  hold, reported as "The engine was unsure of some words". It previously
  shared the `coverage_defect` hold and the wording "Possible missing or
  incomplete content", which claimed more than an engine's doubt shows. New
  jobs record `hold_policy: "0.6"`; jobs written by earlier versions keep
  their original holds and still verify.
- A document job that reaches a processing limit now pauses instead of
  halting. A `max_attempt_pages` smaller than the first batch previously
  stopped the job before any page ran, and a halted job could not continue.
  The job now processes the pages the limit allows and ends `paused_budget`;
  `pageledger resume JOB_DIR --raise-limit max_attempt_pages=N` records the
  higher limit in `limits_history` and continues. Unknown paid usage under a
  token or cost limit still halts.

### Removed

- `examples/openai_image_adapter.py`, the example image adapter. The built-in
  `vision` adapter reads pages in a document job's image and second-opinion
  stages and keeps the same image evidence. To move a config, name
  `adapter: vision` with `base_url`, `model` and `env_key`; a host other than
  this machine also needs an `https://` address and `allow_remote: true`.
  `vision` takes `max_tokens` where the example took `max_output_tokens`, and
  does not limit models to the Gemini and DeepSeek families.

### Fixed

- Encrypted and damaged PDFs stop with a typed message that names the file:
  `unsupported_encryption` for a file that needs a password or uses a
  non-standard handler such as an Internet Archive lending copy,
  `malformed_pdf` for one pypdf cannot parse, and `missing_crypto_dependency`
  when AES support is missing. Previously a password-protected or non-PDF file
  ended `run` with a Python traceback, and a 3.4 GB file with a corrupt
  cross-reference offset failed with "negative seek value" after `pdf_text`
  had read the whole file into memory. `pdf_text` now reads PDFs from disk as
  it parses them. A `process` job halted by such a file records the code, for
  example `source_container_invalid:unsupported_encryption`, and says what to
  do next.
- `pdf_ocr` no longer renders oversized pages at full DPI. Some scans declare
  pages far larger than the paper: an Internet Archive scan of a 1911
  provincial memorial book declares pages 1.75 by 2.47 metres, about 600
  megapixels each at 300 DPI. Each page is now measured first and rendered at
  the highest DPI that fits within the new `max_render_pixels` adapter option
  (default 60,000,000). A lowered page carries the warning
  `render_dpi_capped`, and its provenance records both values, for example
  `dpi=94 (requested 300)`. A page that would need less than 72 DPI stops with
  `render_limit`.
- `pdf_ocr` no longer downsamples scans filed on undersized pages. An 1872
  volume declares 18 by 29 mm pages holding scans about 440 pixels wide, which
  300 DPI rendered at half their resolution. When pypdf is installed, a page
  whose largest embedded image has a higher resolution than the requested DPI
  is rendered at the image's resolution, up to 1200 DPI, and its provenance
  says so, for example `dpi=621 (requested 300, native image)`. Image sizes
  are read from the PDF without decoding the images, once per file.
- A missing `pdftoppm`, `tesseract` or Tesseract language pack now stops a
  `pdf_ocr` run before any page is read, with a message such as
  `missing_language_pack: ... Installed: eng, osd`. Previously the run started,
  failed on the first page, and showed only `RuntimeError: <redacted>`.
  PageLedger's own setup diagnostics carry a typed code and are shown in full;
  messages from adapters stay redacted. A page that fails with a typed
  failure now names its code and HTTP status, for example
  `AdapterFailure: MODEL_QUOTA (HTTP 429)`, on screen and in `run.log`,
  whichever adapter raised it: the codes come from a fixed list.
- Directory inputs skip hidden files such as macOS `.DS_Store` and `._*`
  sidecars. A `.DS_Store` file previously became the first document of a run
  and shifted every document number; on PDF adapters it failed the run. Skipped
  names are reported as `skipped_inputs` in the run result and manifest.
- Replay no longer fails as an incompatible environment when the replay worker
  reports the same text encoding with different capitalization ("UTF-8"
  versus "utf-8"). This happened wherever no `LANG` was set, because Python's
  locale coercion changes the child process's report. Profiles now record
  canonical codec names, and bundles recorded before the fix still replay.

### Maintenance

- A real-document harness for evaluating releases. `corpus/manifest.public.yml`
  lists 23 public-domain scans with their source, SHA-256 and the pages each
  tier runs. `scripts/corpus/run.py` runs a tier as verified document jobs,
  `compare.py` shows what changed between two runs, `sweep.py` checks that every
  PDF under a folder opens, and `score.py` scores a run, or one stage's engine
  within it, against reference transcriptions, reproducing the calibration
  grader's numbers.
- Documentation tests: every complete config in the docs and in
  `docs/examples/` loads, every relative link and heading anchor resolves,
  version strings in the README, docs index, skill and route-map
  specification match the package, and the README's first run executes.

### Compatibility

- New artifact fields are optional, so artifacts written by earlier versions
  still validate: `skipped_inputs` in manifests and checkpoints, the seven new
  `text_quality` counts in quality lines, and `hold_policy` and
  `limits_history` in jobs and document reports. Job and report `status` may
  now be `paused_budget`, and a report's `report_format` may be `"0.6"`. Eight
  retained 0.5.1 document jobs, seven of them with `low_confidence` pages, pass
  `verify-job` unchanged.
- The `pdf` extra adds `cryptography` (through `pypdf[crypto]`), and the new
  `rapidocr` extra installs RapidOCR and ONNX Runtime. Core still depends only
  on PyYAML.

## 0.5.2 - 2026-09-12

### Fixed

- `classify --from-run` rejects truncated, duplicate, out-of-range, and
  misidentified page inventories against the original run manifest.

### Maintenance

- Release preparation now reads and accounts for Copilot review guidance before
  merge and publication.

## 0.5.1 - 2026-09-12

### Changed

- Document reports distinguish the selected extraction from retained review
  evidence, so an empty initial text layer does not obscure a later OCR result.
  Review holds and decisions tied to source pages keep their original meaning.
  Existing 0.5.0 reports still verify without being rewritten.
- Added a maintained document-job tutorial and installed-package verification
  of processing, interrupted recovery, inspection and review. Both the wheel
  and source distribution run both maintained tutorials outside the checkout.
- Added a 60-page source validation report with per-page evidence, observed
  OCR errors, and clear limits. A reader-trial form records the human checks
  still needed; no participant results or accuracy rates are claimed.
- The optional image adapter now accepts explicit JPEG byte and dimension
  limits for gateways with smaller inputs. A separate live VLM test records
  provider failures and retained evidence; it did not produce a transcription.

### Fixed

- Ordinary runs stop before another adapter call when recorded token or cost
  usage has reached its cap. Negative adapter usage is rejected.
- Replay bundles cannot add a page selection that was absent from the original
  run. Malformed review receipts, route fields and schema column types produce
  validation errors with useful context.
- Malformed parent routes are rejected before classification writes new output.

### Maintenance

- Shared package checks replace duplicated workflow code. The locked CI and
  release checks enforce consistent Python formatting across source, tests,
  examples and scripts.

## 0.5.0 - 2026-09-11

### Added

- Document processing jobs with local text extraction, local OCR and optional
  image-model stages. `process` writes a page-referenced transcript, a document
  report and a review queue; `inspect-job` summarizes progress and `verify-job`
  checks the recorded evidence.
- Durable page checkpoints and `resume` for interrupted runs and jobs.
  Completed pages are verified before reuse. Changed sources, configuration
  mismatches and damaged checkpoints stop recovery. Attempts with an unknown
  provider outcome remain held for review rather than being retried automatically.
- Explicit processing limits for attempt pages, image pages, tokens and cost.
  An exhausted limit stops further attempts, including when usage reaches the
  limit exactly.
- Image evidence that records the source page, render or crop, dimensions,
  hashes and adapter identity. An optional OpenAI-compatible example adapter
  supplies image transport without adding a provider SDK to core.

### Changed

- Consolidated CLI error handling, fresh/resumed page accounting, extractor
  metadata and path validation. Removed dead aliases and replay bookkeeping.
- Updated the documentation around document jobs, recovery, review and the
  existing single-run workflow. Removed obsolete implementation plans and
  local readiness reports from the maintained documentation tree.

### Fixed

- When no shared pages are comparable, the human `compare-runs` summary now
  says warning and grade changes were not assessed instead of rendering
  directional zero totals. Per-page evidence and JSON counts are unchanged.
- Verification now rejects incomplete replay-page coverage, malformed page
  identities and malformed worker outcomes. Rerun-depth checks use the same
  configuration interpretation as execution.
- Integer columns and integer-only arithmetic preserve values beyond floating
  point precision. CSV parser failures produce structured error evidence.
- Strict failure-rate policy thresholds handle equality correctly. Existing
  arithmetic failure-delta serialization is retained whenever it preserves
  the exact value.
- Child runs are bound to their job configuration, and source-page links in
  document reports resolve from their output directory.

### Compatibility

- Existing run artifacts retain `schema_version: "0.1"`. New checkpoint,
  image-evidence and job artifacts have their own schemas. Generation-indexed
  `run.adapter_order` semantics are unchanged.
- Core still depends only on PyYAML; PDF support remains optional. OCR engines
  and provider dependencies belong to the selected adapters.
- Grades and verification describe recorded evidence. They do not certify
  transcription accuracy, downstream interpretation or original-file custody.
- Includes the reader fixes prepared for 0.4.2, which was not published.

## 0.4.1 - 2026-09-06

Available as the source tag `v0.4.1`; this version was not published to PyPI.

### Changed

- Hardened replay around a fresh-interpreter worker, one adapter instance per
  execution, canonical verification, and provenance binding.
- Human replay output discloses raw equal/different/missing counts, making its
  exact/evidence/mismatch guarantee inspectable without reading JSON.
- Human run, rerun, and inspection summaries distinguish unknown cost, known
  zero or nonzero cost, and partial known subtotals without changing machine
  result or artifact contracts.
- Added a maintained offline reader journey that runs, inspects raw/audit/CSV
  evidence, verifies, reruns the selected page, records external review notes,
  and exercises relocation/replay from a fresh scratch directory.
- Reordered the beginner path around finding and reviewing extracted text;
  bundle/replay is now a separate optional relocation step.

### Compatibility

- Replay's honest trust boundaries are documented, including adapter-declared
  material evidence and the non-hermetic credential/network boundary.
- The public CLI, JSON/result mappings, and artifact schemas are unchanged.

## 0.4.0 - 2026-08-17

### Added

- Verified directory bundles and replay evidence preserve the baseline run,
  source files, portable route map, and inventory for relocation and replay.
- Reproducibility profiles record adapter-declared material hashes when an
  adapter can provide them, with explicit exact/evidence limitations.
- Raw comparison evidence and replay linkage make exact, mismatch, and
  evidence-only outcomes inspectable without adding a runtime dependency.

### Compatibility

- Verified replay is intentionally non-hermetic: it does not install an
  environment, bundle code or models, establish cloud identity, or guarantee
  identical external services. Missing optional material evidence blocks
  deterministic exactness but does not block ordinary runs.

## 0.3.0a1 - 2026-08-16

### Added

- **Auditable local Docling example:** `examples/docling_adapter.py` invokes a
  machine-level Docling installation without adding its ML stack to PageLedger
  core. Standard mode converts once per document and derives page-level
  Markdown; local VLM mode processes only requested pages. Provenance records
  the Docling version, pipeline, VLM preset, and batch mode, while known visual
  or table serialization losses become adapter-native warnings.
- Raw extraction artifacts now carry SHA-256 identities in provenance and are
  checked by `verify-run`. Current manifests and route maps record the
  PageLedger package version; built-in PDF adapters record their effective
  parser, OCR, renderer, and material runtime settings.
- Latin-script joined/run-on hidden text now produces explicit quality and
  classifier evidence instead of silently receiving an A signals grade.

### Fixed

- Adapter-controlled exception messages, stdout, and stderr are omitted from
  default terminal/log error envelopes, preventing heuristic redaction misses
  from becoming secret disclosures.
- Adapter-native result warnings now enter `quality.jsonl`, grading, and audit
  routing as well as provenance; backends can no longer report known partial
  output without affecting review.
- The Docling example now advertises actions and capabilities by pipeline and
  rejects route prompts it cannot apply, so standard extraction cannot be
  mistaken for a VLM call or record a no-op prompt hash.
- Review-only routes have explicit accounting, grade distributions are labeled
  by evidence basis, and audit Markdown is verified as a rendering of
  `audit.json` rather than an independent source of truth.
- `compare-runs` now ranks transitions only when the full effective extractor
  identity, including adapter-option hashes, matches. Grade direction also
  requires matching PageLedger versions, effective grading policy, evidence
  bases, and schema identity, so incompatible claims remain visible but
  unranked.
- Reruns verify the parent ledger and source bytes before extraction, rederive
  their executable plan, preserve the source document page count, and record
  durable lineage depth.
- Verification now checks retained alignment-schema hashes and comparison uses
  canonical schema content, so pre/post-alignment grades can be related without
  trusting a mutable or missing schema snapshot.
- Missing raw hashes now fail integrity verification even for readable legacy
  evidence, so deleting generator-version metadata cannot obtain warning-only
  treatment; manifest, raw, normalized, and alignment symlinks also cannot make
  verification or comparison read outside the run directory.

### Release process

- Package publication is now a manual, tag-only, environment-gated workflow
  that verifies exact release metadata and smoke-tests the exact built wheel
  before any PyPI upload. Verification is the safe default; production also
  requires the exact tag to be typed and a reviewer-protected, non-bypassable,
  tag-restricted GitHub environment. CI keeps a frozen dependency lane
  alongside latest-dependency coverage, with third-party actions pinned to
  commit SHAs.
- The tracked Archivo font subset now carries its upstream OFL and copyright
  notice inside the brand archive; distributable third-party notices are
  included while brand source assets remain excluded from package archives.

### Compatibility

- Artifact schema version remains `0.1`. New fields are additive and optional
  for older artifacts. Current verification is stricter for raw-file tampering,
  rerun lineage/source drift, and audit-render divergence.
- Core still depends only on PyYAML. Docling remains an optional machine-level
  example integration and is not added to PageLedger dependencies or `uv.lock`.

## 0.2.0 - 2026-07-17

### Added

- **Structural page classification:** `pageledger classify` probes inputs or
  reuses retained evidence from a complete run and emits a route map that can
  execute unchanged through `pageledger run --routes`. The dependency-free
  classifier distinguishes `blank`, `sparse`, `prose`, `table_likely`, and
  `unknown`, records every signal and decision in an evidence sidecar, and
  supports importable classifier hooks for domain taxonomies.
- **Generation-indexed adapter escalation:** `run.adapter_order` selects one
  adapter and option set per original/rerun generation. Manifests record the
  planned chain and current step; exhausted chains leave pending pages in the
  human review queue with `chain_exhausted` unless the independent rerun-depth
  cap was reached first.
- **Budget alerts and grouped cost evidence:** capless `warn_pages`,
  `warn_tokens`, and `warn_usd` thresholds record one first-crossing alert per
  unit. `cost.json` can now group extracted-page usage and resolved cost by
  adapter and routed page type.

### Changed

- Classifier output is now the built-in producer for the reviewed route-map
  executor introduced in 0.1.6. Classification remains an explicit stage;
  ordinary `run` commands do not invoke it automatically.
- Rerun config remains authoritative when it differs from a parent's recorded
  next adapter. PageLedger records and prints the disagreement before using
  the supplied config. `max_rerun_depth` remains independent of chain length.
- Absolute and cap-relative budget thresholds share a first-crossing record;
  the lower effective threshold wins, with explicit absolute thresholds
  winning exact ties. Existing per-page `run.log` warning behavior is retained.

### Compatibility

- The package version is 0.2.0, while every artifact and config contract keeps
  `schema_version: "0.1"`. Manifest escalation fields and cost alerts/rollups
  are additive and optional; existing 0.1 artifacts remain valid.
- Core still depends only on PyYAML. The classifier adds no bundled model,
  computer-vision runtime, provider SDK, pricing catalog, or domain taxonomy.

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
- A source-tree release checklist for maintainers.

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
