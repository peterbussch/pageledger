# Provenance JSONL specification

`provenance.jsonl` records the evidence trail for a PageLedger run. The run
manifest points to this file; each line records one extractor activity and the
metadata needed to understand and reconstruct the recorded method.

## Minimal per-page line

```json
{
  "schema_version": "0.1",
  "run_id": "run-20260619-001",
  "page_id": "doc_0001_page_0002",
  "source": {
    "path": "scans/volume_01.pdf",
    "page_number": 2,
    "sha256": "abc123"
  },
  "route": {
    "type": "table_data",
    "action": "vlm_table",
    "route_confidence": 0.91
  },
  "extractor": {
    "adapter": "pageledger.adapters.openai_compatible",
    "adapter_version": "0.1.0",
    "model": "qwen/qwen3-vl-235b-a22b-instruct",
    "prompt_hash": "def456",
    "deterministic": false,
    "input_types": ["pdf"],
    "output_types": ["markdown_table"],
    "capabilities": ["ocr", "tables", "cloud"]
  },
  "result": {
    "format": "markdown_table",
    "confidence": 0.84,
    "warnings": ["missing_optional_column"],
    "raw_artifact": "raw/doc_0001_page_0002.markdown_table",
    "raw_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "usage": {
    "pages": 1,
    "tokens": 2884,
    "compute_seconds": null,
    "cost_usd": 0.04
  },
  "metrics": {
    "pages": 1,
    "tokens": 2884,
    "compute_seconds": null,
    "cost_usd": 0.04
  },
  "cost": {
    "usd": 0.04,
    "basis": "adapter_reported"
  },
  "extraction_seconds": 21.833,
  "timestamp": "2026-06-19T19:34:00Z"
}
```

## Optional per-record links

Image adapters may add top-level `input_evidence`, described in
[`image-evidence-spec.md`](image-evidence-spec.md). It binds the exact retained
JPEG to the source page, rendering parameters, prompts, requested model and
actual returned model/provider. `result.raw_sha256` continues to hash output;
`input_evidence.sha256` hashes the transmitted input. Existing records may omit
the field or use null. Final verification checks non-null image evidence.

Normalized output is linked at file level, not per record. Each
`normalized/{page_id}.json` carries `run_id`, `page_id`, and `raw_artifact`;
individual records do not carry provenance identifiers. See the
[`normalized` specification](normalized-spec.md).

## Required fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | Provenance schema version. |
| `run_id` | string | Run identifier from `manifest.json`. |
| `page_id` | string | Stable page or region id. |
| `source` | object | Source path, page number, and source checksum. |
| `route` | object | Page type, action, and route confidence. |
| `extractor` | object | Adapter, adapter version, model, prompt hash, determinism flag, and adapter capability metadata. |
| `result` | object | Output format, confidence, warnings, raw artifact path, and (for current writers) the exact raw artifact SHA-256. |
| `usage` | object | Canonical usage fields: `pages`, `tokens`, `compute_seconds`, and `cost_usd`; optional numeric fields are non-negative or null. |
| `metrics` | object | Flat copy of `usage` for analytical workflows; optional numeric fields are non-negative or null. |
| `cost` | object | Optional PageLedger-resolved per-page cost: `usd` plus `basis` (`adapter_reported`, `configured_rate`, or null). |
| `extraction_seconds` | number or null | Wall-clock seconds for the successful extraction attempt, measured by the runner (independent of adapter-reported `compute_seconds`). |
| `timestamp` | ISO timestamp | Extraction time in UTC. |

## Design notes

- Confidence values may be heuristic. Preserve raw route, extractor, and
  alignment confidence values rather than collapsing them too early.
- `usage.pages` is required. Optional usage fields serialize unknown
  values as JSON `null`, not disappear from generated artifacts.
- `prompt_hash` is required whenever a prompt influenced output.
- `deterministic` is `false` unless the adapter can actually guarantee
  stable output for the same input and config.
- PageLedger reads `usage`, the adapter-facing record. `metrics` is a copy
  kept in schema version `"0.1"` for spreadsheet and JSONL analysis workflows
  that flatten page-level rows. If these fields diverge in a
  future artifact schema, the schema version must change.
- `usage.cost_usd` remains adapter-reported evidence. `cost.usd` is the value
  PageLedger actually uses after applying adapter-reported cost first and then
  configured unit rates. Missing token usage never becomes a known zero cost.
