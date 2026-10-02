# Capabilities and limits

This page lists supported workflows, adapters you supply, and limits on what
the recorded files can establish. See the [glossary](glossary.md) for terms
used across the reference.

## Supported workflows

| Task | Built-in support | What remains your responsibility |
|---|---|---|
| Extract pages | `text`, `pdf_text`, `pdf_ocr`, `rapidocr` and `vision`; custom adapters use the adapter protocol. | Select the extractor, configure it, and judge its output. |
| Route pages | `classify` assigns structural types and writes a route map and text-free sidecar. | Review decisions; domain labels require a classifier hook. |
| Process a document | `process` manages local text, OCR and optional image stages with shared limits and retained attempts. | Supply stage config, adapter code and credentials where needed. |
| Review and export | Job reports, source-bound review receipts, review sheets and verified `txt`, `md`, `jsonl`, or `tei` exports. | A human makes and records review decisions. |
| Align structured output | Schema alignment supports `markdown_table`, `json`, and `csv` output. | Produce structured output and declare columns/checks. Plain text is not aligned. |
| Compare or replay | Compare-runs reports page differences; bundles and replay check transport and recorded identities. | Comparison is conditional; replay does not reproduce external services or certify accuracy. |

## Built in and tested

### Extract pages

| Supported | What to rely on | Limit |
|---|---|---|
| `run` with `text`, `pdf_text`, `pdf_ocr`, `rapidocr` or `vision`; custom adapters follow the adapter protocol. | Page-level attempts and recorded source, extractor, quality and cost details. `--pages` preserves source page numbers. | `pdf_text` reads a text layer; `pdf_ocr` needs locally installed Poppler and Tesseract. Quality signals do not establish accuracy. See [PDF first run](pdf-ocr-first-run.md) and [quality warnings](provenance-spec.md#warning-taxonomy). |
| Built-in adapter defaults, `adapter_options`, `--adapter-path`, and dry-run planning. | Defaults are recorded; dry runs do not call extractors. | Only explicitly supplied configuration is read. See [CLI](cli.md), [adapter protocol](adapter-protocol.md), and [OCR options](ocr-options.md). |
| Page budgets, retries and optional continue-on-error. | Page, token and dollar limits; failed or unattempted pages can enter rerun work. | Cost may be unknown or estimated, not provider-billed spend. See [plan time and cost](plan-time-and-cost.md) and [warnings](warnings.md). |
| Text-quality warnings, OCR confidence, historical-orthography signals and grades. | Diagnostics identify some risks and can guide review. | They are not calibrated accuracy measures. See [warnings](warnings.md) and [multilingual OCR](multilingual-ocr.md). |

### Route pages

| Supported | What to rely on | Limit |
|---|---|---|
| `classify` followed by `run --routes`; built-in structural types or a user classifier hook. | Route maps can be reviewed before extraction and are checked against the source and configured actions. | Built-in labels are structural, not semantic; probe mode has no run budget or cost ledger. See [classifier](classifier.md). |

### Process a document

`process` manages configured local-text, OCR and optional image stages, shared
limits, retained attempts and review holds. It keeps the attempts available for
inspection; completion does not mean that the text is accurate or reviewed.
See [document processing](processing-spec.md) for stages and limits.

Pages climb on warning holds and, when configured, on a Russian lexicon's
judgement (`rough`), engine disagreement or every page (`always`). With
`processing.contest`, a model's reading is compared word by word with the
literal OCR reading; rules settle look-alike letters and listed glyph confusions,
and `pageledger adjudicate` settles the rest against the page image through
packets an agent or a person answers. Settled pages are `adjudicated_text`, kept
apart from `reviewed_text`. Contest compares words, not punctuation or the order
of words that moved together; its glyph pairs cover RapidOCR's Cyrillic model
only; and an adjudicator can be wrong, which is why it should come from another
model family than the reader. See [contest](processing-spec.md#contest-a-models-reading).

### Review and export

| Supported | What to rely on | Limit |
|---|---|---|
| Job reports, human review receipts, review sheets, `verify-job`, and verified `txt`, `md`, `jsonl`, or `tei` exports. | A receipt binds a human decision to the page and selected output; exports preserve page boundaries and review state. | A person makes the review decision. Verification and export do not certify transcription accuracy. See [review](processing-spec.md#review-in-a-spreadsheet), [report and receipt](document-report-spec.md), and [export](export.md). |

### Align structured output

Schema alignment maps `markdown_table`, `json`, or `csv` output to declared
columns and checks. It records failed coercions and checks rather than silently
correcting them. Plain text is not aligned. See [normalized records](normalized-spec.md)
and [CLI alignment](cli.md#align).

### Compare or replay

| Supported | What to rely on | Limit |
|---|---|---|
| `compare-runs`, verified bundles and replay. | Comparisons show changes; replay records the locally available extractor and compares output bytes. | Directional comparisons require matching identities. Replay does not recreate external services or certify accuracy. See [comparison](cli.md#compare-runs) and [the replay boundary](#verified-replay-boundary). |

### Recover interrupted work

`run --resumable` and `pageledger resume` recover an interrupted run from its
saved outcomes. A request with no saved outcome is not retried; it is reported
as `outcome_unknown`. See [checkpoint specification](checkpoint-spec.md).

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

## Verified replay boundary

Verified replay checks a bundle's files and compares a run made with the
locally available adapter. It does not install or recreate the original
environment. Its limits are:

- Bundle hashes show that files agree with the bundle's recorded hashes. They
  do not establish who made the bundle or whether it is authentic.
- A reproducibility profile covers PageLedger's recorded details and materials
  declared by the adapter. It may omit dependencies, and does not establish
  that the listed materials are authentic.
- Replay does not isolate network access, credentials or other external effects.
  Credentials are not bundled, cloud identity is not established, and external
  services may change.
- File checks happen as files are read. They do not prevent another process
  changing files during the check.
- `exact` means the replay produced the same bytes as the original on every
  page. When `raw.equal` is 0, no page produced any output (every page was
  skipped, for example), so `exact` shows nothing about the engine.
- Replay startup does not process editable-install `.pth` hooks. Install adapter
  code normally or supply it through `--adapter-path`.

Replay checks the locally available adapter identity and rejects changed
baseline, source, route, config, profile or inventory files. Cloud adapters,
and adapters not declared deterministic, always get `evidence_compared`: the
outputs were compared, but identical bytes were not expected. No outcome is a
claim about accuracy. PageLedger does not perform licensing, privacy or legal
review. [Verified replay](design.md#verified-replay) in the design notes shows
the commands.

## Known limits

### Inputs and routing

- `pdf_text` reads existing text layers. It does not OCR. For scanned PDFs
  use `pdf_ocr` or wrap a stronger engine as a custom adapter.
- `pdf_ocr` needs Poppler and Tesseract installed. PageLedger never
  installs OCR engines; it fails with an install hint when they are
  missing, and refuses to start when `run.adapter_options.lang` names a
  language pack that is not installed. OCR quality is Tesseract's, at the
  DPI and language you configure.
- Encrypted PDFs are read when they open without a password, which covers
  files that only restrict printing or copying. `pageledger[pdf]` includes
  the `cryptography` package that pypdf needs for AES. PageLedger does not
  take passwords: a file that needs one, or that uses a non-standard
  encryption handler such as an Internet Archive lending copy, stops the run
  before any page with `unsupported_encryption`. A damaged file stops it with
  `malformed_pdf`; PageLedger never repairs a source. One unreadable file
  stops the whole run, so remove it from the inputs or run it on its own.
- The built-in classifier is structural, not semantic. It has no image model,
  language model, document-domain labels, or region-level routing. Domain types
  require a project hook.
- Classifier confidences are fixed, uncalibrated scores. They rank the
  built-in rule outcomes but are not probabilities, and they are not directly
  comparable with a hook's confidence scale.
- Table classification uses structured-result format, pipe density, or the
  combination of column spacing and digit density. The
  `table_column_line_ratio: 0.015` default recovered six known OCR table
  spreads in a small census test. This is not broad accuracy testing; layout
  loss and digit-heavy prose can still produce false negatives or positives.
- An empty `pdf_text` probe cannot distinguish a visually blank PDF page from
  an image-only page. It emits `unknown` with null confidence and routes to
  review. An OCR or text probe can emit `blank`, but that remains a text-output
  judgment rather than proof about page pixels.
- `classify` probe mode has no retry, budget enforcement, cost ledger, or run
  directory. A `pdf_ocr` probe still spends OCR time per page. Probe failures
  become `unknown`/`review`; classifier-hook failures abort the command.
- `classify --from-run` accepts only a full-coverage, non-dry-run original run.
  Missing retained data for an individual page becomes `unknown`/`review`;
  reruns and `--pages` partial runs are rejected
  rather than emitting an incomplete map.
- A custom classification probe and the later run adapter may count pages
  differently. `run --routes` fails its complete-coverage check in that case;
  PageLedger does not renumber or reconcile the map silently.

### Extraction quality and review

- Quality signals are diagnostic, not calibrated. `quality.jsonl` records
  per-page details a human should weigh, not accuracy scores. Shape-based
  heuristics cannot detect word-level misrecognition ("matericl" for
  "material"); Tesseract's own word confidence (`low_confidence`) is the
  closest built-in signal, and it reflects the engine's opinion of
  itself, not ground truth.
- Output-integrity signals are deliberately conservative heuristics. A marker
  or large rerun expansion queues review; it does not prove that an adapter
  hallucinated, and absence of a warning does not prove faithful output.
- Grades are deterministic summaries of the quality signals and, for
  structured output, the schema checks. They do not measure accuracy.
  Confidence is uncalibrated across engines, versions, models and prompts, so
  an `A` from one extractor and a `B` from another cannot be ranked. Two
  grades are comparable only when the extractor, PageLedger version, grading
  policy, grade basis and, for schema-checked grades, the schema all match. A
  grade from an adapter that reports no confidence rests on warning counts
  alone. The `(signals)` or `(schema)` label says which basis a grade has.
- Schema alignment consumes structured output only. `pdf_ocr` and other
  plain-text adapters grade on signals alone; producing tables is the
  adapter's job (see
  [`examples/tesseract_tsv_table_adapter.py`](../examples/tesseract_tsv_table_adapter.py)
  for a deliberately naive demonstration).
- A text layer in the wrong script throughout, such as Latin-letter OCR of a
  Cyrillic book, raises no warning: its words are consistent, just wrong.
  Compare a few pages with the images when a collection's language is known.
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
  `classify` then `run --routes` workflow so the map can be reviewed.
- A model's reading that a literal engine confirms word for word becomes
  `adjudicated_text` with no person reading it. Both engines can miss the same
  word; contest finds only where they differ.
- Adapter escalation chains advance only when the user runs `pageledger rerun`.
  They do not automatically call every adapter in one run, merge parent and
  child output, or remove unresolved pages from human review.
- Reruns re-extract listed pages; they do not merge results. Combining
  parent and rerun outputs into one corpus is the project's decision, and
  `pageledger compare-runs` shows per-page differences for making it.
- PageLedger does not make OCR or VLM output correct. It cannot calibrate
  confidence across unrelated extractors, guarantee accuracy, or make a
  right-to-left, mixed-script, tabular, or handwritten collection work
  without explicit adapter and schema configuration. Its job is narrower:
  preserve enough information that a researcher can see what ran, what
  failed, what is uncertain, and what should be reviewed or rerun.

### Replay and custody

- Replay and bundle limits are described in the
  [verified replay boundary](#verified-replay-boundary).
- A run recorded before PageLedger hashed raw output stays readable, but it
  cannot pass `verify-run`, so it cannot be bundled or replayed. See
  [verification](artifacts.md#verification).
- Source custody, licensing, privacy and legal review remain with the project.

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
- The structural classifier was checked against retained OCR from five
  sampled pages of a 1916 Bessarabia address-calendar and seven sampled 1939
  census spreads. That pass tuned the column-line threshold; it supports the
  default, not a general benchmark.

These historical checks describe the tested documents and workloads. They
are not benchmarks of every current workflow; see [performance](maintainers/performance.md)
for the measured serialization improvement and its limits. Stress
tests are marked `@pytest.mark.stress` and skipped in default CI:

```bash
python -m pytest tests/pageledger/ -m stress
```