- `result.raw_sha256` is optional for compatibility with older schema-0.1
  ledgers whose manifest also predates `pageledger_version`. Current writers
  always emit it. `verify-run` fails if raw bytes differ or if any digest is
  absent. Older evidence remains parseable and receives an incomplete-evidence
  warning, but it cannot receive an integrity PASS without a raw digest. This
  detects accidental or local modification; it does not establish
  authenticity without an externally trusted or signed manifest/provenance set.
- The JSON Schema for this artifact is at `schemas/provenance-line.schema.json`.
- Schema validation tests are in `tests/pageledger/test_schemas.py`.

## Companion artifact: quality.jsonl

`quality.jsonl` records per-page diagnostic signals alongside provenance. It is
not a calibrated accuracy score. Fields:

| Field | Type | Required | Nullable | Meaning |
|---|---|---|---|---|
| `schema_version` | string | ✅ | no | Quality schema version. `"0.1"`. |
| `page_id` | string | ✅ | no | Links to `provenance.jsonl` `page_id`. |
| `page_number` | integer | ✅ | no | One-based page number. |
| `adapter` | string | ✅ | no | Adapter name for diagnostics attribution. |
| `character_count` | integer | ✅ | no | Total characters in extractor output. |
| `word_count` | integer | ✅ | no | Count of Unicode letter tokens; combining marks remain attached to their base-letter token. |
| `confidence` | number or null | ❌ | yes | Adapter-reported page confidence, 0–1. Emitted by current runs; optional so original 0.1 lines remain valid. |
| `confidence_detail` | object or null | ❌ | yes | Engine-native confidence evidence. Emitted by current runs; optional for original 0.1 compatibility. |
| `warnings` | array of strings | ✅ | no | Adapter-native warnings plus PageLedger-derived quality warnings (see taxonomy below). Adapter warnings are also retained in the provenance result. |
| `text_quality` | object | ✅ | no | Sub-metrics (see below). |
| `embedded_text_comparison` | object | ❌ | yes | Comparison with PDF embedded text layer. Null for non-PDF sources. |
| `output_integrity` | object | ❌ | no | Conservative chat-template-marker and parent-rerun size evidence. Optional for older lines. |
| `grade` | string | ❌ | no | `A`–`F`. Optional for older lines. |
| `grade_basis` | string | ❌ | no | `signals_only` or `schema_aware`. Optional for older lines. |
| `grade_detail` | object | ❌ | no | Grade evidence detail. Optional for older lines. |

### Grade bands

Grades combine two axes, taking the worst letter of the two:

- **Signals axis**: worst of the confidence band (defaults: A ≥ 0.90,
  B ≥ 0.80, C ≥ 0.70, D ≥ 0.55, else F; skipped when the adapter reports
  no confidence) and the warning-count band (0 → A, 1 → B, 2 → C, 3+ → D).
  `empty_text` forces F.
- **Schema axis** (only when a normalized record exists): worst of the
  required-column-coverage band (A ≥ 1.0, B ≥ 0.9, C ≥ 0.7, else D; F when
  parsing failed, no rows, or all required columns missing) and the
  arithmetic-pass-rate band (A ≥ 0.98, B ≥ 0.90, C ≥ 0.75, else D).
  Coercion errors or recorded structural loss cap the axis at B without
  lifting an already-worse grade.

Confidence/coverage/pass-rate thresholds are overridable under
`run.grading.thresholds`. The schema `quality` section adds floors:
coverage below `minimum_required_column_coverage` forces the schema axis
to F, and page confidence under `low_confidence_threshold` caps the final
grade at C. The structured-format prose heuristics
(`suspicious_symbol_density`, `fragmented_text`, `joined_text`,
`digits_only_text`) do not fire on
`markdown_table`/`json`/`csv` pages: pipes and braces are construction,
not garble.

### text_quality fields

| Field | Type | Meaning |
|---|---|---|
| `replacement_character_count` | integer | Count of `\ufffd` replacement characters. |
| `control_character_count` | integer | Non-whitespace control characters below U+0020. |
| `suspicious_symbol_count` | integer | Explicit extraction-garble markers plus Unicode symbols outside letter, mark, number, punctuation, and separator categories, except documented common typography. |
| `suspicious_symbol_ratio` | number (0–1) | `suspicious_symbol_count / character_count`. |
| `alpha_token_count` | integer | Unicode letter tokens on the page; combining marks remain attached to their base-letter token. |
| `mean_token_length` | number or null | Mean Unicode letter-plus-mark token length; null with no tokens. The `<3` warning boundary is a fragment-noise heuristic, not a cross-language quality score. |
| `max_token_length` | integer | Maximum Unicode letter-plus-mark token length. |
| `short_token_ratio` | number (0–1) or null | Share of Unicode letter-plus-mark tokens with 1–2 code points. |
| `whitespace_character_ratio` | number (0–1) | Share of output characters for which `str.isspace()` is true. |
| `latin_letter_ratio` | number (0–1) | Share of Unicode letters identified as Latin-script letters; used only as a conservative joined-text guard. |
| `prereform_letter_count` | integer | Cyrillic letters abolished by the 1918 Russian reform (ѣ, ѳ, ѵ). Modern Ukrainian/Belarusian і is deliberately not counted. |
| `terminal_hard_sign_count` | integer | Word-final hard signs (ъ): mandatory before 1918, absent from modern Russian. This is the pre-reform signal that survives OCR: engines trained on modern text destroy the abolished letters but keep ъ. |
| `letter_count` | integer | Unicode letters. |
| `digit_count` | integer | Decimal digits. |
| `mixed_script_token_ratio` | number (0–1) | Share of letter tokens that mix Latin, Cyrillic or Greek letters, as look-alike substitutions do (`пpoдoлжoние` with Latin `p` and `o`). |
| `private_use_count` | integer | Private Use Area code points, except one that opens a line before a space: that is a bullet or icon glyph from a symbol font. |
| `largest_identical_line_count` | integer | How often the most repeated non-empty line occurs, ignoring dot leaders and rules. |
| `longest_repeated_tail_length` | integer | Length in characters of the longest ending made of one unit of up to 200 characters repeated at least 20 times; 0 when there is none. |
| `longest_letter_run` | integer | Longest run of one repeated letter; 0 when no letter repeats. |

The last seven fields are new in 0.6; older quality lines omit them.

### Warning taxonomy

Meanings and suggested next steps for warnings are in the
[warnings reference](warnings.md). The table here defines trigger conditions.

| Warning | Trigger |
|---|---|
| `empty_text` | Output contains no Unicode letters or digits (including truly empty, whitespace-only, and OCR-speck/punctuation-only output). |
| `short_text` | `character_count < 10`. |
| `replacement_characters` | `replacement_character_count > 0`. |
| `control_characters` | `control_character_count > 0`. |
| `suspicious_symbol_density` | `suspicious_symbol_ratio >= 0.03` AND `suspicious_symbol_count >= 5`. |
| `fragmented_text` | `mean_token_length < 3.0` AND `alpha_token_count >= 20`. Catches OCR fragment noise; does not catch word-level misrecognition. |
| `joined_text` | `mean_token_length >= 10`, `max_token_length >= 80`, `alpha_token_count >= 20`, `whitespace_character_ratio <= 0.03`, and `latin_letter_ratio >= 0.8`. Catches collapsed Latin word boundaries; it is review evidence, not proof of corruption. |
| `suspicious_embedded_text_delta` | PDF embedded text character ratio < 0.5 or > 1.8, when embedded text is available and adapter does not report `embedded_text` capability. |
| `historical_orthography` | `prereform_letter_count >= 2`, OR `terminal_hard_sign_count >= 2` at a density of ≥1 per 100 alphabetic tokens over ≥20 tokens. The page is pre-1918 Russian orthography and an OCR model trained on modern text is probably mismatched. Measured on an 1850 gubernia review: 21 terminal ъ per 100 tokens vs 0.00 in modern text. |
| `low_confidence` | `confidence_detail.below_60_ratio >= 0.25` over ≥10 words. A quarter of the words under engine confidence 60 marks the page for review; a mean can hide one illegible paragraph on an otherwise clean page. |
| `instruction_echo` | Output contains one of the high-specificity chat-template markers `<think>`, `</think>`, `<|channel`, `<|im_start|>`, `<|im_end|>`, `[INST]`, or `[/INST]`. Generic words such as “instructions” or “channel” do not trigger it. |
| `output_inflation` | On a rerun, output is at least 4× and at least 1,000 characters longer than the same parent page. Parent counts, delta, and ratio are recorded in `output_integrity`; this is review evidence, not proof of hallucination. |
| `digits_only_text` | `digit_count >= 20` and letters under 5% of `letter_count + digit_count`. A table whose text layer kept the digits and lost the words. Not raised for `markdown_table`, `json` or `csv` output. |
| `mixed_script_tokens` | `mixed_script_token_ratio >= 0.05` over ≥20 alphabetic tokens. Look-alike letters from another script inside words: search and alignment miss them. |
| `private_use_characters` | `private_use_count >= 3`. Characters the text layer lost to font-specific code points, such as old-style digits or ligatures. |
| `foreign_script_characters` | At least 3 letters or marks from a script other than Latin, Cyrillic or Greek (letterlike symbols and mathematical alphanumerics excepted), making up under 10% of the page's letters. A symbol font read through the wrong map: a born-digital paper's formulas came out as Ethiopic, Oriya, Tamil and Syriac letters. A page mostly in another script does not raise it. |
| `repeated_page_text` | The page's whole text, under 200 characters after whitespace is collapsed, is identical on at least three pages of the run. A stamp-only text layer repeats a scanner's or website's mark on every page and none of the content. |
| `repetition_loop` | A line other than dot leaders or rules occurs at least 20 times and makes up at least 30% of the page's non-empty lines; or the text ends in one unit of up to 200 characters repeated at least 20 times; or one letter repeats at least 40 times in a row. These are the shapes of a model stuck in a loop; a table that repeats a label on some of its rows stays below them. |
| `script_mismatch` | Only when the config declares `language.script`: the page has at least 200 letters and fewer than half are in that script. |
| `historical_letters_lost` | Only when the config declares `language.orthography: prereform`: at least 300 Cyrillic letters, none of ѣ, ѳ or ѵ, and a word-final ъ on at most 1% of words (or on one word). і is not counted, because modern Ukrainian and Belarusian use it. |

`digits_only_text`, `mixed_script_tokens`, `private_use_characters` and
`repeated_page_text` were measured before their thresholds were set: on
5,702 sampled pages from 1,500 PDFs in a research library, and on 24 pages with
reference transcriptions. Transcribed pages raised none of them. On the
sample, `digits_only_text` fired only on one Internet Archive derivative
format (56 pages, 27 files) and `mixed_script_tokens` on 60 pages of 18
files. Private-use characters appeared in 44 files, mostly as bullets;
`private_use_characters` fired on 19 pages of 12 files. A layer in the wrong script
entirely, such as Latin-letter OCR of a Cyrillic book, contains no mixed
tokens and raises none of these warnings.

`foreign_script_characters` was measured on the text layers of six documents
(252 pages) from a 2026 test with real scholarly PDFs. It fired on the 9 pages
whose formulas a math font had encoded as other scripts and on one page where
Hebrew points stood in for a negation sign, and on no other page.

`repetition_loop` was set on 287 outputs of the same 24 transcribed pages by 11
engines. It flags 16 pages, all from local vision models and every one a loop on
inspection: a line repeated up to 1,684 times, dot leaders running for 11,000
characters, a single letter repeated 5,897 times. It flags nothing from the
classic OCR engines, the hosted models or the reference transcriptions. The two
language checks run only when a config declares the language, because
PageLedger does not guess a collection's script or spelling; they have not yet
been measured on a corpus.

Adapter-native warnings appear in the same `warnings` list. The built-in
`pdf_ocr` adapter adds `render_dpi_capped` when it rendered a page below the
requested DPI to stay within `run.adapter_options.max_render_pixels`; the
page's `model` string then records both values, for example
`dpi=94 (requested 300)`. When a page is rendered above the requested DPI to
preserve embedded image resolution, its `model` string records that reason,
for example `dpi=621 (requested 300, native image)`. See
[page size and rendering](ocr-options.md#page-size-and-rendering).

### output_integrity fields

| Field | Type | Meaning |
|---|---|---|
| `instruction_markers` | array of strings | Exact marker labels found in the output; empty when none match. |
| `parent_character_count` | integer or null | Parent page character count when usable rerun evidence exists. |
| `character_delta` | integer or null | Child count minus parent count. |
| `character_ratio` | number or null | Child count divided by parent count, rounded to four decimals; null when the parent is absent or empty. |

An empty parent can still trigger `output_inflation` when the child is at
least 1,000 characters: the 4× condition is satisfied, but the undefined
ratio remains null. Missing or incomplete legacy parent evidence leaves all
three comparison fields null and cannot trigger inflation.

The JSON Schema is at `schemas/quality-line.schema.json`.
